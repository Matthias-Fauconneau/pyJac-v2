"""
A small collection of tools used in code-generation of various OpenCL files
"""

import re

import loopy as lp
import numpy as np
from pyjac.utils import partition, is_integer

def get_kernel_args(mem, args):
    """
    Parameters
    ----------
    mem: :class:`memory_tools`
        The current memory tool object
    args: list of :class:`loopy.KernelArg`
        The arguments to stringify

    Returns
    -------
    args: str
        a comma separated list of arguments for definition of a kernel function
    """
    return ', '.join([mem.get_signature(False, arr) for arr in args])


def get_temporaries(mem, args):
    """
    Determine which type of temporary variables are required
    for this kernel

    Parameters
    ----------
    mem: :class:`memory_tools`
        The current memory tool object
    args: list of :class:`loopy.KernelArg`
        The arguments to check while determining temporary types

    Returns
    -------
    temps: list of str
        A list of temporary variable definitions for implementation in a kernel
        class
    """
    # always have a double-precision temp
    temps = [lp.GlobalArg('temp_d', dtype=np.float64)]
    if any([x.dtype.is_integral() and isinstance(x, lp.ArrayArg)
            for x in args]):
        # need integer temporary
        arr = next(x for x in args if x.dtype.is_integral())
        temps.append(lp.GlobalArg('temp_i', dtype=arr.dtype))

    return [mem.define(False, temp) for temp in temps]


def max_size(mem, args):
    """
    Find the maximum size (for allocation of a zero-ing buffer) of the given
    arguments

    Parameters
    ----------
    mem: :class:`memory_tools`
        The current memory tool object
    args: list of :class:`loopy.KernelArg`
        The arguments to determine the sizes of

    Returns
    -------
    size: str
        The stringified size
    """
    max_size = [mem.non_ic_size(arr) for arr in args
                if not isinstance(arr, lp.ValueArg)]

    problem_sizes, work_sizes = partition(max_size, is_integer)
    problem_size = '{} * problem_size'.format(max([int(x) for x in problem_sizes]))
    # make sure work sizes are what we expect
    regex = re.compile(r'work_size\s*\*\s*(\d+)')
    assert all(regex.search(x) for x in work_sizes)
    # next, extract the work sizes
    work_size = '{} * work_size'.format(
        max([int(regex.search(x).group(1)) for x in work_sizes]))
    return '({0} > {1} ? {0} : {1})'.format(problem_size, work_size)
