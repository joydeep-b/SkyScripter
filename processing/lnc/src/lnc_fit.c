#include "lnc_fit.h"

#include "lnc_common.h"
#include "lnc_transform.h"

#include <float.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

static int compare_pair_target(const void *a, const void *b) {
  const SamplePair *pa = (const SamplePair *)a;
  const SamplePair *pb = (const SamplePair *)b;
  if (pa->target < pb->target) return -1;
  if (pa->target > pb->target) return 1;
  return 0;
}

static int compare_float(const void *a, const void *b) {
  float fa = *(const float *)a;
  float fb = *(const float *)b;
  if (fa < fb) return -1;
  if (fa > fb) return 1;
  return 0;
}

static float median_float_copy(const float *values, int n) {
  float *copy = (float *)lnc_checked_malloc((size_t)n * sizeof(float));
  memcpy(copy, values, (size_t)n * sizeof(float));
  qsort(copy, (size_t)n, sizeof(float), compare_float);
  float result = (n & 1) ? copy[n / 2] : 0.5f * (copy[n / 2 - 1] + copy[n / 2]);
  free(copy);
  return result;
}

static bool sample_bilinear(const Image *img, double x, double y, float *out) {
  if (!lnc_in_bounds(x, y, img->width, img->height)) return false;
  long x0 = (long)floor(x);
  long y0 = (long)floor(y);
  long x1 = x0 + 1 < img->width ? x0 + 1 : x0;
  long y1 = y0 + 1 < img->height ? y0 + 1 : y0;
  double tx = x - (double)x0;
  double ty = y - (double)y0;
  float v00 = img->data[y0 * img->width + x0];
  float v10 = img->data[y0 * img->width + x1];
  float v01 = img->data[y1 * img->width + x0];
  float v11 = img->data[y1 * img->width + x1];
  if (!isfinite(v00) || !isfinite(v10) || !isfinite(v01) || !isfinite(v11)) return false;
  double v0 = (1.0 - tx) * (double)v00 + tx * (double)v10;
  double v1 = (1.0 - tx) * (double)v01 + tx * (double)v11;
  *out = (float)((1.0 - ty) * v0 + ty * v1);
  return true;
}

static bool mask_at(const unsigned char *mask, long width, long height, double x, double y) {
  if (!mask) return false;
  long xi = lround(x);
  long yi = lround(y);
  if (xi < 0 || yi < 0 || xi >= width || yi >= height) return true;
  return mask[yi * width + xi] != 0;
}

static int collect_paired_samples(const ImagePair *images, long cx, long cy,
                                  const UnregisteredParams *params, SamplePair *pairs) {
  const Image *ref = images->ref;
  const Image *target = images->target;
  int radius = params->window_size / 2;
  long x0 = cx - radius;
  long x1 = cx + radius;
  long y0 = cy - radius;
  long y1 = cy + radius;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= ref->width) x1 = ref->width - 1;
  if (y1 >= ref->height) y1 = ref->height - 1;

  int n = 0;
  double r2 = (double)radius * (double)radius;
  for (long y = y0; y <= y1; ++y) {
    long row = y * ref->width;
    for (long x = x0; x <= x1; ++x) {
      double dx = (double)x - (double)cx;
      double dy = (double)y - (double)cy;
      if (dx * dx + dy * dy > r2) continue;
      long idx = row + x;
      float r = ref->data[idx];
      if (!isfinite(r)) continue;
      if (images->ref_mask && images->ref_mask[idx]) continue;

      double tx = 0.0, ty = 0.0;
      if (!lnc_apply_homography(params->Hinv, (double)x, (double)y, &tx, &ty)) continue;
      if (!lnc_in_bounds(tx, ty, target->width, target->height)) continue;
      if (mask_at(images->target_mask, target->width, target->height, tx, ty)) continue;

      float t = 0.0f;
      if (!sample_bilinear(target, tx, ty, &t)) continue;
      pairs[n].ref = r;
      pairs[n].target = t;
      n++;
    }
  }
  return n;
}

static bool fit_trimmed_affine(SamplePair *pairs, int n, const UnregisteredParams *params,
                               CorrectionFields *fields) {
  qsort(pairs, (size_t)n, sizeof(SamplePair), compare_pair_target);
  int trim = (int)floor((double)n * params->trim_fraction);
  int begin = trim;
  int end = n - trim;
  int kept = end - begin;
  if (kept < params->min_samples / 2 || kept < 16) return false;

  long double sum_t = 0.0;
  long double sum_r = 0.0;
  long double sum_tt = 0.0;
  long double sum_tr = 0.0;
  float *kept_refs = (float *)lnc_checked_malloc((size_t)kept * sizeof(float));
  float *kept_targets = (float *)lnc_checked_malloc((size_t)kept * sizeof(float));
  for (int i = begin; i < end; ++i) {
    int out_i = i - begin;
    long double t = pairs[i].target;
    long double r = pairs[i].ref;
    kept_refs[out_i] = pairs[i].ref;
    kept_targets[out_i] = pairs[i].target;
    sum_t += t;
    sum_r += r;
    sum_tt += t * t;
    sum_tr += t * r;
  }

  long double denom = (long double)kept * sum_tt - sum_t * sum_t;
  long double mean_t = sum_t / (long double)kept;
  long double mean_r = sum_r / (long double)kept;
  float background_r = (float)mean_r;
  float background_t = (float)mean_t;
  if (params->background_estimator != LNC_BACKGROUND_TRIMMED_MEAN) {
    background_r = median_float_copy(kept_refs, kept);
    background_t = median_float_copy(kept_targets, kept);
  }
  free(kept_refs);
  free(kept_targets);

  long double scale = 1.0;
  long double offset = (long double)background_r - (long double)background_t;

  if (fabsl(denom) > LDBL_EPSILON) {
    scale = ((long double)kept * sum_tr - sum_t * sum_r) / denom;
    offset = mean_r - scale * mean_t;
    if (!isfinite((double)scale) || !isfinite((double)offset)) return false;
    if (scale < params->scale_min || scale > params->scale_max) {
      scale = 1.0;
      offset = (long double)background_r - (long double)background_t;
    }
  }

  fields->scale = (float)scale;
  fields->offset = (float)offset;
  fields->ref_bg = background_r;
  fields->target_bg = background_t;
  return true;
}

FitResult lnc_fit_node(const ImagePair *images, long cx, long cy, const UnregisteredParams *params) {
  int radius = params->window_size / 2;
  long x0 = cx - radius;
  long x1 = cx + radius;
  long y0 = cy - radius;
  long y1 = cy + radius;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= images->ref->width) x1 = images->ref->width - 1;
  if (y1 >= images->ref->height) y1 = images->ref->height - 1;

  long capacity = (x1 - x0 + 1) * (y1 - y0 + 1);
  SamplePair *pairs = (SamplePair *)lnc_checked_malloc((size_t)capacity * sizeof(SamplePair));
  int n = collect_paired_samples(images, cx, cy, params, pairs);
  FitResult result = {0};
  result.samples = n;
  if (n >= params->min_samples) {
    result.valid = fit_trimmed_affine(pairs, n, params, &result.fields);
  }
  free(pairs);
  return result;
}
