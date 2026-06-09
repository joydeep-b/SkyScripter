#include "lnc_common.h"

#include <errno.h>
#include <float.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

double lnc_now_seconds(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

void *lnc_checked_malloc(size_t size) {
  void *ptr = malloc(size);
  if (!ptr) {
    fprintf(stderr, "Out of memory allocating %zu bytes\n", size);
    exit(1);
  }
  return ptr;
}

void *lnc_checked_calloc(size_t count, size_t size) {
  void *ptr = calloc(count, size);
  if (!ptr) {
    fprintf(stderr, "Out of memory allocating %zu bytes\n", count * size);
    exit(1);
  }
  return ptr;
}

int lnc_parse_int_arg(const char *value, const char *name) {
  char *end = NULL;
  errno = 0;
  long parsed = strtol(value, &end, 10);
  if (errno || end == value || *end != '\0' || parsed <= 0 || parsed > INT_MAX) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    exit(2);
  }
  return (int)parsed;
}

double lnc_parse_double_arg(const char *value, const char *name) {
  char *end = NULL;
  errno = 0;
  double parsed = strtod(value, &end);
  if (errno || end == value || *end != '\0' || !isfinite(parsed)) {
    fprintf(stderr, "Invalid %s: %s\n", name, value);
    exit(2);
  }
  return parsed;
}

char *lnc_join_path(const char *dir, const char *name) {
  size_t dlen = strlen(dir);
  int need_slash = dlen > 0 && dir[dlen - 1] != '/';
  char *path = (char *)lnc_checked_malloc(dlen + strlen(name) + (need_slash ? 2 : 1));
  sprintf(path, "%s%s%s", dir, need_slash ? "/" : "", name);
  return path;
}

void lnc_min_max_float(const float *data, size_t n, double *min_out, double *max_out) {
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

int lnc_clamp_int(int value, int lo, int hi) {
  if (value < lo) return lo;
  if (value > hi) return hi;
  return value;
}
