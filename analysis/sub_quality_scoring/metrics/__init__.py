from pathlib import Path

from analysis.sub_quality_scoring.metrics import sky_weighted_contrast
from analysis.sub_quality_scoring.metrics import star_yield
from analysis.sub_quality_scoring.metrics import stellar_contrast
from analysis.sub_quality_scoring.metrics import stellar_quality
from analysis.sub_quality_scoring.metrics.registry import MetricMeasurement, MetricSpec


RECOMMENDED_METRIC = stellar_quality.NAME
METRICS: dict[str, MetricSpec] = {
    star_yield.NAME: star_yield.SPEC,
    stellar_contrast.NAME: stellar_contrast.SPEC,
    sky_weighted_contrast.NAME: sky_weighted_contrast.SPEC,
    stellar_quality.NAME: stellar_quality.SPEC,
}
METRIC_NAMES = tuple(METRICS)


def get_metric(metric_name: str) -> MetricSpec:
    try:
        return METRICS[metric_name]
    except KeyError as exc:
        raise ValueError(f"Unknown metric: {metric_name}") from exc


def measure_metric(metric_name: str, sub_path: Path, siril_path: str, timeout: float) -> MetricMeasurement:
    return get_metric(metric_name).measure(sub_path, siril_path, timeout)


def score_metric(metric_name: str, sub_path: Path, siril_path: str, timeout: float) -> float:
    return float(measure_metric(metric_name, sub_path, siril_path, timeout)["score"])
