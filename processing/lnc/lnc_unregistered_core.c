#include "lnc_unregistered_core.h"

#include "lnc_common.h"
#include "lnc_fit.h"
#include "lnc_transform.h"

#include <errno.h>
#include <math.h>
#include <omp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int lnc_estimate_unregistered_grid(const ImagePair *images, const UnregisteredParams *params, Grid *grid) {
  int valid_count = 0;
  #pragma omp parallel for reduction(+:valid_count) schedule(dynamic)
  for (int gy = 0; gy < grid->ny; ++gy) {
    for (int gx = 0; gx < grid->nx; ++gx) {
      size_t gi = (size_t)gy * (size_t)grid->nx + (size_t)gx;
      long cx = (long)gx * params->grid_spacing;
      long cy = (long)gy * params->grid_spacing;
      if (cx >= images->ref->width) cx = images->ref->width - 1;
      if (cy >= images->ref->height) cy = images->ref->height - 1;

      FitResult fit = lnc_fit_node(images, cx, cy, params);
      if (fit.valid) {
        lnc_grid_set_fields(grid, gi, fit.fields);
        grid->valid[gi] = 1;
        valid_count++;
      }
    }
  }
  return valid_count;
}

static CorrectionFields lookup_fields_at_target(const Image *ref, const Grid *grid,
                                                const UnregisteredParams *params, long x, long y) {
  CorrectionFields fields = {1.0f, 0.0f, 0.0f, 0.0f};
  double rx = 0.0;
  double ry = 0.0;
  if (lnc_apply_homography(params->H, (double)x, (double)y, &rx, &ry)) {
    lnc_lookup_correction_fields(grid, params->grid_spacing, rx, ry, ref->width, ref->height, &fields);
  }
  return fields;
}

void lnc_apply_unregistered_correction(const Image *target, const Image *ref, const Grid *grid,
                                       const UnregisteredParams *params, float *corrected,
                                       CorrectionMaps maps) {
  long width = target->width;
  long height = target->height;
  #pragma omp parallel for
  for (long y = 0; y < height; ++y) {
    for (long x = 0; x < width; ++x) {
      long idx = y * width + x;
      CorrectionFields fields = lookup_fields_at_target(ref, grid, params, x, y);
      maps.scale[idx] = fields.scale;
      maps.offset[idx] = fields.offset;
      if (maps.ref_bg) maps.ref_bg[idx] = fields.ref_bg;
      if (maps.target_bg) maps.target_bg[idx] = fields.target_bg;
      float value = target->data[idx];
      corrected[idx] = isfinite(value) ? fields.scale * value + fields.offset : value;
    }
  }
}

long lnc_count_masked(const unsigned char *mask, long npixels) {
  if (!mask) return 0;
  long count = 0;
  #pragma omp parallel for reduction(+:count)
  for (long i = 0; i < npixels; ++i) {
    if (mask[i]) count++;
  }
  return count;
}

void lnc_write_unregistered_report(const char *path, const UnregisteredParams *params,
                                   const Image *ref, const Image *target, const Grid *grid,
                                   int initial_valid, long ref_masked, long target_masked,
                                   double elapsed, const float *scale_map,
                                   const float *offset_map) {
  FILE *f = fopen(path, "w");
  if (!f) {
    fprintf(stderr, "Could not write report %s: %s\n", path, strerror(errno));
    exit(1);
  }
  double scale_min = NAN, scale_max = NAN, offset_min = NAN, offset_max = NAN;
  lnc_min_max_float(scale_map, (size_t)target->width * (size_t)target->height, &scale_min, &scale_max);
  lnc_min_max_float(offset_map, (size_t)target->width * (size_t)target->height, &offset_min, &offset_max);
  int total_nodes = grid->nx * grid->ny;
  fprintf(f,
          "{\n"
          "  \"background_estimator\": \"transform-aware-trimmed-median\",\n"
          "  \"reference_width\": %ld,\n"
          "  \"reference_height\": %ld,\n"
          "  \"target_width\": %ld,\n"
          "  \"target_height\": %ld,\n"
          "  \"grid_spacing\": %d,\n"
          "  \"window_size\": %d,\n"
          "  \"min_samples\": %d,\n"
          "  \"grid_nodes\": [%d, %d],\n"
          "  \"initial_valid_nodes\": %d,\n"
          "  \"total_nodes\": %d,\n"
          "  \"initial_valid_fraction\": %.8f,\n"
          "  \"ref_masked_pixels\": %ld,\n"
          "  \"target_masked_pixels\": %ld,\n"
          "  \"scale_min\": %.9g,\n"
          "  \"scale_max\": %.9g,\n"
          "  \"offset_min\": %.9g,\n"
          "  \"offset_max\": %.9g,\n"
          "  \"elapsed_seconds\": %.6f,\n"
          "  \"openmp_threads\": %d\n"
          "}\n",
          ref->width, ref->height, target->width, target->height, params->grid_spacing,
          params->window_size, params->min_samples, grid->nx, grid->ny, initial_valid,
          total_nodes, (double)initial_valid / (double)total_nodes, ref_masked,
          target_masked, scale_min, scale_max, offset_min, offset_max, elapsed,
          omp_get_max_threads());
  fclose(f);
}
