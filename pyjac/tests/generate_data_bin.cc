#include <stdio.h>
#include "stdlib.h"
#include "mechanism.hpp"
int main(){
  double temp = 1000;
  double pr = 10; // units?
  printf("%f %f", temp, pr);
  for(int i=0; i<NS;i++) printf(" %f", ((float)rand()/(float)(RAND_MAX)));
  printf("\n");
}
