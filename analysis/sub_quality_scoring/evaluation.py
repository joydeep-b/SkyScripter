from dataclasses import dataclass
import math
from pathlib import Path

from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring.metrics import MetricMeasurement


@dataclass(frozen=True)
class PairwiseViolation:
    record: dataset.ComparisonRecord
    left_score: float
    right_score: float
    severity: float


@dataclass
class PairwiseEvaluation:
    non_tie_total: int = 0
    non_tie_correct: int = 0
    rejected: int = 0
    skipped: int = 0
    violations: list[PairwiseViolation] | None = None

    def __post_init__(self) -> None:
        if self.violations is None:
            self.violations = []

    @property
    def non_tie_accuracy(self) -> float:
        if self.non_tie_total == 0:
            return float("nan")
        return self.non_tie_correct / self.non_tie_total


@dataclass(frozen=True)
class MetricComparisonResult:
    name: str
    evaluation: PairwiseEvaluation
    scores: dict[Path, float]
    measurements: dict[Path, MetricMeasurement]
    is_sweep: bool = False


def evaluate_pairwise_scores(
    records: list[dataset.ComparisonRecord],
    scores: dict[Path | str, float],
    *,
    epsilon: float = 0.0,
) -> PairwiseEvaluation:
    normalized_scores = {dataset.canonical_path(Path(path)): score for path, score in scores.items()}
    result = PairwiseEvaluation()

    for record in records:
        if record.winner == "reject":
            result.rejected += 1
            continue
        if record.winner == "tie":
            continue

        left_score = normalized_scores.get(dataset.canonical_path(record.left_path), float("nan"))
        right_score = normalized_scores.get(dataset.canonical_path(record.right_path), float("nan"))
        if not math.isfinite(left_score) or not math.isfinite(right_score):
            result.skipped += 1
            continue

        delta = left_score - right_score
        result.non_tie_total += 1
        if record.winner == "left":
            correct = delta > epsilon
            severity = max(epsilon - delta, 0.0)
        elif record.winner == "right":
            correct = delta < -epsilon
            severity = max(delta + epsilon, 0.0)
        else:
            raise ValueError(f"Unknown comparison winner: {record.winner}")

        if correct:
            result.non_tie_correct += 1
        else:
            result.violations.append(PairwiseViolation(record, left_score, right_score, severity))

    result.violations.sort(key=lambda violation: violation.severity, reverse=True)
    return result


def format_accuracy(correct: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{correct}/{total} ({100.0 * correct / total:.1f}%)"


def rank_metric_results(results: list[MetricComparisonResult]) -> list[MetricComparisonResult]:
    return sorted(
        results,
        key=lambda result: (
            result.evaluation.non_tie_accuracy if math.isfinite(result.evaluation.non_tie_accuracy) else -1.0,
            -len(result.evaluation.violations),
            -sum(violation.severity for violation in result.evaluation.violations),
        ),
        reverse=True,
    )


def print_evaluation(result: PairwiseEvaluation, metric_name: str, worst_limit: int) -> None:
    print(f"Metric: {metric_name}")
    print(f"Pairwise accuracy: {format_accuracy(result.non_tie_correct, result.non_tie_total)}")
    print("Tie labels ignored: yes")
    print(f"Rejected labels skipped: {result.rejected}")
    print(f"Pairs skipped for missing/non-finite scores: {result.skipped}")
    print(f"Violations: {len(result.violations)}")

    if not result.violations:
        return

    print("\nWorst violations:")
    for violation in result.violations[:worst_limit]:
        record = violation.record
        print(
            f"  winner={record.winner} severity={violation.severity:.6g} "
            f"left={violation.left_score:.6g} right={violation.right_score:.6g}"
        )
        print(f"    left:  {record.left_path}")
        print(f"    right: {record.right_path}")


def print_metric_comparison(results: list[MetricComparisonResult], top_limit: int) -> None:
    print("\nMetric comparison:")
    print("rank  accuracy         violations  severity_sum  metric")
    for rank, result in enumerate(results[:top_limit], start=1):
        accuracy = format_accuracy(result.evaluation.non_tie_correct, result.evaluation.non_tie_total)
        severity_sum = sum(violation.severity for violation in result.evaluation.violations)
        print(
            f"{rank:4d}  {accuracy:15s}  {len(result.evaluation.violations):10d}  "
            f"{severity_sum:12.6g}  {result.name}"
        )
