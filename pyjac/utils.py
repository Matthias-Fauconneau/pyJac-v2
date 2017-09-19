# -*- coding: utf-8 -*-
"""Module containing utility functions.
"""

# Standard libraries
import os
import errno
import argparse

__all__ = ['line_start', 'comment', 'langs', 'file_ext',
           'header_ext', 'line_end', 'exp_10_fun', 'array_chars',
           'get_species_mappings', 'get_nu', 'read_str_num', 'split_str',
           'create_dir', 'reassign_species_lists', 'is_integer', 'get_parser']

line_start = '  '
comment = dict(c='//', cuda='//',
               fortran='!', matlab='%'
               )
"""dict: comment characters for each language"""

langs = ['c', 'opencl', 'ispc']  # , 'cuda'
"""list(`str`): list of supported languages"""

file_ext = dict(c='.c', cuda='.cu', opencl='.ocl')
"""dict: source code file extensions based on language"""


header_ext = dict(c='.h', cuda='.cuh', opencl='.oclh')
"""dict: header extensions based on language"""

line_end = dict(c=';', cuda=';',
                opencl=';'
                )
"""dict: line endings dependent on language"""

decl_map = {'opencl': '__global',
            'cuda': '__device__',
            'c': ''}
"""dict: declaration modification string for global memory"""

can_vectorize_lang = {'c': False,
                      'cuda': True,
                      'opencl': True,
                      'ispc': True}
"""dict: defines whether a language can be 'vectorized' in the loopy sense"""

exp_10_fun = dict(c="pow(10.0, ", cuda='exp10(',
                  fortran='exp(log(10) * ', matlab='exp(log(10.0) * '
                  )
"""dict: exp10 functions for various languages"""

array_chars = dict(c="[{}]", cuda="[INDEX({})]",
                   fortran="({})", matlab="({})"
                   )
"""dict: the characters to format an index into an array per language"""


class EnumType(object):
    """Factory for working with argparse for creating enum object types"""
    def __init__(self, enumclass):
        self.enums = enumclass

    def __call__(self, astring):
        name = self.enums.__name__
        try:
            return self.enums[astring.upper()]
        except KeyError:
            msg = ', '.join([t.name.lower() for t in self.enums])
            msg = '{0}: use one of {1}'.format(name, msg)
            raise argparse.ArgumentTypeError(msg)

    def __repr__(self):
        astr = ', '.join([t.name.lower() for t in self.enums])
        return '{0}({1})' % (self.enums.__name__, astr)


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
    Checks that 'lang' is a valid identified

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

    import multiprocessing
    # command line arguments
    parser = ArgumentParser(description='pyJac: Generates source code '
                                        'for analytical chemical '
                                        'Jacobians.'
                            )
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
    parser.add_argument('-ic', '--initial-conditions',
                        type=str,
                        dest='initial_conditions',
                        default='',
                        required=False,
                        help='A comma separated list of initial initial '
                             'conditions to set in the '
                             'set_same_initial_conditions method.\n'
                             '   Expected Form: T,P,Species1=...,Species2=...,...\n'
                             '   Temperature in K\n'
                             '   Pressure in Atm\n'
                             '   Species in moles'
                        )
    # cuda specific
    parser.add_argument('-co', '--cache-optimizer',
                        dest='cache_optimizer',
                        action='store_true',
                        default=False,
                        help='Attempt to optimize cache store/loading '
                             'via use of a greedy selection algorithm. (Experimental)'
                        )
    parser.add_argument('-nosmem', '--no-shared-memory',
                        dest='no_shared',
                        action='store_true',
                        default=False,
                        help='Use this option to turn off attempted shared '
                             'memory acceleration for CUDA.'
                        )
    parser.add_argument('-pshare', '--prefer-shared',
                        dest='L1_preferred',
                        action='store_false',
                        default=True,
                        help='Use this option to allocate more space for '
                             'shared memory than the L1 cache for CUDA '
                             '(not recommended).'
                        )
    parser.add_argument('-nb', '--num-blocks',
                        type=int,
                        dest='num_blocks',
                        default=8,
                        required=False,
                        help='The target number of blocks / sm for CUDA.'
                        )
    parser.add_argument('-nt', '--num-threads',
                        type=int,
                        dest='num_threads',
                        default=64,
                        required=False,
                        help='The target number of threads / block for CUDA.'
                        )
    parser.add_argument('-mt', '--multi-threaded',
                        type=int,
                        dest='multi_thread',
                        default=multiprocessing.cpu_count(),
                        required=False,
                        help='The number of threads to use during the '
                             'optimization process.'
                        )
    parser.add_argument('-fopt', '--force-optimize',
                        dest='force_optimize',
                        action='store_true',
                        default=False,
                        help='Use this option to force a reoptimization of '
                             'the mechanism (usually only happens when '
                             'generating for a different mechanism).'
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
    parser.add_argument('-ad', '--auto_diff',
                        default=False,
                        action='store_true',
                        help='Use this option to generate file for use with the '
                             'Adept autodifferentiation library.')
    parser.add_argument('-sj', '--skip_jac',
                        required=False,
                        default=False,
                        action='store_true',
                        help='If specified, this option turns off Jacobian generation '
                             '(only rate subs are generated)')

    args = parser.parse_args()
    return args
