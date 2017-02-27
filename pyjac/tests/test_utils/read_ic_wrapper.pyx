import cython
import numpy as np
cimport numpy as np
from cpython cimport bool

cdef extern from "read_initial_conditions.h":
    void read_initial_conditions (const char *filename, unsigned int NUM,
                         double *T_host, double *P_host, double *conc_host,
                         const char order);

cdef const char* filename = 'data.bin'
cdef char C_ord = 'C'
cdef char F_ord = 'F'

@cython.boundscheck(False)
@cython.wraparound(False)
def read_ics(np.uint_t NUM,
            np.ndarray[np.float64_t] T,
            np.ndarray[np.float64_t] P,
            np.ndarray[np.float64_t] conc,
            bool C_order):
    read_initial_conditions(filename, NUM, &T[0], &P[0], &conc[0], C_ord if C_order else F_ord)
    return None