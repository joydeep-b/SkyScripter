import math
from pathlib import Path

from analysis.sub_quality_scoring import features
from analysis.sub_quality_scoring.metrics.registry import MetricSpec


NAME = "sky_weighted_contrast"
TITLE = "Sky Weighted Contrast"
DESCRIPTION = (
    "Experimental metric that rewards star contrast and detection count while penalizing bright sky background."
)
FORMULA = "log1p(star_count) * median_mean_star_flux / (bgnoise * sqrt(max(background, eps)))"
EPSILON = 1.0e-12


def score_from_features(feature_values: dict[str, float | int]) -> float:
    star_count = float(feature_values["star_count"])
    median_mean_star_flux = float(feature_values["median_mean_star_flux"])
    background = float(feature_values["background"])
    bgnoise = float(feature_values["bgnoise"])
    values = (star_count, median_mean_star_flux, background, bgnoise)
    if not all(math.isfinite(value) for value in values):
        return float("nan")
    if median_mean_star_flux <= 0.0 or bgnoise <= 0.0:
        return float("nan")
    return float(
        math.log1p(max(star_count, 0.0))
        * median_mean_star_flux
        / (bgnoise * math.sqrt(max(background, EPSILON)))
    )


def measure(sub_path: Path, siril_path: str, timeout: float) -> dict[str, float | int]:
    feature_values = features.extract_star_background_features(sub_path, siril_path, timeout)
    return feature_values | {"score": score_from_features(feature_values)}


SPEC = MetricSpec(NAME, TITLE, DESCRIPTION, FORMULA, measure)
