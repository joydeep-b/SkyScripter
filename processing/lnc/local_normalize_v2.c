#include <ctype.h>
#include <errno.h>
#include <float.h>
#include <math.h>
#include <omp.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <fitsio.h>

typedef struct {
  float t;
  float r;
} Pair;

typedef struct {
  char *ref_path;
  char *target_path;
  char *out_path;
  char *mask_path;
  char *diag_dir;
  char *report_path;
  int save_backgrounds;
  int grid_spacing;
  int window_size;
  int min_samples;
  double trim_fraction;
  double scale_min;
  double scale_max;
  int smooth_passes;
  double min_valid_fraction;
  int sample_patch_size;
  int sample_stride;
  int min_patches;
  double sample_min_valid_fraction;
  double sample_reject_k;
} Options;

typedef struct {
  long width;
  long height;
  float *data;
} Image;

typedef struct {
  int nx;
  int ny;
  float *scale;
  float *offset;
  float *ref_bg;
  float *target_bg;
  float *accept_fraction;
  unsigned char *valid;
} Grid;

static double now_seconds(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static void die_fits(int status, const char *message) {
  if (status) {
    fprintf(stderr, "%s\n", message);
    fits_report_error(stderr, status);
    exit(1);
  }
}

static void *checked_calloc(size_t count, size_t size) {
  void *ptr = calloc(count, size);
  if (!ptr) {
    fprintf(stderr, "Out of memory allocating %zu bytes\n", count * size);
    exit(1);
  }
  return ptr;
}

static void *checked_malloc(size_t size) {
  void *ptr = malloc(size);
  if (!ptr) {
    fprintf(stderr, "Out of memory allocating %zu bytes\n", size);
    exit(1);
  }
  return ptr;
}

static int compare_pair_target(const void *a, const void *b) {
  const Pair *pa = (const Pair *)a;
  const Pair *pb = (const Pair *)b;
  if (pa->t < pb->t) return -1;
  if (pa->t > pb->t) return 1;
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
  float *copy = (float *)checked_malloc((size_t)n * sizeof(float));
  memcpy(copy, values, (size_t)n * sizeof(float));
  qsort(copy, (size_t)n, sizeof(float), compare_float);
  float result = (n & 1) ? copy[n / 2] : 0.5f * (copy[n / 2 - 1] + copy[n / 2]);
  free(copy);
  return result;
}

static Options default_options(void) {
  Options opt;
  memset(&opt, 0, sizeof(opt));
  opt.grid_spacing = 128;
  opt.window_size = 256;
  opt.min_samples = 2000;
  opt.trim_fraction = 0.10;
  opt.scale_min = 0.5;
  opt.scale_max = 2.0;
  opt.smooth_passes = 2;
  opt.min_valid_fraction = 0.30;
  opt.sample_patch_size = 25;
  opt.sample_stride = 32;
  opt.min_patches = 8;
  opt.sample_min_valid_fraction = 0.60;
  opt.sample_reject_k = 2.5;
  return opt;
}

static void usage(FILE *stream) {
  fprintf(stream,
          "Usage: local_normalize_v2 [options] ref.fit target.fit out.fit\n"
          "\n"
          "Options:\n"
          "  --mask PATH              uint8/FITS mask; nonzero pixels are excluded\n"
          "  --diag-dir DIR           write scale/offset diagnostic FITS\n"
          "  --save-backgrounds       also write ref/target background FITS maps\n"
          "  --report PATH            write JSON report\n"
          "  --grid-spacing N         grid spacing in pixels (default 128)\n"
          "  --window-size N          square fit window in pixels (default 256)\n"
          "  --min-samples N          minimum valid samples per grid node (default 2000)\n"
          "  --trim-fraction F        fraction trimmed from both tails by target value (default 0.10)\n"
          "  --scale-min F            minimum local scale (default 0.5)\n"
          "  --scale-max F            maximum local scale (default 2.0)\n"
          "  --smooth-passes N        3x3 grid smoothing passes (default 2)\n"
          "  --min-valid-fraction F   fail below this initial valid grid fraction (default 0.30)\n"
          "  --sample-patch-size N    V2 median sample patch size, odd pixels (default 25)\n"
          "  --sample-stride N        V2 sample patch stride in pixels (default 32)\n"
          "  --min-patches N          minimum accepted V2 patches per grid node (default 8)\n"
          "  --sample-min-valid F     minimum unmasked fraction per V2 patch (default 0.60)\n"
          "  --sample-reject-k F      reject patches above median + k*MAD; negative disables (default 2.5)\n"
          "  -h, --help               show this help\n");
}

static int parse_int_arg(const char *value, const char *name) {
  char *end = NULL;
  errno = 0;
  long parsed = strtol(value, &end, 10);
  if (errno || end == value || *end != '\0' || parsed <= 0 || parsed > INT_MAX) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    exit(2);
  }
  return (int)parsed;
}

static double parse_double_arg(const char *value, const char *name) {
  char *end = NULL;
  errno = 0;
  double parsed = strtod(value, &end);
  if (errno || end == value || *end != '\0' || !isfinite(parsed)) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    exit(2);
  }
  return parsed;
}

static Options parse_options(int argc, char **argv) {
  Options opt = default_options();
  char *positionals[3] = {0};
  int positional_count = 0;

  for (int i = 1; i < argc; ++i) {
    char *arg = argv[i];
    if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
      usage(stdout);
      exit(0);
    } else if (strcmp(arg, "--mask") == 0 && i + 1 < argc) {
      opt.mask_path = argv[++i];
    } else if (strcmp(arg, "--diag-dir") == 0 && i + 1 < argc) {
      opt.diag_dir = argv[++i];
    } else if (strcmp(arg, "--save-backgrounds") == 0) {
      opt.save_backgrounds = 1;
    } else if (strcmp(arg, "--report") == 0 && i + 1 < argc) {
      opt.report_path = argv[++i];
    } else if (strcmp(arg, "--grid-spacing") == 0 && i + 1 < argc) {
      opt.grid_spacing = parse_int_arg(argv[++i], "--grid-spacing");
    } else if (strcmp(arg, "--window-size") == 0 && i + 1 < argc) {
      opt.window_size = parse_int_arg(argv[++i], "--window-size");
    } else if (strcmp(arg, "--min-samples") == 0 && i + 1 < argc) {
      opt.min_samples = parse_int_arg(argv[++i], "--min-samples");
    } else if (strcmp(arg, "--trim-fraction") == 0 && i + 1 < argc) {
      opt.trim_fraction = parse_double_arg(argv[++i], "--trim-fraction");
    } else if (strcmp(arg, "--scale-min") == 0 && i + 1 < argc) {
      opt.scale_min = parse_double_arg(argv[++i], "--scale-min");
    } else if (strcmp(arg, "--scale-max") == 0 && i + 1 < argc) {
      opt.scale_max = parse_double_arg(argv[++i], "--scale-max");
    } else if (strcmp(arg, "--smooth-passes") == 0 && i + 1 < argc) {
      opt.smooth_passes = parse_int_arg(argv[++i], "--smooth-passes");
    } else if (strcmp(arg, "--min-valid-fraction") == 0 && i + 1 < argc) {
      opt.min_valid_fraction = parse_double_arg(argv[++i], "--min-valid-fraction");
    } else if (strcmp(arg, "--sample-patch-size") == 0 && i + 1 < argc) {
      opt.sample_patch_size = parse_int_arg(argv[++i], "--sample-patch-size");
    } else if (strcmp(arg, "--sample-stride") == 0 && i + 1 < argc) {
      opt.sample_stride = parse_int_arg(argv[++i], "--sample-stride");
    } else if (strcmp(arg, "--min-patches") == 0 && i + 1 < argc) {
      opt.min_patches = parse_int_arg(argv[++i], "--min-patches");
    } else if (strcmp(arg, "--sample-min-valid") == 0 && i + 1 < argc) {
      opt.sample_min_valid_fraction = parse_double_arg(argv[++i], "--sample-min-valid");
    } else if (strcmp(arg, "--sample-reject-k") == 0 && i + 1 < argc) {
      opt.sample_reject_k = parse_double_arg(argv[++i], "--sample-reject-k");
    } else if (arg[0] == '-') {
      fprintf(stderr, "Unknown or incomplete option: %s\n", arg);
      usage(stderr);
      exit(2);
    } else {
      if (positional_count >= 3) {
        fprintf(stderr, "Too many positional arguments\n");
        usage(stderr);
        exit(2);
      }
      positionals[positional_count++] = arg;
    }
  }

  if (positional_count != 3) {
    usage(stderr);
    exit(2);
  }
  if (opt.trim_fraction < 0.0 || opt.trim_fraction >= 0.45) {
    fprintf(stderr, "--trim-fraction must be in [0, 0.45)\n");
    exit(2);
  }
  if (opt.scale_min <= 0.0 || opt.scale_max <= opt.scale_min) {
    fprintf(stderr, "Invalid scale clamp range\n");
    exit(2);
  }
  if (opt.min_valid_fraction <= 0.0 || opt.min_valid_fraction > 1.0) {
    fprintf(stderr, "--min-valid-fraction must be in (0, 1]\n");
    exit(2);
  }
  if ((opt.sample_patch_size & 1) == 0) {
    fprintf(stderr, "--sample-patch-size must be odd\n");
    exit(2);
  }
  if (opt.sample_min_valid_fraction <= 0.0 || opt.sample_min_valid_fraction > 1.0) {
    fprintf(stderr, "--sample-min-valid must be in (0, 1]\n");
    exit(2);
  }

  opt.ref_path = positionals[0];
  opt.target_path = positionals[1];
  opt.out_path = positionals[2];
  return opt;
}

static Image read_fits_float(const char *path) {
  fitsfile *fptr = NULL;
  int status = 0;
  int naxis = 0;
  long naxes[3] = {1, 1, 1};
  fits_open_file(&fptr, path, READONLY, &status);
  die_fits(status, "Could not open FITS file");
  fits_get_img_dim(fptr, &naxis, &status);
  fits_get_img_size(fptr, 3, naxes, &status);
  die_fits(status, "Could not read FITS geometry");
  if (naxis != 2) {
    fprintf(stderr, "%s is not a 2D monochrome FITS image (NAXIS=%d)\n", path, naxis);
    exit(1);
  }

  long npixels = naxes[0] * naxes[1];
  float *data = (float *)checked_malloc((size_t)npixels * sizeof(float));
  long fpixel[2] = {1, 1};
  int anynul = 0;
  fits_read_pix(fptr, TFLOAT, fpixel, npixels, NULL, data, &anynul, &status);
  die_fits(status, "Could not read FITS pixels");
  fits_close_file(fptr, &status);
  die_fits(status, "Could not close FITS file");

  Image img = {naxes[0], naxes[1], data};
  return img;
}

static unsigned char *read_mask_fits(const char *path, long width, long height) {
  Image mask_img = read_fits_float(path);
  if (mask_img.width != width || mask_img.height != height) {
    fprintf(stderr, "Mask dimensions do not match input images\n");
    exit(1);
  }
  long npixels = width * height;
  unsigned char *mask = (unsigned char *)checked_calloc((size_t)npixels, sizeof(unsigned char));
  #pragma omp parallel for
  for (long i = 0; i < npixels; ++i) {
    mask[i] = (isfinite(mask_img.data[i]) && mask_img.data[i] != 0.0f) ? 1 : 0;
  }
  free(mask_img.data);
  return mask;
}

static void write_fits_float(const char *path, long width, long height, const float *data) {
  fitsfile *fptr = NULL;
  int status = 0;
  char *filename = checked_malloc(strlen(path) + 2);
  sprintf(filename, "!%s", path);
  long naxes[2] = {width, height};
  fits_create_file(&fptr, filename, &status);
  die_fits(status, "Could not create FITS file");
  fits_create_img(fptr, FLOAT_IMG, 2, naxes, &status);
  die_fits(status, "Could not create FITS image");
  long fpixel[2] = {1, 1};
  fits_write_pix(fptr, TFLOAT, fpixel, width * height, (void *)data, &status);
  die_fits(status, "Could not write FITS pixels");
  fits_close_file(fptr, &status);
  die_fits(status, "Could not close output FITS file");
  free(filename);
}

static char *join_path(const char *dir, const char *name) {
  size_t dlen = strlen(dir);
  bool need_slash = dlen > 0 && dir[dlen - 1] != '/';
  char *path = (char *)checked_malloc(dlen + strlen(name) + (need_slash ? 2 : 1));
  sprintf(path, "%s%s%s", dir, need_slash ? "/" : "", name);
  return path;
}

static Grid create_grid(long width, long height, int spacing) {
  Grid g;
  g.nx = (int)((width - 1) / spacing) + 1;
  g.ny = (int)((height - 1) / spacing) + 1;
  size_t n = (size_t)g.nx * (size_t)g.ny;
  g.scale = (float *)checked_calloc(n, sizeof(float));
  g.offset = (float *)checked_calloc(n, sizeof(float));
  g.ref_bg = (float *)checked_calloc(n, sizeof(float));
  g.target_bg = (float *)checked_calloc(n, sizeof(float));
  g.accept_fraction = (float *)checked_calloc(n, sizeof(float));
  g.valid = (unsigned char *)checked_calloc(n, sizeof(unsigned char));
  return g;
}

static void free_grid(Grid *g) {
  free(g->scale);
  free(g->offset);
  free(g->ref_bg);
  free(g->target_bg);
  free(g->accept_fraction);
  free(g->valid);
}

static bool patch_median_pair(const Image *ref, const Image *target, const unsigned char *mask,
                              long cx, long cy, const Options *opt, Pair *pair_out) {
  int radius = opt->sample_patch_size / 2;
  long x0 = cx - radius;
  long x1 = cx + radius;
  long y0 = cy - radius;
  long y1 = cy + radius;
  if (x0 < 0 || y0 < 0 || x1 >= ref->width || y1 >= ref->height) return false;

  int area = opt->sample_patch_size * opt->sample_patch_size;
  int min_valid = (int)ceil((double)area * opt->sample_min_valid_fraction);
  float *rvals = (float *)checked_malloc((size_t)area * sizeof(float));
  float *tvals = (float *)checked_malloc((size_t)area * sizeof(float));
  int n = 0;
  for (long y = y0; y <= y1; ++y) {
    long row = y * ref->width;
    for (long x = x0; x <= x1; ++x) {
      long idx = row + x;
      if (mask && mask[idx]) continue;
      float r = ref->data[idx];
      float t = target->data[idx];
      if (!isfinite(r) || !isfinite(t)) continue;
      rvals[n] = r;
      tvals[n] = t;
      n++;
    }
  }

  bool ok = n >= min_valid;
  if (ok) {
    pair_out->r = median_float_copy(rvals, n);
    pair_out->t = median_float_copy(tvals, n);
  }
  free(rvals);
  free(tvals);
  return ok;
}

static float high_rejection_threshold(const float *values, int n, double k) {
  if (k < 0.0) return INFINITY;
  float median = median_float_copy(values, n);
  float *deviations = (float *)checked_malloc((size_t)n * sizeof(float));
  for (int i = 0; i < n; ++i) {
    deviations[i] = fabsf(values[i] - median);
  }
  float mad = median_float_copy(deviations, n);
  free(deviations);
  return median + (float)(k * (double)mad);
}

static bool fit_node(const Image *ref, const Image *target, const unsigned char *mask,
                     long cx, long cy, const Options *opt, float *scale_out,
                     float *offset_out, float *ref_bg_out, float *target_bg_out,
                     float *accept_fraction_out, int *samples_out) {
  int half = opt->window_size / 2;
  long x0 = cx - half;
  long x1 = cx + half;
  long y0 = cy - half;
  long y1 = cy + half;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 >= ref->width) x1 = ref->width - 1;
  if (y1 >= ref->height) y1 = ref->height - 1;

  int patch_radius = opt->sample_patch_size / 2;
  long sx0 = x0 + patch_radius;
  long sx1 = x1 - patch_radius;
  long sy0 = y0 + patch_radius;
  long sy1 = y1 - patch_radius;
  if (sx0 > sx1 || sy0 > sy1) return false;

  long nx = ((sx1 - sx0) / opt->sample_stride) + 1;
  long ny = ((sy1 - sy0) / opt->sample_stride) + 1;
  long capacity = nx * ny;
  Pair *pairs = (Pair *)checked_malloc((size_t)capacity * sizeof(Pair));
  int n = 0;
  for (long y = sy0; y <= sy1; y += opt->sample_stride) {
    for (long x = sx0; x <= sx1; x += opt->sample_stride) {
      Pair pair;
      if (patch_median_pair(ref, target, mask, x, y, opt, &pair)) {
        pairs[n++] = pair;
      }
    }
  }

  *samples_out = n;
  if (n < opt->min_patches) {
    free(pairs);
    return false;
  }

  float *targets = (float *)checked_malloc((size_t)n * sizeof(float));
  float *refs = (float *)checked_malloc((size_t)n * sizeof(float));
  for (int i = 0; i < n; ++i) {
    targets[i] = pairs[i].t;
    refs[i] = pairs[i].r;
  }
  float target_threshold = high_rejection_threshold(targets, n, opt->sample_reject_k);
  float ref_threshold = high_rejection_threshold(refs, n, opt->sample_reject_k);

  int accepted = 0;
  for (int i = 0; i < n; ++i) {
    if (pairs[i].t <= target_threshold && pairs[i].r <= ref_threshold) {
      pairs[accepted++] = pairs[i];
    }
  }
  *accept_fraction_out = n > 0 ? (float)accepted / (float)n : 0.0f;
  free(targets);
  free(refs);

  if (accepted < opt->min_patches) {
    free(pairs);
    return false;
  }

  qsort(pairs, (size_t)accepted, sizeof(Pair), compare_pair_target);
  int trim = (int)floor((double)accepted * opt->trim_fraction);
  int begin = trim;
  int end = accepted - trim;
  int kept = end - begin;
  if (kept < opt->min_patches || kept < 3) {
    free(pairs);
    return false;
  }

  long double sum_t = 0.0;
  long double sum_r = 0.0;
  long double sum_tt = 0.0;
  long double sum_tr = 0.0;
  for (int i = begin; i < end; ++i) {
    long double t = pairs[i].t;
    long double r = pairs[i].r;
    sum_t += t;
    sum_r += r;
    sum_tt += t * t;
    sum_tr += t * r;
  }

  long double denom = (long double)kept * sum_tt - sum_t * sum_t;
  long double mean_t = sum_t / (long double)kept;
  long double mean_r = sum_r / (long double)kept;
  if (fabsl(denom) <= LDBL_EPSILON) {
    *scale_out = 1.0f;
    *offset_out = (float)(mean_r - mean_t);
    *ref_bg_out = (float)mean_r;
    *target_bg_out = (float)mean_t;
    free(pairs);
    return true;
  }
  long double scale = ((long double)kept * sum_tr - sum_t * sum_r) / denom;
  long double offset = mean_r - scale * mean_t;
  if (!isfinite((double)scale) || !isfinite((double)offset)) {
    free(pairs);
    return false;
  }

  if (scale < opt->scale_min || scale > opt->scale_max) {
    scale = 1.0;
    offset = mean_r - mean_t;
  }

  *scale_out = (float)scale;
  *offset_out = (float)offset;
  float *accepted_refs = (float *)checked_malloc((size_t)accepted * sizeof(float));
  float *accepted_targets = (float *)checked_malloc((size_t)accepted * sizeof(float));
  for (int i = 0; i < accepted; ++i) {
    accepted_refs[i] = pairs[i].r;
    accepted_targets[i] = pairs[i].t;
  }
  *ref_bg_out = median_float_copy(accepted_refs, accepted);
  *target_bg_out = median_float_copy(accepted_targets, accepted);
  free(accepted_refs);
  free(accepted_targets);
  free(pairs);
  return true;
}

static int estimate_grid(const Image *ref, const Image *target, const unsigned char *mask,
                         const Options *opt, Grid *grid) {
  int valid_count = 0;
  #pragma omp parallel for reduction(+:valid_count) schedule(dynamic)
  for (int gy = 0; gy < grid->ny; ++gy) {
    for (int gx = 0; gx < grid->nx; ++gx) {
      size_t gi = (size_t)gy * (size_t)grid->nx + (size_t)gx;
      long cx = (long)gx * opt->grid_spacing;
      long cy = (long)gy * opt->grid_spacing;
      if (cx >= ref->width) cx = ref->width - 1;
      if (cy >= ref->height) cy = ref->height - 1;
      int samples = 0;
      if (fit_node(ref, target, mask, cx, cy, opt, &grid->scale[gi],
                   &grid->offset[gi], &grid->ref_bg[gi], &grid->target_bg[gi],
                   &grid->accept_fraction[gi], &samples)) {
        grid->valid[gi] = 1;
        valid_count++;
      }
    }
  }
  return valid_count;
}

static void fill_missing_grid(Grid *grid) {
  size_t n = (size_t)grid->nx * (size_t)grid->ny;
  float *new_scale = (float *)checked_calloc(n, sizeof(float));
  float *new_offset = (float *)checked_calloc(n, sizeof(float));
  float *new_ref_bg = (float *)checked_calloc(n, sizeof(float));
  float *new_target_bg = (float *)checked_calloc(n, sizeof(float));
  float *new_accept_fraction = (float *)checked_calloc(n, sizeof(float));
  unsigned char *new_valid = (unsigned char *)checked_calloc(n, sizeof(unsigned char));

  for (int iter = 0; iter < grid->nx + grid->ny; ++iter) {
    int filled = 0;
    memcpy(new_scale, grid->scale, n * sizeof(float));
    memcpy(new_offset, grid->offset, n * sizeof(float));
    memcpy(new_ref_bg, grid->ref_bg, n * sizeof(float));
    memcpy(new_target_bg, grid->target_bg, n * sizeof(float));
    memcpy(new_accept_fraction, grid->accept_fraction, n * sizeof(float));
    memcpy(new_valid, grid->valid, n * sizeof(unsigned char));

    for (int gy = 0; gy < grid->ny; ++gy) {
      for (int gx = 0; gx < grid->nx; ++gx) {
        size_t gi = (size_t)gy * (size_t)grid->nx + (size_t)gx;
        if (grid->valid[gi]) continue;
        double scale = 0.0, offset = 0.0, ref_bg = 0.0, target_bg = 0.0, accept_fraction = 0.0;
        int count = 0;
        for (int dy = -1; dy <= 1; ++dy) {
          int yy = gy + dy;
          if (yy < 0 || yy >= grid->ny) continue;
          for (int dx = -1; dx <= 1; ++dx) {
            int xx = gx + dx;
            if (xx < 0 || xx >= grid->nx || (dx == 0 && dy == 0)) continue;
            size_t ni = (size_t)yy * (size_t)grid->nx + (size_t)xx;
            if (!grid->valid[ni]) continue;
            scale += grid->scale[ni];
            offset += grid->offset[ni];
            ref_bg += grid->ref_bg[ni];
            target_bg += grid->target_bg[ni];
            accept_fraction += grid->accept_fraction[ni];
            count++;
          }
        }
        if (count > 0) {
          new_scale[gi] = (float)(scale / count);
          new_offset[gi] = (float)(offset / count);
          new_ref_bg[gi] = (float)(ref_bg / count);
          new_target_bg[gi] = (float)(target_bg / count);
          new_accept_fraction[gi] = (float)(accept_fraction / count);
          new_valid[gi] = 1;
          filled++;
        }
      }
    }

    memcpy(grid->scale, new_scale, n * sizeof(float));
    memcpy(grid->offset, new_offset, n * sizeof(float));
    memcpy(grid->ref_bg, new_ref_bg, n * sizeof(float));
    memcpy(grid->target_bg, new_target_bg, n * sizeof(float));
    memcpy(grid->accept_fraction, new_accept_fraction, n * sizeof(float));
    memcpy(grid->valid, new_valid, n * sizeof(unsigned char));
    if (filled == 0) break;
  }

  free(new_scale);
  free(new_offset);
  free(new_ref_bg);
  free(new_target_bg);
  free(new_accept_fraction);
  free(new_valid);
}

static void smooth_one(float *values, const unsigned char *valid, int nx, int ny) {
  size_t n = (size_t)nx * (size_t)ny;
  float *tmp = (float *)checked_calloc(n, sizeof(float));
  #pragma omp parallel for
  for (int gy = 0; gy < ny; ++gy) {
    for (int gx = 0; gx < nx; ++gx) {
      double sum = 0.0;
      double weight_sum = 0.0;
      for (int dy = -1; dy <= 1; ++dy) {
        int yy = gy + dy;
        if (yy < 0 || yy >= ny) continue;
        for (int dx = -1; dx <= 1; ++dx) {
          int xx = gx + dx;
          if (xx < 0 || xx >= nx) continue;
          size_t ni = (size_t)yy * (size_t)nx + (size_t)xx;
          if (!valid[ni]) continue;
          double w = (dx == 0 && dy == 0) ? 4.0 : ((dx == 0 || dy == 0) ? 2.0 : 1.0);
          sum += w * values[ni];
          weight_sum += w;
        }
      }
      size_t gi = (size_t)gy * (size_t)nx + (size_t)gx;
      tmp[gi] = weight_sum > 0.0 ? (float)(sum / weight_sum) : values[gi];
    }
  }
  memcpy(values, tmp, n * sizeof(float));
  free(tmp);
}

static void smooth_grid(Grid *grid, int passes) {
  for (int i = 0; i < passes; ++i) {
    smooth_one(grid->scale, grid->valid, grid->nx, grid->ny);
    smooth_one(grid->offset, grid->valid, grid->nx, grid->ny);
    smooth_one(grid->ref_bg, grid->valid, grid->nx, grid->ny);
    smooth_one(grid->target_bg, grid->valid, grid->nx, grid->ny);
    smooth_one(grid->accept_fraction, grid->valid, grid->nx, grid->ny);
  }
}

static float bilinear_grid_value(const float *values, const Grid *grid, const Options *opt,
                                 long x, long y, long width, long height) {
  double gx_f = (double)x / (double)opt->grid_spacing;
  double gy_f = (double)y / (double)opt->grid_spacing;
  int gx0 = (int)floor(gx_f);
  int gy0 = (int)floor(gy_f);
  if (gx0 < 0) gx0 = 0;
  if (gy0 < 0) gy0 = 0;
  if (gx0 >= grid->nx - 1) gx0 = grid->nx - 1;
  if (gy0 >= grid->ny - 1) gy0 = grid->ny - 1;
  int gx1 = gx0 + 1 < grid->nx ? gx0 + 1 : gx0;
  int gy1 = gy0 + 1 < grid->ny ? gy0 + 1 : gy0;

  long x0_coord = (long)gx0 * opt->grid_spacing;
  long y0_coord = (long)gy0 * opt->grid_spacing;
  long x1_coord = (long)gx1 * opt->grid_spacing;
  long y1_coord = (long)gy1 * opt->grid_spacing;
  if (x1_coord >= width) x1_coord = width - 1;
  if (y1_coord >= height) y1_coord = height - 1;

  double tx = (x1_coord == x0_coord) ? 0.0 : (double)(x - x0_coord) / (double)(x1_coord - x0_coord);
  double ty = (y1_coord == y0_coord) ? 0.0 : (double)(y - y0_coord) / (double)(y1_coord - y0_coord);

  float v00 = values[(size_t)gy0 * (size_t)grid->nx + (size_t)gx0];
  float v10 = values[(size_t)gy0 * (size_t)grid->nx + (size_t)gx1];
  float v01 = values[(size_t)gy1 * (size_t)grid->nx + (size_t)gx0];
  float v11 = values[(size_t)gy1 * (size_t)grid->nx + (size_t)gx1];
  double v0 = (1.0 - tx) * v00 + tx * v10;
  double v1 = (1.0 - tx) * v01 + tx * v11;
  return (float)((1.0 - ty) * v0 + ty * v1);
}

static void render_map(const float *grid_values, const Grid *grid, const Options *opt,
                       long width, long height, float *out) {
  #pragma omp parallel for
  for (long y = 0; y < height; ++y) {
    for (long x = 0; x < width; ++x) {
      out[y * width + x] = bilinear_grid_value(grid_values, grid, opt, x, y, width, height);
    }
  }
}

static void apply_correction(const Image *target, const Grid *grid, const Options *opt,
                             float *corrected, float *scale_map, float *offset_map) {
  long width = target->width;
  long height = target->height;
  #pragma omp parallel for
  for (long y = 0; y < height; ++y) {
    for (long x = 0; x < width; ++x) {
      long idx = y * width + x;
      float scale = bilinear_grid_value(grid->scale, grid, opt, x, y, width, height);
      float offset = bilinear_grid_value(grid->offset, grid, opt, x, y, width, height);
      scale_map[idx] = scale;
      offset_map[idx] = offset;
      float value = target->data[idx];
      corrected[idx] = isfinite(value) ? scale * value + offset : value;
    }
  }
}

static void min_max_float(const float *data, size_t n, double *min_out, double *max_out) {
  double mn = DBL_MAX;
  double mx = -DBL_MAX;
  for (size_t i = 0; i < n; ++i) {
    if (!isfinite(data[i])) continue;
    if (data[i] < mn) mn = data[i];
    if (data[i] > mx) mx = data[i];
  }
  *min_out = mn == DBL_MAX ? NAN : mn;
  *max_out = mx == -DBL_MAX ? NAN : mx;
}

static long count_masked(const unsigned char *mask, long npixels) {
  if (!mask) return 0;
  long count = 0;
  #pragma omp parallel for reduction(+:count)
  for (long i = 0; i < npixels; ++i) {
    if (mask[i]) count++;
  }
  return count;
}

static void write_report(const char *path, const Options *opt, long width, long height,
                         int grid_nx, int grid_ny, int initial_valid, int total_nodes,
                         long masked_pixels, double elapsed, const float *scale_map,
                         const float *offset_map) {
  FILE *f = fopen(path, "w");
  if (!f) {
    fprintf(stderr, "Could not write report %s: %s\n", path, strerror(errno));
    exit(1);
  }
  double scale_min = NAN, scale_max = NAN, offset_min = NAN, offset_max = NAN;
  min_max_float(scale_map, (size_t)width * (size_t)height, &scale_min, &scale_max);
  min_max_float(offset_map, (size_t)width * (size_t)height, &offset_min, &offset_max);
  fprintf(f,
          "{\n"
          "  \"width\": %ld,\n"
          "  \"height\": %ld,\n"
          "  \"grid_spacing\": %d,\n"
          "  \"window_size\": %d,\n"
          "  \"min_samples\": %d,\n"
          "  \"background_estimator\": \"sample_median_v2\",\n"
          "  \"sample_patch_size\": %d,\n"
          "  \"sample_stride\": %d,\n"
          "  \"min_patches\": %d,\n"
          "  \"sample_min_valid_fraction\": %.8f,\n"
          "  \"sample_reject_k\": %.8f,\n"
          "  \"grid_nodes\": [%d, %d],\n"
          "  \"initial_valid_nodes\": %d,\n"
          "  \"total_nodes\": %d,\n"
          "  \"initial_valid_fraction\": %.8f,\n"
          "  \"masked_pixels\": %ld,\n"
          "  \"masked_fraction\": %.8f,\n"
          "  \"scale_min\": %.9g,\n"
          "  \"scale_max\": %.9g,\n"
          "  \"offset_min\": %.9g,\n"
          "  \"offset_max\": %.9g,\n"
          "  \"elapsed_seconds\": %.6f,\n"
          "  \"openmp_threads\": %d\n"
          "}\n",
          width, height, opt->grid_spacing, opt->window_size, opt->min_samples,
          opt->sample_patch_size, opt->sample_stride, opt->min_patches,
          opt->sample_min_valid_fraction, opt->sample_reject_k,
          grid_nx, grid_ny, initial_valid, total_nodes,
          (double)initial_valid / (double)total_nodes, masked_pixels,
          (double)masked_pixels / (double)(width * height),
          scale_min, scale_max, offset_min, offset_max, elapsed, omp_get_max_threads());
  fclose(f);
}

int main(int argc, char **argv) {
  double start = now_seconds();
  Options opt = parse_options(argc, argv);

  Image ref = read_fits_float(opt.ref_path);
  Image target = read_fits_float(opt.target_path);
  if (ref.width != target.width || ref.height != target.height) {
    fprintf(stderr, "Reference and target dimensions differ: %ldx%ld vs %ldx%ld\n",
            ref.width, ref.height, target.width, target.height);
    return 1;
  }

  long npixels = ref.width * ref.height;
  unsigned char *mask = NULL;
  if (opt.mask_path) {
    mask = read_mask_fits(opt.mask_path, ref.width, ref.height);
  }
  long masked_pixels = count_masked(mask, npixels);

  Grid grid = create_grid(ref.width, ref.height, opt.grid_spacing);
  int initial_valid = estimate_grid(&ref, &target, mask, &opt, &grid);
  int total_nodes = grid.nx * grid.ny;
  double valid_fraction = (double)initial_valid / (double)total_nodes;
  if (valid_fraction < opt.min_valid_fraction) {
    fprintf(stderr, "Too few valid grid nodes: %d/%d (%.3f)\n",
            initial_valid, total_nodes, valid_fraction);
    return 1;
  }
  fill_missing_grid(&grid);
  smooth_grid(&grid, opt.smooth_passes);

  float *corrected = (float *)checked_malloc((size_t)npixels * sizeof(float));
  float *scale_map = (float *)checked_malloc((size_t)npixels * sizeof(float));
  float *offset_map = (float *)checked_malloc((size_t)npixels * sizeof(float));
  apply_correction(&target, &grid, &opt, corrected, scale_map, offset_map);
  write_fits_float(opt.out_path, ref.width, ref.height, corrected);

  if (opt.diag_dir) {
    float *accept_fraction_map = (float *)checked_malloc((size_t)npixels * sizeof(float));
    render_map(grid.accept_fraction, &grid, &opt, ref.width, ref.height, accept_fraction_map);

    char *scale_path = join_path(opt.diag_dir, "scale_map.fits");
    char *offset_path = join_path(opt.diag_dir, "offset_map.fits");
    char *accept_path = join_path(opt.diag_dir, "accepted_patch_fraction.fits");
    write_fits_float(scale_path, ref.width, ref.height, scale_map);
    write_fits_float(offset_path, ref.width, ref.height, offset_map);
    write_fits_float(accept_path, ref.width, ref.height, accept_fraction_map);
    free(scale_path);
    free(offset_path);
    free(accept_path);
    free(accept_fraction_map);

    if (opt.save_backgrounds) {
      float *ref_bg_map = (float *)checked_malloc((size_t)npixels * sizeof(float));
      float *target_bg_map = (float *)checked_malloc((size_t)npixels * sizeof(float));
      render_map(grid.ref_bg, &grid, &opt, ref.width, ref.height, ref_bg_map);
      render_map(grid.target_bg, &grid, &opt, ref.width, ref.height, target_bg_map);

      char *ref_bg_path = join_path(opt.diag_dir, "ref_background.fits");
      char *target_bg_path = join_path(opt.diag_dir, "target_background.fits");
      write_fits_float(ref_bg_path, ref.width, ref.height, ref_bg_map);
      write_fits_float(target_bg_path, ref.width, ref.height, target_bg_map);
      free(ref_bg_path);
      free(target_bg_path);
      free(ref_bg_map);
      free(target_bg_map);
    }
  }

  if (opt.report_path) {
    write_report(opt.report_path, &opt, ref.width, ref.height, grid.nx, grid.ny,
                 initial_valid, total_nodes, masked_pixels, now_seconds() - start,
                 scale_map, offset_map);
  }

  free(ref.data);
  free(target.data);
  free(mask);
  free(corrected);
  free(scale_map);
  free(offset_map);
  free_grid(&grid);
  return 0;
}
