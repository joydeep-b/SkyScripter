import math
from pathlib import Path

from analysis.sub_quality_scoring import siril
from analysis.sub_quality_scoring.metrics.registry import MetricSpec


NAME = "star_yield"
TITLE = "Star Yield"
DESCRIPTION = "Counts Siril-detected stars per unit background noise."
FORMULA = "star_count / bgnoise"


def score_from_values(star_count: int, bgnoise: float) -> float:
    if star_count > 0 and math.isfinite(bgnoise) and bgnoise > 0.0:
        return float(star_count / bgnoise)
    return float("nan")


def run_star_yield_stats(sub_path: Path, siril_path: str, timeout: float) -> tuple[int, float]:
    script = f"""requires 1.2.0
load {siril.quote(sub_path.name)}
findstar
stat
close
"""
    output = siril.run_siril_script(
        script,
        sub_path.parent,
        siril_path,
        timeout,
        failure_context=f"Siril star-yield measurement for {sub_path}",
    )
    return siril.parse_star_count(output), siril.parse_bgnoise(output)


def measure(sub_path: Path, siril_path: str, timeout: float) -> dict[str, float | int]:
    star_count, bgnoise = run_star_yield_stats(sub_path, siril_path, timeout)
    return {
        "score": score_from_values(star_count, bgnoise),
        "star_count": star_count,
        "bgnoise": bgnoise,
    }


SPEC = MetricSpec(NAME, TITLE, DESCRIPTION, FORMULA, measure)
