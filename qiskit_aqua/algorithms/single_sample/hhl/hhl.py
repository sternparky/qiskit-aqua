# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""
The HHL algorithm.
"""

import logging
import numpy as np

from qiskit import QuantumRegister, ClassicalRegister, QuantumCircuit
from qiskit_aqua.algorithms import QuantumAlgorithm
from qiskit_aqua import AquaError, PluggableType, get_pluggable_class
import qiskit.tools.qcvv.tomography as tomo

logger = logging.getLogger(__name__)


class HHL(QuantumAlgorithm):
    """The HHL algorithm."""

    PROP_MODE = 'mode'

    CONFIGURATION = {
        'name': 'HHL',
        'description': 'The HHL Algorithm for Solving Linear Systems of '
                       'equations',
        'input_schema': {
            '$schema': 'http://json-schema.org/schema#',
            'id': 'hhl_schema',
            'type': 'object',
            'properties': {
                PROP_MODE: {
                    'type': 'string',
                    'oneOf': [
                        {'enum': [
                            'circuit',
                            'evaluate'
                        ]}
                    ],
                    'default': 'circuit'
                }
            },
            'additionalProperties': False
        },
        'problems': ['linear_system'],
        'depends': ['eigs', 'reciprocal'],
        'defaults': {
            'eigs': {
                'name': 'QPE',
                'num_ancillae': 6,
                'num_time_slices': 50,
                'expansion_mode': 'suzuki',
                'expansion_order': 2,
                'qft': {'name': 'STANDARD'}
            },
            'reciprocal': {
                'name': 'Lookup'
            },
        }
    }

    def __init__(self, matrix=None, vector=None, eigs=None, init_state=None,
                 reciprocal=None, mode='circuit', num_q=0, num_a=0):
        super().__init__()
        super().validate(locals())
        self._matrix = matrix
        self._vector = vector
        self._eigs = eigs
        self._init_state = init_state
        self._reciprocal = reciprocal
        self._mode = mode
        self._num_q = num_q
        self._num_a = num_a
        self._circuit = None
        self._io_register = None
        self._eigenvalue_register = None
        self._ancilla_register = None
        self._success_bit = None
        self._ret = {}

    @classmethod
    def init_params(cls, params, algo_input):
        """
        Initialize via parameters dictionary and algorithm input instance
        Args:
            params: parameters dictionary
            algo_input: LinearSystemInput instance
        """
        if algo_input is None:
            raise AquaError("LinearSystemInput instance is required.")
        matrix = algo_input.matrix
        vector = algo_input.vector
        if not isinstance(matrix, np.ndarray):
            matrix = np.array(matrix)
        if not isinstance(vector, np.ndarray):
            vector = np.array(vector)

        # extend vector and matrix for nonhermitian/non 2**n size matrices
        if np.log2(matrix.shape[0]) % 1 != 0:
            next_higher = int(np.ceil(np.log2(matrix.shape[0])))
            new_matrix = np.identity(2**next_higher)
            new_matrix = np.array(new_matrix, dtype=complex)
            new_matrix[:matrix.shape[0], :matrix.shape[0]] = matrix[:, :]
            matrix = new_matrix
            new_vector = np.ones((1, 2**next_higher))
            new_vector[0, :vector.shape[0]] = vector
            vector = new_vector.reshape(np.shape(new_vector)[1])
        if matrix.shape[0] != len(vector):
            raise ValueError("Input vector dimension does not match input "
                             "matrix dimension!")

        hhl_params = params.get(QuantumAlgorithm.SECTION_KEY_ALGORITHM) or {}
        mode = hhl_params.get(HHL.PROP_MODE)

        # Initialize eigenvalue finding module
        eigs_params = params.get(QuantumAlgorithm.SECTION_KEY_EIGS) or {}
        eigs = get_pluggable_class(PluggableType.EIGENVALUES,
                                   eigs_params['name']).init_params(eigs_params, matrix)
        num_q, num_a = eigs.get_register_sizes()

        # Initialize initial state module
        tmpvec = vector
        init_state_params = {"name": "CUSTOM"}
        init_state_params["num_qubits"] = num_q
        init_state_params["state_vector"] = tmpvec
        init_state = get_pluggable_class(PluggableType.INITIAL_STATE,
                                         init_state_params['name']).init_params(init_state_params)

        # Initialize reciprocal rotation module
        reciprocal_params = params.get(QuantumAlgorithm.SECTION_KEY_RECIPROCAL) or {}
        reciprocal_params["negative_evals"] = eigs._negative_evals
        reciprocal_params["evo_time"] = eigs._evo_time
        reci = get_pluggable_class(PluggableType.RECIPROCAL,
                                   reciprocal_params['name']).init_params(reciprocal_params)

        return cls(matrix, vector, eigs, init_state, reci, mode, num_q, num_a)

    def _construct_circuit(self):
        """ Constructing the HHL circuit """

        q = QuantumRegister(self._num_q, name="io")
        qc = QuantumCircuit(q)

        # InitialState
        qc += self._init_state.construct_circuit("circuit", q)

        # EigenvalueEstimation (QPE)
        qc += self._eigs.construct_circuit("circuit", q)
        a = self._eigs._output_register

        # Reciprocal calculation with rotation
        qc += self._reciprocal.construct_circuit("circuit", a)
        s = self._reciprocal._anc

        # Inverse EigenvalueEstimation
        qc += self._eigs.construct_inverse("circuit")

        # Measurement of the ancilla qubit
        if not self._quantum_instance.is_statevector_backend(
                    self._quantum_instance._backend):
            c = ClassicalRegister(1)
            qc.add_register(c)
            qc.measure(s, c)
            self._success_bit = c

        self._io_register = q
        self._eigenvalue_register = a
        self._ancilla_register = s
        self._circuit = qc

    def _statevector_simulation(self):
        """
        The statevector simulation mode: The result of the HHL gets extracted
        from the statevector.
        """
        res = self._quantum_instance.execute(self._circuit)
        sv = np.asarray(res.get_statevector(self._circuit))
        # Extract result from statevector
        half = int(len(sv)/2) # select valid result with |anc>=1
        if self._reciprocal.CONFIGURATION["name"] == "Lookup":
            vec = sv[half:half + 2 ** self._num_q]
        elif self._reciprocal.CONFIGURATION["name"] == "LongDivision":
            sv_good = sv[half:]
            vec = np.array([])
            for i in range(2**self._num_q):
                vec = np.append(vec, sum(x for x in sv_good[i::2**self._num_q]))
        self._ret["probability_result"] = vec.dot(vec.conj())
        vec = vec/np.linalg.norm(vec)
        self._hhl_results(vec)

    def _state_tomography(self):
        """
        The state tomography mode. Extracting the solution vector information
        via state tomography. Inefficient, uses 3**n*shots executions of the
        circuit.
        """
        # Preparing the state tomography circuits
        c = ClassicalRegister(self._num_q)
        self._circuit.add_register(c)
        tomo_set = tomo.state_tomography_set(list(range(self._num_q)))
        tomo_circuits = \
            tomo.create_tomography_circuits(self._circuit, self._io_register,
                                            c, tomo_set)
        # Handling the results
        result = self._quantum_instance.execute(tomo_circuits)
        probs = []
        for circ in tomo_circuits:
            counts = result.get_counts(circ)
            s, f = 0, 0
            for k, v in counts.items():
                if k[-1] == "1":
                    s += v
                else:
                    f += v
            probs.append(s/(f+s))
        self._ret["probability_result"] = probs
        # Fitting the tomography data
        tomo_data = tomo.tomography_data(result, self._circuit.name, tomo_set)
        rho_fit = tomo.fit_tomography_data(tomo_data)
        vec = rho_fit[:, 0]/np.sqrt(rho_fit[0, 0])
        self._hhl_results(vec)

    def _hhl_results(self, vec):
        self._ret["output_hhl"] = vec
        # Calculating the fidelity with the classical solution
        theo = np.linalg.solve(self._matrix, self._vector)
        theo = theo/np.linalg.norm(theo)
        self._ret["fidelity_hhl_to_classical"] = abs(theo.dot(vec.conj()))**2
        # Rescaling the output vector to the real solution vector
        tmp_vec = self._matrix.dot(vec)
        f1 = np.linalg.norm(self._vector)/np.linalg.norm(tmp_vec)
        f2 = sum(np.angle(self._vector*tmp_vec.conj()-1+1))/self._num_q # "-1+1" to fix angle error for -0.-0.j
        self._ret["solution_hhl"] = f1*vec*np.exp(-1j*f2)

    def _run(self):
        self._construct_circuit()
        # Handling the modes
        if self._mode == "circuit":
            self._ret["circuit"] = self._circuit.name
            regs = {
                "io_register": self._io_register,
                "eigenvalue_register": self._eigenvalue_register,
                "ancilla_register": self._ancilla_register,
                "_success_bit": self._success_bit
            }
            self._ret["regs"] = regs
        elif self._mode == "evaluate":
            if self._quantum_instance.is_statevector_backend(
                    self._quantum_instance._backend):
                self._statevector_simulation()
            else:
                self._state_tomography()
        # Adding a bit of general result information
        self._ret["input_matrix"] = self._matrix
        self._ret["input_vector"] = self._vector
        self._ret["eigenvalues_classical"] = np.linalg.eig(self._matrix)[0]
        self._ret["solution_classical"] = list(np.linalg.solve(self._matrix, self._vector))
        self._ret["circuit_width"] = self._circuit.width()
        self._ret["circuit_depth"] = self._circuit.depth()
        self._ret["gate_count_total"] = self._circuit.number_atomic_gates()
        return self._ret
