#ifndef LNC_COMMON_H
#define LNC_COMMON_H

#include <stddef.h>
#include <stdio.h>

double lnc_now_seconds(void);
void *lnc_checked_malloc(size_t size);
void *lnc_checked_calloc(size_t count, size_t size);
int lnc_parse_int_arg(const char *value, const char *name);
double lnc_parse_double_arg(const char *value, const char *name);
char *lnc_join_path(const char *dir, const char *name);
void lnc_min_max_float(const float *data, size_t n, double *min_out, double *max_out);
int lnc_clamp_int(int value, int lo, int hi);

#endif
