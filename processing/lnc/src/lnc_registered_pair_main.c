#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <omp.h>

int lnc_registered_trimmed_mean_main(int argc, char **argv);
int lnc_registered_trimmed_median_main(int argc, char **argv);
int lnc_registered_sample_median_main(int argc, char **argv);

static void usage(FILE *stream, const char *program) {
  fprintf(stream,
          "Usage: %s [--background-estimator NAME] [options] ref.fit target.fit out.fit\n"
          "\n"
          "Background estimators:\n"
          "  trimmed-mean      default registered-pair estimator\n"
          "  trimmed-median    median-background registered-pair estimator\n"
          "  sample-median     experimental patch-sampled median estimator\n"
          "\n"
          "Threading:\n"
          "  --threads N       OpenMP threads for one registered LNC pair\n"
          "\n"
          "Other options are forwarded to the selected estimator implementation.\n",
          program);
}

static int is_estimator(const char *value, const char *expected) {
  return value && strcmp(value, expected) == 0;
}

static int parse_positive_int(const char *value, const char *name) {
  char *end = NULL;
  long parsed = strtol(value, &end, 10);
  if (end == value || *end != '\0' || parsed <= 0 || parsed > 2147483647L) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    return 0;
  }
  return (int)parsed;
}

int main(int argc, char **argv) {
  const char *estimator = "trimmed-mean";
  int threads = 0;
  char **forward_argv = (char **)calloc((size_t)argc + 1, sizeof(char *));
  if (!forward_argv) {
    fprintf(stderr, "Out of memory\n");
    return 1;
  }

  int forward_argc = 0;
  forward_argv[forward_argc++] = argv[0];
  for (int i = 1; i < argc; ++i) {
    const char *arg = argv[i];
    if (strcmp(arg, "--background-estimator") == 0) {
      if (i + 1 >= argc) {
        fprintf(stderr, "--background-estimator requires a value\n");
        usage(stderr, argv[0]);
        free(forward_argv);
        return 2;
      }
      estimator = argv[++i];
    } else if (strncmp(arg, "--background-estimator=", 23) == 0) {
      estimator = arg + 23;
    } else if (strcmp(arg, "--threads") == 0) {
      if (i + 1 >= argc) {
        fprintf(stderr, "--threads requires a value\n");
        usage(stderr, argv[0]);
        free(forward_argv);
        return 2;
      }
      threads = parse_positive_int(argv[++i], "--threads");
      if (threads <= 0) {
        free(forward_argv);
        return 2;
      }
    } else if (strncmp(arg, "--threads=", 10) == 0) {
      threads = parse_positive_int(arg + 10, "--threads");
      if (threads <= 0) {
        free(forward_argv);
        return 2;
      }
    } else if (strcmp(arg, "-h") == 0 || strcmp(arg, "--help") == 0) {
      usage(stdout, argv[0]);
      forward_argv[forward_argc++] = argv[i];
    } else {
      forward_argv[forward_argc++] = argv[i];
    }
  }
  forward_argv[forward_argc] = NULL;

  if (threads > 0) {
    omp_set_num_threads(threads);
  }

  int status;
  if (is_estimator(estimator, "trimmed-mean")) {
    status = lnc_registered_trimmed_mean_main(forward_argc, forward_argv);
  } else if (is_estimator(estimator, "trimmed-median")) {
    status = lnc_registered_trimmed_median_main(forward_argc, forward_argv);
  } else if (is_estimator(estimator, "sample-median")) {
    status = lnc_registered_sample_median_main(forward_argc, forward_argv);
  } else {
    fprintf(stderr, "Unknown --background-estimator: %s\n", estimator);
    usage(stderr, argv[0]);
    status = 2;
  }

  free(forward_argv);
  return status;
}
