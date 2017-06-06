#! /usr/bin/env python

"""
A simple script that acts as the 'EDITOR' for loopy code generation,
changing simple code-generation patterns that cause errors for various
OpenCL implementations (mostly Intel)
"""

import sys
import re
from collections import OrderedDict

swaps = OrderedDict([
    # replace "bad" lid()/gid() subtractions in for loop clauses
    # re: https://software.intel.com/en-us/forums/opencl/topic/704155
    (r'(for.+)\+\s*-1\s*\*\s*((?:lid|gid)\((?:\d+)\))([^;]+)', r'\1\3 - \2'),
    # replace "bad" lid()/gid() subtractions in general
    # re: https://software.intel.com/en-us/forums/opencl/topic/709578
    (r'(\+\s*-\s*)(\d+)\s*\*\s*((?:lid|gid)\((?:\d+)\))', r'- \2 * \3'),
    # edit in &'s for atomic add/div/etc operators
    (r'([^\s]+)\b\s*=\s*(atomic\w+_\w+\s*\(\s*)\1', r'\2&\1')
])


def __get_file(filename, text_in=None):
    if filename.lower() == 'stdin':
        lines = text_in.split('\n')
    else:
        with open(filename, 'r') as file:
            lines = file.readlines()
    return lines


def __save_file(filename, lines):
    if filename.lower() != 'stdin':
        with open(filename, 'w') as file:
            file.writelines(lines)


def substitute(filename, text_in=None):
    lines = __get_file(filename, text_in=text_in)

    # do any replacements
    for swap in swaps:
        lines = [re.sub(swap, swaps[swap], line) for line in lines]

    __save_file(filename, lines)
    return '\n'.join(lines)


if __name__ == '__main__':
    substitute(sys.argv[1], sys.argv[2:])
