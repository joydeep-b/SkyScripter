# Unregistered Local Normalization Algorithm

This document describes the unregistered local normalization correction (LNC)
implemented by:

- `processing/local_normalize_unregistered.py`
- `processing/lnc/local_normalize_unregistered.c`

The goal is to photometrically correct an unregistered target image while
preserving the target image geometry.

## How to Run

Use the unregistered wrapper when the reference and target are not already
geometrically registered:

```bash
python processing/local_normalize_unregistered.py \
  "path/to/reference.fit" \
  "path/to/target_to_correct.fit" \
  "path/to/corrected_output.fit" \
  --diag-dir "path/to/lnc_diag" \
  --save-intermediate-fits \
  --output-format float32
```

For the Markarians test pair:

```bash
python processing/local_normalize_unregistered.py \
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
python processing/local_normalize.py \
  "path/to/registered_reference.fit" \
  "path/to/registered_target.fit" \
  "path/to/corrected_output.fit" \
  --diag-dir "path/to/lnc_diag" \
  --save-intermediate-fits \
  --background-estimator trimmed-median \
  --output-format float32
```

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
