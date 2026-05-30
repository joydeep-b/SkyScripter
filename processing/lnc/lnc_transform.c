#include "lnc_transform.h"

#include <float.h>
#include <math.h>

bool lnc_invert_homography(const double H[9], double inv[9]) {
  double a = H[0], b = H[1], c = H[2];
  double d = H[3], e = H[4], f = H[5];
  double g = H[6], h = H[7], i = H[8];
  double det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
  if (!isfinite(det) || fabs(det) <= DBL_EPSILON) return false;
  inv[0] = (e * i - f * h) / det;
  inv[1] = (c * h - b * i) / det;
  inv[2] = (b * f - c * e) / det;
  inv[3] = (f * g - d * i) / det;
  inv[4] = (a * i - c * g) / det;
  inv[5] = (c * d - a * f) / det;
  inv[6] = (d * h - e * g) / det;
  inv[7] = (b * g - a * h) / det;
  inv[8] = (a * e - b * d) / det;
  return true;
}

bool lnc_apply_homography(const double H[9], double x, double y, double *xo, double *yo) {
  double w = H[6] * x + H[7] * y + H[8];
  if (!isfinite(w) || fabs(w) <= DBL_EPSILON) return false;
  *xo = (H[0] * x + H[1] * y + H[2]) / w;
  *yo = (H[3] * x + H[4] * y + H[5]) / w;
  return isfinite(*xo) && isfinite(*yo);
}

bool lnc_in_bounds(double x, double y, long width, long height) {
  return x >= 0.0 && y >= 0.0 && x <= (double)(width - 1) && y <= (double)(height - 1);
}
