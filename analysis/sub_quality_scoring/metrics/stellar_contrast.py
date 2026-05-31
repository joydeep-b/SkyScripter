import math
from pathlib import Path

from analysis.sub_quality_scoring import features
from analysis.sub_quality_scoring.metrics.registry import MetricSpec


NAME = "stellar_contrast"
TITLE = "Stellar Contrast"
DESCRIPTION = "Measures the median background-subtracted detected-star flux relative to background noise."
FORMULA = "median_mean_star_flux / bgnoise"


def score_from_features(feature_values: dict[str, float | int]) -> float:
    median_mean_star_flux = float(feature_values["median_mean_star_flux"])
    bgnoise = float(feature_values["bgnoise"])
    if math.isfinite(median_mean_star_flux) and math.isfinite(bgnoise) and bgnoise > 0.0:
        return float(median_mean_star_flux / bgnoise)
    return float("nan")


def measure(sub_path: Path, siril_path: str, timeout: float) -> dict[str, float | int]:
    feature_values = features.extract_star_background_features(sub_path, siril_path, timeout)
    return feature_values | {"score": score_from_features(feature_values)}


SPEC = MetricSpec(NAME, TITLE, DESCRIPTION, FORMULA, measure)
