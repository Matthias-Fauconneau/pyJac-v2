#compatibility
from builtins import range

#system
import os
import filecmp
from collections import OrderedDict

#local imports
from ..core.rate_subs import polyfit_kernel_gen, write_chem_utils
from ..loopy_utils.loopy_utils import auto_run, loopy_options, get_device_list, kernel_call
from ..utils import create_dir
from ..kernel_utils import kernel_gen as k_gen
from . import TestClass

#modules
from optionloop import OptionLoop
import cantera as ct
import numpy as np
from nose.plugins.attrib import attr

class SubTest(TestClass):
    def __subtest(self, T, ref_ans, nicename, eqs):
        oploop = OptionLoop(OrderedDict([('lang', ['opencl']),
            ('width', [4, None]),
            ('depth', [4, None]),
            ('ilp', [True, False]),
            ('unr', [None, 4]),
            ('order', ['C', 'F']),
            ('device', get_device_list())]))

        #create the kernel call
        kc = kernel_call('eval_' + nicename,
                            [ref_ans], T_arr=T)

        specs = self.store.specs
        test_size = self.store.test_size
        for i, state in enumerate(oploop):
            try:
                opt = loopy_options(**{x : state[x] for x in state if x != 'device'})
                knl = polyfit_kernel_gen(nicename, eqs, specs,
                                            opt, test_size=test_size)

                #create a dummy kernel generator
                knl = k_gen.wrapping_kernel_generator(
                    name='chem_utils',
                    loopy_opts=opt,
                    kernels=[knl],
                    test_size=test_size
                    )
                knl._make_kernels()

                #now run
                kc.set_state(state['order'])
                assert auto_run(knl.kernels, kc, device=state['device'])
            except Exception as e:
                if not(state['width'] and state['depth']):
                    raise e

    @attr('long')
    def test_cp(self):
        self.__subtest(self.store.T, self.store.spec_cp,
            'cp', self.store.conp_eqs)

    @attr('long')
    def test_cv(self):
        self.__subtest(self.store.T, self.store.spec_cv,
            'cv', self.store.conp_eqs)

    @attr('long')
    def test_h(self):
        self.__subtest(self.store.T, self.store.spec_h,
            'h', self.store.conp_eqs)

    @attr('long')
    def test_u(self):
        self.__subtest(self.store.T, self.store.spec_u,
            'u', self.store.conv_eqs)

    @attr('long')
    def test_b(self):
        self.__subtest(self.store.T, self.store.spec_b,
            'b', self.store.conp_eqs)

    def test_write_chem_utils(self):
        script_dir = self.store.script_dir
        build_dir = self.store.build_dir
        k = write_chem_utils(self.store.specs,
            {'conp' : self.store.conp_eqs, 'conv' : self.store.conv_eqs},
                loopy_options(lang='opencl',
                    width=None, depth=None, ilp=False,
                    unr=None, order='C', platform='CPU'))
        k._make_kernels()
        k._generate_wrapping_kernel(build_dir)

        assert filecmp.cmp(os.path.join(build_dir, 'chem_utils.oclh'),
                        os.path.join(script_dir, 'blessed', 'chem_utils.oclh'))
        assert filecmp.cmp(os.path.join(build_dir, 'chem_utils.ocl'),
                        os.path.join(script_dir, 'blessed', 'chem_utils.ocl'))
