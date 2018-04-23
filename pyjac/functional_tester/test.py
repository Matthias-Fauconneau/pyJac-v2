"""Module for performance testing of pyJac and related tools.
"""

# Python 2 compatibility
from __future__ import division
from __future__ import print_function

# Standard libraries
import os
import subprocess
import logging

# Related modules
import numpy as np
import numpy.ma as ma
import cantera as ct
from six.moves import range
from six import iteritems
from nose.tools import nottest

# pytables
import tables

# Local imports
from ..core.mech_interpret import read_mech_ct

from ..tests.test_utils import parse_split_index, _run_mechanism_tests, runner, inNd
from ..tests import test_utils, get_platform_file, _get_test_input, \
    get_mem_limits_file
from ..loopy_utils.loopy_utils import JacobianFormat, RateSpecialization
from ..libgen import build_type, generate_library
from ..core.create_jacobian import determine_jac_inds
from ..utils import EnumType

# turn off cache
import loopy as lp
lp.set_caching_enabled(False)


def getf(x):
    return os.path.basename(x)


class hdf5_store(object):
    def __init__(self, chunk_size=_get_test_input('chunk_size', 10000)):
        """
        Initialize :class:`hdf5_store`

        Parameters
        ----------
        chunk_size: int [10000]
            The default chunk size for reading into hdf5 arrays
        """
        self.handles = {}
        self._chunk_size = int(chunk_size)

    @property
    def chunk_size(self):
        return self._chunk_size

    @chunk_size.setter
    def chunk_size(self, new_size):
        self._chunk_size = int(new_size)

    def _nicename(self, filename):
        return filename[:filename.index('.hdf5')]

    def to_file(self, arr, filename):
        """
        Transfers a large numpy array to a pytables HDF5 file and returns
        the pytables handle

        Parameters
        ----------
        arr: :class:`numpy.ndarray`
            The numpy array to store
        filename: str
            The file to store the array in

        Returns
        -------
        arr: :class:`pytables.array`
            The converted pytables array
        """

        # get nicename
        name = self._nicename(filename)

        # open file
        hdf5_file = tables.open_file(filename, mode='w')
        # add compression
        filters = tables.Filters(complevel=5, complib='blosc')
        # create hdf5 array
        data_storage = hdf5_file.create_carray(
            hdf5_file.root, name, tables.Atom.from_dtype(arr.dtype),
            shape=arr.shape, filters=filters)
        # copy in
        data_storage[:] = arr[:]
        # store
        hdf5_file.close()
        # now reopen w/ read & return handle
        return self.open_for_read(filename)

    def open_for_chunked_write(self, filename, shape, num_conds):
        """
        Opens a new file :param:`filename` and creates an :class:`pytables.EArray`
        (extendable array) for writing

        Parameters
        ----------
        filename: str
            The file to open
        shape: tuple of int
            The shape of the array.  Note that the initial conditions axis should
            be set to zero
        """
        assert filename not in self.handles

        name = self._nicename(filename)
        # open
        hdf5_file = tables.open_file(filename, mode='w')
        # add compression
        filters = tables.Filters(complevel=5, complib='blosc')
        dtype = np.dtype('Float64')
        # create hdf5 array
        arr = hdf5_file.create_earray(
            hdf5_file.root, name, tables.Atom.from_dtype(dtype),
            shape=shape, filters=filters, expectedrows=num_conds)
        self.handles[filename] = hdf5_file
        return arr

    def open_for_read(self, filename):
        """
        Stores an already open :class:`pytables.array`, and reopens for reading

        Parameters
        ----------
        arr: :class:`pytables.array`
            The array to close
        filename: str
            The filename
        """

        if filename in self.handles:
            # close old
            self.handles[filename].close()

        # reopen as read
        hdf5_file = tables.open_file(filename, mode='r')
        self.handles[filename] = hdf5_file
        name = self._nicename(filename)
        return getattr(hdf5_file.root, name)

    def release(self):
        """
        Closes all open hdf5 file references, and removes the file

        Parameters
        ----------
        None

        Returns
        -------
        None
        """

        for f, h in iteritems(self.handles):
            h.close()
            if os.path.exists(f):
                # it may have already been removed in
                # :func:`_run_mechanism_tests.__cleanup()`
                os.remove(f)
        self.handles.clear()

    def output_to_pytables(self, name, dirname, ref_ans, order, asplit,
                           filename=None, pytables_name=None):
        """
        Converts the binary output file in :param:`filename` to a HDF5 pytables file
        in order to avoid memory errors

        Note
        ----
        Processes the file in chunks of :attr:`error_chunk_size`

        Parameters
        ----------
        name: str
            The name of the output array
        dir: str
            The directory the output is stored in
        ref_ans: :class:`pytables.array`
            The corresponding reference answer, from which shape / dtype information
            will be taken
        order: ['C', 'F']
            The storage order of the data in the binary file
        asplit: :class:`pyjac.core.array_creator.array_splitter`
            The array splitter, needed to find out the shape out the output from
            the reference answer's shape
        filename: str [None]
            The filename of the output, if not supplied it will be assumed to be
            :param:`dir`/name.bin
        pytables_name: str [None]
            The filename of the pytables_output.  If not supplied, it will be the
            same as :param:`filename` with the '.bin' prefix replaced with
            '.hdf5'

        Returns
        -------
        arr: :class:`pytables.array`
            The pytables
        """

        # filenames
        if filename is None:
            filename = os.path.join(dirname, name + '.bin')
        if pytables_name is None:
            pytables_name = filename.replace('.bin', '.hdf5')

        if pytables_name in self.handles:
            # close open handle
            file = self.handles[pytables_name]
            file.close()
            # first check if the file still exists -- it might have been removed in
            # the :func:`_run_mechanism_tests.__cleanup()`
            if os.path.exists(pytables_name):
                os.remove(pytables_name)
            # and remove from handles
            del self.handles[pytables_name]

        # open the pytables file for writing
        hdf5_file = tables.open_file(pytables_name, mode='w')
        # add compression
        filters = tables.Filters(complevel=5, complib='blosc')
        # get the reference answer in order to get shape, etc.
        shape, grow_axis, split_axis = asplit.split_shape(ref_ans)

        # and set the enlargable shape for the pytables array
        eshape = tuple(shape[i] if i != grow_axis else 0
                       for i in range(len(shape)))
        # create hdf5 array
        data_storage = hdf5_file.create_earray(
            hdf5_file.root, name, tables.Atom.from_dtype(ref_ans.dtype),
            shape=eshape, filters=filters,
            expectedrows=shape[grow_axis])
        # and read the number of conditions in the grow dimension
        num_conds = shape[grow_axis]
        # and cut down by vec width to respect chunk size
        chunk_size = self.chunk_size
        if split_axis is not None:
            assert chunk_size % shape[split_axis] == 0
            chunk_size = int(chunk_size / shape[split_axis])
        # open the data as a memmap to avoid loading it all into memory
        file = np.memmap(filename, mode='r', dtype=ref_ans.dtype,
                         shape=shape, order=order)
        # now read in chunks
        for i in range(0, num_conds, chunk_size):
            # find out how many ICs to read
            end = np.minimum(i + chunk_size, num_conds)
            # set the read region
            rslice = tuple(slice(None) if ax != grow_axis else slice(i, end)
                           for ax in range(len(shape)))
            # read in data and place into storage
            data_storage.append(file[rslice])
        # close memmap
        del file

        # close hdf5 file
        hdf5_file.close()
        # now reopen w/ read & return handle
        hdf5_file = tables.open_file(pytables_name, mode='r')
        # and store the handle
        self.handles[pytables_name] = hdf5_file
        # and reutrn data
        return getattr(hdf5_file.root, name)


class validation_runner(runner, hdf5_store):
    def __init__(self, eval_class, rtype=build_type.jacobian):
        """Runs validation testing for pyJac for a mechanism

        Properties
        ----------
        eval_class: :class:`eval`
            Evaluate the answer and error for the current state, called on every
            iteration
        rtype: :class:`build_type` [build_type.jacobian]
            The type of test to run
        """
        runner.__init__(self, rtype)
        hdf5_store.__init__(self)
        self.base_chunk_size = self.chunk_size

        self.eval_class = eval_class
        self.mod_test = test_utils.get_run_source()

    def check_file(self, filename, _):
        """Checks file for existing data, returns number of completed runs

        Parameters
        ----------
        filename : str
            Name of file with data

        Returns
        -------
        completed : bool
            True if the file is complete

        """

        Ns = self.gas.n_species
        Nr = self.gas.n_reactions
        Nrev = len([x for x in self.gas.reactions() if x.reversible])
        return self.helper.check_file(filename, Ns, Nr, Nrev, self.current_vecwidth)

    def get_filename(self, state):
        self.current_vecwidth = state['vecsize']
        desc = self.descriptor
        if self.rtype == build_type.jacobian:
            desc += '_sparse' if EnumType(JacobianFormat)(state['sparse'])\
                 == JacobianFormat.sparse else '_full'
        return '{}_{}_{}_{}_{}_{}_{}_{}_{}_{}'.format(
                desc, state['lang'], state['vecsize'], state['order'],
                'w' if state['wide'] else 'd' if state['deep'] else 'par',
                state['platform'], state['rate_spec'],
                'split' if state['split_kernels'] else 'single',
                state['num_cores'], 'conp' if state['conp'] else 'conv') + '_err.npz'

    def pre(self, gas, data, num_conditions, max_vec_width):
        """
        Initializes the validation runner

        Parameters
        ----------
        gas: :class:`cantera.Solution`
            The cantera object representing this mechanism
        data: dict
            A dictionary with keys T, P, V and moles, representing the test data
            for this mechanism
        num_conditions: int
            The number of conditions to test
        max_vec_width: int
            The maximum vector width considered for this test. The number of
            conditions per run must be a multiple of this for proper functioning
        """

        self.gas = gas
        self.gas.basis = 'molar'
        T = data['T']
        P = data['P']
        V = data['V']
        moles = data['moles']
        # get phi vectors
        self.phi_cp = self.get_phi(T, P, V, moles)
        self.phi_cv = self.get_phi(T, V, P, moles)
        # convert to hdf
        self.phi_cp = self.to_file(self.phi_cp, 'phi_cp.hdf5')
        self.phi_cv = self.to_file(self.phi_cv, 'phi_cv.hdf5')
        # and free old data
        del T
        del P
        del V
        del moles
        self.num_conditions = num_conditions
        self.max_vec_width = max_vec_width

        # reset
        self.chunk_size = self.base_chunk_size

        # ensure our chunk size matches vec width
        if self.chunk_size % max_vec_width != 0:
            self.chunk_size = int(
                np.ceil(self.chunk_size / max_vec_width) * max_vec_width)

        self.helper = self.eval_class(gas, num_conditions)
        # and check for helper
        if self.helper.chunk_size % max_vec_width != 0:
            self.helper.chunk_size = self.chunk_size

    def post(self):
        """
        Cleanup HDF5 files
        """
        self.helper.release()
        self.release()

    def arrays_per_run(self, offset, this_run, order, answers, outputs, asplit):
        """
        Converts reference answers & outputs from :class:`pytable.arrays` to
        in-memory :class:`numpy.ndarrays` arrays, applying splitting if necessary

        Parameters
        ----------
        offset: int
            The initial condition offset to use
        this_run: int
            How many initial conditions to select from the offset
        order: ['C', 'F']
            The data ordering
        answers: list of :class:`pytables.Array`
            The reference answers
        outputs: list of :class:`pytables.Array`
            The outputs to check
        asplit: :class:`array_splitter
            The splitting object

        Returns
        -------
        converted_answers: list of :class:`numpy.ndarray`
            The converted in-memory reference arrays
        converted_outputs: list of :class:`numpy.ndarray`
            The converted in-memory outputs
        """

        # outputs require a bit of parsing
        out = []
        ic_mask = np.arange(offset, offset + this_run)
        for i, arr in enumerate(outputs):
            mask = parse_split_index(
                arr, [ic_mask], order, ref_ndim=answers[i].ndim, axis=(0,))
            # next find the grow dimension
            _, grow_dim, split_dim = asplit.split_shape(arr)
            # we need to slice on the grow dim to pull into memory
            # create empty slices in other dimensions -- vecwidth dim is full by
            # defn
            inds = [slice(None) for x in range(arr.ndim)]
            inds[grow_dim] = np.unique(mask[grow_dim])
            out.append(arr[tuple(inds)])

        # simply need to reference and split answers
        answers = [x[offset:offset + this_run, :] for x in answers]
        answers = asplit.split_numpy_arrays(answers)
        return answers, out

    def run(self, state, asplit, dirs, phi_path, data_output):
        """
        Run the validation test for the given state

        Parameters
        ----------
        state: dict
            A dictionary containing the state of the current optimization / language
            / vectorization patterns, etc.
        asplit: :class:`array_splitter`
            The array splitter to use in modifying state arrays
        dirs: dict
            A dictionary of directories to use for building / testing, etc.
            Has the keys "build", "test", "obj" and "run"
        phi_path: str
            The path expected by the generated kernel for the state vector
            phi to be saved in (as a binary file)
        data_output: str
            The file to output the results to

        Returns
        -------
        None
        """

        # get the answer
        phi = self.phi_cp if state['conp'] else self.phi_cv
        self.helper.eval_answer(phi, state)

        my_test = dirs['test']
        my_build = dirs['build']
        my_obj = dirs['obj']

        # compile library as executable
        lib = generate_library(state['lang'], my_build, obj_dir=my_obj,
                               out_dir=my_test, btype=self.rtype, shared=True,
                               as_executable=True)

        # store phi array to file
        np.array(phi, order='C', copy=True).flatten('C').tofile(phi_path)
        # call
        subprocess.check_call([os.path.join(my_test, lib),
                               str(self.num_conditions), str(state['num_cores'])],
                              cwd=my_test)

        answers = self.helper.ref_answers(state)
        outputs = []
        # convert output to hdf5 files
        for name, ref_ans in zip(*(self.helper.output_names, answers)):
            outputs.append(
                self.output_to_pytables(name, my_test, ref_ans, state['order'],
                                        asplit))

        # now loop through the output in error chunks increments to get error
        offset = 0
        # store the error dict
        err_dict = {}
        while offset < self.num_conditions:
            this_run = np.minimum(
                self.chunk_size, self.num_conditions - offset)

            # convert our chunks to workable numpy arrays
            ans, out = self.arrays_per_run(
                offset, this_run, state['order'], answers, outputs, asplit)

            # get error
            err_dict = self.helper.eval_error(
                offset, this_run, state, out, ans, err_dict)

            # finally update the offset
            offset += this_run

        # and write to file
        np.savez(data_output, **err_dict)


class eval(hdf5_store):
    def eval_answer(self, phi, param, state):
        raise NotImplementedError

    def eval_error(self, offset, this_run, state, output, answers, err_dict):
        raise NotImplementedError

    def ref_answers(self, state):
        raise NotImplementedError

    @property
    def output_names(self):
        raise NotImplementedError

    def _check_file(self, err, names, mods):
        try:
            return all(n + mod in err and np.all(np.isfinite(err[n + mod]))
                       for n in names for mod in mods)
        except:
            return False


class spec_rate_eval(eval):
    """
    Helper class for the species rates tester
    """
    def __init__(self, gas, num_conditions, atol=1e-10, rtol=1e-6):
        self.atol = atol
        self.rtol = rtol
        self.molar_rates = np.zeros((num_conditions, gas.n_species - 1))
        self.conp_temperature_rates = np.zeros((num_conditions, 1))
        self.conv_temperature_rates = np.zeros((num_conditions, 1))
        self.conp_extra_rates = np.zeros((num_conditions, 1))
        self.conv_extra_rates = np.zeros((num_conditions, 1))
        self.h = np.zeros((gas.n_species))
        self.u = np.zeros((gas.n_species))
        self.cp = np.zeros((gas.n_species))
        self.cv = np.zeros((gas.n_species))
        self.num_conditions = num_conditions

        # get mappings
        self.fwd_map = np.array(range(gas.n_reactions))
        self.rev_map = np.array(
            [x for x in range(gas.n_reactions) if gas.is_reversible(x)])
        self.thd_map = []
        for x in range(gas.n_reactions):
            try:
                gas.reaction(x).efficiencies
                self.thd_map.append(x)
            except:
                pass
        self.thd_map = np.array(self.thd_map, dtype=np.int32)
        self.rop_fwd_test = np.zeros((num_conditions, self.fwd_map.size))
        self.rop_rev_test = np.zeros((num_conditions, self.rev_map.size))
        self.rop_net_test = np.zeros((num_conditions, self.fwd_map.size))
        # need special maps for rev/thd
        self.rev_to_thd_map = np.where(np.in1d(self.rev_map, self.thd_map))[0]
        self.thd_to_rev_map = np.where(np.in1d(self.thd_map, self.rev_map))[0]
        # it's a pain to actually calcuate this
        # and we don't need it directly, since cantera computes
        # pdep terms in the forward / reverse ROP automatically
        # hence we create it as a placeholder for the testing script
        self.pres_mod_test = np.zeros((num_conditions, self.thd_map.size))

        # molecular weight fraction
        self.mw_frac = 1 - gas.molecular_weights[:-1] / gas.molecular_weights[-1]

        # predefines
        self.specs = gas.species()[:]
        self.gas = gas
        self.evaled = False
        self.name = 'spec'

        super(spec_rate_eval, self).__init__()

    @property
    def output_names(self):
        # outputs
        return ['dphi', 'rop_fwd', 'rop_rev', 'pres_mod', 'rop_net']

    def ref_answers(self, state):
        return self.outputs_cp if state['conp'] else self.outputs_cv

    def eval_answer(self, phi, state):
        def __eval_cp(j, T):
            return self.specs[j].thermo.cp(T)
        eval_cp = np.vectorize(__eval_cp, cache=True)

        def __eval_h(j, T):
            return self.specs[j].thermo.h(T)
        eval_h = np.vectorize(__eval_h, cache=True)

        if not self.evaled:
            ns_range = np.arange(self.gas.n_species)

            T = phi[:, 0]
            P = phi[:, 1] if state['conp'] else phi[:, 2]
            V = phi[:, 2] if state['conp'] else phi[:, 1]
            # it's actually more accurate to set the density
            # (total concentration) due to the cantera internals
            D = P / (ct.gas_constant * T)

            # get the last species's concentrations as D - sum(other species)
            concs = phi[:, 3:] / V[:, np.newaxis]
            last_spec = np.expand_dims(D - np.sum(concs, axis=1), 1)
            concs = np.concatenate((concs, last_spec), axis=1)

            self.gas.basis = 'molar'
            with np.errstate(divide='ignore', invalid='ignore'):
                for i in range(self.num_conditions):
                    if not i % 10000:
                        print(i)
                    # first, set T / D
                    self.gas.TD = T[i], D[i]
                    # now set concentrations
                    self.gas.concentrations = concs[i]
                    # assert allclose
                    assert np.allclose(self.gas.T, T[i], atol=1e-12)
                    assert np.allclose(self.gas.density, D[i], atol=1e-12)
                    assert np.allclose(self.gas.concentrations, concs[i], atol=1e-12)
                    # get molar species rates
                    spec_rates = self.gas.net_production_rates[:]
                    self.molar_rates[i, :] = spec_rates[:-1] * V[i]
                    # info vars
                    self.rop_fwd_test[i, :] = self.gas.forward_rates_of_progress[:]
                    self.rop_rev_test[i, :] = self.gas.reverse_rates_of_progress[:][
                        self.rev_map]
                    self.rop_net_test[i, :] = self.gas.net_rates_of_progress[:]

                    # find temperature rates
                    cp = eval_cp(ns_range, T[i])
                    h = eval_h(ns_range, T[i])
                    cv = cp - ct.gas_constant
                    u = h - T[i] * ct.gas_constant
                    np.divide(-np.dot(h, spec_rates), np.dot(cp, concs[i]),
                              out=self.conp_temperature_rates[i, :])
                    np.divide(-np.dot(u, spec_rates), np.dot(cv, concs[i]),
                              out=self.conv_temperature_rates[i, :])

                    # finally find extra variable rates
                    self.conp_extra_rates[i] = V[i] * (
                        T[i] * ct.gas_constant * np.sum(
                            self.mw_frac * spec_rates[:-1]) / P[i] +
                        self.conp_temperature_rates[i, :] / T[i])
                    self.conv_extra_rates[i] = (
                        P[i] / T[i]) * self.conv_temperature_rates[i, :] + \
                        T[i] * ct.gas_constant * np.sum(
                            self.mw_frac * spec_rates[:-1])

            def _dphi(conp):
                temperature_rates = self.conp_temperature_rates if conp \
                    else self.conv_temperature_rates
                extra_rates = self.conp_extra_rates if conp else\
                    self.conv_extra_rates
                return np.concatenate((temperature_rates, extra_rates,
                                      self.molar_rates), axis=1)

            # finally convert to HDF5
            self.dphi_cp = _dphi(True)
            self.dphi_cp = self.to_file(self.dphi_cp, 'dphi_cp.hdf5')
            self.dphi_cv = _dphi(False)
            self.dphi_cv = self.to_file(self.dphi_cp, 'dphi_cv.hdf5')
            self.molar_rates = self.to_file(self.molar_rates, 'molar_rates.hdf5')
            self.rop_fwd_test = self.to_file(self.rop_fwd_test, 'rop_fwd_test.hdf5')
            self.rop_rev_test = self.to_file(self.rop_rev_test, 'rop_rev_test.hdf5')
            self.rop_net_test = self.to_file(self.rop_net_test, 'rop_net_test.hdf5')
            self.conp_extra_rates = self.to_file(
                self.conp_extra_rates, 'conp_extra_rates.hdf5')
            self.conv_extra_rates = self.to_file(
                self.conv_extra_rates, 'conv_extra_rates.hdf5')
            # and store outputs
            outputs = [self.rop_fwd_test, self.rop_rev_test, self.pres_mod_test,
                       self.rop_net_test]
            self.outputs_cp = [self.dphi_cp] + outputs
            self.outputs_cv = [self.dphi_cv] + outputs
            self.evaled = True

    def eval_error(self, offset, this_run, state, output, answers, err_dict):
        # get indicies
        pmod_ind = next(
            i for i, x in enumerate(self.output_names) if 'pres_mod' in x)
        fwd_ind = next(
            i for i, x in enumerate(self.output_names) if 'rop_fwd' in x)
        rev_ind = next(
            i for i, x in enumerate(self.output_names) if 'rop_rev' in x)

        # pull order
        order = state['order']

        # multiply fwd/rev ROP by pressure rates to compare w/ Cantera
        # fwd
        fwd_masked = parse_split_index(output[fwd_ind], self.thd_map, order)
        output[fwd_ind][fwd_masked] *= output[pmod_ind][parse_split_index(
            output[pmod_ind], np.arange(self.thd_map.size, dtype=np.int32),
            order)]
        # rev
        rev_masked = parse_split_index(output[rev_ind], self.rev_to_thd_map,
                                       order)
        # thd to rev map already in thd index list, so don't need to do arange
        output[rev_ind][rev_masked] *= output[pmod_ind][parse_split_index(
            output[pmod_ind], self.thd_to_rev_map, order)]

        # simple test to get IC index
        mask_dummy = parse_split_index(answers[0], np.array([this_run]),
                                       order=order, axis=(0,))
        IC_axis = next(i for i, ax in enumerate(mask_dummy) if ax != slice(None))

        # load output
        for name, out, check_arr in zip(*(self.output_names, output, answers)):
            if name == 'pres_mod':
                continue
            # get err
            err = np.abs(out - check_arr)
            err_compare = err / (self.atol + self.rtol * np.abs(check_arr))

            # find the split, if any
            def __get_locs_and_mask(arr, locs=None, inds=None):
                size = int(np.prod(arr.shape) / this_run)
                if inds is None:
                    inds = np.arange(size, dtype=np.int32)
                mask = parse_split_index(arr, inds, order, axis=(1,))
                # get maximum relative error locations
                if locs is None:
                    locs = np.argmax(err_compare[mask], axis=IC_axis)
                if locs.ndim >= 2:
                    # C-split, need to convert to two 1-d arrays
                    lrange = np.arange(locs[0].size, dtype=np.int32)
                    fixed = [np.zeros(size, dtype=np.int32),
                             np.zeros(size, dtype=np.int32)]
                    for i, x in enumerate(locs):
                        # find max in err_locs
                        ind = np.argmax(err_compare[x, [i], lrange])
                        fixed[0][i] = x[ind]
                        fixed[1][i] = ind
                    mask = (fixed[0], mask[1], fixed[1])
                else:
                    mask = tuple(
                        x if i != IC_axis else locs for i, x in enumerate(mask))
                return locs, mask

            err_locs, err_mask = __get_locs_and_mask(err_compare)

            # take err norm
            err_comp_store = err_compare[err_mask]
            err_inf = err[err_mask]
            if name == 'rop_net':
                # need to find the fwd / rop error at the max locations
                # here
                rop_fwd_err = np.abs(output[fwd_ind][err_mask] -
                                     answers[fwd_ind][err_mask])

                rop_rev_err = np.zeros(rop_fwd_err.size)
                # get err locs for rev reactions
                rev_err_locs = err_locs[self.rev_map]
                # get reversible mask using the error locations for the reversible
                # reactions, and the rev_map size for the mask
                _, rev_mask = __get_locs_and_mask(
                    output[rev_ind], locs=rev_err_locs,
                    inds=np.arange(self.rev_map.size))
                # and finally update
                rop_rev_err[self.rev_map] = np.abs(
                    output[rev_ind][rev_mask] -
                    answers[rev_ind][rev_mask])
                # now find maximum of error in fwd / rev ROP
                rop_component_error = np.maximum(rop_fwd_err, rop_rev_err)

            if name not in err_dict:
                err_dict[name] = np.zeros_like(err_inf)
                err_dict[name + '_value'] = np.zeros_like(err_inf)
                err_dict[name + '_store'] = np.zeros_like(err_inf)
                if name == 'rop_net':
                    err_dict['rop_component'] = np.zeros_like(err_inf)
            # get locations to update
            update_locs = np.where(
                err_comp_store >= err_dict[name + '_store'])
            # and update
            err_dict[name][update_locs] = err_inf[update_locs]
            err_dict[
                name + '_store'][update_locs] = err_comp_store[update_locs]
            # need to take max and update precision as necessary
            if name == 'rop_net':
                err_dict['rop_component'][
                    update_locs] = rop_component_error[update_locs]
            # update the values for normalization
            err_dict[name + '_value'][update_locs] = check_arr[err_mask][update_locs]

        return err_dict

    def _check_size(self, err, names, mods, size, vecwidth):
        non_conformant = [n + mod for n in names for mod in mods
                          if err[n + mod].size != size]
        if not non_conformant:
            return True
        return all(np.all(err[x][size:] == 0) and err[x].size % vecwidth == 0
                   for x in non_conformant)

    def check_file(self, filename, Ns, Nr, Nrev, current_vecwidth):
        """
        Checks a species validation file for completion

        Parameters
        ----------
        filename: str
            The file to check
        Ns: int
            The number of species in the mechanism
        Nr: int
            The number of reactions in the mechanism
        Nrev: int
            The number of reversible reactions in the mechanism
        current_vecwidth: int
            The curent vector width being used.  If the current state results in
            an array split, this may make the stored error arrays larger than
            expected, so we must check that they are divisible by current_vecwidth
            and the extra entries are identically zero

        Returns
        -------
        valid: bool
            If true, the test case is complete and can be skipped
        """

        try:
            err = np.load(filename)
            names = ['rop_fwd', 'rop_rev', 'rop_net', 'dphi']
            mods = ['', '_value', '_store']
            # check that we have all expected keys, and there is no nan's, etc.
            allclear = self._check_file(err, names, mods)
            # check Nr size
            allclear = allclear and self._check_size(
                err, [x for x in names if ('rop_fwd' in x or 'rop_net' in x)],
                mods, Nr, current_vecwidth)
            # check reversible
            allclear = allclear and self._check_size(
                err, [x for x in names if 'rop_rev' in x],
                mods, Nrev, current_vecwidth)
            # check Ns size
            allclear = allclear and self._check_size(
                err, [x for x in names if 'phi' in x], mods, Ns + 1,
                current_vecwidth)
            return allclear
        except:
            return False


class jacobian_eval(eval):
    """
    Helper class for the Jacobian tester
    """
    def __init__(self, gas, num_conditions, atol=1e-2, rtol=1e-6):
        self.atol = atol
        self.rtol = rtol
        self.evaled = False

        self.num_conditions = num_conditions
        # read mech
        _, self.specs, self.reacs = read_mech_ct(gas=gas)

        # predefines
        self.gas = gas
        self.evaled = False
        self.name = 'jac'
        ret = determine_jac_inds(self.reacs, self.specs, RateSpecialization.fixed)
        self.inds = ret['jac_inds']
        self.non_zero_specs = ret['net_per_spec']['map']
        if self.gas.n_species - 1 in self.non_zero_specs:
            # remove last species
            self.non_zero_specs = self.non_zero_specs[:-1]

        super(jacobian_eval, self).__init__()

    def __sparsify(self, jac, name, order, check=True):
        # get the sparse indicies
        inds = self.inds['flat_' + order]
        if check:
            # get check array as max(|jac|) down the IC axis
            check = np.amax(jac, axis=0)
            # set T / parameter derivativs to non-zero by assumption
            check[self.non_zero_specs + 2, :2] = 1
            # convert nan's or inf's to some non-zero number
            check[np.where(~np.isfinite(check))] = 1
            # get masked where > 0
            mask = np.asarray(np.where(ma.masked_where(check != 0, check).mask)).T
            # and check that all our non-zero entries are in the sparse indicies
            if inNd(mask, inds).size != mask.shape[0]:
                logger = logging.getLogger(__name__)
                logger.warn(
                    "Autodifferentiated Jacobian sparsity pattern "
                    "does not match pyJac's.  There are legitimate reasons"
                    "why this might be the case -- e.g., matching "
                    "arrhenius parameters for two reactions containing "
                    "the same species, with one reaction involving the "
                    "(selected) last species in the mechanism -- if you "
                    "are not sure why this error is appearing, feel free to "
                    "contact the developers to ensure this is not a bug.")
            del mask
            del check

        # and finally return the sparse array
        # need to do this in chunks to avoid memory errors
        num_conds = jac.shape[0]
        shape = (0, inds[:, 0].size)
        out = self.open_for_chunked_write(name + '.hdf5', shape, num_conds)
        threshold = 0
        for offset in range(0, num_conds, self.chunk_size):
            end = np.minimum(num_conds, offset + self.chunk_size)
            # need to preslice the pytables array to get numpy indexing
            jtemp = jac[offset:end][:, inds[:, 0], inds[:, 1]]
            threshold += np.linalg.norm(jtemp) ** 2
            out.append(jtemp)
        return out, np.sqrt(threshold)

    def __fast_jac(self, conp, sparse, order):
        jac = None
        # check for stored jacobian
        name = 'fd_jac_' + ('cp' if conp else 'cv')
        if hasattr(self, name):
            jac = getattr(self, name)

        if jac is None:
            return None

        if sparse == 'sparse':
            # check for stored sparse matrix
            name += '_sp'
            if hasattr(self, name):
                return getattr(self, name)
        return jac

    def eval_answer(self, phi, state):
        jac = self.__fast_jac(state['conp'], state['sparse'], state['order'])
        if jac is not None:
            return jac

        # number of IC's
        num_conds = self.num_conditions
        # open the pytables file for writing
        name = 'fd_jac_' + ('cp' if state['conp'] else 'cv')
        jac = self.open_for_chunked_write(
            name + '.hdf5', (0, len(self.specs) + 1, len(self.specs) + 1),
            num_conds)

        # create the "store" for the AD-jacobian eval
        # mask phi to get rid of parameter stored in there for data input
        phi_mask = np.array([0] + list(range(2, phi.shape[1])))
        # note that we have to do this piecewise in order to avoid memory overflows
        # eval jacobian
        from ..tests.test_jacobian import _get_fd_jacobian
        P = phi[:, 1] if state['conp'] else phi[:, 2]
        V = phi[:, 2] if state['conp'] else phi[:, 1]

        def __get_state(offset, end):
            end = np.minimum(end, num_conds)
            return type('', (object,), {
                'reacs': self.reacs,
                'specs': self.specs,
                'phi_cp': (phi[offset:end, phi_mask].copy() if state['conp']
                           else None),
                'phi_cv': (phi[offset:end, phi_mask].copy() if not state['conp']
                           else None),
                'P': P[offset:end],
                'V': V[offset:end],
                'test_size': end - offset
                })

        # pregenerated kernel for speed
        self.store = __get_state(0, self.chunk_size)
        pregen = _get_fd_jacobian(self, self.store.test_size, state['conp'],
                                  None, True)
        store_size = self.store.test_size
        threshold = 0
        for offset in range(0, num_conds, self.chunk_size):
            self.store = __get_state(offset, offset + self.chunk_size)
            if self.store.test_size != store_size:
                # need to regenerate
                pregen = _get_fd_jacobian(self, self.store.test_size, state['conp'],
                                          None, True)
                # and store size to check for regen
                store_size = self.store.test_size

            # and add to Jacobian
            jtemp = _get_fd_jacobian(self, self.store.test_size, state['conp'],
                                     pregen)
            # get threshold
            threshold += np.linalg.norm(jtemp) ** 2
            # and add to data array
            jac.append(jtemp)

        # and reload for reading
        jac = self.open_for_read(name + '.hdf5')
        # store for later use
        setattr(self, name, jac)

        # store threshold for single computation
        thresh_name = 'threshold_' + 'cp' if state['conp'] else 'cv'
        setattr(self, thresh_name, np.sqrt(threshold))

        if state['sparse'] == 'sparse':
            name += '_sp'
            jac, threshold = self.__sparsify(jac, name, state['order'], check=True)
            # store
            setattr(self, name, jac)
            setattr(self, thresh_name + '_sp', threshold)

        return jac

    def threshold(self, state):
        # find appropriate threshold from state
        name = 'threshold_' + 'cp' if state['conp'] else 'cv'
        if state['sparse'] == 'sparse':
            name += '_sp'
        return getattr(self, name)

    @property
    def output_names(self):
        # outputs
        return ['jac']

    def ref_answers(self, state):
        return [self.__fast_jac(state['conp'], state['sparse'], state['order'])]

    def eval_error(self, offset, this_run, state, outputs, answers, err_dict):
        def __update_key(key, value, op='norm'):
            if key in err_dict:
                if op == 'norm':
                    # update sqrt in correct way
                    # i.e. sqrt(sqrt(a^2)^2 + sqrt(b^2)^2) == sqrt(a^2 + b^2)
                    value = np.sqrt(
                        err_dict[key] * err_dict[key] + value * value)
                elif op == 'max':
                    value = np.maximum(err_dict[key], value)
                else:
                    raise NotImplementedError(op)
            err_dict[key] = value

        threshold = self.threshold(state)
        # load output
        for out, ans in zip(*(outputs, answers)):
            # get err
            err = np.abs(out - ans)
            # do these once
            out = np.abs(out)
            denom = np.abs(ans)
            # regular frobenieus norm, have to filter out zero entries for our
            # norm here
            non_zero = np.where(denom > 0)
            __update_key('jac', np.linalg.norm(err[non_zero] / denom[non_zero]))
            del non_zero
            zero = np.where(denom == 0)
            __update_key('jac_zero', np.linalg.norm(err[zero]))
            del zero
            # norm suggested by lapack
            __update_key('jac_lapack', np.linalg.norm(err) / np.linalg.norm(
                denom))

            # thresholded error
            locs = np.where(out > threshold / 1.e20)
            thresholded_err = err[locs] / denom[locs]
            amax = np.argmax(thresholded_err)
            __update_key('jac_thresholded_20', np.linalg.norm(thresholded_err))
            del thresholded_err
            __update_key('jac_thresholded_20_PJ_amax', out[locs][amax], op='max')
            __update_key('jac_thresholded_20_AD_amax', denom[locs][amax], op='max')
            del locs

            locs = np.where(out > threshold / 1.e15)
            thresholded_err = err[locs] / denom[locs]
            amax = np.argmax(thresholded_err)
            __update_key('jac_thresholded_15', np.linalg.norm(thresholded_err))
            del thresholded_err
            __update_key('jac_thresholded_15_PJ_amax', out[locs][amax], op='max')
            __update_key('jac_thresholded_15_AD_amax', denom[locs][amax], op='max')

            # largest relative errors for different absolute toleratnces
            denom = self.rtol * denom
            for mul in [1, 10, 100, 1000]:
                atol = self.atol * mul
                err_weighted = err / (atol + denom)
                amax = np.unravel_index(np.argmax(err_weighted), err_weighted.shape)
                __update_key('jac_weighted_{}'.format(atol), np.linalg.norm(
                             err_weighted))
                del err_weighted
                __update_key('jac_weighted_{}_PJ_amax'.format(atol), out[amax],
                             op='max')
                __update_key('jac_weighted_{}_AD_amax'.format(atol),
                             denom[amax] / self.rtol, op='max')

            # info values for lookup
            __update_key('jac_max_value', np.amax(out), op='max')
            __update_key('jac_threshold_value', threshold, op='max')

        return err_dict

    def check_file(self, filename, Ns, Nr, Nrev, current_vecwidth):
        """
        Checks a jacobian validation file for completion

        Parameters
        ----------
        filename: str
            The file to check
        Ns: int
            Unused
        Nr: int
            Unused
        Nrev: int
            Unused
        current_vecwidth: int
            Unused

        Returns
        -------
        valid: bool
            If true, the test case is complete and can be skipped
        """

        try:
            err = np.load(filename)
            # check basic error stats
            names = ['jac']
            mods = ['', '_zero', '_lapack']
            # check that we have all expected keys, and there is no nan's, etc.
            allclear = self._check_file(err, names, mods)
            # check thresholded error
            names = ['jac_thresholded_15', 'jac_thresholded_20']
            mods = ['', '_PJ_amax', '_AD_amax']
            # check that we have all expected keys, and there is no nan's, etc.
            allclear = allclear and self._check_file(err, names, mods)
            # check that we have the weighted jacobian error
            names = ['jac_weighted_' + x for x in ['0.01', '0.1', '1.0', '10.0']]
            mods = ['', '_PJ_amax', '_AD_amax']
            allclear = allclear and self._check_file(err, names, mods)
            # check for max / threshold value
            names = ['jac_']
            mods = ['max_value', 'threshold_value']
            allclear = allclear and self._check_file(err, names, mods)
            return allclear
        except:
            return False


@nottest
def species_rate_tester(work_dir='error_checking', test_platform=None, prefix='',
                        mem_limits=''):
    """Runs validation testing on pyJac's species_rate kernel, reading a series
    of mechanisms and datafiles from the :param:`work_dir`, and outputting
    a numpy zip file (.npz) with the error of various outputs (rhs vector, ROP, etc.)
    as compared to Cantera, for each configuration tested.

    Parameters
    ----------
    work_dir : str
        Working directory with mechanisms and for data

    Returns
    -------
    None

    """

    raise_on_missing = True
    if not test_platform:
        # pull default test platforms if available
        test_platform = get_platform_file()
        # and let the tester know we can pull default opencl values if not found
        raise_on_missing = False
    if not mem_limits:
        # pull user specified memory limits if available
        mem_limits = get_mem_limits_file()

    valid = validation_runner(spec_rate_eval, build_type.species_rates)
    _run_mechanism_tests(work_dir, test_platform, prefix, valid,
                         mem_limits, raise_on_missing=raise_on_missing)


@nottest
def jacobian_tester(work_dir='error_checking', test_platform=None, prefix='',
                    mem_limits=''):
    """Runs validation testing on pyJac's jacobian kernel, reading a series
    of mechanisms and datafiles from the :param:`work_dir`, and outputting
    a numpy zip file (.npz) with the error of Jacobian as compared to a
    autodifferentiated reference answer, for each configuration tested.

    Parameters
    ----------
    work_dir : str
        Working directory with mechanisms and for data

    Returns
    -------
    None

    """

    raise_on_missing = True
    if not test_platform:
        # pull default test platforms if available
        test_platform = get_platform_file()
        # and let the tester know we can pull default opencl values if not found
        raise_on_missing = False
    if not mem_limits:
        # pull user specified memory limits if available
        mem_limits = get_mem_limits_file()

    valid = validation_runner(jacobian_eval, build_type.jacobian)
    _run_mechanism_tests(work_dir, test_platform, prefix, valid,
                         mem_limits, raise_on_missing=raise_on_missing)
