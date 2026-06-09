# Unregistered Local Normalization Algorithm

This document describes the unregistered local normalization correction (LNC)
implemented by:

- `processing/lnc/scripts/lnc_unregistered_pair.py`
- `processing/lnc/src/lnc_unregistered_pair_main.c`
- `processing/lnc/scripts/lnc_group_sequence.py`
- `processing/lnc/src/lnc_group_subs_main.c`

The goal is to photometrically correct an unregistered target image while
preserving the target image geometry.

## How to Run

Use the unregistered wrapper when the reference and target are not already
geometrically registered:

```bash
python processing/lnc/scripts/lnc_unregistered_pair.py \
  "path/to/reference.fit" \
  "path/to/target_to_correct.fit" \
  "path/to/corrected_output.fit" \
  --diag-dir "path/to/lnc_diag" \
  --save-intermediate-fits \
  --output-format float32
```

For the Markarians test pair:

```bash
python processing/lnc/scripts/lnc_unregistered_pair.py \
  "LNC_Test_Data/markarians/ref.fit" \
  "LNC_Test_Data/markarians/bad1.fit" \
  "LNC_Test_Data/markarians/bad1_lnc.fit" \
  --diag-dir "LNC_Test_Data/markarians/bad1_lnc_diag" \
  --save-intermediate-fits \
  --output-format float32
```

The required positional arguments are:

- `reference`: the photometric reference image.
- `target`: the unregistered target image to correct.
- `output`: the corrected target image, still in original target geometry.

Common options:

- `--diag-dir DIR`: writes diagnostic products and JSON reports into `DIR`.
- `--save-intermediate-fits`: writes masked reference/target images plus
  background diagnostic maps.
- `--output-format float32`: writes final science output as FP32 clipped or
  normalized to `[0, 1]`.
- `--output-format uint16`: writes final science output as unsigned 16-bit data.
- `--registration-transform homography`: uses Siril two-pass homography
  registration metadata. This is the default.
- `--rebuild`: rebuilds the C normalizer before running.

The diagnostic directory normally contains:

- `reference_mask.fits` and `target_mask.fits`: native-coordinate exclusion
  masks.
- `reference_masked.fits` and `target_masked.fits`: optional masked image
  views, written when `--save-intermediate-fits` is set.
- `scale_map.fits`: display-normalized target-geometry scale field.
- `offset_map.fits`: display-normalized target-geometry offset field.
- `ref_background.fits` and `target_background.fits`: optional background
  diagnostics, written when `--save-intermediate-fits` is set.
- `transform_report.json`: parsed homography and star-match validation.
- `local_normalize_unregistered_report.json`: C-core summary.
- `wrapper_report.json`: full wrapper command, parameters, timings, and
  diagnostics.

All user-facing FP32 FITS outputs are written in `[0, 1]`. Signed or unitless
diagnostics such as `offset_map.fits` and `scale_map.fits` are display-normalized
with the original finite range recorded in `LNCVMIN` and `LNCVMAX`.

Use the original registered-image wrapper only when the reference and target are
already registered and share the same geometry:

```bash
python processing/lnc/scripts/lnc_registered_pair.py \
  "path/to/registered_reference.fit" \
  "path/to/registered_target.fit" \
  "path/to/corrected_output.fit" \
  --diag-dir "path/to/lnc_diag" \
  --save-intermediate-fits \
  --background-estimator trimmed-median \
  --output-format float32
```

Use the group wrapper when the inputs are already organized as a Siril sequence
and every included frame should be normalized to one reference frame:

```bash
python processing/lnc/scripts/lnc_group_sequence.py \
  "path/to/sequence_dir" \
  "sequence_name" \
  --output-dir "path/to/lnc_sequence_out" \
  --diagnostics \
  --background-estimator trimmed-median
```

The required positional arguments are:

- `sequence_dir`: directory containing the Siril `.seq` file and FITS frames.
- `sequence_name`: Siril sequence name, without the `.seq` suffix.

Common group options:

- `--output-dir DIR`: writes corrected sequence frames and group reports into
  `DIR`. If omitted, outputs are written next to the input sequence.
- `--diagnostics`: enables per-target diagnostic directories. Diagnostics are
  disabled by default to avoid tripling the write I/O for large sequences.
- `--lnc-threads N`: OpenMP threads used inside each target normalization.
- `--lnc-workers N`: number of targets normalized concurrently.
- `--background-estimator NAME`: uses `trimmed-mean`, `trimmed-median`, or
  `sample-median` for local background estimation.
- `--rebuild`: rebuilds the C group normalizer before running.

The group output directory normally contains:

- `lnc_<source_stem>.fits`: corrected FITS file for each included sequence
  frame. Corrected targets preserve the target frame's science header and add
  LNC provenance cards. The reference frame is copied with identical pixels and
  original science metadata, then stamped with `LNCMODE=reference-passthrough`.
- `lnc_group_manifest.json`: manifest consumed by the C group normalizer.
- `lnc_group_summary.json`: per-target C-core status, valid-node counts, and
  elapsed time.
- `lnc_group_sequence_report.json`: wrapper-level sequence, reference,
  skipped-frame, concurrency, and timing summary.
- `lnc_<source_stem>_lnc_diag/`: optional per-target diagnostic directory
  written when `--diagnostics` is set. It contains `scale_map.fits`,
  `offset_map.fits`, and `lnc_report.json`.

Group science outputs are written directly by the C core as 32-bit FITS images
in each target's original geometry. The group wrapper does not expose the pair
wrapper's `--output-format` conversion path. Before returning, the wrapper
checks that every output still has exposure metadata (`LIVETIME`, `EXPTIME`, or
`EXPOSURE`) and `LNCMODE`; this prevents header-stripped normalized subs from
being cached or stacked.

## Symbols

Let:

- `R`: reference image.
- `T`: target image to correct.
- `C`: corrected target image.
- `W_R, H_R`: reference width and height.
- `W_T, H_T`: target width and height.
- `x, y`: FITS/NumPy array coordinates, with origin at the top-left pixel,
  `x` increasing rightward and `y` increasing downward.
- `p_T = (x_T, y_T, 1)^T`: homogeneous target coordinate.
- `p_R = (x_R, y_R, 1)^T`: homogeneous reference coordinate.
- `M`: homography mapping target array coordinates to reference array
  coordinates.
- `M^{-1}`: homography mapping reference array coordinates to target array
  coordinates.
- `S(p_R)`: local multiplicative scale field in reference coordinates.
- `O(p_R)`: local additive offset field in reference coordinates.

The correction model is:

```text
R(p_R) ~= S(p_R) * T(M^{-1} p_R) + O(p_R)
```

The saved science image remains in target geometry:

```text
C(p_T) = S(M p_T) * T(p_T) + O(M p_T)
```

## Coordinate Systems

Siril star-list coordinates use a different vertical convention than FITS array
coordinates. A Siril point `(x_s, y_s)` is converted to array coordinates by:

```text
x = x_s
y = (H - 1) - y_s
```

For a Siril-space homography `M_s` from target to reference, the array-space
homography is:

```text
F_H = [[1,  0,      0],
       [0, -1,  H - 1],
       [0,  0,      1]]

M = F_{H_R} * M_s * F_{H_T}
```

where `F_{H_R}` flips reference `y`, and `F_{H_T}` flips target `y`.

Homogeneous application is:

```text
q = M p
x' = q_x / q_w
y' = q_y / q_w
```

Coordinates with non-finite or near-zero `q_w` are invalid.

## Siril Registration

The Python wrapper creates a deterministic two-image Siril sequence:

```text
lnc_pair_00001.fit = reference
lnc_pair_00002.fit = target
```

It runs:

```text
setref lnc_pair 1
register lnc_pair -2pass -transf=homography
```

Siril writes registration matrices into the generated `.seq` file. The wrapper
parses the `R... H h00 h01 ... h22` entries.

Because Siril may store matrices relative to an internal registration reference,
the wrapper tests candidate target-to-reference matrix conventions by applying
them to detected target stars and measuring nearest-neighbor residuals against
the full detected reference star catalog:

```text
d_i(M) = min_j || M t_i - r_j ||_2
score(M) = median_i d_i(M)
```

The candidate matrix with the smaller `score(M)` is used.

For group LNC, the Python wrapper operates on the existing Siril sequence
instead of creating one two-image sequence per target. It parses:

- the sequence metadata row, including `start_index`, fixed filename width, and
  optional Siril reference index;
- each image row's included/excluded flag;
- each registration row's homography matrix.

The group reference is chosen as:

```text
reference_index =
  sequence reference index, if it exists and is included
  otherwise the first included sequence index
```

The wrapper then registers the whole sequence once:

```text
setref <sequence_name> <reference_index>
register <sequence_name> -2pass
```

After registration, it re-parses the `.seq` file and derives one
target-to-reference homography for each included non-reference frame. If
`H^s_i` is the Siril-space matrix stored for frame `i`, and `r` is the chosen
reference frame, then:

```text
M^s_i = (H^s_r)^{-1} H^s_i
M_i = F_{H_R} M^s_i F_{H_T}
```

where `M_i` maps target `i` array coordinates into reference array coordinates.
This is the same target-to-reference convention used by the pair-wise C core.

## Group Sequence LNC

Group LNC is a batch wrapper around the same unregistered pair normalization
model described below. It differs from pair-wise LNC in orchestration:

1. Parse the Siril sequence and choose one included frame as the photometric and
   geometric reference.
2. Run sequence registration once with that reference.
3. Build a JSON manifest containing the reference frame, every included
   non-reference target, each target's output path, each target-to-reference
   homography, and the LNC parameter block.
4. Run `lnc_group_subs`, which loads the reference image once and normalizes all
   targets listed in the manifest.
5. Copy the reference frame unchanged to the output sequence and write a group
   summary.

For target `i`, the correction model is still:

```text
R(p_R) ~= S_i(p_R) * T_i(M_i^{-1} p_R) + O_i(p_R)
C_i(p_T) = S_i(M_i p_T) * T_i(p_T) + O_i(M_i p_T)
```

Each target gets its own grid, scale field, offset field, validity checks,
missing-node fill, and smoothing pass. The fields are not shared across targets;
only the loaded reference image is shared.

The current group wrapper does not generate the pair wrapper's per-frame
star/saturation masks. For group runs, the C core receives empty native masks,
so sample rejection comes from finite-value checks, overlap checks, and the
trimmed local fit.

## Group Manifest

The group wrapper writes `lnc_group_manifest.json` for the C group normalizer.
Its important fields are:

```text
sequence_name
params
reference.work_sequence_file
reference.corrected_sequence_file
reference.sequence_index
targets[].work_sequence_file
targets[].corrected_sequence_file
targets[].sequence_index
targets[].target_to_reference_homography
output_summary
```

The reference entry always uses the identity homography in the manifest because
it is not normalized against itself; it is copied to the output path. Each target
entry supplies the target-to-reference homography used by the unregistered pair
core.

## Group Parallelism

`lnc_group_subs` has two concurrency levels:

```text
lnc_workers = number of targets processed at the same time
lnc_threads = OpenMP threads used inside each target's grid estimation/application
```

If `--lnc-workers` is not provided, the wrapper chooses:

```text
lnc_workers = max(1, detected_cpu_count / lnc_threads)
```

Each worker takes the next manifest target from a shared queue. A target failure
is recorded in `lnc_group_summary.json`; the process exits non-zero if any
target fails.

## Masks

Reference and target masks remain in native image coordinates:

- `K_R(x, y) = 1` means the reference pixel is excluded.
- `K_T(x, y) = 1` means the target pixel is excluded.

Star masks are circles centered on detected stars after Siril-to-array `y`
conversion:

```text
(x - x_star)^2 + (y - y_star)^2 <= radius^2
```

where:

```text
radius = clamp(radius_factor * FWHM, radius_min, radius_max)
```

Saturation masks are added in each native image. Masks are not warped to disk.
During paired sampling, a pair is rejected if either native mask rejects its
corresponding sample.

## Normalization Grid

The local correction fields are estimated on a grid in reference coordinates.

For grid spacing `g`, grid node `(i, j)` is centered at:

```text
c_ij = (min(i g, W_R - 1), min(j g, H_R - 1))
```

with:

```text
N_x = floor((W_R - 1) / g) + 1
N_y = floor((H_R - 1) / g) + 1
```

Each node estimates:

```text
S_ij, O_ij, B^R_ij, B^T_ij
```

where `B^R_ij` and `B^T_ij` are local median backgrounds for diagnostics and
fallbacks.

## Paired Sampling

For each grid node center `c = (c_x, c_y)`, the C core samples a circular
footprint in reference coordinates:

```text
rho = window_size / 2
A(c) = { r = (x, y): ||r - c||_2^2 <= rho^2 }
```

For every integer reference sample `r in A(c)`:

1. Reject if `r` is outside the reference image.
2. Reject if `K_R(r) = 1`.
3. Compute corresponding target coordinate:

   ```text
   t = M^{-1} r
   ```

4. Reject if `t` is outside the target image.
5. Reject if nearest-pixel `K_T(t) = 1`.
6. Read `R(r)` directly.
7. Read `T(t)` by bilinear interpolation.
8. Reject if either value is non-finite.

This produces paired samples:

```text
P_c = {(t_k, r_k)}_{k=1..n}
```

where:

```text
t_k = T(M^{-1} r_k)
r_k = R(r_k)
```

The node is invalid if:

```text
n < min_samples
```

## Trimmed Local Fit

Samples are sorted by target value `t_k`. With trim fraction `f`:

```text
m = floor(f n)
P'_c = sorted(P_c by t)[m : n - m]
```

Let `K = |P'_c|`. The node is invalid if:

```text
K < max(16, min_samples / 2)
```

For the retained samples:

```text
mean_T = (1/K) sum_k t_k
mean_R = (1/K) sum_k r_k
```

The least-squares scale and offset minimize:

```text
sum_k (r_k - (s t_k + o))^2
```

The closed form is:

```text
D = K sum_k t_k^2 - (sum_k t_k)^2

s = (K sum_k t_k r_k - (sum_k t_k)(sum_k r_k)) / D
o = mean_R - s mean_T
```

The median-centered backgrounds are:

```text
B^R = median_k r_k
B^T = median_k t_k
```

If `D` is numerically zero, or if `s` falls outside:

```text
scale_min <= s <= scale_max
```

then the fallback model is:

```text
s = 1
o = B^R - B^T
```

The final node values are:

```text
S_ij = s
O_ij = o
B^R_ij = B^R
B^T_ij = B^T
```

## Filling Missing Grid Nodes

Invalid grid nodes are filled iteratively from valid 8-neighbors. For an invalid
node `q`, let `N(q)` be its valid neighboring nodes. If `N(q)` is non-empty:

```text
S_q = mean_{u in N(q)} S_u
O_q = mean_{u in N(q)} O_u
B^R_q = mean_{u in N(q)} B^R_u
B^T_q = mean_{u in N(q)} B^T_u
```

The process repeats until no more nodes can be filled or the iteration bound is
reached.

## Grid Smoothing

After filling, each field is smoothed for `smooth_passes` iterations using a
weighted 3x3 stencil over valid nodes:

```text
w(dx, dy) =
  4, if dx = 0 and dy = 0
  2, if exactly one of dx, dy is 0
  1, otherwise
```

For each grid node:

```text
V'_ij = sum_{(a,b)} w(a-i,b-j) V_ab / sum_{(a,b)} w(a-i,b-j)
```

where only valid neighbors contribute.

## Field Lookup Inside the Reference Frame

For a target pixel `p_T`, compute:

```text
p_R = M p_T
```

If `p_R` is inside the reference image, `S(p_R)`, `O(p_R)`, `B^R(p_R)`, and
`B^T(p_R)` are bilinearly interpolated from the reference-coordinate grid.

For a field `V`, with fractional grid coordinate:

```text
u = x_R / g
v = y_R / g
```

and surrounding grid samples `V_00, V_10, V_01, V_11`, bilinear interpolation is:

```text
V(u, v) =
  (1-a)(1-b) V_00 +
      a(1-b) V_10 +
  (1-a)    b V_01 +
      a    b V_11
```

where:

```text
a = frac(u)
b = frac(v)
```

## Field Extrapolation Outside the Reference Frame

If `p_R` lies outside the reference image, the C core extrapolates from nearby
grid nodes instead of using identity correction.

Let:

```text
u = x_R / g
v = y_R / g
i_0 = clamp(round(u), 0, N_x - 1)
j_0 = clamp(round(v), 0, N_y - 1)
```

Use valid nodes in the local 5x5 grid neighborhood:

```text
E = {(i, j): |i - i_0| <= 2, |j - j_0| <= 2, node (i,j) valid}
```

For each `(i, j) in E`, define:

```text
d^2_ij = (u - i)^2 + (v - j)^2
w_ij = 1 / (d^2_ij + 0.25)
```

The extrapolated field is:

```text
V_ext(u, v) = sum_{(i,j) in E} w_ij V_ij / sum_{(i,j) in E} w_ij
```

This same rule is used for:

```text
S, O, B^R, B^T
```

If no valid extrapolation nodes exist, the implementation falls back to:

```text
S = 1
O = 0
```

That fallback should only occur if the grid is effectively unusable.

## Applying the Correction

For every target pixel `p_T`:

```text
p_R = M p_T
s = lookup_or_extrapolate(S, p_R)
o = lookup_or_extrapolate(O, p_R)
C(p_T) = s * T(p_T) + o
```

If `T(p_T)` is non-finite:

```text
C(p_T) = T(p_T)
```

The corrected image has the same width, height, and geometry as the target.
It is not registered to the reference.

## Diagnostic Maps

The C core writes target-geometry diagnostic maps:

```text
scale_map(p_T) = S(M p_T)
offset_map(p_T) = O(M p_T)
ref_background(p_T) = B^R(M p_T)
target_background(p_T) = B^T(M p_T)
```

These are later encoded by the Python wrapper for display as FP32 FITS files in
the range `[0, 1]`.

Signed or unitless diagnostics, such as `offset_map` and `scale_map`, are
display-normalized by finite min/max:

```text
D_display = (D_raw - min(D_raw)) / (max(D_raw) - min(D_raw))
```

The original finite range is preserved in FITS header cards:

```text
LNCVMIN = min(D_raw)
LNCVMAX = max(D_raw)
```

Image-valued diagnostics, such as backgrounds and masked images, are clipped and
normalized according to the detected input scale:

```text
D_display = clip(D_raw, 0, U) / U
```

where:

```text
U = 1,      for normalized-float inputs
U = 65535, for ADU-valued inputs
```

## Output FITS Encoding

The wrapper detects the input value scale from the prepared FITS inputs:

```text
normalized_float if p99.9 <= 1.5 and median <= 1.0 and min >= -0.25
adu otherwise
```

Reference and target must agree on this scale.

For final `--output-format float32`, the saved image is always clipped or
normalized to `[0, 1]`.

For final `--output-format uint16`, the normalized display image is converted by:

```text
uint16_value = round(65535 * clip(C, 0, 1))
```

FITS scaling cards that would confuse downstream tools are removed:

```text
BZERO
BSCALE
BLANK
DATAMIN
DATAMAX
```

The output header records:

```text
LNCVRS  = '2-unregistered'
LNCFMT  = output encoding
LNCVSCL = detected input value scale
```

Science outputs preserve the target FITS primary header wherever possible,
including exposure, date, filter, instrument, and WCS cards. The C science
writer copies the target header, updates only structural/scaling cards required
by the corrected float image, and appends LNC provenance such as:

```text
LNCMODE = correction path, for example group-target or reference-passthrough
LNCBKG  = background estimator
LNCREF  = reference filename
LNCTARG = target filename
LNCGRID = grid spacing
LNCWIN  = sampling window size
LNCSAMP = minimum samples per grid node
LNCTRIM = trim fraction
LNCSMIN = minimum scale clamp
LNCSMAX = maximum scale clamp
LNCSMTH = smoothing passes
LNCMINV = minimum valid grid fraction
LNCRMSK = reference masked pixels, when available
LNCTMSK = target masked pixels, when available
LNCSEQ  = sequence index, for group outputs
```

Diagnostic FITS products (`scale_map.fits`, `offset_map.fits`, background maps,
and patch-acceptance maps) are derived maps, not exposures. They may use compact
diagnostic metadata and must not inherit science exposure/date cards.

Existing `normalized_subs` caches created before this metadata contract may
contain header-stripped FITS files. Batch stacking rejects such cached subs and
recomputes LNC; if a cache is reused manually, rebuild or repair it by copying
headers from the matching source sequence frames only after verifying geometry
and sequence index.

## Assumptions

1. The registration homography is accurate enough that paired samples represent
   the same sky location.
2. Local photometric differences are well approximated by an affine intensity
   model:

   ```text
   R ~= s T + o
   ```

3. Scale and offset vary smoothly over the image.
4. Stars, saturated pixels, and other bright structures are rejected well enough
   by the native masks and trimming.
5. Extrapolated correction outside the reference frame is less reliable than
   correction inside the overlap region, but smoother than an identity fallback.
6. The target geometry is authoritative for the saved science image.
7. User-facing FP32 FITS outputs must be in `[0, 1]`.
