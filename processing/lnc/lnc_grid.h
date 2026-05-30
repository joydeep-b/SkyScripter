#ifndef LNC_GRID_H
#define LNC_GRID_H

#include <stdbool.h>
#include <stddef.h>

typedef struct {
  float scale;
  float offset;
  float ref_bg;
  float target_bg;
} CorrectionFields;

typedef struct {
  int nx;
  int ny;
  float *scale;
  float *offset;
  float *ref_bg;
  float *target_bg;
  unsigned char *valid;
} Grid;

Grid lnc_create_grid(long width, long height, int spacing);
void lnc_free_grid(Grid *grid);
void lnc_grid_set_fields(Grid *grid, size_t index, CorrectionFields fields);
void lnc_fill_missing_grid(Grid *grid);
void lnc_smooth_grid(Grid *grid, int passes);
bool lnc_bilinear_grid_value(const float *values, const Grid *grid, int spacing,
                             double x, double y, long width, long height, float *out);
bool lnc_extrapolated_grid_value(const float *values, const Grid *grid, int spacing,
                                 double x, double y, long width, long height, float *out);
bool lnc_lookup_correction_fields(const Grid *grid, int spacing, double x, double y,
                                  long width, long height, CorrectionFields *fields);

#endif
