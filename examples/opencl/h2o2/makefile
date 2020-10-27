CXX = g++
CXXFLAGS=-x c++
DEPS =
LDFLAGS = -lOpenCL

%.o: %.ocl $(DEPS)
	$(CXX) $(CXXFLAGS) -c $< -o $@

.PHONY: all clean

all: genic jac

jac: jacobian_main.o read_initial_conditions.o error_check.o timer.o jacobian_compiler.o
	$(CXX) -o $@ $^ $(LDFLAGS)

genic: genic.cpp
	$(CXX) -o $@ genic.cpp

clean:
	rm -f *.o jac genic jacobian.bin 
