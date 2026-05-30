#ifndef LNC_FIT_H
#define LNC_FIT_H

#include "lnc_grid.h"
#include "lnc_unregistered_core.h"

#include <stdbool.h>
#include <stddef.h>

typedef struct {
  float target;
  float ref;
} SamplePair;

typedef struct {
  bool valid;
  int samples;
  CorrectionFields fields;
} FitResult;

FitResult lnc_fit_node(const ImagePair *images, long cx, long cy, const UnregisteredParams *params);

#endif
