# -*- coding: utf-8 -*-
"""Module containing utility functions.
"""

# Standard libraries
import os
import errno
import argparse
import logging.config
import yaml
import functools
import six
from six.moves import reduce

from pyjac.core import exceptions

__all__ = ['langs', 'file_ext', 'header_ext', 'line_end', 'exp_10_fun',
           'get_species_mappings', 'get_nu', 'read_str_num', 'split_str',
           'create_dir', 'reassign_species_lists', 'is_integer', 'get_parser']

langs = ['c', 'opencl']  # ispc' , 'cuda'
"""list(`str`): list of supported languages"""

package_lang = {'opencl': 'ocl',
                'c': 'c'}
"""dict: str->str
   short-names for the python wrappers for each language
"""


def stringify_args(arglist, kwd=False, joiner=', ', use_quotes=False):
    template = '{}' if not use_quotes else '"{}"'
    if kwd:
        template = template + '=' + template
        return joiner.join(template.format(str(k), str(v))
                           for k, v in six.iteritems(arglist))
    else:
        return joiner.join(template.format(str(a)) for a in arglist)


def partition(tosplit, predicate):
    """
    Splits the list :param:`tosplit` based on the :param:`predicate` applied to each
    list element and returns the two resulting lists

    Parameters
    ----------
    tosplit: list
        The list to split
    predicate: :class:`six.Callable`
        A callable predicate that takes as an argument a list element to test.

    Returns
    -------
    true_list: list
        The list of elements in :param:`tosplit` for which :param:`predicate` were
        True
    false_list: list
        The list of elements in :param:`tosplit` for which :param:`predicate` were
        False
    """
    return reduce(lambda x, y: x[not predicate(y)].append(y) or x, tosplit,
                  ([], []))


def func_logger(*args, **kwargs):
    # This wrapper is to be used to provide a simple function decorator that logs
    # function exit / entrance, as well as optional logging of arguements, etc.

    cname = kwargs.pop('name', '')
    log_args = kwargs.pop('log_args', False)
    allowed_errors = kwargs.pop('allowed_errors', [])

    assert not len(kwargs), 'Unknown keyword args passed to @func_logger: {}'.format(
        stringify_args(kwargs, True))

    def decorator(func):
        """
        A decorator that wraps the passed in function and logs
        exceptions should one occur
        """

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            logger = logging.getLogger(__name__)
            try:
                name = func.__name__
                if cname:
                    name = cname + '::' + name
                msg = 'Entering function {}'.format(name)
                if log_args:
                    msg += ', with arguments: {} and keyword args: {}'.format(
                        stringify_args(args),
                        stringify_args(kwargs, True))
                logger.debug(msg)
                return func(*args, **kwargs)
            except Exception as e:
                # we've explicitly allowed these
                log = logger.exception
                if any(isinstance(e, a) for a in allowed_errors):
                    log = logger.debug
                    err = 'Allowed error of type {} in '.format(str(e))
                else:
                    err = "There was an unhandled exception in "
                # log the exception
                err += func.__name__
                log(err)
                # re-raise the exception
                raise e
            finally:
                logging.debug('Exiting function {}'.format(name))
        return wrapper
    if len(args):
        assert len(args) == 1, (
            ('Unknown arguements passed to @func_logger: {}.'
             ' Was expecting a function and possible keywords.'.format(
                stringify_args(args))))
        return decorator(args[0])
    return decorator


file_ext = dict(c='.c', cuda='.cu', opencl='.ocl')
"""dict: source code file extensions based on language"""


header_ext = dict(c='.h', cuda='.cuh', opencl='.oclh')
"""dict: header extensions based on language"""

line_end = dict(c=';', cuda=';',
                opencl=';'
                )
"""dict: line endings dependent on language"""

can_vectorize_lang = {'c': False,
                      'cuda': True,
                      'opencl': True,
                      'ispc': True}
"""dict: defines whether a language can be 'vectorized' in the loopy sense"""

exp_10_fun = dict(c='exp(log(10) * {val})', cuda='exp10({val})',
                  opencl='exp10({val})', fortran='exp(log(10) * {val})'
                  )
"""dict: exp10 functions for various languages"""


class PowerFunction(object):
    """
    A simple wrapper that contains the name of a power function for a given language
    as well as any options
    """

    def __init__(self, name, lang, guard_nonzero=False):
        self.name = name
        self.lang = lang
        self.guard_nonzero = guard_nonzero

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.__repr__()

    def __call__(self, base, power):
        template = '{func}({base}, {power})'
        guard = 'fmax(1e-300d, {base})'
        if self.guard_nonzero:
            guard = guard.format(base=base)
            return template.format(func=self, base=guard, power=power)
        return template.format(func=self, base=base, power=power)


def power_function(lang, is_integer_power=False, is_positive_power=False,
                   guard_nonzero=False):
    """
    Returns the best power function to use for a given :param:`lang` and
    choice of :param:`is_integer_power` / :param:`is_positive_power`
    """

    if lang == 'opencl' and is_integer_power:
        # opencl has it's own integer power function
        # this also is nice for loopy, as it handles the vectorizability check
        return PowerFunction('pown', lang, guard_nonzero=guard_nonzero)
    elif lang == 'opencl' and is_positive_power:
        # opencl positive power function -- no need for guard
        return PowerFunction('powr', lang)
    elif is_integer_power:
        # use internal integer power function
        return PowerFunction('fast_powi', lang, guard_nonzero=guard_nonzero)
    else:
        # use default
        return PowerFunction('pow', lang, guard_nonzero=guard_nonzero)


inf_cutoff = 1e285
"""float: A cutoff above which values are considered infinite.
          Used in testing and validation to filter values that should only
          be compared as 'large numbers'"""


# https://fangpenlin.com/posts/2012/08/26/good-logging-practice-in-python/
def setup_logging(
    default_path='logging.yaml',
    default_level=logging.INFO,
    env_key='LOG_CFG'
):
    """Setup logging configuration"""
    this_dir = os.path.abspath(os.path.dirname(__file__))
    path = os.path.join(this_dir, default_path)
    value = os.getenv(env_key, None)
    if value:
        path = value
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=default_level)

    # make set depenendencies logging levels to be less verbose
    logging.getLogger('loopy').setLevel(logging.WARNING)
    logging.getLogger('pyopencl').setLevel(logging.WARNING)
    logging.getLogger('pytools').setLevel(logging.WARNING)
    logging.getLogger('codepy').setLevel(logging.WARNING)


class EnumType(object):
    """Factory for working with argparse for creating enum object types"""
    def __init__(self, enumclass):
        self.enums = enumclass

    def __call__(self, astring):
        name = self.enums.__name__
        try:
            return self.enums[astring.lower()]
        except KeyError:
            msg = ', '.join([t.name.lower() for t in self.enums])
            msg = '{0}: use one of {1}'.format(name, msg)
            raise argparse.ArgumentTypeError(msg)

    def __repr__(self):
        astr = ', '.join([t.name.lower() for t in self.enums])
        return '{0}({1})'.format(self.enums.__name__, astr)


def enum_to_string(enum):
    """
    Convenience method that converts an IntEnum/Enum to string

    Parameters
    ----------
    enum: Enum
        The enum to convert

    Returns
    -------
    name: str
        The stringified enum
    """

    enum = str(enum)
    return enum[enum.index('.') + 1:]


def is_iterable(value):
    """
    Custom iterable test that excludes string types

    Parameters
    ----------
    value: object
        The value to test if iterable

    Returns
    -------
    iterable: bool
        True if the value is iterable and not a string, false otherwise
    """
    if isinstance(value, six.string_types):
        return False

    try:
        [vi for vi in value]
        return True
    except TypeError:
        return False


def listify(value):
    """
    Convert value to list

    Parameters
    ----------
    value: object
        The value to convert

    Returns
    -------
    listified: list
        If string, return [string]
        If tuple or other iterable, convert to lsit
        If not iterable, return [value]
    """
    if isinstance(value, six.string_types):
        return [value]

    try:
        return [vi for vi in value]
    except TypeError:
        return [value]


def get_species_mappings(num_specs, last_species):
    """
    Maps species indices around species moved to last position.

    Parameters
    ----------
    num_specs : int
        Number of species.
    last_species : int
        Index of species being moved to end of system.

    Returns
    -------
    fwd_species_map : list of `int`
        List of original indices in new order
    back_species_map : list of `int`
        List of new indicies in original order

    """

    fwd_species_map = list(range(num_specs))
    back_species_map = list(range(num_specs))

    # in the forward mapping process
    # last_species -> end
    # all entries after last_species are reduced by one
    back_species_map[last_species + 1:] = back_species_map[last_species:-1]
    back_species_map[last_species] = num_specs - 1

    # in the backwards mapping
    # end -> last_species
    # all entries with value >= last_species are increased by one
    ind = fwd_species_map.index(last_species)
    fwd_species_map[ind:-1] = fwd_species_map[ind + 1:]
    fwd_species_map[-1] = last_species

    return fwd_species_map, back_species_map


def get_nu(isp, rxn):
    """Returns the net nu of species isp for the reaction rxn

    Parameters
    ----------
    isp : int
        Species index
    rxn : `ReacInfo`
        Reaction

    Returns
    -------
    nu : int
        Overall stoichiometric coefficient of species ``isp`` in reaction ``rxn``

    """
    if isp in rxn.prod and isp in rxn.reac:
        nu = (rxn.prod_nu[rxn.prod.index(isp)] -
              rxn.reac_nu[rxn.reac.index(isp)])
        # check if net production zero
        if nu == 0:
            return 0
    elif isp in rxn.prod:
        nu = rxn.prod_nu[rxn.prod.index(isp)]
    elif isp in rxn.reac:
        nu = -rxn.reac_nu[rxn.reac.index(isp)]
    else:
        # doesn't participate in reaction
        return 0
    return nu


def read_str_num(string, sep=None):
    """Returns a list of floats pulled from a string.

    Delimiter is optional; if not specified, uses whitespace.

    Parameters
    ----------
    string : str
        String to be parsed.
    sep : str, optional
        Delimiter (default is None, which means consecutive whitespace).

    Returns
    -------
    list of `float`
        Floats separated by ``sep`` in ``string``.

    """

    # separate string into space-delimited strings of numbers
    num_str = string.split(sep)
    return [float(n) for n in num_str]


def split_str(seq, length):
    """Separate a string seq into length-sized pieces.

    Parameters
    ----------
    seq : str
        String containing sequence of smaller strings of constant length.
    length : int
        Length of individual sequences.

    Returns
    -------
    list of `str`
        List of strings of length ``length`` from ``seq``.

    """
    return [seq[i: i + length] for i in range(0, len(seq), length)]


def create_dir(path):
    """Creates a new directory based on input path.

    No error if path already exists, but other error is reported.

    Parameters
    ----------
    path : str
        Path of directory to be created

    Returns
    -------
    None

    """
    try:
        os.makedirs(path)
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise


def reassign_species_lists(reacs, specs):
    """
    Given a list of `ReacInfo`, and `SpecInfo`, this method will update the
    `ReacInfo` reactants / products / third body list to integers
    representing the species' index in the list.

    Parameters
    ----------
    reacs : list of `ReacInfo`
        List of reactions to be updated.
    specs : list of `SpecInfo`
        List of species

    Returns
    -------
    None

    """

    species_map = {sp.name: i for i, sp in enumerate(specs)}
    for rxn in reacs:
        rxn.reac, rxn.reac_nu = zip(*[(species_map[sp], nu) for sp, nu in
                                      sorted(zip(rxn.reac, rxn.reac_nu),
                                             key=lambda x:species_map[x[0]])])
        rxn.prod, rxn.prod_nu = zip(*[(species_map[sp], nu) for sp, nu in
                                      sorted(zip(rxn.prod, rxn.prod_nu),
                                             key=lambda x:species_map[x[0]])])
        rxn.thd_body_eff = sorted([(species_map[thd[0]], thd[1])
                                   for thd in rxn.thd_body_eff], key=lambda x: x[0])
        if rxn.pdep_sp != '':
            rxn.pdep_sp = species_map[rxn.pdep_sp]
        else:
            rxn.pdep_sp = None


def is_integer(val):
    """Returns `True` if argument is an integer or whole number.

    Parameters
    ----------
    val : int, float
        Value to be checked.

    Returns
    -------
    bool
        ``True`` if ``val`` is `int` or whole number (if `float`).

    """
    try:
        return val.is_integer()
    except:
        if isinstance(val, int):
            return True
        # last ditch effort
        try:
            return int(val) == float(val)
        except:
            return False


def check_lang(lang):
    """
    Checks that 'lang' is a valid identifier

    Parameters
    ----------
    lang : {'c', 'opencl', 'cuda'}
        The language to check

    Notes
    -----
    Raised NotImplementedError if incorrect lang given
    """
    if lang not in langs:
        raise NotImplementedError('Language {} not supported'.format(lang))


def check_order(order):
    """
    Checks that the :param:`order` is valid

    Parameters
    ----------
    order: ['C', 'F']
        The order to use, 'C' corresponds to a row-major data ordering, while
        'F' is a column-major data ordering.  See `row major`_ and `col major`_
        for more info

    .. _row major: https://docs.scipy.org/doc/numpy/glossary.html#term-row-major
    .. _col major: https://docs.scipy.org/doc/numpy/glossary.html#term-column-major

    Notes
    -----
    :class:`InvalidInputSpecificationException` raised if :param:`order` is not
        valid
    """

    if order not in ['C', 'F']:
        logger = logging.getLogger(__name__)
        logger.error("Invalid data-ordering ('{}') supplied, allowed values are 'C'"
                     " and 'F'".format(order))
        raise exceptions.InvalidInputSpecificationException('order')


def get_parser():
    """

    Parameters
    ----------
    None

    Returns
    -------
    args : `argparse.Namespace`
        Command line arguments for running pyJac.

    """

    # command line arguments
    parser = argparse.ArgumentParser(description='pyJac: Generates source code '
                                     'for analytical chemical Jacobians.')
    parser.add_argument('-l', '--lang',
                        type=str,
                        choices=langs,
                        required=True,
                        help='Programming language for output source files.'
                        )
    parser.add_argument('-i', '--input',
                        type=str,
                        required=True,
                        help='Input mechanism filename (e.g., mech.dat).'
                        )
    parser.add_argument('-t', '--thermo',
                        type=str,
                        default=None,
                        help='Thermodynamic database filename (e.g., '
                             'therm.dat), or nothing if in mechanism.'
                        )
    parser.add_argument('-v', '--vector_size',
                        type=int,
                        default=0,
                        required=False,
                        help='The SIMD/SIMT vector width to use in code-generation.'
                             '  This corresponds to the "blocksize" in CUDA'
                             'terminology.')
    parser.add_argument('-w', '--wide',
                        required=False,
                        default=False,
                        action='store_true',
                        help='Use a "wide" vectorization, where the calculation '
                        'of Jacobian / species rates is vectorized over the '
                        'set of thermo-chemical state.  That is, each '
                        'work-item (CUDA thread) operates independently.')
    parser.add_argument('-d', '--deep',
                        required=False,
                        default=False,
                        action='store_true',
                        help='Use a "deep" vectorization, where the calculation '
                        'of Jacobian / species rates is vectorized within each '
                        'thermo-chemical state.  That is, all the work-items '
                        '(CUDA threads) operates cooperate to evaluate a single '
                        'state.')
    parser.add_argument('-u', '--unroll',
                        type=int,
                        default=None,
                        required=False,
                        help='If supplied, a length to unroll the inner loops (e.g. '
                        'evaluation of the species rates for a single '
                        'thermo-chemical state) in the generated code. Turned off '
                        'by default.'
                        )
    parser.add_argument('-b', '--build_path',
                        required=False,
                        default='./out/',
                        help='The folder to generate the Jacobian and rate '
                             'subroutines in.'
                        )
    parser.add_argument('-ls', '--last_species',
                        required=False,
                        type=str,
                        default=None,
                        help='The name of the species to set as the last in '
                             'the mechanism. If not specifed, defaults to '
                             'the first of N2, AR, and HE in the mechanism.'
                        )
    parser.add_argument('-sj', '--skip_jac',
                        required=False,
                        default=False,
                        action='store_true',
                        help='If specified, this option turns off Jacobian '
                             'generation i.e. only the rate subs are generated')
    parser.add_argument('-p', '--platform',
                        required=False,
                        default='',
                        type=str,
                        help='The name (or subset thereof) of the OpenCL platform '
                             'to run on, e.g. "Intel", "nvidia", "pocl". '
                             'Must be supplied to properly generate the compilation '
                             'wrapper for OpenCL code, but may be ignored if not '
                             'using the OpenCL target.')
    parser.add_argument('-o', '--data_order',
                        default='C',
                        type=str,
                        choices=['C', 'F'],
                        help="The data ordering, 'C' (row-major, recommended for "
                        "CPUs) or 'F' (column-major, recommended for GPUs)")
    parser.add_argument('-rs', '--rate_specialization',
                        type=str,
                        default='hybrid',
                        choices=['fixed', 'hybrid', 'full'],
                        help="The level of specialization in evaluating reaction "
                        "rates. 'Full' is the full form suggested by Lu et al. "
                        "(citation) 'Hybrid' turns off specializations in the "
                        "exponential term (Ta = 0, b = 0) 'Fixed' is a fixed"
                        " expression exp(logA + b logT + Ta / T)")
    parser.add_argument('-rk', '--split_rate_kernels',
                        type=bool,
                        default=True,
                        help="If True, and the :param`rate_specialization` is not "
                        "'Fixed', split different rate evaluation types into "
                        "different kernels")
    parser.add_argument('-rn', '--split_rop_net_kernels',
                        type=bool,
                        default=False,
                        help="If True, break evaluation of different rate of "
                        "progress values (fwd / back / pdep) into different "
                        "kernels. Note that for a deep vectorization this will "
                        "introduce additional synchronization requirements.")
    parser.add_argument('-conv', '--constant_volume',
                        required=False,
                        dest='conp',
                        action='store_false',
                        help='If supplied, use the constant volume assumption in '
                        'generation of the rate subs / Jacobian code. Otherwise, '
                        'use the constant pressure assumption [default].')
    parser.add_argument('-nad', '--no_atomic_doubles',
                        dest='use_atomic_doubles',
                        action='store_false',
                        required=False,
                        help='If supplied, the targeted language / platform is not'
                        'capable of using atomic instructions for double-precision '
                        'floating point types. This affects how deep vectorization '
                        'code is generated, and will force any potential data-races '
                        'to be run in serial/sequential form, resulting in '
                        'suboptimal deep vectorizations.'
                        )
    parser.add_argument('-nai', '--no_atomic_ints',
                        dest='use_atomic_ints',
                        action='store_false',
                        required=False,
                        help='If supplied, the targeted language / platform is not'
                        'capable of using atomic instructions for single-precision '
                        'integer types. This affects the generated driver kernel, '
                        'see "Driver Kernel Types" in the documentation.'
                        )
    parser.add_argument('-jt', '--jac_type',
                        choices=['exact', 'approximate', 'finite_difference'],
                        required=False,
                        default='exact',
                        help='The type of Jacobian kernel to generate.  An '
                        'approximate Jacobian ignores derivatives of the last '
                        'species with respect to other species in the mechanism.'
                        'This can significantly increase sparsity for mechanisms '
                        'containing reactions that include the last species '
                        'directly, or as a third-body species with a non-unity '
                        'efficiency, but gives results in an approxmiate Jacobian, '
                        'and thus is more suitable to use with implicit integration '
                        'techniques.'
                        )
    parser.add_argument('-jf', '--jac_format',
                        choices=['sparse', 'full'],
                        required=False,
                        default='sparse',
                        help='If "sparse", the Jacobian will be encoded using a '
                        'compressed row or column storage format (for a data order '
                        'of "C" and "F" respectively).'
                        )
    parser.add_argument('-f', '--fixed_size',
                        required=False,
                        default=None,
                        type=int,
                        help='If specified, this is the number of thermo-chemical '
                             'states that pyJac should evaluate in the generated '
                             'source code.  This is most useful for limiting the '
                             'number of states to one (in order to couple with an '
                             'external library that that has already been '
                             'parallelized, e.g., via OpenMP).  This setting will '
                             'also fix array strides as discussed in the '
                             'documentation.'
                        )
    parser.add_argument('-m', '--memory_limits',
                        required=False,
                        type=str,
                        default='',
                        help='Path to a .yaml file indicating desired memory limits '
                             'that control the desired maximum amount of global / '
                             'local / or constant memory that the generated pyjac '
                             'code may allocate.  Useful for testing, or otherwise '
                             'limiting memory usage during runtime. '
                             'The keys of this file are the members of '
                             ':class:`pyjac.kernel_utils.memory_manager.mem_type`')

    args = parser.parse_args()
    return args


def create():
    args = get_parser()
    from .core.create_jacobian import create_jacobian
    create_jacobian(lang=args.lang,
                    mech_name=args.input,
                    therm_name=args.thermo,
                    vector_size=args.vector_size,
                    wide=args.wide,
                    deep=args.deep,
                    unr=args.unroll,
                    build_path=args.build_path,
                    last_spec=args.last_species,
                    skip_jac=args.skip_jac,
                    platform=args.platform,
                    data_order=args.data_order,
                    rate_specialization=args.rate_specialization,
                    split_rate_kernels=args.split_rate_kernels,
                    split_rop_net_kernels=args.split_rop_net_kernels,
                    conp=args.conp,
                    use_atomic_doubles=args.use_atomic_doubles,
                    use_atomic_ints=args.use_atomic_ints,
                    jac_type=args.jac_type,
                    jac_format=args.jac_format,
                    mem_limits=args.memory_limits,
                    fixed_size=args.fixed_size
                    )
