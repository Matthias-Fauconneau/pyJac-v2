# Python 2 compatibility
from __future__ import print_function
from __future__ import division

import sys

from pyjac.performance_tester import performance_tester  # noqa


class TestPerformanceTester(object):
    """
    """
    def test_imported(self):
        """Ensure performance_tester module imported.
        """
        assert 'pyjac.performance_tester.performance_tester' in sys.modules
