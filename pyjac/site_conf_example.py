import os
def get_paths():
    path = os.path.join('/etc', 'OpenCL', 'vendors')
    vendors = [f for f in os.path.listdir(path) if os.path.isfile(
                    os.path.join(path, f))]

    def __get_vendor_name(vendor):
        if 'intel' in vendor.lower():
            return 'Intel'
        elif 'amd' in vendor.lower():
            return 'AMD'
        elif 'nvidia' in vendor.lower():
            return 'NVIDIA'

    paths = {}
    #now scan vendors, and get lib paths
    for v in vendors:
        with open(os.path.join(path, v), 'r') as file:
            vendor = file.read()
        paths[__get_vendor_name(v)] = os.path.dirname(os.path.readpath(vendor))

CL_INC_DIR = ['/path/to/cl/headers']
CL_LIBNAME = ['OpenCL']
CL_VERSION = '1.2'
CC_FLAGS = ['-mtune=native', '-03', '-std=c99']
CL_PATHS = get_paths()