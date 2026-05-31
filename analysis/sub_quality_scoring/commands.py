from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import json
import random
from pathlib import Path
import sys

from analysis.sub_quality_scoring import dataset
from analysis.sub_quality_scoring import discovery
from analysis.sub_quality_scoring import evaluation
from analysis.sub_quality_scoring import features
from analysis.sub_quality_scoring import formula_sweep
from analysis.sub_quality_scoring import previews
from analysis.sub_quality_scoring import siril
from analysis.sub_quality_scoring import metrics
from analysis.sub_quality_scoring.metrics import MetricMeasurement


def measure_metric_for_path(args: tuple[str, Path, str, float]) -> tuple[Path, MetricMeasurement]:
    metric_name, sub_path, siril_path, timeout = args
    return sub_path, metrics.measure_metric(metric_name, sub_path, siril_path, timeout)


def extract_features_for_path(args: tuple[Path, str, float]) -> tuple[Path, MetricMeasurement]:
    sub_path, siril_path, timeout = args
    return sub_path, features.extract_star_background_features(sub_path, siril_path, timeout)


def collect_scores(
    records: list[dataset.ComparisonRecord],
    metric_name: str,
    siril_path: str,
    timeout: float,
    metric_measurements: dict[Path, MetricMeasurement] | None = None,
    workers: int = 1,
) -> dict[Path, float]:
    paths = sorted(
        {
            dataset.canonical_path(path)
            for record in records
            if record.winner in dataset.CLEAR_WINNERS
            for path in (record.left_path, record.right_path)
        }
    )
    measurements = measure_paths(paths, metric_name, siril_path, timeout, workers)
    scores = {path: float(measurement["score"]) for path, measurement in measurements.items()}
    if metric_measurements is not None:
        metric_measurements.update(measurements)
    return scores


def measure_paths(
    sub_paths: list[Path],
    metric_name: str,
    siril_path: str,
    timeout: float,
    workers: int,
    on_result: Callable[[Path, MetricMeasurement], None] | None = None,
) -> dict[Path, MetricMeasurement]:
    if workers < 1:
        raise ValueError("--workers must be at least 1.")
    measurements = {}
    if workers == 1 or len(sub_paths) <= 1:
        for index, sub_path in enumerate(sub_paths, start=1):
            print(f"Scoring {index:3d}/{len(sub_paths):3d} with {metric_name}: {sub_path.name}", file=sys.stderr)
            measurement = metrics.measure_metric(metric_name, sub_path, siril_path, timeout)
            measurements[sub_path] = measurement
            if on_result is not None:
                on_result(sub_path, measurement)
        return measurements

    max_workers = min(workers, len(sub_paths))
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(measure_metric_for_path, (metric_name, sub_path, siril_path, timeout)): sub_path
            for sub_path in sub_paths
        }
        for index, future in enumerate(as_completed(future_to_path), start=1):
            sub_path = future_to_path[future]
            print(f"Scored  {index:3d}/{len(sub_paths):3d} with {metric_name}: {sub_path.name}", file=sys.stderr)
            try:
                _path, measurement = future.result()
            except Exception as exc:
                raise RuntimeError(f"Failed to compute {metric_name} for {sub_path}") from exc
            measurements[sub_path] = measurement
            if on_result is not None:
                on_result(sub_path, measurement)
    return measurements


def collect_star_background_features(
    records: list[dataset.ComparisonRecord],
    siril_path: str,
    timeout: float,
    workers: int,
) -> dict[Path, MetricMeasurement]:
    paths = sorted(
        {
            dataset.canonical_path(path)
            for record in records
            if record.winner in dataset.CLEAR_WINNERS
            for path in (record.left_path, record.right_path)
        }
    )
    if workers < 1:
        raise ValueError("--workers must be at least 1.")
    feature_rows = {}
    if workers == 1 or len(paths) <= 1:
        for index, sub_path in enumerate(paths, start=1):
            print(f"Extracting features {index:3d}/{len(paths):3d}: {sub_path.name}", file=sys.stderr)
            feature_rows[sub_path] = features.extract_star_background_features(sub_path, siril_path, timeout)
        return feature_rows

    max_workers = min(workers, len(paths))
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(extract_features_for_path, (sub_path, siril_path, timeout)): sub_path
            for sub_path in paths
        }
        for index, future in enumerate(as_completed(future_to_path), start=1):
            sub_path = future_to_path[future]
            print(f"Extracted  features {index:3d}/{len(paths):3d}: {sub_path.name}", file=sys.stderr)
            try:
                _path, feature_row = future.result()
            except Exception as exc:
                raise RuntimeError(f"Failed to extract star/background features for {sub_path}") from exc
            feature_rows[sub_path] = feature_row
    return feature_rows


def choose_pair_winner(
    left_path: Path,
    right_path: Path,
    left_preview: Path,
    right_preview: Path,
) -> str:
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt

    left_image = mpimg.imread(left_preview)
    right_image = mpimg.imread(right_preview)
    answer = {"winner": "quit"}

    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    fig.suptitle("Choose the better sub: left/l, right/r, tie/t, reject/x, quit/q")

    axes[0].imshow(left_image, cmap="gray")
    axes[0].set_title(f"Left\n{left_path.name}")
    axes[0].axis("off")

    axes[1].imshow(right_image, cmap="gray")
    axes[1].set_title(f"Right\n{right_path.name}")
    axes[1].axis("off")
    fig.tight_layout()

    def on_key(event) -> None:
        key = (event.key or "").lower()
        if key in {"left", "l", "1"}:
            answer["winner"] = "left"
        elif key in {"right", "r", "2"}:
            answer["winner"] = "right"
        elif key in {"t", "=", "0"}:
            answer["winner"] = "tie"
        elif key in {"x", "delete", "backspace"}:
            answer["winner"] = "reject"
        elif key in {"q", "escape"}:
            answer["winner"] = "quit"
        else:
            return
        plt.close(fig)

    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show(block=True)
    return answer["winner"]


def show_labeled_pair(record: dataset.ComparisonRecord, index: int, total: int) -> str:
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt

    left_image = mpimg.imread(record.left_preview)
    right_image = mpimg.imread(record.right_preview)
    action = {"value": "next"}

    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    title = (
        f"Label {index}/{total}: {record.winner.upper()} "
        "(space/n next, q quit, Shift+L/Shift+R relabel, Shift+D delete)"
    )
    if record.note:
        title += f"\nNote: {record.note}"
    fig.suptitle(title)

    left_title = f"Left\n{record.left_path.name}"
    right_title = f"Right\n{record.right_path.name}"
    if record.winner == "left":
        left_title = f"Left (WINNER)\n{record.left_path.name}"
    elif record.winner == "right":
        right_title = f"Right (WINNER)\n{record.right_path.name}"
    elif record.winner == "tie":
        left_title = f"Left (TIE)\n{record.left_path.name}"
        right_title = f"Right (TIE)\n{record.right_path.name}"
    elif record.winner == "reject":
        left_title = f"Left (REJECTED PAIR)\n{record.left_path.name}"
        right_title = f"Right (REJECTED PAIR)\n{record.right_path.name}"

    axes[0].imshow(left_image, cmap="gray")
    axes[0].set_title(left_title)
    axes[0].axis("off")

    axes[1].imshow(right_image, cmap="gray")
    axes[1].set_title(right_title)
    axes[1].axis("off")
    fig.tight_layout()

    def on_key(event) -> None:
        key = event.key or ""
        lower_key = key.lower()
        if lower_key in {"q", "escape"}:
            action["value"] = "quit"
            plt.close(fig)
        elif lower_key in {" ", "space", "n", "right", "enter"}:
            action["value"] = "next"
            plt.close(fig)
        elif key in {"L", "shift+l", "shift+L"}:
            action["value"] = "set_left"
            plt.close(fig)
        elif key in {"R", "shift+r", "shift+R"}:
            action["value"] = "set_right"
            plt.close(fig)
        elif key in {"D", "shift+d", "shift+D"}:
            action["value"] = "delete"
            plt.close(fig)

    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show(block=True)
    return action["value"]


def format_metric_measurement(measurement: dict[str, float | int]) -> str:
    lines = [f"score={float(measurement['score']):.6g}"]
    if "median_mean_star_flux" in measurement:
        lines.append(f"median mean star flux={float(measurement['median_mean_star_flux']):.6g}")
    if "background" in measurement:
        lines.append(f"background={float(measurement['background']):.6g}")
    if "bgnoise" in measurement:
        lines.append(f"bgnoise={float(measurement['bgnoise']):.6g}")
    if "star_count" in measurement:
        lines.append(f"star count={int(measurement['star_count'])}")
    return "\n".join(lines)


def show_metric_violation_pair(
    violation: evaluation.PairwiseViolation,
    index: int,
    total: int,
    metric_name: str,
    metric_measurements: dict[Path, MetricMeasurement],
) -> str:
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt

    record = violation.record
    left_image = mpimg.imread(record.left_preview)
    right_image = mpimg.imread(record.right_preview)
    action = {"value": "next"}

    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    title = (
        f"Worst Pair {index}/{total}: dataset winner={record.winner.upper()} "
        f"metric={metric_name} severity={violation.severity:.6g}\n"
        f"space/n next, q quit"
    )
    if record.note:
        title += f"\nNote: {record.note}"
    fig.suptitle(title)

    left_details = format_metric_measurement(
        metric_measurements.get(dataset.canonical_path(record.left_path), {"score": violation.left_score})
    )
    right_details = format_metric_measurement(
        metric_measurements.get(dataset.canonical_path(record.right_path), {"score": violation.right_score})
    )
    left_title = f"Left\n{left_details}\n{record.left_path.name}"
    right_title = f"Right\n{right_details}\n{record.right_path.name}"
    if record.winner == "left":
        left_title = f"Left (DATASET WINNER)\n{left_details}\n{record.left_path.name}"
    elif record.winner == "right":
        right_title = f"Right (DATASET WINNER)\n{right_details}\n{record.right_path.name}"

    axes[0].imshow(left_image, cmap="gray")
    axes[0].set_title(left_title)
    axes[0].axis("off")

    axes[1].imshow(right_image, cmap="gray")
    axes[1].set_title(right_title)
    axes[1].axis("off")
    fig.tight_layout()

    def on_key(event) -> None:
        key = (event.key or "").lower()
        if key in {"q", "escape"}:
            action["value"] = "quit"
            plt.close(fig)
        elif key in {" ", "space", "n", "right", "enter"}:
            action["value"] = "next"
            plt.close(fig)

    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show(block=True)
    return action["value"]


def visualize_metric_violations(
    violations: list[evaluation.PairwiseViolation],
    *,
    metric_name: str,
    worst_limit: int,
    metric_measurements: dict[Path, MetricMeasurement],
) -> None:
    selected_violations = violations[:worst_limit]
    if not selected_violations:
        print("No metric violations to visualize.")
        return

    missing_previews = [
        path
        for violation in selected_violations
        for path in (violation.record.left_preview, violation.record.right_preview)
        if not path.exists()
    ]
    if missing_previews:
        print("Cannot visualize metric violations because preview files are missing:")
        for path in missing_previews[:20]:
            print(f"  {path}")
        if len(missing_previews) > 20:
            print(f"  ... and {len(missing_previews) - 20} more")
        raise SystemExit(1)

    print(f"Visualizing {len(selected_violations)} worst metric violation(s).")
    for index, violation in enumerate(selected_violations, start=1):
        record = violation.record
        print(
            f"{index}/{len(selected_violations)} winner={record.winner} "
            f"left={violation.left_score:.6g} right={violation.right_score:.6g} "
            f"severity={violation.severity:.6g}"
        )
        action = show_metric_violation_pair(
            violation,
            index,
            len(selected_violations),
            metric_name,
            metric_measurements,
        )
        if action == "quit":
            break


def run_score(args) -> None:
    siril_path = siril.get_siril_path(args.siril_path)
    sub_paths = discovery.discover_subs(args.input_dirs)
    if not sub_paths:
        print("No FITS subs found.", file=sys.stderr)
        return
    measurements = measure_paths(sub_paths, args.metric, siril_path, args.siril_timeout, args.workers)
    write_score_rows(measurements, args.metric, args.output, args.output_format)


def write_score_rows(
    measurements: dict[Path, MetricMeasurement],
    metric_name: str,
    output_path: Path | None,
    output_format: str,
) -> None:
    rows = [
        {"sub_path": str(sub_path), "metric": metric_name} | measurement
        for sub_path, measurement in sorted(measurements.items())
    ]
    if output_format == "jsonl":
        text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    elif output_format == "csv":
        fieldnames = ["sub_path", "metric", "score", "star_count", "median_mean_star_flux", "background", "bgnoise"]
        from io import StringIO

        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()
    else:
        raise ValueError(f"Unknown score output format: {output_format}")

    if output_path is None:
        print(text, end="")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")


def run_label(args) -> None:
    dataset_path = args.dataset.expanduser().resolve()
    records = dataset.read_comparisons(dataset_path)
    grouped_paths = discovery.discover_candidates(args.input_dirs)
    pairs = discovery.generate_pairs(
        grouped_paths,
        mode=args.pair_mode,
        seed=args.seed,
        max_pairs=args.max_pairs,
        skip_pairs=dataset.existing_pair_keys(records),
    )
    if not pairs:
        print("No unlabeled compatible pairs found.")
        return

    siril_path = siril.get_siril_path(args.siril_path)
    cache_dir = previews.preview_cache_dir(dataset_path, args.cache_dir)
    rng = random.Random(args.seed)

    print(f"Found {len(pairs)} unlabeled pair(s).")
    for index, pair in enumerate(pairs, start=1):
        first_path, second_path = pair.first_path, pair.second_path
        if rng.random() < 0.5:
            left_path, right_path = first_path, second_path
        else:
            left_path, right_path = second_path, first_path

        print(f"\nPair {index}/{len(pairs)}")
        print(f"  left:  {left_path}")
        print(f"  right: {right_path}")
        left_preview = previews.render_preview(left_path, cache_dir, siril_path, args.siril_timeout)
        right_preview = previews.render_preview(right_path, cache_dir, siril_path, args.siril_timeout)
        winner = choose_pair_winner(left_path, right_path, left_preview, right_preview)
        if winner == "quit":
            print("Stopped without saving this pair.")
            break

        note = input("Optional note (blank for none): ").strip() if args.ask_note else ""
        dataset.append_comparison(
            dataset_path,
            dataset.ComparisonRecord(
                timestamp=dataset.now_timestamp(),
                group=pair.group,
                left_path=left_path,
                right_path=right_path,
                winner=winner,
                left_preview=left_preview,
                right_preview=right_preview,
                note=note,
            ),
        )
        print(f"Saved label: {winner}")


def run_eval(args) -> None:
    dataset_path = args.dataset.expanduser().resolve()
    records = dataset.read_comparisons(dataset_path)
    if not records:
        raise SystemExit(f"No comparisons found in {dataset_path}")

    siril_path = siril.get_siril_path(args.siril_path)
    metric_measurements = {} if args.visualize else None
    scores = collect_scores(
        records,
        args.metric,
        siril_path,
        args.siril_timeout,
        metric_measurements=metric_measurements,
        workers=args.workers,
    )
    result = evaluation.evaluate_pairwise_scores(records, scores, epsilon=args.epsilon)
    evaluation.print_evaluation(result, args.metric, args.worst)
    if args.visualize:
        visualize_metric_violations(
            result.violations,
            metric_name=args.metric,
            worst_limit=args.worst,
            metric_measurements=metric_measurements or {},
        )


def run_compare_metrics(args) -> None:
    dataset_path = args.dataset.expanduser().resolve()
    records = dataset.read_comparisons(dataset_path)
    if not records:
        raise SystemExit(f"No comparisons found in {dataset_path}")

    siril_path = siril.get_siril_path(args.siril_path)
    results = []
    for metric_name in metrics.METRIC_NAMES:
        print(f"\nEvaluating registered metric: {metric_name}")
        measurements = {}
        scores = collect_scores(
            records,
            metric_name,
            siril_path,
            args.siril_timeout,
            metric_measurements=measurements,
            workers=args.workers,
        )
        results.append(
            evaluation.MetricComparisonResult(
                name=metric_name,
                evaluation=evaluation.evaluate_pairwise_scores(records, scores, epsilon=args.epsilon),
                scores=scores,
                measurements=measurements,
            )
        )

    if args.sweep:
        print("\nExtracting star/background features for formula sweep")
        features_by_path = collect_star_background_features(records, siril_path, args.siril_timeout, args.workers)
        for (
            name,
            _star_term_name,
            star_exponent,
            flux_exponent,
            bgnoise_exponent,
            background_exponent,
        ) in formula_sweep.formula_sweep_candidates():
            measurements = {}
            scores = {}
            for sub_path, feature_values in features_by_path.items():
                score = formula_sweep.formula_sweep_score(
                    feature_values,
                    star_exponent=star_exponent,
                    flux_exponent=flux_exponent,
                    bgnoise_exponent=bgnoise_exponent,
                    background_exponent=background_exponent,
                )
                scores[sub_path] = score
                measurements[sub_path] = feature_values | {"score": score}
            results.append(
                evaluation.MetricComparisonResult(
                    name=name,
                    evaluation=evaluation.evaluate_pairwise_scores(records, scores, epsilon=args.epsilon),
                    scores=scores,
                    measurements=measurements,
                    is_sweep=True,
                )
            )

    ranked_results = evaluation.rank_metric_results(results)
    evaluation.print_metric_comparison(ranked_results, args.top)
    if args.visualize_best and ranked_results:
        best = ranked_results[0]
        visualize_metric_violations(
            best.evaluation.violations,
            metric_name=best.name,
            worst_limit=args.worst,
            metric_measurements=best.measurements,
        )


def run_visualize(args) -> None:
    dataset_path = args.dataset.expanduser().resolve()
    records = dataset.read_comparisons(dataset_path)
    if not records:
        raise SystemExit(f"No comparisons found in {dataset_path}")

    winners = set(args.winner) if args.winner else dataset.CLEAR_WINNERS
    selected_records = dataset.filter_records_for_visualization(
        records,
        winners=winners,
        start=args.start,
        limit=args.limit,
    )
    if not selected_records:
        print("No clear left/right preference labels matched the requested filters.")
        return

    missing_previews = [
        path
        for record in selected_records
        for path in (record.left_preview, record.right_preview)
        if not path.exists()
    ]
    if missing_previews:
        print("Cannot visualize labels because preview files are missing:")
        for path in missing_previews[:20]:
            print(f"  {path}")
        if len(missing_previews) > 20:
            print(f"  ... and {len(missing_previews) - 20} more")
        raise SystemExit(1)

    print(f"Visualizing {len(selected_records)} clear preference label(s) from {dataset_path}")
    for index, record in enumerate(selected_records, start=1):
        print(
            f"{index}/{len(selected_records)} winner={record.winner} "
            f"left={record.left_path.name} right={record.right_path.name}"
        )
        action = show_labeled_pair(record, index, len(selected_records))
        if action == "quit":
            break
        if action == "set_left":
            record.winner = "left"
            dataset.write_comparisons(dataset_path, records)
            print("  Updated label to left")
        elif action == "set_right":
            record.winner = "right"
            dataset.write_comparisons(dataset_path, records)
            print("  Updated label to right")
        elif action == "delete":
            records.remove(record)
            dataset.write_comparisons(dataset_path, records)
            print("  Deleted pair from dataset")
