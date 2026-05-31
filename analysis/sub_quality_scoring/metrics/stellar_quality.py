import math
from pathlib import Path

from analysis.sub_quality_scoring import features
from analysis.sub_quality_scoring.metrics.registry import MetricSpec


NAME = "stellar_quality"
TITLE = "Stellar Quality"
DESCRIPTION = (
    "Recommended metric that balances useful star detections, typical star signal, background noise, and sky brightness."
)
FORMULA = "star_count^0.75 * signed(median_mean_star_flux)^0.75 / (bgnoise^0.5 * background^0.25)"
EPSILON = 1.0e-12


def signed_power(value: float, exponent: float) -> float:
    if not math.isfinite(value):
        return float("nan")
    if value == 0.0:
        return 0.0
    return math.copysign(abs(value) ** exponent, value)


def score_from_features(feature_values: dict[str, float | int]) -> float:
    star_count = float(feature_values["star_count"])
    median_mean_star_flux = float(feature_values["median_mean_star_flux"])
    background = float(feature_values["background"])
    bgnoise = float(feature_values["bgnoise"])
    values = (star_count, median_mean_star_flux, background, bgnoise)
    if not all(math.isfinite(value) for value in values):
        return float("nan")
    if star_count <= 0.0 or bgnoise <= 0.0:
        return float("nan")
    return float(
        (star_count**0.75)
        * signed_power(median_mean_star_flux, 0.75)
        / ((bgnoise**0.5) * (max(background, EPSILON) ** 0.25))
    )


def measure(sub_path: Path, siril_path: str, timeout: float) -> dict[str, float | int]:
    feature_values = features.extract_star_background_features(sub_path, siril_path, timeout)
    return feature_values | {"score": score_from_features(feature_values)}


SPEC = MetricSpec(NAME, TITLE, DESCRIPTION, FORMULA, measure)
