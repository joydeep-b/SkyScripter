#include "lnc_pair_process.h"

#include "lnc_common.h"
#include "lnc_fits.h"
#include "lnc_grid.h"
#include "lnc_transform.h"
#include "lnc_unregistered_core.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static UnregisteredParams default_params(void) {
  UnregisteredParams params;
  memset(&params, 0, sizeof(params));
  params.grid_spacing = 128;
  params.window_size = 256;
  params.min_samples = 2000;
  params.background_estimator = LNC_BACKGROUND_TRIMMED_MEDIAN;
  params.photometric_model = LNC_PHOTOMETRIC_LOCAL_LINEAR;
  params.trim_fraction = 0.10;
  params.scale_min = 0.5;
  params.scale_max = 2.0;
  params.global_scale = 1.0;
  params.smooth_passes = 2;
  params.min_valid_fraction = 0.30;
  params.H[0] = 1.0;
  params.H[4] = 1.0;
  params.H[8] = 1.0;
  params.Hinv[0] = 1.0;
  params.Hinv[4] = 1.0;
  params.Hinv[8] = 1.0;
  return params;
}

static void write_diagnostics(const char *diag_dir, const Image *target, CorrectionMaps maps) {
  if (!diag_dir) return;
  char *scale_path = lnc_join_path(diag_dir, "scale_map.fits");
  char *offset_path = lnc_join_path(diag_dir, "offset_map.fits");
  lnc_write_fits_float(scale_path, target->width, target->height, maps.scale);
  lnc_write_fits_float(offset_path, target->width, target->height, maps.offset);
  free(scale_path);
  free(offset_path);
}

static void free_outputs(float *corrected, CorrectionMaps maps) {
  free(corrected);
  free(maps.scale);
  free(maps.offset);
  free(maps.ref_bg);
  free(maps.target_bg);
}

static const char *background_estimator_name(int estimator) {
  switch (estimator) {
    case LNC_BACKGROUND_TRIMMED_MEAN:
      return "trimmed-mean";
    case LNC_BACKGROUND_SAMPLE_MEDIAN:
      return "sample-median";
    case LNC_BACKGROUND_TRIMMED_MEDIAN:
    default:
      return "trimmed-median";
  }
}

static LncFitsMetadata metadata_from_request(const LncPairRequest *request,
                                             const UnregisteredParams *params,
                                             long ref_masked,
                                             long target_masked) {
  LncFitsMetadata metadata = {
      .version = "2-unregistered",
      .mode = request->mode ? request->mode : "unregistered-pair",
      .output_format = "float32-raw",
      .value_scale = "adu",
      .background_estimator = background_estimator_name(params->background_estimator),
      .photometric_model = params->photometric_model == LNC_PHOTOMETRIC_STAR_SCALE_ADDITIVE
                               ? "star-scale-additive"
                               : "local-linear",
      .reference_path = request->ref_path,
      .target_path = request->target_path,
      .report_path = request->report_path,
      .sequence_index = request->sequence_index,
      .grid_spacing = params->grid_spacing,
      .window_size = params->window_size,
      .min_samples = params->min_samples,
      .smooth_passes = params->smooth_passes,
      .trim_fraction = params->trim_fraction,
      .scale_min = params->scale_min,
      .scale_max = params->scale_max,
      .global_scale = params->global_scale,
      .min_valid_fraction = params->min_valid_fraction,
      .ref_masked_pixels = ref_masked,
      .target_masked_pixels = target_masked,
  };
  return metadata;
}

int lnc_load_reference(const char *ref_path, const char *ref_mask_path, LncLoadedReference *reference) {
  if (!ref_path || !reference) return 2;
  memset(reference, 0, sizeof(*reference));
  reference->image = lnc_read_fits_float(ref_path);
  long ref_npixels = reference->image.width * reference->image.height;
  reference->mask = lnc_read_mask_fits(ref_mask_path, reference->image.width, reference->image.height);
  reference->masked_pixels = lnc_count_masked(reference->mask, ref_npixels);
  return 0;
}

void lnc_free_loaded_reference(LncLoadedReference *reference) {
  if (!reference) return;
  lnc_free_image(&reference->image);
  free(reference->mask);
  memset(reference, 0, sizeof(*reference));
}

int lnc_normalize_unregistered_target(const LncLoadedReference *reference,
                                      const LncPairRequest *request,
                                      LncPairResult *result) {
  if (!request || !request->ref_path || !request->target_path || !request->out_path) {
    return 2;
  }
  if (!reference || !reference->image.data) return 2;

  memset(result, 0, sizeof(*result));
  double start = lnc_now_seconds();
  UnregisteredParams params = request->has_params ? request->params : default_params();
  memcpy(params.H, request->homography, sizeof(params.H));
  if (!lnc_invert_homography(params.H, params.Hinv)) {
    fprintf(stderr, "Homography is singular for %s\n", request->target_path);
    return 1;
  }

  Image target = lnc_read_fits_float(request->target_path);
  long target_npixels = target.width * target.height;
  unsigned char *target_mask = lnc_read_mask_fits(request->target_mask_path, target.width, target.height);
  long target_masked = lnc_count_masked(target_mask, target_npixels);

  ImagePair images = {&reference->image, &target, reference->mask, target_mask};
  Grid grid = lnc_create_grid(reference->image.width, reference->image.height, params.grid_spacing);
  int initial_valid = lnc_estimate_unregistered_grid(&images, &params, &grid);
  int total_nodes = grid.nx * grid.ny;
  double valid_fraction = (double)initial_valid / (double)total_nodes;
  if (valid_fraction < params.min_valid_fraction) {
    fprintf(stderr, "Too few valid grid nodes for %s: %d/%d (%.3f)\n",
            request->target_path, initial_valid, total_nodes, valid_fraction);
    lnc_free_grid(&grid);
    lnc_free_image(&target);
    free(target_mask);
    return 1;
  }
  lnc_fill_missing_grid(&grid);
  lnc_smooth_grid(&grid, params.smooth_passes);

  float *corrected = (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float));
  CorrectionMaps maps = {
      (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)),
      (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)),
      NULL,
      NULL,
  };
  lnc_apply_unregistered_correction(&target, &reference->image, &grid, &params, corrected, maps);
  LncFitsMetadata metadata = metadata_from_request(request, &params, reference->masked_pixels, target_masked);
  lnc_write_science_fits_float(request->out_path, request->target_path, target.width, target.height,
                               corrected, &metadata);
  write_diagnostics(request->diag_dir, &target, maps);

  if (request->report_path) {
    lnc_write_unregistered_report(request->report_path, &params, &reference->image, &target, &grid,
                                  initial_valid, reference->masked_pixels, target_masked,
                                  lnc_now_seconds() - start, maps.scale, maps.offset);
  }

  result->initial_valid_nodes = initial_valid;
  result->total_nodes = total_nodes;
  result->valid_fraction = valid_fraction;
  result->elapsed_seconds = lnc_now_seconds() - start;

  lnc_free_image(&target);
  free(target_mask);
  free_outputs(corrected, maps);
  lnc_free_grid(&grid);
  return 0;
}
