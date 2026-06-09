#ifndef LNC_PAIR_PROCESS_H
#define LNC_PAIR_PROCESS_H

#include "lnc_unregistered_core.h"

typedef struct {
  const char *ref_path;
  const char *target_path;
  const char *out_path;
  const char *ref_mask_path;
  const char *target_mask_path;
  const char *diag_dir;
  const char *report_path;
  const char *mode;
  int sequence_index;
  int has_params;
  UnregisteredParams params;
  double homography[9];
} LncPairRequest;

typedef struct {
  int initial_valid_nodes;
  int total_nodes;
  double valid_fraction;
  double elapsed_seconds;
} LncPairResult;

typedef struct {
  Image image;
  unsigned char *mask;
  long masked_pixels;
} LncLoadedReference;

int lnc_load_reference(const char *ref_path, const char *ref_mask_path, LncLoadedReference *reference);
void lnc_free_loaded_reference(LncLoadedReference *reference);
int lnc_normalize_unregistered_target(const LncLoadedReference *reference,
                                      const LncPairRequest *request,
                                      LncPairResult *result);

#endif
