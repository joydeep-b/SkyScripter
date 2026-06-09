#ifndef LNC_UNREGISTERED_CORE_H
#define LNC_UNREGISTERED_CORE_H

#include <stddef.h>

#include "lnc_fits.h"
#include "lnc_grid.h"

enum {
  LNC_BACKGROUND_TRIMMED_MEAN = 0,
  LNC_BACKGROUND_TRIMMED_MEDIAN = 1,
  LNC_BACKGROUND_SAMPLE_MEDIAN = 2,
};

typedef struct {
  int grid_spacing;
  int window_size;
  int min_samples;
  int background_estimator;
  double trim_fraction;
  double scale_min;
  double scale_max;
  int smooth_passes;
  double min_valid_fraction;
  double H[9];
  double Hinv[9];
} UnregisteredParams;

typedef struct {
  const Image *ref;
  const Image *target;
  const unsigned char *ref_mask;
  const unsigned char *target_mask;
} ImagePair;

typedef struct {
  float *scale;
  float *offset;
  float *ref_bg;
  float *target_bg;
} CorrectionMaps;

int lnc_estimate_unregistered_grid(const ImagePair *images, const UnregisteredParams *params, Grid *grid);
void lnc_apply_unregistered_correction(const Image *target, const Image *ref, const Grid *grid,
                                       const UnregisteredParams *params, float *corrected,
                                       CorrectionMaps maps);
void lnc_write_unregistered_report(const char *path, const UnregisteredParams *params,
                                   const Image *ref, const Image *target, const Grid *grid,
                                   int initial_valid, long ref_masked, long target_masked,
                                   double elapsed, const float *scale_map,
                                   const float *offset_map);
long lnc_count_masked(const unsigned char *mask, long npixels);

#endif
