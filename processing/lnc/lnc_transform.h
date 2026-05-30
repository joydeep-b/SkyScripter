#ifndef LNC_TRANSFORM_H
#define LNC_TRANSFORM_H

#include <stdbool.h>

bool lnc_invert_homography(const double H[9], double inv[9]);
bool lnc_apply_homography(const double H[9], double x, double y, double *xo, double *yo);
bool lnc_in_bounds(double x, double y, long width, long height);

#endif
