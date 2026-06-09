#ifndef LNC_FITS_H
#define LNC_FITS_H

typedef struct {
  long width;
  long height;
  float *data;
} Image;

typedef struct {
  const char *version;
  const char *mode;
  const char *output_format;
  const char *value_scale;
  const char *background_estimator;
  const char *reference_path;
  const char *target_path;
  const char *report_path;
  int sequence_index;
  int grid_spacing;
  int window_size;
  int min_samples;
  int smooth_passes;
  double trim_fraction;
  double scale_min;
  double scale_max;
  double min_valid_fraction;
  long ref_masked_pixels;
  long target_masked_pixels;
} LncFitsMetadata;

Image lnc_read_fits_float(const char *path);
unsigned char *lnc_read_mask_fits(const char *path, long width, long height);
void lnc_write_fits_float(const char *path, long width, long height, const float *data);
void lnc_write_science_fits_float(const char *path, const char *template_path,
                                  long width, long height, const float *data,
                                  const LncFitsMetadata *metadata);
void lnc_stamp_fits_metadata(const char *path, const LncFitsMetadata *metadata);
void lnc_free_image(Image *img);

#endif
