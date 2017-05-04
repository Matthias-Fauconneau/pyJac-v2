from __future__ import print_function

# package imports
from enum import IntEnum
import loopy as lp
from loopy.kernel.data import temp_var_scope as scopes
from loopy.target.c.c_execution import CppCompiler
import numpy as np
import pyopencl as cl
from .. import utils
import os
import stat

# local imports
from ..utils import check_lang
from .loopy_edit_script import substitute as codefix

# make loopy's logging less verbose
import logging
from string import Template
logging.getLogger('loopy').setLevel(logging.WARNING)

edit_script = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                           'loopy_edit_script.py')
adept_edit_script = os.path.join(os.path.abspath(os.path.dirname(__file__)),
                                 'adept_edit_script.py')


class RateSpecialization(IntEnum):
    fixed = 0,
    hybrid = 1,
    full = 2


class loopy_options(object):

    """
    Loopy Objects class

    Attributes
    ----------
    width : int
        If not None, the SIMD lane/SIMT block width.
        Cannot be specified along with depth
    depth : int
        If not None, the SIMD lane/SIMT block depth.
        Cannot be specified along with width
    ilp : bool
        If True, use the ILP tag on the species loop.
        Cannot be specified along with unr
    unr : int
        If not None, the unroll length to apply to the species loop.
        Cannot be specified along with ilp
    order : {'C', 'F'}
        The memory layout of the arrays, C (row major)
        or Fortran (column major)
    lang : ['opencl', 'c', 'cuda']
        One of the supported languages
    rate_spec : RateSpecialization
        Controls the level to which Arrenhius rate evaluations are specialized
    rate_spec_kernels : bool
        If True, break different Arrenhius rate specializations into different
        kernels
    rop_net_kernels : bool
        If True, break different ROP values (fwd / back / pdep) into different
        kernels
    spec_rates_sum_over_reac : bool
        Controls the manner in which the species rates are calculated
        *  If True, the summation occurs as:
            for reac:
                rate = reac_rates[reac]
                for spec in reac:
                    spec_rate[spec] += nu(spec, reac) * reac_rate
        *  If False, the summation occurs as:
            for spec:
                for reac in spec_to_reacs[spec]:
                    rate = reac_rates[reac]
                    spec_rate[spec] += nu(spec, reac) * reac_rate
        *  Of these, the first choice appears to be slightly more efficient,
           likely due to less thread divergence / SIMD wastage, HOWEVER it
           causes issues with deep vectorization -- an atomic update of the
           spec_rate is needed, and doesn't appear to work in loopy/OpenCL1.2,
           hence, we supply both.
        *  Note that if True, and deep vectorization is passed this switch will
        be ignored and a warning will be issued
    platform : {'CPU', 'GPU', or other vendor specific name}
        The OpenCL platform to run on.
        *   If 'CPU' or 'GPU', the first available matching platform will be
            used
        *   If a vendor specific string, it will be passed to pyopencl to get
            the platform
    knl_type : ['mask', 'map']
        The type of opencl kernels to create:
        * A masked kernel loops over all available indicies (e.g. reactions)
            and uses a mask to determine what to do.
            Note: **Suggested for deep vectorization**
        * A mapped kernel loops over only necessary indicies
            (e.g. plog reactions vs all) This may be faster for a
            non-vectorized kernel or wide-vectorization
    """

    def __init__(self,
                 width=None,
                 depth=None,
                 ilp=False,
                 unr=None,
                 lang='opencl',
                 order='C',
                 rate_spec=RateSpecialization.fixed,
                 rate_spec_kernels=False,
                 rop_net_kernels=False,
                 spec_rates_sum_over_reac=True,
                 platform='',
                 knl_type='map',
                 auto_diff=False):
        self.width = width
        self.depth = depth
        if not utils.can_vectorize_lang[lang]:
            assert width is None and depth is None, (
                "Can't use a vectorized form with unvectorizable language,"
                " {}".format(lang))
        self.ilp = ilp
        self.unr = unr
        check_lang(lang)
        self.lang = lang
        assert order in ['C', 'F'], 'Order {} unrecognized'.format(order)
        self.order = order
        self.rate_spec = rate_spec
        self.rate_spec_kernels = rate_spec_kernels
        self.rop_net_kernels = rop_net_kernels
        self.spec_rates_sum_over_reac = spec_rates_sum_over_reac
        self.platform = platform
        self.device_type = None
        self.device = None
        assert knl_type in ['mask', 'map']
        self.knl_type = knl_type
        self.auto_diff = auto_diff
        # need to find the first platform that has the device of the correct
        # type
        if self.lang == 'opencl' and self.platform:
            self.device_type = cl.device_type.ALL
            check_name = None
            if platform.lower() == 'cpu':
                self.device_type = cl.device_type.CPU
            elif platform.lower() == 'gpu':
                self.device_type = cl.device_type.GPU
            else:
                check_name = self.platform
            self.platform = None
            platforms = cl.get_platforms()
            for p in platforms:
                try:
                    cl.Context(
                        dev_type=self.device_type,
                        properties=[(cl.context_properties.PLATFORM, p)])
                    if not check_name or check_name.lower() in p.get_info(
                            cl.platform_info.NAME).lower():
                        self.platform = p
                        break
                except cl.cffi_cl.RuntimeError:
                    pass
            if not self.platform:
                raise Exception(
                    'Cannot find matching platform for string: {}'.format(
                        platform))
            # finally a matching device
            self.device = self.platform.get_devices(
                device_type=self.device_type)
            if not self.device:
                raise Exception(
                    'Cannot find devices of type {} on platform {}'.format(
                        self.device_type, self.platform))
            self.device = self.device[0]
            self.device_type = self.device.get_info(cl.device_info.TYPE)


def get_device_list():
    """
    Returns the available pyopencl devices

    Parameters
    ----------
    None

    Returns
    -------
    devices : list of :class:`pyopencl.Device`
        The devices recognized by pyopencl
    """
    device_list = []
    for p in cl.get_platforms():
        device_list.append(p.get_devices())
    # don't need multiple gpu's etc.
    for i in range(len(device_list)):
        device_list[i] = device_list[i][0]

    return device_list


def get_context(device='0'):
    """
    Simple method to generate a pyopencl context

    Parameters
    ----------
    device : str or :class:`pyopencl.Device`
        The pyopencl string (or device class) denoting the device to use,
        defaults to '0'

    Returns
    -------
    ctx : :class:`pyopencl.Context`
        The running context
    queue : :class:`pyopencl.Queue`
        The command queue
    """

    os.environ['PYOPENCL_COMPILER_OUTPUT'] = '1'
    if isinstance(device, str):
        os.environ['PYOPENCL_CTX'] = device
        ctx = cl.create_some_context(interactive=False)
    else:
        ctx = cl.Context(devices=[device])

    lp.set_caching_enabled(False)
    queue = cl.CommandQueue(ctx)
    return ctx, queue


def get_header(knl):
    """
    Returns header definition code for a :class:`loopy.LoopKernel`

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel`
        The kernel to generate a header definition for

    Returns
    -------
    Generated device header code

    Notes
    -----
    The kernel's Target and name should be set for proper functioning
    """

    return str(lp.generate_header(knl)[0])


def __set_editor(knl, script):
    # set the edit script as the 'editor'
    os.environ['EDITOR'] = script

    # turn on code editing
    edit_knl = lp.set_options(knl, edit_code=True)

    return edit_knl


def set_editor(knl):
    """
    Returns a copy of knl set up for various automated bug-fixes

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel`
        The kernel to generate code for

    Returns
    -------
    edit_knl : :class:`loopy.LoopKernel`
        The kernel set up for editing
    """

    return __set_editor(knl, edit_script)


def set_adept_editor(knl,
                     problem_size=8192,
                     independent_variable=None,
                     dependent_variable=None,
                     output=None):
    """
    Returns a copy of knl set up for various automated bug-fixes

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel`
        The kernel to generate code for
    problem_size : int
        The size of the testing problem
    independent_variable : :class:`array_creator.creator`
        The independent variables to compute the Jacobian with respect to
    dependent_variable : :class:`array_creator.creator`
        The dependent variables to find the Jacobian of
    output : :class:`array_creator.creator`
        The array to store the column-major
        Jacobian in, ordered by thermo-chemical condition

    Returns
    -------
    edit_knl : :class:`loopy.LoopKernel`
        The kernel set up for editing
    """

    # load template
    with open(adept_edit_script + '.in', 'r') as file:
        src = Template(file.read())

    def __get_size_and_stringify(variable):
        sizes = variable.shape
        if variable.order == 'F':
            indicies = ['j', 'i']
        else:
            indicies = ['i', 'j']

        assert len(variable.shape) == 2

        out_str = variable.name + '[{index}]'
        out_index = ''
        offset = 1
        out_size = None
        for size, index in zip(sizes, indicies):
            if out_index:
                out_index += ' + '
            if size != problem_size:
                assert out_size is None, (
                    'Cannot determine variable size!')
                out_size = size
            out_index += '{} * {}'.format(index, offset)
            offset *= size

        return out_size, out_str.format(index=out_index)

    # find the dimension / string representation of the independent
    # and dependent variables

    indep_size, indep = __get_size_and_stringify(independent_variable)
    dep_size, dep = __get_size_and_stringify(dependent_variable)

    # initializers
    init_template = Template("""
        std::vector ad_${name} (${size});
        for (int i = 0; i < ${size}; ++i)
        {
            ad_${name}.set_value(${indexed})
        }
        """)
    initializers = []
    for arg in knl.args:
        if arg.name != dependent_variable.name \
                and not isinstance(arg, lp.ValueArg):
            size, indexed = __get_size_and_stringify(arg)
            # add initializer
            initializers.append(init_template.substitute(
                name=arg.name,
                size=size,
                indexed=indexed
                ))
    # find the output name
    output_name = '&' + output.name + '[ad_j * {dep_size} * {indep_size}]'.format(
        dep_size=dep_size, indep_size=indep_size)

    # get header defn
    header = get_header(knl)
    header = header[:header.index(';')]

    # fill in template
    with open(adept_edit_script, 'w') as file:
        file.write(src.substitute(
            problem_size=problem_size,
            ad_indep_name='ad_' + independent_variable.name,
            indep=indep,
            indep_name=independent_variable.name,
            indep_size=indep_size,
            ad_dep_name='ad_' + dependent_variable.name,
            dep=dep,
            dep_name=dependent_variable.name,
            dep_size=dep_size,
            output=output_name,
            function_defn=header,
            kernel_call='{name}({args});'.format(
                name=knl.name,
                args=', '.join('ad_' + arg.name for arg in knl.args)),
            initializers='\n'.join(initializers)
        ))

    # and make it executable
    st = os.stat(adept_edit_script)
    os.chmod(adept_edit_script, st.st_mode | stat.S_IEXEC)

    return __set_editor(knl, adept_edit_script)


def get_code(knl):
    """
    Returns the device code for a :class:`loopy.LoopKernel`

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel`
        The kernel to generate code for

    Returns
    -------
    Generated device code

    Notes
    -----
    The kernel's Target and name should be set for proper functioning
    """

    code, _ = lp.generate_code(knl)
    return codefix('stdin', text_in=code)


class kernel_call(object):

    """
    A wrapper for the various parameters (e.g. args, masks, etc.)
    for calling / executing a loopy kernel
    """

    def __init__(self, name, ref_answer, compare_axis=0, compare_mask=None,
                 out_mask=None, input_mask=[], strict_name_match=False,
                 chain=None, check=True, post_process=None,
                 **input_args):
        """
        The initializer for the :class:`kernel_call` object

        Parameters
        ----------
        name : str
            The kernel name, used for matching
        ref_answer : :class:`numpy.ndarray` or list of :class:`numpy.ndarray`
            The reference answer to compare to
        compare_axis : int, optional
            An axis to apply the compare_mask along, unused if compare_mask
            is None
        compare_mask : :class:`numpy.ndarray` or :class:`numpy.ndarray`
            An optional list of indexes to compare, useful when the kernel only
            computes partial results.
            Should match length of ref_answer
        out_mask : int, optional
            The index(ices) of the returned array to aggregate.
            Should match length of ref_answer
        input_mask : list of str or function, optional
            An optional list of input arguements to filter out
            If a function is passed, the expected signature is along the
            lines of:
                def fcn(self, arg_name):
                    ...
            and returns True iff this arg_name should be used
        strict_name_match : bool, optional
            If true, only kernels exactly matching this name will be excecuted
            Defaut is False
        chain : function, optional
            If not None, a function of signature similar to:
                def fcn(self, out_values):
                    ....
            is expected.
            This function should take the output values from a previous
            kernel call, and place in the input args for this kernel call as
            necessary
        post_process : function, optional
            If not None, a function of signature similar to:
                def fcn(self, out_values):
                    ....
            is expected.  This function should take the output values from this
            kernel call, and process them as expected to compare to results.
            Currently used only in comparison of reaction rates to
            Cantera (to deal w/ falloff etc.)
        check : bool
            If False, do not check result (useful when chaining to check only
            the last result)
            Default is True
        input_args : dict of `numpy.array`s
            The arguements to supply to the kernel

        Returns
        -------
        out_ref : list of :class:`numpy.ndarray`
            The value(s) of the evaluated :class:`loopy.LoopKernel`
        """

        self.name = name
        self.ref_answer = ref_answer
        if isinstance(ref_answer, list):
            num_check = len(ref_answer)
        else:
            num_check = 1
            self.ref_answer = [ref_answer]
        self.compare_axis = compare_axis
        if compare_mask is not None:
            self.compare_mask = compare_mask
        else:
            self.compare_mask = [None for i in range(num_check)]
        self.out_mask = out_mask
        self.input_mask = input_mask
        self.input_args = input_args
        self.strict_name_match = strict_name_match
        self.kernel_args = None
        self.chain = chain
        self.post_process = post_process
        self.check = check
        self.current_order = None

    def is_my_kernel(self, knl):
        """
        Tests whether this kernel should be run with this call

        Parameters
        ----------
        knl : :class:`loopy.LoopKernel`
            The kernel to call
        """

        if self.strict_name_match:
            return self.name == knl.name
        return True

    def set_state(self, order='F'):
        """
        Updates the kernel arguements, and  and compare axis to the order given
        If the 'arg' is a function, it will be called to get the correct answer

        Parameters
        ----------
        order : {'C', 'F'}
            The memory layout of the arrays, C (row major) or
            Fortran (column major)
        """
        self.compare_axis = 1  # always C order now (in numpy comparisons)
        self.current_order = order

        # filter out bad input
        args_copy = self.input_args.copy()
        if self.input_mask is not None:
            if hasattr(self.input_mask, '__call__'):
                args_copy = {x: args_copy[x] for x in args_copy
                             if self.input_mask(self, x)}
            else:
                args_copy = {x: args_copy[x] for x in args_copy
                             if x not in self.input_mask}

        for key in args_copy:
            if hasattr(args_copy[key], '__call__'):
                # it's a function
                args_copy[key] = args_copy[key](order)

        self.kernel_args = args_copy
        self.transformed_ref_ans = [np.array(ans, order=order, copy=True)
                                    for ans in self.ref_answer]

    def __call__(self, knl, queue):
        """
        Calls the kernel, filtering input / output args as required

        Parameters
        ----------
        knl : :class:`loopy.LoopKernel`
            The kernel to call
        queue : :class:`pyopencl.Queue`
            The command queue

        Returns
        -------
        out : list of :class:`numpy.ndarray`
            The (potentially filtered) output variables
        """

        try:
            evt, out = knl(queue, out_host=True, **self.kernel_args)
        except Exception as e:
            raise e

        if self.out_mask is not None:
            return [out[ind] for ind in self.out_mask]
        else:
            return [out[0]]

    def compare(self, output_variables):
        """
        Compare the output variables to the given reference answer

        Parameters
        ----------
        output_variables : :class:`numpy.ndarray` or :class:`numpy.ndarray`
            The output variables to test

        Returns
        -------
        match : bool
            True IFF the masked output variables match the input
        """

        assert isinstance(self.compare_mask, list) and \
            len(self.compare_mask) == len(output_variables), (
                'Compare mask does not match output variables!')

        allclear = True
        for i in range(len(output_variables)):
            outv = output_variables[i].copy().squeeze()
            ref_answer = self.transformed_ref_ans[i].copy().squeeze()
            if self.compare_mask[i] is not None:
                outv = np.take(output_variables[i],
                               self.compare_mask[i], self.compare_axis).squeeze()
                if outv.shape != ref_answer.shape:
                    # apply the same transformation to the answer
                    ref_answer = np.take(ref_answer,
                                         self.compare_mask[i], self.compare_axis).squeeze()
            allclear = allclear and np.allclose(outv, ref_answer)
        return allclear


def populate(knl, kernel_calls, device='0',
             editor=None):
    """
    This method runs the supplied :class:`loopy.LoopKernel` (or list thereof),
    and is often used by :function:`auto_run`

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel` or list of :class:`loopy.LoopKernel`
        The kernel to test, if a list of kernels they will be successively
        applied and the end result compared
    kernel_calls : :class:`kernel_call` or list thereof
        The masks / ref_answers, etc. to use in testing
    device : str
        The pyopencl string denoting the device to use, defaults to '0'
    editor : callable
        If not none, a callable function or object that takes a
        :class:`loopy.LoopKernel` as the sole arguement, and returns the kernel
        with editing turned on (for used with auto-differentiation)

        If not specified, the default (opencl) editor will be invoked

    Returns
    -------
    out_ref : list of :class:`numpy.ndarray`
        The value(s) of the evaluated :class:`loopy.LoopKernel`
    """

    assert len(knl), 'No kernels supplied!'

    # create context
    ctx, queue = get_context(device)

    if editor is None:
        editor = set_editor

    output = []
    kc_ind = 0
    oob = False
    while not oob:
        # handle weirdness between list / non-list input
        try:
            kc = kernel_calls[kc_ind]
            kc_ind += 1
        except IndexError:
            oob = True
            break  # reached end of list
        except TypeError:
            # not a list
            oob = True  # break on next run
            kc = kernel_calls

        # create the outputs
        if kc.out_mask is not None:
            out_ref = [None for i in kc.out_mask]
        else:
            out_ref = [None]

        found = False
        # run kernels
        for k in knl:
            # test that we want to run this one
            if kc.is_my_kernel(k):
                found = True
                # set the editor to avoid intel bugs
                test_knl = editor(k)
                if isinstance(test_knl.target, lp.PyOpenCLTarget):
                    # recreate with device
                    test_knl = test_knl.copy(
                        target=lp.PyOpenCLTarget(device=device))

                # check for chaining
                if kc.chain:
                    kc.chain(kc, output)

                # run!
                out = kc(test_knl, queue)

                if kc.post_process:
                    kc.post_process(kc, out)

                # output mapping
                if all(x is None for x in out_ref):
                    # if the outputs are none, we init to zeros
                    # and avoid copying zeros over later data!
                    out_ref = [np.zeros_like(x) for x in out]

                for ind in range(len(out)):
                    # get indicies that are non-zero (already in there)
                    # or non infinity/nan
                    copy_inds = np.where(np.logical_not(
                        np.logical_or(np.isinf(out[ind]),
                                      out[ind] == 0, np.isnan(out[ind]))),
                    )
                    out_ref[ind][copy_inds] = out[ind][copy_inds]

        output.append(out_ref)
        assert found, 'No kernels could be found to match kernel call {}'.format(
            kc.name)
    return output


def auto_run(knl, kernel_calls, device='0', editor=None):
    """
    This method tests the supplied :class:`loopy.LoopKernel` (or list thereof)
    against a reference answer

    Parameters
    ----------
    knl : :class:`loopy.LoopKernel` or list of :class:`loopy.LoopKernel`
        The kernel to test, if a list of kernels they will be successively
        applied and the end result compared
    kernel_calls : :class:`kernel_call`
        The masks / ref_answers, etc. to use in testing
    device : str
        The pyopencl string denoting the device to use, defaults to '0'
    input_args : dict of `numpy.array`s
        The arguements to supply to the kernel
    editor : callable
        If not none, a callable function or object that takes a
        :class:`loopy.LoopKernel` as the sole arguement, and returns the kernel
        with editing turned on (for used with auto-differentiation)

        If not specified, the default (opencl) editor will be invoked

    Returns
    -------
    result : bool
        True if all tests pass
    """

    # run kernel

    # check lists
    if not isinstance(knl, list):
        knl = [knl]

    out = populate(knl, kernel_calls, device=device, editor=editor)
    try:
        result = True
        for i, kc in enumerate(kernel_calls):
            if kc.check:
                result = result and kc.compare(out[i])
        return result
    except:
        return kernel_calls.compare(out[0])


def generate_map_instruction(oldname, newname, map_arr, affine=''):
    """
    Generates a loopy instruction that maps oldname -> newname via the
    mapping array

    Parameters
    ----------
    oldname : str
        The old index to map from
    newname : str
        The new temporary variable to map to
    map_arr : str
        The array that holds the mappings
    affine : str, optional
        An optional affine mapping term that may be passed in

    Returns
    -------
    map_inst : str
        A strings to be used `loopy.Instruction`'s) for
                given mapping
    """

    if affine and not affine.startswith(' '):
        affine = ' ' + affine

    return '<>{newname} = {mapper}[{oldname}]{affine}'.format(
        newname=newname,
        mapper=map_arr,
        oldname=oldname,
        affine=affine)


def get_loopy_order(indicies, dimensions, order, numpy_arg=None):
    """
    This method serves to reorder loopy (and optionally corresponding numpy arrays)
    to ensure proper cache-access patterns

    Parameters
    ----------
    indicies : list of str
        The `loopy.iname`'s in current order
    dimensions : list of str/int
        The numerical / string dimensions of the loopy array.
    order : list of str
        If not equal to 'F' (the order used internally in pyJac), the
        indicies and dimensions must be transposed
    numpy_arg : :class:`numpy.ndarray`, optional
        If supplied, the same transformations will be applied to the numpy
        array as well.

    Returns
    -------
    indicies : list of str
        The `loopy.iname`'s in transformed order
    dimensions : list of str/int
        The transformed dimensions
    numpy_arg : :class:`numpy.ndarray`
        The transformed numpy array, is None
    """

    if order not in ['C', 'F']:
        raise Exception(
            'Parameter order passed with unknown value: {}'.format(order))

    if order != 'F':
        # need to flip indicies / dimensions
        indicies = indicies[::-1]
        dimensions = dimensions[::-1]
        if numpy_arg is not None:
            numpy_arg = np.ascontiguousarray(np.copy(numpy_arg.T))
    return indicies, dimensions, numpy_arg


def get_loopy_arg(arg_name, indicies, dimensions,
                  order, map_name=None,
                  initializer=None,
                  scope=scopes.GLOBAL,
                  dtype=np.float64,
                  force_temporary=False,
                  read_only=True,
                  map_result='',
                  **kwargs):
    """
    Convience method that generates a loopy GlobalArg with correct indicies
    and sizes.

    Parameters
    ----------
    arg_name : str
        The name of the array
    indicies : list of str
        See :param:`indicies`
    dimensions : list of str or int
        The dimensions of the `loopy.inames` in :param:`order`
    last_ind : str
        See :param:`last_ind` in :func:`get_loopy_order`
    additional_ordering : list of str/int
        See :param:`additional_ordering` in :func:`get_loopy_order`
    map_name : dict
        If not None, contains replacements for various indicies
    initializer : `numpy.array`
        If not None, the arg is assumed to be a :class:`loopy.TemporaryVariable`
        with :param:`scope`
    scope : :class:`loopy.temp_var_scope`
        The scope of the temporary variable definition,
        if initializer is not None, this must be supplied
    force_temporary: bool
        If true, this arg is a :class:`loopy.TemporaryVariable` regardless of value
        of initializer
    read_only: bool
        If True, the :class:`loopy.TemporaryVariable` will be readonly
    map_result : str
        If not empty, use instead of the default 'variable_name'_map
    kwargs : **'d dict
        The keyword args to pass to the resulting arg

    Returns
    -------
    * arg : `loopy.GlobalArg`
        The generated loopy arg
    * arg_str : str
        A string form of the argument
    * map_instructs : list of str
        A list of strings to be used `loopy.Instruction`'s for
        given mappings
    """

    if initializer is not None:
        assert initializer.dtype == dtype

    # first do any reordering
    indicies, dimensions, initializer = get_loopy_order(indicies, dimensions, order,
                                                        numpy_arg=initializer)

    # next, figure out mappings
    string_inds = indicies[:]
    map_instructs = {}
    if map_name is not None:
        for imap in map_name:
            # make a new name off the replaced iname
            if map_result:
                mapped_name = map_result
            else:
                mapped_name = '{}_map'.format(imap)
            if map_name[imap].startswith('<>'):
                # already an instruction
                map_instructs[imap] = map_name[imap]
                continue
            # add a mapping instruction
            map_instructs[imap] = generate_map_instruction(
                newname=mapped_name,
                map_arr=map_name[imap],
                oldname=imap)
            # and replace the index
            string_inds[string_inds.index(imap)] = mapped_name

    # finally make the argument
    if initializer is None and not force_temporary:
        arg = lp.GlobalArg(arg_name, shape=tuple(dimensions), dtype=dtype,
                           **kwargs)
    else:
        if initializer is not None:
            initializer = np.asarray(initializer, order=order, dtype=dtype)
        arg = lp.TemporaryVariable(arg_name,
                                   shape=tuple(dimensions),
                                   initializer=initializer,
                                   scope=scope,
                                   read_only=read_only,
                                   dtype=dtype,
                                   **kwargs)

    # and return
    return arg, '{name}[{inds}]'.format(name=arg_name,
                                        inds=','.join(string_inds)), map_instructs


def get_target(lang, device=None, compiler=None):
    """

    Parameters
    ----------
    lang : str
        One of the supported languages, {'c', 'cuda', 'opencl'}
    device : :class:`pyopencl.Device`
        If supplied, and lang is 'opencl', passed to the :class:`loopy.PyOpenCLTarget`

    Returns
    -------
    The correct loopy target type
    """

    check_lang(lang)

    # set target
    if lang == 'opencl':
        return lp.PyOpenCLTarget(device=device)
    elif lang == 'c':
        return lp.CTarget(compiler=compiler)
    elif lang == 'cuda':
        return lp.CudaTarget()


class AdeptCompiler(CppCompiler):
    default_compile_flags = '-g -O3 -fopenmp'.split()
    default_link_flags = '-shared -ladept -fopenmp'.split()
