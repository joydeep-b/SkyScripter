#include "lnc_grid.h"

#include "lnc_common.h"
#include "lnc_transform.h"

#include <math.h>
#include <omp.h>
#include <stdlib.h>
#include <string.h>

enum {
  FIELD_SCALE = 0,
  FIELD_OFFSET = 1,
  FIELD_REF_BG = 2,
  FIELD_TARGET_BG = 3,
  FIELD_COUNT = 4,
};

static float *field_ptr(Grid *grid, int field) {
  switch (field) {
    case FIELD_SCALE: return grid->scale;
    case FIELD_OFFSET: return grid->offset;
    case FIELD_REF_BG: return grid->ref_bg;
    case FIELD_TARGET_BG: return grid->target_bg;
    default: return NULL;
  }
}

static CorrectionFields fields_from_grid(const Grid *grid, size_t index) {
  CorrectionFields fields = {
      grid->scale[index],
      grid->offset[index],
      grid->ref_bg[index],
      grid->target_bg[index],
  };
  return fields;
}

static void add_fields(CorrectionFields *sum, CorrectionFields value) {
  sum->scale += value.scale;
  sum->offset += value.offset;
  sum->ref_bg += value.ref_bg;
  sum->target_bg += value.target_bg;
}

static CorrectionFields divide_fields(CorrectionFields value, float divisor) {
  CorrectionFields result = {
      value.scale / divisor,
      value.offset / divisor,
      value.ref_bg / divisor,
      value.target_bg / divisor,
  };
  return result;
}

Grid lnc_create_grid(long width, long height, int spacing) {
  Grid g;
  g.nx = (int)((width - 1) / spacing) + 1;
  g.ny = (int)((height - 1) / spacing) + 1;
  size_t n = (size_t)g.nx * (size_t)g.ny;
  g.scale = (float *)lnc_checked_calloc(n, sizeof(float));
  g.offset = (float *)lnc_checked_calloc(n, sizeof(float));
  g.ref_bg = (float *)lnc_checked_calloc(n, sizeof(float));
  g.target_bg = (float *)lnc_checked_calloc(n, sizeof(float));
  g.valid = (unsigned char *)lnc_checked_calloc(n, sizeof(unsigned char));
  return g;
}

void lnc_free_grid(Grid *grid) {
  if (!grid) return;
  free(grid->scale);
  free(grid->offset);
  free(grid->ref_bg);
  free(grid->target_bg);
  free(grid->valid);
  memset(grid, 0, sizeof(*grid));
}

void lnc_grid_set_fields(Grid *grid, size_t index, CorrectionFields fields) {
  grid->scale[index] = fields.scale;
  grid->offset[index] = fields.offset;
  grid->ref_bg[index] = fields.ref_bg;
  grid->target_bg[index] = fields.target_bg;
}

void lnc_fill_missing_grid(Grid *grid) {
  size_t n = (size_t)grid->nx * (size_t)grid->ny;
  float *new_values[FIELD_COUNT] = {
      (float *)lnc_checked_calloc(n, sizeof(float)),
      (float *)lnc_checked_calloc(n, sizeof(float)),
      (float *)lnc_checked_calloc(n, sizeof(float)),
      (float *)lnc_checked_calloc(n, sizeof(float)),
  };
  unsigned char *new_valid = (unsigned char *)lnc_checked_calloc(n, sizeof(unsigned char));

  for (int iter = 0; iter < grid->nx + grid->ny; ++iter) {
    int filled = 0;
    for (int field = 0; field < FIELD_COUNT; ++field) {
      memcpy(new_values[field], field_ptr(grid, field), n * sizeof(float));
    }
    memcpy(new_valid, grid->valid, n * sizeof(unsigned char));

    for (int gy = 0; gy < grid->ny; ++gy) {
      for (int gx = 0; gx < grid->nx; ++gx) {
        size_t gi = (size_t)gy * (size_t)grid->nx + (size_t)gx;
        if (grid->valid[gi]) continue;

        CorrectionFields sum = {0.0f, 0.0f, 0.0f, 0.0f};
        int count = 0;
        for (int dy = -1; dy <= 1; ++dy) {
          int yy = gy + dy;
          if (yy < 0 || yy >= grid->ny) continue;
          for (int dx = -1; dx <= 1; ++dx) {
            int xx = gx + dx;
            if (xx < 0 || xx >= grid->nx || (dx == 0 && dy == 0)) continue;
            size_t ni = (size_t)yy * (size_t)grid->nx + (size_t)xx;
            if (!grid->valid[ni]) continue;
            add_fields(&sum, fields_from_grid(grid, ni));
            count++;
          }
        }

        if (count > 0) {
          CorrectionFields mean = divide_fields(sum, (float)count);
          new_values[FIELD_SCALE][gi] = mean.scale;
          new_values[FIELD_OFFSET][gi] = mean.offset;
          new_values[FIELD_REF_BG][gi] = mean.ref_bg;
          new_values[FIELD_TARGET_BG][gi] = mean.target_bg;
          new_valid[gi] = 1;
          filled++;
        }
      }
    }

    for (int field = 0; field < FIELD_COUNT; ++field) {
      memcpy(field_ptr(grid, field), new_values[field], n * sizeof(float));
    }
    memcpy(grid->valid, new_valid, n * sizeof(unsigned char));
    if (filled == 0) break;
  }

  for (int field = 0; field < FIELD_COUNT; ++field) {
    free(new_values[field]);
  }
  free(new_valid);
}

static void smooth_one(float *values, const unsigned char *valid, int nx, int ny) {
  size_t n = (size_t)nx * (size_t)ny;
  float *tmp = (float *)lnc_checked_calloc(n, sizeof(float));
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

void lnc_smooth_grid(Grid *grid, int passes) {
  for (int i = 0; i < passes; ++i) {
    for (int field = 0; field < FIELD_COUNT; ++field) {
      smooth_one(field_ptr(grid, field), grid->valid, grid->nx, grid->ny);
    }
  }
}

bool lnc_bilinear_grid_value(const float *values, const Grid *grid, int spacing,
                             double x, double y, long width, long height, float *out) {
  if (!lnc_in_bounds(x, y, width, height)) return false;
  double gx_f = x / (double)spacing;
  double gy_f = y / (double)spacing;
  int gx0 = (int)floor(gx_f);
  int gy0 = (int)floor(gy_f);
  if (gx0 < 0) gx0 = 0;
  if (gy0 < 0) gy0 = 0;
  if (gx0 >= grid->nx - 1) gx0 = grid->nx - 1;
  if (gy0 >= grid->ny - 1) gy0 = grid->ny - 1;
  int gx1 = gx0 + 1 < grid->nx ? gx0 + 1 : gx0;
  int gy1 = gy0 + 1 < grid->ny ? gy0 + 1 : gy0;

  long x0_coord = (long)gx0 * spacing;
  long y0_coord = (long)gy0 * spacing;
  long x1_coord = (long)gx1 * spacing;
  long y1_coord = (long)gy1 * spacing;
  if (x1_coord >= width) x1_coord = width - 1;
  if (y1_coord >= height) y1_coord = height - 1;

  double tx = (x1_coord == x0_coord) ? 0.0 : (x - (double)x0_coord) / (double)(x1_coord - x0_coord);
  double ty = (y1_coord == y0_coord) ? 0.0 : (y - (double)y0_coord) / (double)(y1_coord - y0_coord);
  float v00 = values[(size_t)gy0 * (size_t)grid->nx + (size_t)gx0];
  float v10 = values[(size_t)gy0 * (size_t)grid->nx + (size_t)gx1];
  float v01 = values[(size_t)gy1 * (size_t)grid->nx + (size_t)gx0];
  float v11 = values[(size_t)gy1 * (size_t)grid->nx + (size_t)gx1];
  double v0 = (1.0 - tx) * v00 + tx * v10;
  double v1 = (1.0 - tx) * v01 + tx * v11;
  *out = (float)((1.0 - ty) * v0 + ty * v1);
  return true;
}

bool lnc_extrapolated_grid_value(const float *values, const Grid *grid, int spacing,
                                 double x, double y, long width, long height, float *out) {
  if (lnc_bilinear_grid_value(values, grid, spacing, x, y, width, height, out)) return true;

  double gx_f = x / (double)spacing;
  double gy_f = y / (double)spacing;
  int cx = lnc_clamp_int((int)llround(gx_f), 0, grid->nx - 1);
  int cy = lnc_clamp_int((int)llround(gy_f), 0, grid->ny - 1);
  double weighted_sum = 0.0;
  double weight_total = 0.0;

  for (int dy = -2; dy <= 2; ++dy) {
    int yy = cy + dy;
    if (yy < 0 || yy >= grid->ny) continue;
    for (int dx = -2; dx <= 2; ++dx) {
      int xx = cx + dx;
      if (xx < 0 || xx >= grid->nx) continue;
      size_t gi = (size_t)yy * (size_t)grid->nx + (size_t)xx;
      if (!grid->valid[gi] || !isfinite(values[gi])) continue;
      double ddx = gx_f - (double)xx;
      double ddy = gy_f - (double)yy;
      double distance2 = ddx * ddx + ddy * ddy;
      double weight = 1.0 / (distance2 + 0.25);
      weighted_sum += weight * (double)values[gi];
      weight_total += weight;
    }
  }

  if (weight_total <= 0.0) return false;
  *out = (float)(weighted_sum / weight_total);
  return true;
}

bool lnc_lookup_correction_fields(const Grid *grid, int spacing, double x, double y,
                                  long width, long height, CorrectionFields *fields) {
  if (!lnc_extrapolated_grid_value(grid->scale, grid, spacing, x, y, width, height, &fields->scale)) {
    return false;
  }
  if (!lnc_extrapolated_grid_value(grid->offset, grid, spacing, x, y, width, height, &fields->offset)) {
    return false;
  }
  lnc_extrapolated_grid_value(grid->ref_bg, grid, spacing, x, y, width, height, &fields->ref_bg);
  lnc_extrapolated_grid_value(grid->target_bg, grid, spacing, x, y, width, height, &fields->target_bg);
  return true;
}
