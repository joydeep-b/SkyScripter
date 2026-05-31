# Sub Quality Scoring

This package scores astrophotography subs and evaluates score formulas against a human-labeled pairwise benchmark.

Use the top-level script:

```bash
python analysis/sub_quality.py --help
```

The implementation lives in `analysis/sub_quality_scoring/`. The old `analysis/sub_quality_benchmark.py` script is only a compatibility wrapper.

## Requirements

- Python packages already used by the repository, including `astropy`, `numpy`, and `matplotlib`.
- Siril 1.2 or newer available as `siril-cli`, `siril`, or via `--siril-path`.
- FITS subs readable by `astropy.io.fits`.

All star/background features are measured in the same scale as Siril's `stat` output. If FITS data loads as normalized `0..1` floats, it is converted to Siril's 16-bit statistic scale before computing star flux.

## Quick Start

Score every FITS sub in a directory:

```bash
python analysis/sub_quality.py score /path/to/subs --metric stellar_quality --output scores.csv
```

Create a pairwise benchmark:

```bash
python analysis/sub_quality.py label /path/to/subs --dataset sub_quality_pairs.jsonl
```

View or edit clear left/right labels:

```bash
python analysis/sub_quality.py visualize --dataset sub_quality_pairs.jsonl
```

Evaluate one metric against the benchmark:

```bash
python analysis/sub_quality.py eval --dataset sub_quality_pairs.jsonl --metric stellar_quality
```

Compare all registered metrics and report-only formula sweep candidates:

```bash
python analysis/sub_quality.py compare-metrics --dataset sub_quality_pairs.jsonl --workers 10
```

## Feature Definitions

The metrics use these measured features:

- `star_count`: number of Siril-detected stars.
- `median_mean_star_flux`: median, over detected stars, of the mean pixel value inside the star aperture after subtracting Siril's median background.
- `background`: Siril image median background from `stat`.
- `bgnoise`: Siril background noise from `stat`.

For a star aperture, flux is computed as:

```text
mean(aperture_pixels - background)
```

Then `median_mean_star_flux` is the median of that value across all accepted detected-star apertures.

## Available Metrics

### `stellar_quality`

Recommended current metric. It balances useful star detections, typical detected-star signal, background noise, and sky brightness. It uses a weak star-count reward and a signed flux term so low or negative median star flux remains a valid low score instead of being skipped.

```text
stellar_quality =
  star_count^0.75 * signed(median_mean_star_flux)^0.75 /
  (bgnoise^0.5 * background^0.25)
```

where:

```text
signed(x)^p = sign(x) * abs(x)^p
```

### `stellar_contrast`

Baseline metric for typical detected-star contrast above noise. It ignores star count and sky brightness.

```text
stellar_contrast = median_mean_star_flux / bgnoise
```

### `star_yield`

Baseline metric for the number of Siril-detected stars per unit background noise. It can reward images with many dim detections, so it is useful as a baseline but not recommended as the primary quality metric.

```text
star_yield = star_count / bgnoise
```

### `sky_weighted_contrast`

Archived experimental metric. It rewards detected-star contrast and a logarithmic star-count term, while penalizing brighter sky. It was superseded by `stellar_quality` because it can still over-reward many dim detected stars and skips non-positive median flux cases.

```text
sky_weighted_contrast =
  log1p(star_count) * median_mean_star_flux /
  (bgnoise * sqrt(max(background, eps)))
```

## Benchmark Format

Benchmark labels are newline-delimited JSON. Each row stores:

- `left_path`, `right_path`: the compared FITS subs.
- `winner`: one of `left`, `right`, `tie`, or `reject`.
- `left_preview`, `right_preview`: cached autostretched PNG previews.
- `group`: compatibility group used for pair generation.
- `timestamp`: label time.
- `note`: optional human note.

Evaluation ignores `tie` labels entirely and skips `reject` labels. Accuracy is computed only over clear left/right preferences with finite scores for both subs.

## Formula Sweep

`compare-metrics` can run a report-only formula sweep over combinations of:

```text
score = star_term * signed(median_mean_star_flux)^b / (bgnoise^c * background^d)
```

Sweep candidates are printed for experimentation only. Promote a formula to a named metric only after inspecting failures and adding documentation.

## Integration Guide

For another Python program, call the metric registry:

```python
from pathlib import Path

from analysis.sub_quality_scoring import metrics

measurement = metrics.measure_metric(
    "stellar_quality",
    Path("/path/to/sub.fit"),
    "siril-cli",
    120.0,
)
score = measurement["score"]
```

To integrate the idea into a non-Python program such as Siril, reproduce the same feature extraction and formula:

1. Detect stars and collect `star_count`.
2. Measure median background and `bgnoise`.
3. For each detected star, compute `mean(aperture_pixels - background)`.
4. Take the median of those per-star means.
5. Apply the metric formula exactly as documented above.

The feature extraction code in `features.py` is intentionally separate from the metric formulas so it can serve as a reference implementation.
