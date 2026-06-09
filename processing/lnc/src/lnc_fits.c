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

static const char *basename_or_empty(const char *path) {
  if (!path) return "";
  const char *slash = strrchr(path, '/');
  return slash ? slash + 1 : path;
}

static void write_limited_string_key(fitsfile *fptr, const char *key, const char *value,
                                     const char *comment, int *status) {
  if (*status || !value || !value[0]) return;
  char limited[69];
  snprintf(limited, sizeof(limited), "%s", value);
  fits_update_key(fptr, TSTRING, (char *)key, limited, (char *)comment, status);
}

static void write_int_key_if_set(fitsfile *fptr, const char *key, int value,
                                 const char *comment, int *status) {
  if (*status || value < 0) return;
  fits_update_key(fptr, TINT, (char *)key, &value, (char *)comment, status);
}

static void write_long_key_if_set(fitsfile *fptr, const char *key, long value,
                                  const char *comment, int *status) {
  if (*status || value < 0) return;
  fits_update_key(fptr, TLONG, (char *)key, &value, (char *)comment, status);
}

static void write_double_key_if_set(fitsfile *fptr, const char *key, double value,
                                    const char *comment, int *status) {
  if (*status || !isfinite(value)) return;
  fits_update_key(fptr, TDOUBLE, (char *)key, &value, (char *)comment, status);
}

static void delete_key_if_present(fitsfile *fptr, const char *key) {
  int status = 0;
  fits_delete_key(fptr, (char *)key, &status);
}

static void add_history_chunks(fitsfile *fptr, const char *prefix, const char *value, int *status) {
  if (*status || !value || !value[0]) return;
  enum { CHUNK = 54 };
  size_t len = strlen(value);
  for (size_t start = 0; start < len; start += CHUNK) {
    char line[FLEN_VALUE];
    snprintf(line, sizeof(line), "%s: %.*s", prefix, (int)CHUNK, value + start);
    fits_write_history(fptr, line, status);
    if (*status) return;
  }
}

static void stamp_lnc_metadata(fitsfile *fptr, const LncFitsMetadata *metadata, int *status) {
  if (!metadata || *status) return;

  write_limited_string_key(fptr, "LNCVRS", metadata->version, "Local normalization correction version", status);
  write_limited_string_key(fptr, "LNCMODE", metadata->mode, "Local normalization correction mode", status);
  write_limited_string_key(fptr, "LNCFMT", metadata->output_format, "LNC final science image encoding", status);
  write_limited_string_key(fptr, "LNCVSCL", metadata->value_scale, "LNC input value scale", status);
  write_limited_string_key(fptr, "LNCBKG", metadata->background_estimator, "LNC background estimator", status);
  write_limited_string_key(fptr, "LNCREF", basename_or_empty(metadata->reference_path), "LNC reference filename", status);
  write_limited_string_key(fptr, "LNCTARG", basename_or_empty(metadata->target_path), "LNC target filename", status);
  write_int_key_if_set(fptr, "LNCSEQ", metadata->sequence_index, "LNC sequence index", status);
  write_int_key_if_set(fptr, "LNCGRID", metadata->grid_spacing, "LNC grid spacing in pixels", status);
  write_int_key_if_set(fptr, "LNCWIN", metadata->window_size, "LNC sampling window size", status);
  write_int_key_if_set(fptr, "LNCSAMP", metadata->min_samples, "LNC minimum samples per node", status);
  write_double_key_if_set(fptr, "LNCTRIM", metadata->trim_fraction, "LNC trim fraction", status);
  write_double_key_if_set(fptr, "LNCSMIN", metadata->scale_min, "LNC minimum scale clamp", status);
  write_double_key_if_set(fptr, "LNCSMAX", metadata->scale_max, "LNC maximum scale clamp", status);
  write_int_key_if_set(fptr, "LNCSMTH", metadata->smooth_passes, "LNC smoothing passes", status);
  write_double_key_if_set(fptr, "LNCMINV", metadata->min_valid_fraction, "LNC minimum valid grid fraction", status);
  write_long_key_if_set(fptr, "LNCRMSK", metadata->ref_masked_pixels, "LNC reference masked pixels", status);
  write_long_key_if_set(fptr, "LNCTMSK", metadata->target_masked_pixels, "LNC target masked pixels", status);

  if (*status) return;
  if (metadata->mode && strcmp(metadata->mode, "reference-passthrough") == 0) {
    fits_write_history(fptr, "LNC: reference frame copied without photometric correction", status);
  } else {
    fits_write_history(fptr, "LNC: corrected = scale(x,y) * target + offset(x,y)", status);
  }
  add_history_chunks(fptr, "LNC ref", metadata->reference_path, status);
  add_history_chunks(fptr, "LNC target", metadata->target_path, status);
  add_history_chunks(fptr, "LNC report", metadata->report_path, status);
}

static void remove_scaling_cards(fitsfile *fptr) {
  delete_key_if_present(fptr, "BZERO");
  delete_key_if_present(fptr, "BSCALE");
  delete_key_if_present(fptr, "BLANK");
  delete_key_if_present(fptr, "DATAMIN");
  delete_key_if_present(fptr, "DATAMAX");
}

void lnc_write_science_fits_float(const char *path, const char *template_path,
                                  long width, long height, const float *data,
                                  const LncFitsMetadata *metadata) {
  fitsfile *template_fptr = NULL;
  fitsfile *out_fptr = NULL;
  int status = 0;
  char *filename = lnc_checked_malloc(strlen(path) + 2);
  sprintf(filename, "!%s", path);

  fits_open_file(&template_fptr, template_path, READONLY, &status);
  die_fits(status, "Could not open FITS template file");
  fits_create_file(&out_fptr, filename, &status);
  die_fits(status, "Could not create science FITS file");
  fits_copy_header(template_fptr, out_fptr, &status);
  die_fits(status, "Could not copy FITS template header");

  long naxes[2] = {width, height};
  fits_resize_img(out_fptr, FLOAT_IMG, 2, naxes, &status);
  die_fits(status, "Could not resize science FITS image");
  remove_scaling_cards(out_fptr);
  stamp_lnc_metadata(out_fptr, metadata, &status);
  die_fits(status, "Could not stamp LNC FITS metadata");

  long fpixel[2] = {1, 1};
  fits_write_pix(out_fptr, TFLOAT, fpixel, width * height, (void *)data, &status);
  die_fits(status, "Could not write science FITS pixels");
  fits_close_file(out_fptr, &status);
  die_fits(status, "Could not close science FITS file");
  fits_close_file(template_fptr, &status);
  die_fits(status, "Could not close FITS template file");
  free(filename);
}

void lnc_stamp_fits_metadata(const char *path, const LncFitsMetadata *metadata) {
  fitsfile *fptr = NULL;
  int status = 0;
  fits_open_file(&fptr, path, READWRITE, &status);
  die_fits(status, "Could not open FITS file for metadata update");
  stamp_lnc_metadata(fptr, metadata, &status);
  die_fits(status, "Could not stamp LNC FITS metadata");
  fits_close_file(fptr, &status);
  die_fits(status, "Could not close metadata-stamped FITS file");
}

void lnc_free_image(Image *img) {
  if (!img) return;
  free(img->data);
  img->data = NULL;
  img->width = 0;
  img->height = 0;
}
