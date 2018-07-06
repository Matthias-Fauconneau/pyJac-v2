from os.path import join, abspath, exists
import psutil
import cantera as ct
import logging
from collections import OrderedDict
from nose.tools import nottest
import six
from optionloop import OptionLoop

from pyjac.tests import _get_test_input, get_test_langs, platform_is_gpu
from pyjac.libgen import build_type
from pyjac.utils import enum_to_string, can_vectorize_lang, listify, EnumType, \
    stringify_args
from pyjac.loopy_utils.loopy_utils import JacobianType, JacobianFormat
from pyjac.schemas import build_and_validate
from pyjac.core.exceptions import OverrideCollisionException, \
    DuplicateTestException, InvalidTestEnivironmentException, \
    UnknownOverrideException, InvalidOverrideException

model_key = r'model-list'
platform_list_key = r'platform-list'
test_matrix_key = r'test-list'


def load_models(work_dir, matrix):
    """
    Load models from parsed test matrix

    Parameters
    ----------
    work_dir : str
        Working directory with mechanisms and for data
    matrix: dict
        The parsed test matrix, i.e., output of :func:`build_and_validate`

    Returns
    -------
    models : dict
        A dictionary indicating which models are available for testing,
        The structure is as follows:
            mech_name : {'mech' : file path to the Cantera mechanism
                         'ns' : number of species in the mechanism
                         'limits' : {
                                'species_rates': XXX,
                                'jacobian': {
                                    'sparse': XXX
                                    'full' : XXX}
                                }
                            A dictionary of limits on the number of conditions that
                            can be evaluated for this mechanism for various
                            eval-types due to memory constraints
    """

    models = matrix[model_key]
    # find the mechanisms to test
    mechanism_list = {}

    for model in models:
        # load
        mech = model['mech']
        name = model['name']
        # default path
        path = model['path'] if 'path' in model else join(work_dir, name)

        # load mechanism
        if path is not None:
            mech = join(path, mech)
        gas = ct.Solution(mech)
        # get stats
        mechanism_list[name] = {}
        mechanism_list[name]['mech'] = mech
        # num species
        mechanism_list[name]['ns'] = gas.n_species
        del gas
        # if we have limits
        if 'limits' in model:
            mechanism_list[name]['limits'] = model['limits'].copy()
        else:
            mechanism_list[name]['limits'] = {}

    return mechanism_list


def load_platforms(matrix, langs=get_test_langs(), raise_on_empty=False):
    """
    Loads test platforms from the :param:`matrix` file, for the :param:`langs`

    Parameters
    ----------
    matrix: dict
        A loaded test matrix from :func:`get_test_matrix`
    langs: list of str
        The allowed languages, modifiable by the :envvar:`TEST_LANGS` or test_langs
        in :file:`test_setup.py`
    raise_on_empty: bool [False]
        If True, and the supplied matrix has no platforms raise an exception

    Returns
    -------
    pre-loop: list of tuples
        The parameters that may be converted into a :class:`optionloop.OptionLoop`
    """

    oploop = []
    try:
        # try to use user-specified platforms
        platforms = matrix[platform_list_key]
        # put into oploop form, and make repeatable
        for p in sorted(platforms, key=lambda x: x['name']):
            # limit to supplied languages
            inner_loop = []
            allowed_langs = langs[:]
            if 'lang' in p:
                # pull from platform languages if possible
                allowed_langs = p['lang'] if p['lang'] in allowed_langs else []
            if not allowed_langs:
                # can't use language
                continue

            # set lang
            inner_loop.append(('lang', allowed_langs))

            # get vectorization type and size
            vectype = listify('par' if (
                'vectype' not in p or not can_vectorize_lang[allowed_langs])
                else p['vectype'])
            # check if we have a vectorization
            if not (len(vectype) == 1 and vectype[0] == 'par'):
                # try load the vecsize, fail on missing
                try:
                    vecsize = [x for x in listify(p['vecsize'])]
                except TypeError:
                    raise Exception(
                        'Platform {} has non-parallel vectype(s) {} but no supplied '
                        'vector size.'.format(
                            p['name'], [x for x in vectype if x != 'par']))

                add_none = 'par' in vectype
                for v in [x.lower() for x in vectype]:
                    def _get(add_none):
                        if add_none:
                            return vecsize + [None]
                        return vecsize
                    if v == 'wide':
                        inner_loop.append(('width', _get(add_none)))
                        add_none = False
                    elif v == 'deep':
                        inner_loop.append(('depth', _get(add_none)))
                        add_none = False
                    elif v != 'par':
                        raise Exception('Platform {} has invalid supplied vectype '
                                        '{}'.format(p['name'], v))

            # fill in missing vectypes
            for x in ['width', 'depth']:
                if next((y for y in inner_loop if y[0] == x), None) is None:
                    inner_loop.append((x, None))

            # check for atomics
            if 'atomic_doubles' in p:
                inner_loop.append(('use_atomic_doubles', p['atomic_doubles']))
            if 'atomic_ints' in p:
                inner_loop.append(('use_atomic_ints', p['atomic_ints']))

            # and store platform
            inner_loop.append(('platform', p['name']))

            # finally check for seperate_kernels
            sep_knl = True
            if 'seperate_kernels' in p and not p['seperate_kernels']:
                sep_knl = False
            inner_loop.append(('seperate_kernels', sep_knl))

            # create option loop and add
            oploop += [inner_loop]
    except (TypeError, KeyError):
        if raise_on_empty:
            raise Exception('Supplied test matrix has no platforms.')

    finally:
        if not oploop:
            # file not found, or no appropriate targets for specified languages
            for lang in langs:
                inner_loop = []
                vecsizes = [4, None] if can_vectorize_lang[lang] else [None]
                inner_loop = [('lang', lang)]
                if lang == 'opencl':
                    import pyopencl as cl
                    inner_loop += [('width', vecsizes[:]),
                                   ('depth', vecsizes[:])]
                    # add all devices
                    device_types = [cl.device_type.CPU, cl.device_type.GPU,
                                    cl.device_type.ACCELERATOR]
                    platforms = cl.get_platforms()
                    platform_list = []
                    for p in platforms:
                        for dev_type in device_types:
                            devices = p.get_devices(dev_type)
                            if devices:
                                plist = [('platform', p.name)]
                                use_atomic_doubles = (
                                    'cl_khr_int64_base_atomics' in
                                    devices[0].extensions)
                                use_atomic_ints = (
                                    'cl_khr_global_int32_base_atomics' in
                                    devices[0].extensions)
                                plist.append(('use_atomic_doubles',
                                              use_atomic_doubles))
                                plist.append(('use_atomic_ints',
                                              use_atomic_ints))
                                platform_list.append(plist)
                    for p in platform_list:
                        # create option loop and add
                        oploop += [inner_loop + p]
                elif lang == 'c':
                    inner_loop += [('platform', 'OpenMP')]
                    oploop += [inner_loop]
    return oploop


# todo -- feed these directly into override schema
allowed_overrides = ['num_cores', 'gpuorder', 'order', 'conp', 'vecsize', 'vectype',
                     'gpuvecsize', 'gpuvectype', 'models']
jacobian_sub_override_keys = {enum_to_string(JacobianFormat.sparse):
                              allowed_overrides,
                              enum_to_string(JacobianFormat.full):
                              allowed_overrides,
                              }
jacobian_sub_override_keys['both'] = jacobian_sub_override_keys.copy()
allowed_override_keys = {
    enum_to_string(JacobianType.exact): jacobian_sub_override_keys,
    enum_to_string(JacobianType.finite_difference): jacobian_sub_override_keys,
    enum_to_string(build_type.species_rates): allowed_overrides}
# and add handlers here


@nottest
def load_tests(matrix, filename):
    """
    Load the tests from the pre-parsed test matrix file and check for duplicates

    Parameters
    ----------
    matrix: dict
        The parsed test matrix, i.e., output of :func:`build_and_validate`

    Returns
    -------
    tests: list
        The list of valid tests
    """

    tests = matrix[test_matrix_key]
    dupes = set()

    def __getenumtype(ttype):
        from argparse import ArgumentTypeError
        try:
            EnumType(JacobianType)(ttype)
            return JacobianType
        except ArgumentTypeError:
            try:
                EnumType(build_type)(ttype)
                return build_type
            except:
                EnumType(JacobianFormat)(ttype)
                return JacobianFormat

    from collections import defaultdict
    for test in tests:
        # first check that the
        descriptors = [test['test-type'] + ' - ' + test['eval-type']]
        if test['eval-type'] == 'both':
            descriptors = [test['test-type'] + ' - ' + enum_to_string(
                                build_type.jacobian),
                           test['test-type'] + ' - ' + enum_to_string(
                                build_type.species_rates)]
        if set(descriptors) & dupes:
            raise DuplicateTestException(test['test-type'], test['eval-type'],
                                         filename)

        dupes.update(descriptors)

        # now go through overrides
        def roverride_check(root, keydict, dupes, path=''):
            def __basecheck(k, o, d):
                if isinstance(k[o], list):
                    # the next level down is the base, hence add a list
                    if o not in d:
                        d[o] = []
            if path:
                path += '.'
            # find the keys in the root
            for override in [k for k in keydict if k in root]:
                # if the override is a 'both' type, apply to all subvalues
                if override == 'both':
                    for val in keydict[override]:
                        __basecheck(keydict[override], val, dupes)
                        roverride_check(
                            root[override], keydict[override][val],
                            dupes[val], path + val)

                # test if we've reached the base of the tree
                if isinstance(keydict, list):
                    # we've reached the base
                    if override not in keydict:
                        # this should be handled by validation, but just double check
                        raise UnknownOverrideException(override, path)
                    if override in dupes:
                        # check for collision
                        raise OverrideCollisionException(override, path)
                    # finally, just add the override
                    dupes.append(override)
                else:
                    __basecheck(keydict, override, dupes)

                    # we're still on a leaf
                    roverride_check(root[override], keydict[override],
                                    dupes[override], path + override)

        # overrides only need to be checked within a test
        nesteddict = lambda: defaultdict(nesteddict)  # noqa
        roverride_check(test, allowed_override_keys, nesteddict())

    return tests


def num_cores_default():
    """
    Returns the default number of cores for testing.

    This may be affected by the following test input:
        num_threads
        max_threads

    If neither are specified, it will return powers of 2 under the maximum
    hardware cores
    """
    nc = 1
    default_num_cores = []
    can_override_cores = True
    if _get_test_input('num_threads', None) is not None:
        can_override_cores = False
        default_num_cores = [int(_get_test_input('num_threads'))]
    else:
        max_threads = int(_get_test_input('max_threads',
                                          psutil.cpu_count(logical=False)))
        while nc <= max_threads:
            default_num_cores.append(nc)
            nc *= 2
    return default_num_cores, can_override_cores


def get_overrides(test, eval_type, jac_type, sparse_type):
    """
    Convenience method to extract overrides from the given test

    Parameters
    ----------
    test: dict
        The test with specified overrides
    eval_type: str
        The stringified :class:`build_type`
    jac_type: str
        The stringified :class:`JacobianType`
    sparse_type: str
        The stringified :class:`JacobianFormat`
    """

    if eval_type == enum_to_string(build_type.species_rates):
        if eval_type in test:
            return test[eval_type].copy()
        return {}
    else:
        # first check for the base type
        overrides = {}
        if jac_type in test:
            if sparse_type in test[jac_type]:
                overrides.update(test[jac_type][sparse_type])
            # next check for "both"
            if 'both' in test[jac_type]:
                # note: these are guarenteed not to collide, as they've been
                # previously checked
                overrides.update(test[jac_type]['both'])
        return overrides.copy()


@nottest
def get_test_matrix(work_dir, test_type, test_matrix, for_validation,
                    raise_on_missing=True, langs=get_test_langs()):
    """Runs a set of mechanisms and an ordered dictionary for
    performance and functional testing

    Parameters
    ----------
    work_dir : str
        Working directory with mechanisms and for data
    test_type: :class:`build_type.jacobian`
        Controls some testing options (e.g., whether to do a sparse matrix or not)
    test_matrix: str
        The test matrix file to load
    for_validation: bool
        If determines which test type to load from the test matrix,
        validation or performance
    raise_on_missing: bool
        Raise an exception of the specified :param:`test_matrix` file is not found
    langs: list of str
        The allowed languages, modifiable by the :envvar:`TEST_LANGS` or test_langs
        in :file:`test_setup.py`
    Returns
    -------
    mechanisms : dict
        A dictionary indicating which mechanism are available for testing,
        The structure is as follows:
            mech_name : {'mech' : file path to the Cantera mechanism
                         'ns' : number of species in the mechanism
                         'limits' : {'full': XXX, 'sparse': XXX}}: a dictionary of
                            limits on the number of conditions that can be evaluated
                            for this mechanism (full & sparse jacobian respectively)
                            due to memory constraints
    params  : OrderedDict
        The parameters to put in an oploop
    max_vec_width : int
        The maximum vector width to test

    """
    work_dir = abspath(work_dir)

    # validate the test matrix
    matrix_name = test_matrix
    test_matrix = build_and_validate('test_matrix_schema.yaml', test_matrix)

    # check that we have the working directory
    if not exists(work_dir):
        raise Exception('Work directory {} for '.format(work_dir) +
                        'testing not found, exiting...')

    # load the models
    models = load_models(work_dir, test_matrix)
    assert isinstance(test_type, build_type)

    # load tests
    tests = load_tests(test_matrix, matrix_name)
    # filter those that match the test type
    valid_str = 'validation' if for_validation else 'performance'
    tests = [test for test in tests if test['test-type'] == valid_str]
    tests = [test for test in tests if test['eval-type'] == enum_to_string(
        test_type) or test['eval-type'] == 'both']
    # and dictify
    tests = [OrderedDict(test) for test in tests]
    if not tests:
        raise Exception('No tests found in matrix {} for {} test of {}, '
                        'exiting...'.format(matrix_name, valid_str, enum_to_string(
                         test_type)))

    # get defaults we haven't migrated to schema yet
    rate_spec = ['fixed', 'hybrid'] if test_type != build_type.jacobian \
        else ['fixed']
    sparse = ([enum_to_string(JacobianFormat.sparse),
               enum_to_string(JacobianFormat.full)]
              if test_type == build_type.jacobian else [
               enum_to_string(JacobianFormat.full)])
    jac_types = [enum_to_string(JacobianType.exact),
                 enum_to_string(JacobianType.finite_difference)] if (
                    test_type == build_type.jacobian and not for_validation) else [
                 enum_to_string(JacobianType.exact)]
    split_kernels = [False]

    # and default # of cores, this may be overriden
    default_num_cores, can_override_cores = num_cores_default()

    # load platforms
    platforms = load_platforms(test_matrix, langs=langs,
                               raise_on_empty=raise_on_missing)
    platforms = [OrderedDict(platform) for platform in platforms]
    out_params = []
    logger = logging.getLogger(__name__)
    for test in tests:
        # filter platforms
        plats = [p.copy() for p in platforms]
        if 'platforms' in test:
            plats = [plat for plat in plats
                     if plat['platform'] in test['platforms']]
            if len(plats) < len(platforms):
                logger.info('Platforms ({}) filtered out for test type: {}'.format(
                    ', '.join([p['platform'] for p in platforms if p not in plats]),
                    ' - '.join([test['test-type'], test['eval-type']])))
        if not len(plats):
            logger.warn('No platforms found for test {}, skipping...'.format(
                ' - '.join([test['test-type'], test['eval-type']])))
            continue

        for plookup in plats:
            clean = plookup.copy()
            # get default number of cores
            cores = default_num_cores[:]
            # get default vector widths
            widths = plookup['width']
            is_wide = widths is not None
            depths = plookup['depth']
            is_deep = depths is not None
            if is_deep and not is_wide:
                widths = depths[:]
            # sanity check
            if is_wide or is_deep:
                assert widths is not None
            # special gpu handling for cores
            is_gpu = False
            # test platform type
            if platform_is_gpu(plookup['platform']):
                # set cores to 1
                is_gpu = True
                cores = [1]

            def apply_vectypes(lookup, widths, is_wide=is_wide, is_deep=is_deep):
                if is_wide or is_deep:
                    # set vec widths
                    use_par = None in widths or (is_wide and is_deep)
                    lookup['vecsize'] = [x for x in widths[:] if x is not None]
                    base = [True] if not use_par else [True, False]
                    if is_wide:
                        lookup['wide'] = base[:]
                        base.pop()
                    if is_deep:
                        lookup['deep'] = base[:]
                else:
                    lookup['vecsize'] = [None]
                    lookup['wide'] = [False]
                    lookup['deep'] = [False]
                del lookup['width']
                del lookup['depth']

            apply_vectypes(plookup, widths)

            # default is both conp / conv
            conp = [True, False]
            order = ['C', 'F']

            # loop over possible overrides
            oploop = OptionLoop(OrderedDict(
                [('ttype', [enum_to_string(test_type)]),
                 ('jtype', jac_types),
                 ('stype', sparse)]))
            for i, state in enumerate(oploop):
                ttype = state['ttype']
                jtype = state['jtype']
                stype = state['stype']

                def override_log(key, old, new):
                    logging.info('Replacing {} for test type: {}. Old value:'
                                 ' ({}), New value: ({})'.format(
                                    key,
                                    stringify_args([
                                        ttype, test['eval-type'],
                                        jtype, stype],
                                        joiner='.'),
                                    stringify_args(listify(old)),
                                    stringify_args(listify(new))
                                    ))
                # copy defaults
                icores = cores[:]
                iorder = order[:]
                iconp = conp[:]
                ivecsizes = widths[:] if widths is not None else [None]
                imodels = tuple(models.keys())
                # load overides
                overrides = get_overrides(test, ttype, jtype, stype)

                # check that we can apply
                if 'num_cores' in overrides and not can_override_cores:
                    raise InvalidTestEnivironmentException(
                        ttype, 'num_cores', matrix_name, 'num_threads')
                elif 'num_cores' in overrides and is_gpu:
                    logger = logging.getLogger(__name__)
                    logger.info(
                        'Discarding unused "num_cores" override for GPU '
                        'platform {}'.format(plookup['platform']))
                    del overrides['num_cores']

                # 'num_cores', 'order', 'conp', 'vecsize', 'vectype'
                # now apply overrides
                outplat = plookup.copy()
                for current in overrides:
                    ivectypes_override = None
                    for override in overrides:
                        if override == 'num_cores':
                            override_log('num_cores', icores,
                                         overrides[override])
                            icores = overrides[override]
                        elif override == 'order' and not is_gpu:
                            override_log('order', iorder,
                                         overrides[override])
                            iorder = overrides[override]
                        elif override == 'gpuorder' and is_gpu:
                            override_log('order', iorder,
                                         overrides[override])
                            iorder = overrides[override]
                        elif override == 'conp':
                            iconp_save = iconp[:]
                            iconp = []
                            if 'conp' in overrides[override]:
                                iconp.append(True)
                            if 'conv' in overrides[override]:
                                iconp.append(False)
                            override_log('conp', iconp_save,
                                         iconp)
                        elif override == 'vecsize' and not is_gpu:
                            override_log('vecsize', ivecsizes,
                                         overrides[override])
                            outplat['vecsize'] = listify(overrides[override])
                        elif override == 'gpuvecsize' and is_gpu:
                            override_log('gpuvecsize', ivecsizes,
                                         overrides[override])
                            outplat['vecsize'] = listify(overrides[override])
                        elif override == 'vectype' and not is_gpu:
                            # we have to do this at the end
                            ivectypes_override = overrides[override]
                        elif override == 'gpuvectype' and is_gpu:
                            ivectypes_override = overrides[override]
                        elif override == 'models':
                            # check that all models are valid
                            for model in overrides[override]:
                                if model not in imodels:
                                    raise InvalidOverrideException(
                                        override, model, imodels)
                            # and replace
                            override_log('models', stringify_args(imodels),
                                         stringify_args(overrides[override]))
                            imodels = tuple(overrides[override])

                    if ivectypes_override is not None:
                        c = clean.copy()
                        apply_vectypes(c, outplat['vecsize'],
                                       is_wide='wide' in ivectypes_override,
                                       is_deep='deep' in ivectypes_override)
                        # and copy into working
                        outplat['wide'] = c['wide'] if 'wide' in c else [False]
                        outplat['deep'] = c['deep'] if 'deep' in c else [False]
                        outplat['vecsize'] = c['vecsize']
                        old = ['']
                        if is_wide:
                            old += ['wide']
                        if is_deep:
                            old += ['deep']
                        elif not is_wide:
                            old += ['par']
                        override_log('vecsize', old,
                                     ivectypes_override)

                # and finally, convert back to an option loop format
                out_params.append([
                    ('num_cores', icores),
                    ('order', iorder),
                    ('rate_spec', rate_spec),
                    ('split_kernels', split_kernels),
                    ('conp', iconp),
                    ('sparse', [stype]),
                    ('jac_type', [jtype]),
                    ('models', [imodels])] +
                    [(key, value) for key, value in six.iteritems(
                        outplat)])

    max_vec_width = 1
    vector_params = [dict(p)['vecsize'] for p in out_params if 'vecsize' in
                     dict(p) and dict(p)['vecsize'] != [None]]
    if vector_params:
        max_vec_width = max(max_vec_width, max(
            [max(x) for x in vector_params]))
    from . import reduce_oploop
    loop = reduce_oploop(out_params)
    return models, loop, max_vec_width
