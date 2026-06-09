#define _POSIX_C_SOURCE 200809L

#include "lnc_pair_process.h"

#include "lnc_common.h"

#include <ctype.h>
#include <errno.h>
#include <omp.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

typedef struct {
  char *work_sequence_file;
  char *corrected_sequence_file;
  double homography[9];
  int sequence_index;
} TargetEntry;

typedef struct {
  char *work_sequence_file;
  char *corrected_sequence_file;
  int sequence_index;
} ReferenceEntry;

typedef struct {
  ReferenceEntry reference;
  TargetEntry *targets;
  size_t target_count;
  char *output_summary;
  char *background_estimator;
  UnregisteredParams params;
  int has_params;
  char *ref_mask_path;
  char *target_mask_path;
} GroupManifest;

typedef struct {
  const char *manifest_path;
  int lnc_threads;
  int lnc_workers;
} RunOptions;

typedef struct {
  int status;
  LncPairResult result;
} TargetRunResult;

typedef struct {
  const GroupManifest *manifest;
  const LncLoadedReference *reference;
  TargetRunResult *results;
  size_t next_index;
  pthread_mutex_t mutex;
  int lnc_threads;
  int write_diagnostics;
} WorkQueue;

static char *read_file(const char *path, size_t *out_len) {
  FILE *handle = fopen(path, "r");
  if (!handle) return NULL;
  if (fseek(handle, 0, SEEK_END) != 0) {
    fclose(handle);
    return NULL;
  }
  long size = ftell(handle);
  if (size < 0) {
    fclose(handle);
    return NULL;
  }
  rewind(handle);
  char *buffer = (char *)malloc((size_t)size + 1);
  if (!buffer) {
    fclose(handle);
    return NULL;
  }
  size_t read = fread(buffer, 1, (size_t)size, handle);
  fclose(handle);
  buffer[read] = '\0';
  if (out_len) *out_len = read;
  return buffer;
}

static char *json_strdup_string(const char *start) {
  while (*start && *start != '"') start++;
  if (*start != '"') return NULL;
  start++;
  const char *end = start;
  while (*end && *end != '"') {
    if (*end == '\\') end++;
    end++;
  }
  size_t len = (size_t)(end - start);
  char *copy = (char *)malloc(len + 1);
  if (!copy) return NULL;
  memcpy(copy, start, len);
  copy[len] = '\0';
  return copy;
}

static const char *find_key(const char *text, const char *key) {
  char pattern[128];
  snprintf(pattern, sizeof(pattern), "\"%s\"", key);
  return strstr(text, pattern);
}

static UnregisteredParams default_params(void) {
  UnregisteredParams params;
  memset(&params, 0, sizeof(params));
  params.grid_spacing = 128;
  params.window_size = 256;
  params.min_samples = 2000;
  params.background_estimator = LNC_BACKGROUND_TRIMMED_MEDIAN;
  params.trim_fraction = 0.10;
  params.scale_min = 0.5;
  params.scale_max = 2.0;
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

static int parse_json_int(const char *object, const char *key, int *out) {
  const char *found = find_key(object, key);
  if (!found) return 0;
  const char *colon = strchr(found, ':');
  if (!colon) return 0;
  *out = atoi(colon + 1);
  return 1;
}

static int parse_json_double(const char *object, const char *key, double *out) {
  const char *found = find_key(object, key);
  if (!found) return 0;
  const char *colon = strchr(found, ':');
  if (!colon) return 0;
  char *end = NULL;
  double value = strtod(colon + 1, &end);
  if (end == colon + 1) return 0;
  *out = value;
  return 1;
}

static void parse_params_object(const char *text, GroupManifest *manifest) {
  manifest->params = default_params();
  const char *params_key = find_key(text, "params");
  if (!params_key) return;
  const char *params_block = strchr(params_key, '{');
  if (!params_block) return;

  manifest->has_params = 1;
  const char *estimator_key = find_key(params_block, "background_estimator");
  if (estimator_key) {
    manifest->background_estimator = json_strdup_string(strchr(estimator_key, ':'));
    if (strcmp(manifest->background_estimator, "trimmed-mean") == 0) {
      manifest->params.background_estimator = LNC_BACKGROUND_TRIMMED_MEAN;
    } else if (strcmp(manifest->background_estimator, "trimmed-median") == 0) {
      manifest->params.background_estimator = LNC_BACKGROUND_TRIMMED_MEDIAN;
    } else if (strcmp(manifest->background_estimator, "sample-median") == 0) {
      manifest->params.background_estimator = LNC_BACKGROUND_SAMPLE_MEDIAN;
    }
  }
  parse_json_int(params_block, "grid_spacing", &manifest->params.grid_spacing);
  parse_json_int(params_block, "window_size", &manifest->params.window_size);
  parse_json_int(params_block, "min_samples", &manifest->params.min_samples);
  parse_json_double(params_block, "trim_fraction", &manifest->params.trim_fraction);
  parse_json_double(params_block, "scale_min", &manifest->params.scale_min);
  parse_json_double(params_block, "scale_max", &manifest->params.scale_max);
  parse_json_int(params_block, "smooth_passes", &manifest->params.smooth_passes);
  parse_json_double(params_block, "min_valid_fraction", &manifest->params.min_valid_fraction);
}

static int parse_homography_array(const char *text, double out[9]) {
  const char *open = strchr(text, '[');
  if (!open) return 0;
  open++;
  for (int i = 0; i < 9; ++i) {
    char *end = NULL;
    out[i] = strtod(open, &end);
    if (end == open) return 0;
    open = end;
    while (*open && (*open == ',' || isspace((unsigned char)*open))) open++;
  }
  return 1;
}

static int parse_target_object(const char *object, TargetEntry *entry) {
  const char *work_key = find_key(object, "work_sequence_file");
  const char *out_key = find_key(object, "corrected_sequence_file");
  const char *index_key = find_key(object, "sequence_index");
  const char *h_key = find_key(object, "target_to_reference_homography");
  if (!work_key || !out_key || !h_key) return 0;
  entry->work_sequence_file = json_strdup_string(strchr(work_key, ':'));
  entry->corrected_sequence_file = json_strdup_string(strchr(out_key, ':'));
  if (index_key) {
    const char *digits = strchr(index_key, ':');
    if (digits) entry->sequence_index = atoi(digits + 1);
  }
  const char *array = strchr(h_key, '[');
  return parse_homography_array(array, entry->homography) && entry->work_sequence_file && entry->corrected_sequence_file;
}

static void free_manifest(GroupManifest *manifest) {
  free(manifest->reference.work_sequence_file);
  free(manifest->reference.corrected_sequence_file);
  free(manifest->output_summary);
  free(manifest->background_estimator);
  free(manifest->ref_mask_path);
  free(manifest->target_mask_path);
  for (size_t i = 0; i < manifest->target_count; ++i) {
    free(manifest->targets[i].work_sequence_file);
    free(manifest->targets[i].corrected_sequence_file);
  }
  free(manifest->targets);
  memset(manifest, 0, sizeof(*manifest));
}

static int parse_manifest(const char *text, GroupManifest *manifest) {
  memset(manifest, 0, sizeof(*manifest));
  parse_params_object(text, manifest);
  const char *ref_block = find_key(text, "reference");
  if (!ref_block) return 0;
  ref_block = strchr(ref_block, '{');
  if (!ref_block) return 0;
  const char *ref_work = find_key(ref_block, "work_sequence_file");
  const char *ref_out = find_key(ref_block, "corrected_sequence_file");
  const char *ref_index = find_key(ref_block, "sequence_index");
  if (!ref_work || !ref_out) return 0;
  manifest->reference.work_sequence_file = json_strdup_string(strchr(ref_work, ':'));
  manifest->reference.corrected_sequence_file = json_strdup_string(strchr(ref_out, ':'));
  if (ref_index) {
    const char *digits = strchr(ref_index, ':');
    if (digits) manifest->reference.sequence_index = atoi(digits + 1);
  }

  const char *summary_key = find_key(text, "output_summary");
  if (summary_key) {
    manifest->output_summary = json_strdup_string(strchr(summary_key, ':'));
  }

  const char *targets_key = find_key(text, "targets");
  if (!targets_key) return 0;
  const char *cursor = strchr(targets_key, '[');
  if (!cursor) return 0;
  cursor++;
  size_t capacity = 4;
  manifest->targets = (TargetEntry *)calloc(capacity, sizeof(TargetEntry));
  if (!manifest->targets) return 0;

  while ((cursor = strchr(cursor, '{'))) {
    TargetEntry entry;
    memset(&entry, 0, sizeof(entry));
    if (!parse_target_object(cursor, &entry)) {
      cursor++;
      continue;
    }
    if (manifest->target_count >= capacity) {
      capacity *= 2;
      TargetEntry *resized = (TargetEntry *)realloc(manifest->targets, capacity * sizeof(TargetEntry));
      if (!resized) {
        free(entry.work_sequence_file);
        free(entry.corrected_sequence_file);
        return 0;
      }
      manifest->targets = resized;
    }
    manifest->targets[manifest->target_count++] = entry;
    cursor = strchr(cursor, '}');
    if (!cursor) break;
    cursor++;
    if (!strchr(cursor, '{')) break;
  }

  return manifest->reference.work_sequence_file && manifest->reference.corrected_sequence_file;
}

static int ensure_directory_path(const char *path) {
  char buffer[4096];
  snprintf(buffer, sizeof(buffer), "%s", path);
  size_t len = strlen(buffer);
  if (len == 0) return 0;
  if (buffer[len - 1] == '/') buffer[len - 1] = '\0';
  for (char *p = buffer + 1; *p; ++p) {
    if (*p == '/') {
      *p = '\0';
      if (mkdir(buffer, 0775) != 0 && errno != EEXIST) return 1;
      *p = '/';
    }
  }
  if (mkdir(buffer, 0775) != 0 && errno != EEXIST) return 1;
  return 0;
}

static int ensure_parent_directory(const char *path) {
  char buffer[4096];
  snprintf(buffer, sizeof(buffer), "%s", path);
  char *slash = strrchr(buffer, '/');
  if (!slash) return 0;
  *slash = '\0';
  if (buffer[0] == '\0') return 0;
  return ensure_directory_path(buffer);
}

static int copy_reference_file(const GroupManifest *manifest) {
  if (ensure_parent_directory(manifest->reference.corrected_sequence_file) != 0) return 1;
  FILE *in = fopen(manifest->reference.work_sequence_file, "rb");
  if (!in) return 1;
  FILE *out = fopen(manifest->reference.corrected_sequence_file, "wb");
  if (!out) {
    fclose(in);
    return 1;
  }
  char buffer[65536];
  size_t read;
  while ((read = fread(buffer, 1, sizeof(buffer), in)) > 0) {
    if (fwrite(buffer, 1, read, out) != read) {
      fclose(in);
      fclose(out);
      return 1;
    }
  }
  fclose(in);
  fclose(out);
  return 0;
}

static int parse_positive_int_arg(const char *value, const char *name) {
  char *end = NULL;
  errno = 0;
  long parsed = strtol(value, &end, 10);
  if (errno || end == value || *end != '\0' || parsed <= 0 || parsed > 2147483647L) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    exit(2);
  }
  return (int)parsed;
}

static int detected_cpu_count(void) {
  long value = sysconf(_SC_NPROCESSORS_ONLN);
  return value > 0 && value < 2147483647L ? (int)value : 1;
}

static int default_lnc_threads(void) {
  const char *env = getenv("OMP_NUM_THREADS");
  if (env && env[0]) {
    char *end = NULL;
    long parsed = strtol(env, &end, 10);
    if (end != env && parsed > 0 && parsed <= 2147483647L) return (int)parsed;
  }
  return 8;
}

static void usage(FILE *stream, const char *program) {
  fprintf(stream,
          "Usage: %s [--lnc-threads N] [--lnc-workers N] <manifest.json>\n"
          "\n"
          "Options:\n"
          "  --lnc-threads N    OpenMP threads per LNC target (default: OMP_NUM_THREADS or 8)\n"
          "  --lnc-workers N    LNC targets to process concurrently (default: CPUs / lnc-threads)\n"
          "  -h, --help         show this help\n",
          program);
}

static RunOptions parse_options(int argc, char **argv) {
  RunOptions opt = {0};
  opt.lnc_threads = default_lnc_threads();

  for (int i = 1; i < argc; ++i) {
    const char *arg = argv[i];
    if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
      usage(stdout, argv[0]);
      exit(0);
    } else if (strcmp(arg, "--lnc-threads") == 0 && i + 1 < argc) {
      opt.lnc_threads = parse_positive_int_arg(argv[++i], "--lnc-threads");
    } else if (strncmp(arg, "--lnc-threads=", 14) == 0) {
      opt.lnc_threads = parse_positive_int_arg(arg + 14, "--lnc-threads");
    } else if (strcmp(arg, "--lnc-workers") == 0 && i + 1 < argc) {
      opt.lnc_workers = parse_positive_int_arg(argv[++i], "--lnc-workers");
    } else if (strncmp(arg, "--lnc-workers=", 14) == 0) {
      opt.lnc_workers = parse_positive_int_arg(arg + 14, "--lnc-workers");
    } else if (arg[0] == '-') {
      fprintf(stderr, "Unknown or incomplete option: %s\n", arg);
      usage(stderr, argv[0]);
      exit(2);
    } else {
      if (opt.manifest_path) {
        fprintf(stderr, "Too many positional arguments\n");
        usage(stderr, argv[0]);
        exit(2);
      }
      opt.manifest_path = arg;
    }
  }

  if (!opt.manifest_path) {
    usage(stderr, argv[0]);
    exit(2);
  }
  if (opt.lnc_workers <= 0) {
    opt.lnc_workers = detected_cpu_count() / opt.lnc_threads;
    if (opt.lnc_workers < 1) opt.lnc_workers = 1;
  }
  return opt;
}

static int run_target(const GroupManifest *manifest, const LncLoadedReference *reference,
                      TargetEntry *target, int lnc_threads, int write_diagnostics,
                      TargetRunResult *target_result) {
  int status = 1;
  memset(target_result, 0, sizeof(*target_result));
  if (ensure_parent_directory(target->corrected_sequence_file) != 0) {
    target_result->status = 1;
    return 1;
  }

  char diag_dir[4096];
  char *report_path = NULL;
  const char *diag_dir_ptr = NULL;
  if (write_diagnostics) {
    snprintf(diag_dir, sizeof(diag_dir), "%s", target->corrected_sequence_file);
    char *dot = strrchr(diag_dir, '.');
    if (dot) {
      *dot = '\0';
    }
    strcat(diag_dir, "_lnc_diag");
    if (ensure_directory_path(diag_dir) != 0) {
      target_result->status = 1;
      return 1;
    }
    report_path = lnc_join_path(diag_dir, "lnc_report.json");
    diag_dir_ptr = diag_dir;
  }

  LncPairRequest request = {
      .ref_path = manifest->reference.work_sequence_file,
      .target_path = target->work_sequence_file,
      .out_path = target->corrected_sequence_file,
      .ref_mask_path = manifest->ref_mask_path,
      .target_mask_path = manifest->target_mask_path,
      .diag_dir = diag_dir_ptr,
      .report_path = report_path,
      .mode = "group-target",
      .sequence_index = target->sequence_index,
      .has_params = manifest->has_params,
      .params = manifest->params,
  };
  memcpy(request.homography, target->homography, sizeof(request.homography));

  omp_set_num_threads(lnc_threads);
  status = lnc_normalize_unregistered_target(reference, &request, &target_result->result);
  target_result->status = status;
  free(report_path);
  return status;
}

static void *worker_main(void *arg) {
  WorkQueue *queue = (WorkQueue *)arg;
  while (1) {
    pthread_mutex_lock(&queue->mutex);
    size_t index = queue->next_index++;
    pthread_mutex_unlock(&queue->mutex);

    if (index >= queue->manifest->target_count) break;
    run_target(queue->manifest, queue->reference, &queue->manifest->targets[index],
               queue->lnc_threads, queue->write_diagnostics, &queue->results[index]);
  }
  return NULL;
}

int main(int argc, char **argv) {
  RunOptions options = parse_options(argc, argv);

  size_t len = 0;
  char *text = read_file(options.manifest_path, &len);
  if (!text) {
    fprintf(stderr, "Could not read manifest: %s\n", options.manifest_path);
    return 2;
  }

  GroupManifest manifest;
  if (!parse_manifest(text, &manifest)) {
    fprintf(stderr, "Could not parse manifest: %s\n", options.manifest_path);
    free(text);
    return 2;
  }
  free(text);

  if (copy_reference_file(&manifest) != 0) {
    fprintf(stderr, "Failed to copy reference frame\n");
    free_manifest(&manifest);
    return 1;
  }
  LncFitsMetadata reference_metadata = {
      .version = "2-unregistered",
      .mode = "reference-passthrough",
      .output_format = "reference-original",
      .value_scale = "unchanged",
      .background_estimator = manifest.background_estimator ? manifest.background_estimator : "trimmed-median",
      .reference_path = manifest.reference.work_sequence_file,
      .target_path = manifest.reference.work_sequence_file,
      .report_path = manifest.output_summary,
      .sequence_index = manifest.reference.sequence_index,
      .grid_spacing = manifest.params.grid_spacing,
      .window_size = manifest.params.window_size,
      .min_samples = manifest.params.min_samples,
      .smooth_passes = manifest.params.smooth_passes,
      .trim_fraction = manifest.params.trim_fraction,
      .scale_min = manifest.params.scale_min,
      .scale_max = manifest.params.scale_max,
      .min_valid_fraction = manifest.params.min_valid_fraction,
      .ref_masked_pixels = 0,
      .target_masked_pixels = 0,
  };
  lnc_stamp_fits_metadata(manifest.reference.corrected_sequence_file, &reference_metadata);

  LncLoadedReference reference;
  if (lnc_load_reference(manifest.reference.work_sequence_file, manifest.ref_mask_path, &reference) != 0) {
    fprintf(stderr, "Failed to load reference frame\n");
    free_manifest(&manifest);
    return 1;
  }

  /* Diagnostics (per-target scale_map/offset_map FITS + report) are disabled by
   * default because they triple per-target write I/O. Enable by setting
   * LNC_WRITE_DIAGNOSTICS=1 in the environment. */
  const char *diag_env = getenv("LNC_WRITE_DIAGNOSTICS");
  int write_diagnostics = diag_env && (diag_env[0] == '1' || diag_env[0] == 't' ||
                                       diag_env[0] == 'T' || diag_env[0] == 'y' ||
                                       diag_env[0] == 'Y');

  TargetRunResult *results = (TargetRunResult *)calloc(manifest.target_count ? manifest.target_count : 1,
                                                       sizeof(TargetRunResult));
  if (!results) {
    fprintf(stderr, "Out of memory allocating target results\n");
    lnc_free_loaded_reference(&reference);
    free_manifest(&manifest);
    return 1;
  }
  for (size_t i = 0; i < manifest.target_count; ++i) {
    results[i].status = 1;
  }

  int worker_count = options.lnc_workers;
  if ((size_t)worker_count > manifest.target_count) worker_count = (int)manifest.target_count;
  if (worker_count < 1 && manifest.target_count > 0) worker_count = 1;

  pthread_t *threads = NULL;
  WorkQueue queue = {
      .manifest = &manifest,
      .reference = &reference,
      .results = results,
      .next_index = 0,
      .lnc_threads = options.lnc_threads,
      .write_diagnostics = write_diagnostics,
  };
  pthread_mutex_init(&queue.mutex, NULL);

  if (worker_count > 0) {
    threads = (pthread_t *)calloc((size_t)worker_count, sizeof(pthread_t));
    if (!threads) {
      fprintf(stderr, "Out of memory allocating workers\n");
      pthread_mutex_destroy(&queue.mutex);
      free(results);
      lnc_free_loaded_reference(&reference);
      free_manifest(&manifest);
      return 1;
    }
    for (int i = 0; i < worker_count; ++i) {
      if (pthread_create(&threads[i], NULL, worker_main, &queue) != 0) {
        fprintf(stderr, "Failed to create LNC worker\n");
        worker_count = i;
        break;
      }
    }
    for (int i = 0; i < worker_count; ++i) {
      pthread_join(threads[i], NULL);
    }
  }
  pthread_mutex_destroy(&queue.mutex);
  free(threads);

  int failures = 0;
  for (size_t i = 0; i < manifest.target_count; ++i) {
    if (results[i].status != 0) failures++;
  }

  if (manifest.output_summary) {
    ensure_parent_directory(manifest.output_summary);
    FILE *summary = fopen(manifest.output_summary, "w");
    if (summary) {
      fprintf(summary,
              "{\n"
              "  \"target_count\": %zu,\n"
              "  \"reference_loaded_once\": true,\n"
              "  \"lnc_threads\": %d,\n"
              "  \"lnc_workers\": %d,\n"
              "  \"targets\": [\n",
              manifest.target_count, options.lnc_threads, worker_count);
      for (size_t i = 0; i < manifest.target_count; ++i) {
        TargetEntry *target = &manifest.targets[i];
        LncPairResult result = results[i].result;
        fprintf(summary,
                "    {"
                "\"sequence_index\": %d, "
                "\"work_sequence_file\": \"%s\", "
                "\"corrected_sequence_file\": \"%s\", "
                "\"status\": \"%s\", "
                "\"initial_valid_nodes\": %d, "
                "\"total_nodes\": %d, "
                "\"valid_fraction\": %.9g, "
                "\"elapsed_seconds\": %.9g"
                "}%s\n",
                target->sequence_index,
                target->work_sequence_file ? target->work_sequence_file : "",
                target->corrected_sequence_file ? target->corrected_sequence_file : "",
                results[i].status == 0 ? "success" : "failed",
                result.initial_valid_nodes,
                result.total_nodes,
                result.valid_fraction,
                result.elapsed_seconds,
                (i + 1 < manifest.target_count) ? "," : "");
      }
      fprintf(summary,
              "  ],\n"
              "  \"failures\": %d\n"
              "}\n",
              failures);
      fclose(summary);
    }
  }

  free(results);
  lnc_free_loaded_reference(&reference);
  free_manifest(&manifest);
  return failures > 0 ? 1 : 0;
}
