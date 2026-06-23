#include "lnc_common.h"
#include "lnc_fits.h"
#include "lnc_grid.h"
#include "lnc_transform.h"
#include "lnc_unregistered_core.h"

#include <omp.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
  char *ref_path;
  char *target_path;
  char *out_path;
  char *ref_mask_path;
  char *target_mask_path;
  char *diag_dir;
  char *report_path;
  int save_backgrounds;
  int threads;
  UnregisteredParams params;
} Options;

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

static Options default_options(void) {
  Options opt;
  memset(&opt, 0, sizeof(opt));
  opt.params = default_params();
  return opt;
}

static void usage(FILE *stream) {
  fprintf(stream,
          "Usage: lnc_unregistered_pair [options] ref.fit target.fit out.fit\n"
          "\n"
          "Options:\n"
          "  --ref-mask PATH          uint8/FITS reference mask; nonzero pixels excluded\n"
          "  --target-mask PATH       uint8/FITS target mask; nonzero pixels excluded\n"
          "  --homography 9*F         target-array to reference-array homography\n"
          "  --diag-dir DIR           write scale/offset diagnostic FITS\n"
          "  --save-backgrounds       also write ref/target background FITS maps\n"
          "  --report PATH            write JSON report\n"
          "  --threads N              OpenMP threads for one unregistered LNC pair\n"
          "  --background-estimator NAME  trimmed-mean, trimmed-median, or sample-median\n"
          "  --photometric-model NAME local-linear or star-scale-additive\n"
          "  --global-scale F         fixed target-to-reference scale for star-scale-additive\n"
          "  --grid-spacing N         grid spacing in reference pixels (default 128)\n"
          "  --window-size N          circular fit footprint diameter (default 256)\n"
          "  --min-samples N          minimum valid samples per grid node (default 2000)\n"
          "  --trim-fraction F        fraction trimmed from both tails by target value (default 0.10)\n"
          "  --scale-min F            minimum local scale (default 0.5)\n"
          "  --scale-max F            maximum local scale (default 2.0)\n"
          "  --smooth-passes N        3x3 grid smoothing passes (default 2)\n"
          "  --min-valid-fraction F   fail below this initial valid grid fraction (default 0.30)\n"
          "  -h, --help               show this help\n");
}

static Options parse_options(int argc, char **argv) {
  Options opt = default_options();
  char *positionals[3] = {0};
  int positional_count = 0;
  bool have_homography = false;

  for (int i = 1; i < argc; ++i) {
    char *arg = argv[i];
    if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
      usage(stdout);
      exit(0);
    } else if (strcmp(arg, "--ref-mask") == 0 && i + 1 < argc) {
      opt.ref_mask_path = argv[++i];
    } else if (strcmp(arg, "--target-mask") == 0 && i + 1 < argc) {
      opt.target_mask_path = argv[++i];
    } else if (strcmp(arg, "--homography") == 0 && i + 9 < argc) {
      for (int k = 0; k < 9; ++k) {
        opt.params.H[k] = lnc_parse_double_arg(argv[++i], "--homography");
      }
      have_homography = true;
    } else if (strcmp(arg, "--diag-dir") == 0 && i + 1 < argc) {
      opt.diag_dir = argv[++i];
    } else if (strcmp(arg, "--save-backgrounds") == 0) {
      opt.save_backgrounds = 1;
    } else if (strcmp(arg, "--report") == 0 && i + 1 < argc) {
      opt.report_path = argv[++i];
    } else if (strcmp(arg, "--threads") == 0 && i + 1 < argc) {
      opt.threads = lnc_parse_int_arg(argv[++i], "--threads");
    } else if (strcmp(arg, "--background-estimator") == 0 && i + 1 < argc) {
      const char *value = argv[++i];
      if (strcmp(value, "trimmed-mean") == 0) {
        opt.params.background_estimator = LNC_BACKGROUND_TRIMMED_MEAN;
      } else if (strcmp(value, "trimmed-median") == 0) {
        opt.params.background_estimator = LNC_BACKGROUND_TRIMMED_MEDIAN;
      } else if (strcmp(value, "sample-median") == 0) {
        opt.params.background_estimator = LNC_BACKGROUND_SAMPLE_MEDIAN;
      } else {
        fprintf(stderr, "Unknown --background-estimator: %s\n", value);
        exit(2);
      }
    } else if (strcmp(arg, "--photometric-model") == 0 && i + 1 < argc) {
      const char *value = argv[++i];
      if (strcmp(value, "local-linear") == 0) {
        opt.params.photometric_model = LNC_PHOTOMETRIC_LOCAL_LINEAR;
      } else if (strcmp(value, "star-scale-additive") == 0) {
        opt.params.photometric_model = LNC_PHOTOMETRIC_STAR_SCALE_ADDITIVE;
      } else {
        fprintf(stderr, "Unknown --photometric-model: %s\n", value);
        exit(2);
      }
    } else if (strcmp(arg, "--global-scale") == 0 && i + 1 < argc) {
      opt.params.global_scale = lnc_parse_double_arg(argv[++i], "--global-scale");
    } else if (strcmp(arg, "--grid-spacing") == 0 && i + 1 < argc) {
      opt.params.grid_spacing = lnc_parse_int_arg(argv[++i], "--grid-spacing");
    } else if (strcmp(arg, "--window-size") == 0 && i + 1 < argc) {
      opt.params.window_size = lnc_parse_int_arg(argv[++i], "--window-size");
    } else if (strcmp(arg, "--min-samples") == 0 && i + 1 < argc) {
      opt.params.min_samples = lnc_parse_int_arg(argv[++i], "--min-samples");
    } else if (strcmp(arg, "--trim-fraction") == 0 && i + 1 < argc) {
      opt.params.trim_fraction = lnc_parse_double_arg(argv[++i], "--trim-fraction");
    } else if (strcmp(arg, "--scale-min") == 0 && i + 1 < argc) {
      opt.params.scale_min = lnc_parse_double_arg(argv[++i], "--scale-min");
    } else if (strcmp(arg, "--scale-max") == 0 && i + 1 < argc) {
      opt.params.scale_max = lnc_parse_double_arg(argv[++i], "--scale-max");
    } else if (strcmp(arg, "--smooth-passes") == 0 && i + 1 < argc) {
      opt.params.smooth_passes = lnc_parse_int_arg(argv[++i], "--smooth-passes");
    } else if (strcmp(arg, "--min-valid-fraction") == 0 && i + 1 < argc) {
      opt.params.min_valid_fraction = lnc_parse_double_arg(argv[++i], "--min-valid-fraction");
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
  if (!have_homography) {
    fprintf(stderr, "--homography is required\n");
    exit(2);
  }
  if (!lnc_invert_homography(opt.params.H, opt.params.Hinv)) {
    fprintf(stderr, "--homography is singular\n");
    exit(2);
  }
  if (opt.params.trim_fraction < 0.0 || opt.params.trim_fraction >= 0.45) {
    fprintf(stderr, "--trim-fraction must be in [0, 0.45)\n");
    exit(2);
  }
  if (opt.params.scale_min <= 0.0 || opt.params.scale_max <= opt.params.scale_min) {
    fprintf(stderr, "Invalid scale clamp range\n");
    exit(2);
  }
  if (opt.params.global_scale <= 0.0 || !isfinite(opt.params.global_scale)) {
    fprintf(stderr, "--global-scale must be positive and finite\n");
    exit(2);
  }
  if (opt.params.min_valid_fraction <= 0.0 || opt.params.min_valid_fraction > 1.0) {
    fprintf(stderr, "--min-valid-fraction must be in (0, 1]\n");
    exit(2);
  }

  opt.ref_path = positionals[0];
  opt.target_path = positionals[1];
  opt.out_path = positionals[2];
  return opt;
}

static void write_diagnostics(const Options *opt, const Image *target, CorrectionMaps maps) {
  if (!opt->diag_dir) return;

  char *scale_path = lnc_join_path(opt->diag_dir, "scale_map.fits");
  char *offset_path = lnc_join_path(opt->diag_dir, "offset_map.fits");
  lnc_write_fits_float(scale_path, target->width, target->height, maps.scale);
  lnc_write_fits_float(offset_path, target->width, target->height, maps.offset);
  free(scale_path);
  free(offset_path);

  if (opt->save_backgrounds) {
    char *ref_bg_path = lnc_join_path(opt->diag_dir, "ref_background.fits");
    char *target_bg_path = lnc_join_path(opt->diag_dir, "target_background.fits");
    lnc_write_fits_float(ref_bg_path, target->width, target->height, maps.ref_bg);
    lnc_write_fits_float(target_bg_path, target->width, target->height, maps.target_bg);
    free(ref_bg_path);
    free(target_bg_path);
  }
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

int main(int argc, char **argv) {
  double start = lnc_now_seconds();
  Options opt = parse_options(argc, argv);
  if (opt.threads > 0) {
    omp_set_num_threads(opt.threads);
  }

  Image ref = lnc_read_fits_float(opt.ref_path);
  Image target = lnc_read_fits_float(opt.target_path);
  long ref_npixels = ref.width * ref.height;
  long target_npixels = target.width * target.height;
  unsigned char *ref_mask = lnc_read_mask_fits(opt.ref_mask_path, ref.width, ref.height);
  unsigned char *target_mask = lnc_read_mask_fits(opt.target_mask_path, target.width, target.height);
  long ref_masked = lnc_count_masked(ref_mask, ref_npixels);
  long target_masked = lnc_count_masked(target_mask, target_npixels);

  ImagePair images = {&ref, &target, ref_mask, target_mask};
  Grid grid = lnc_create_grid(ref.width, ref.height, opt.params.grid_spacing);
  int initial_valid = lnc_estimate_unregistered_grid(&images, &opt.params, &grid);
  int total_nodes = grid.nx * grid.ny;
  double valid_fraction = (double)initial_valid / (double)total_nodes;
  if (valid_fraction < opt.params.min_valid_fraction) {
    fprintf(stderr, "Too few valid grid nodes: %d/%d (%.3f)\n", initial_valid, total_nodes, valid_fraction);
    return 1;
  }
  lnc_fill_missing_grid(&grid);
  lnc_smooth_grid(&grid, opt.params.smooth_passes);

  float *corrected = (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float));
  CorrectionMaps maps = {
      (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)),
      (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)),
      opt.save_backgrounds ? (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)) : NULL,
      opt.save_backgrounds ? (float *)lnc_checked_malloc((size_t)target_npixels * sizeof(float)) : NULL,
  };
  lnc_apply_unregistered_correction(&target, &ref, &grid, &opt.params, corrected, maps);
  LncFitsMetadata metadata = {
      .version = "2-unregistered",
      .mode = "unregistered-pair",
      .output_format = "float32-raw",
      .value_scale = "adu",
      .background_estimator = background_estimator_name(opt.params.background_estimator),
      .photometric_model = opt.params.photometric_model == LNC_PHOTOMETRIC_STAR_SCALE_ADDITIVE
                               ? "star-scale-additive"
                               : "local-linear",
      .reference_path = opt.ref_path,
      .target_path = opt.target_path,
      .report_path = opt.report_path,
      .sequence_index = -1,
      .grid_spacing = opt.params.grid_spacing,
      .window_size = opt.params.window_size,
      .min_samples = opt.params.min_samples,
      .smooth_passes = opt.params.smooth_passes,
      .trim_fraction = opt.params.trim_fraction,
      .scale_min = opt.params.scale_min,
      .scale_max = opt.params.scale_max,
      .global_scale = opt.params.global_scale,
      .min_valid_fraction = opt.params.min_valid_fraction,
      .ref_masked_pixels = ref_masked,
      .target_masked_pixels = target_masked,
  };
  lnc_write_science_fits_float(opt.out_path, opt.target_path, target.width, target.height,
                               corrected, &metadata);
  write_diagnostics(&opt, &target, maps);

  if (opt.report_path) {
    lnc_write_unregistered_report(opt.report_path, &opt.params, &ref, &target, &grid,
                                  initial_valid, ref_masked, target_masked,
                                  lnc_now_seconds() - start, maps.scale, maps.offset);
  }

  lnc_free_image(&ref);
  lnc_free_image(&target);
  free(ref_mask);
  free(target_mask);
  free_outputs(corrected, maps);
  lnc_free_grid(&grid);
  return 0;
}
