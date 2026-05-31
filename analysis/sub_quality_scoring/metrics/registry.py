from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


MetricMeasurement = dict[str, float | int]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    title: str
    description: str
    formula: str
    measure: Callable[[Path, str, float], MetricMeasurement]


def metric_names(metrics: dict[str, MetricSpec]) -> tuple[str, ...]:
    return tuple(metrics)
