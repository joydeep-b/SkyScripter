#ifndef LNC_FITS_H
#define LNC_FITS_H

typedef struct {
  long width;
  long height;
  float *data;
} Image;

Image lnc_read_fits_float(const char *path);
unsigned char *lnc_read_mask_fits(const char *path, long width, long height);
void lnc_write_fits_float(const char *path, long width, long height, const float *data);
void lnc_free_image(Image *img);

#endif
