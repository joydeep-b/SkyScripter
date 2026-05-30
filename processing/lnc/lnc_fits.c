#include "lnc_fits.h"

#include "lnc_common.h"

#include <fitsio.h>
#include <math.h>
#include <omp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void die_fits(int status, const char *message) {
  if (status) {
    fprintf(stderr, "%s\n", message);
    fits_report_error(stderr, status);
    exit(1);
  }
}

Image lnc_read_fits_float(const char *path) {
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
  float *data = (float *)lnc_checked_malloc((size_t)npixels * sizeof(float));
  long fpixel[2] = {1, 1};
  int anynul = 0;
  fits_read_pix(fptr, TFLOAT, fpixel, npixels, NULL, data, &anynul, &status);
  die_fits(status, "Could not read FITS pixels");
  fits_close_file(fptr, &status);
  die_fits(status, "Could not close FITS file");

  Image img = {naxes[0], naxes[1], data};
  return img;
}

unsigned char *lnc_read_mask_fits(const char *path, long width, long height) {
  if (!path) return NULL;
  Image mask_img = lnc_read_fits_float(path);
  if (mask_img.width != width || mask_img.height != height) {
    fprintf(stderr, "Mask dimensions do not match input image\n");
    exit(1);
  }
  long npixels = width * height;
  unsigned char *mask = (unsigned char *)lnc_checked_calloc((size_t)npixels, sizeof(unsigned char));
  #pragma omp parallel for
  for (long i = 0; i < npixels; ++i) {
    mask[i] = (isfinite(mask_img.data[i]) && mask_img.data[i] != 0.0f) ? 1 : 0;
  }
  lnc_free_image(&mask_img);
  return mask;
}

void lnc_write_fits_float(const char *path, long width, long height, const float *data) {
  fitsfile *fptr = NULL;
  int status = 0;
  char *filename = lnc_checked_malloc(strlen(path) + 2);
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

void lnc_free_image(Image *img) {
  if (!img) return;
  free(img->data);
  img->data = NULL;
  img->width = 0;
  img->height = 0;
}
