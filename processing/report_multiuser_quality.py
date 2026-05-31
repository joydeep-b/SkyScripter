#!/usr/bin/env python3

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
from datetime import datetime
from html import escape
import json
import math
import os
from pathlib import Path
import sys
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.sub_quality_scoring import metrics, previews, siril
from analysis.sub_quality_scoring.commands import measure_paths
from processing.stack_multiuser_subs import (
    DEFAULT_REPORT,
    FILTER_ORDER,
    VALID_FILTERS,
    parse_report,
    safe_name,
    sort_group_key,
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


MEASUREMENT_KEYS = ("score", "star_count", "median_mean_star_flux", "background", "bgnoise")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a per-user sub-quality PDF report from collect_multiuser_project_subs.py "
            "filter_lookup_report.json."
        )
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to filter_lookup_report.json.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory. Defaults to <report_dir>/quality_report.",
    )
    parser.add_argument(
        "--metric",
        default=metrics.RECOMMENDED_METRIC,
        choices=metrics.METRIC_NAMES,
        help="Quality metric to use for best-sub selection. Default: stellar_quality.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel scoring workers. Default: CPU count.",
    )
    parser.add_argument(
        "--siril-path",
        type=Path,
        default=None,
        help="Optional explicit path to Siril executable.",
    )
    parser.add_argument(
        "--siril-timeout",
        type=float,
        default=300.0,
        help="Per-Siril-command timeout in seconds. Default: 300.",
    )
    parser.add_argument(
        "--min-frames",
        type=int,
        default=3,
        help="Minimum frames required for a group detail block. Summary still counts all groups.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached measurements and rescore all subs.",
    )
    parser.add_argument(
        "--cache-flush-interval",
        type=int,
        default=50,
        help=(
            "Write the measurement cache to disk every N newly scored subs so the "
            "run can be stopped and resumed. Use 0 to only write once at the end."
        ),
    )
    return parser.parse_args()


def load_measurement_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    if not cache_path.exists():
        return {}
    with cache_path.open("r", encoding="utf-8") as handle:
        cache = json.load(handle)
    if not isinstance(cache, dict):
        return {}
    return cache


def cache_entry_is_valid(path: Path, entry: object, force: bool) -> bool:
    if force or not path.exists() or not isinstance(entry, dict):
        return False
    try:
        cached_mtime = float(entry["mtime"])
    except (KeyError, TypeError, ValueError):
        return False
    if "measurement" not in entry or not isinstance(entry["measurement"], dict):
        return False
    return abs(cached_mtime - path.stat().st_mtime) < 1.0e-6


def write_measurement_cache(cache_path: Path, measurements: dict[Path, dict[str, float | int]]) -> None:
    cache = {
        str(path): {
            "mtime": path.stat().st_mtime,
            "measurement": {key: measurement.get(key) for key in MEASUREMENT_KEYS},
        }
        for path, measurement in sorted(measurements.items())
        if path.exists()
    }
    # Write atomically so an interruption mid-write cannot corrupt the cache and
    # break resumability.
    tmp_path = cache_path.with_name(cache_path.name + ".tmp")
    tmp_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp_path, cache_path)


def measure_all_paths(
    all_paths: list[Path],
    *,
    metric_name: str,
    siril_path: str,
    timeout: float,
    workers: int,
    cache_path: Path,
    force: bool,
    flush_interval: int = 50,
) -> dict[Path, dict[str, float | int]]:
    cache = load_measurement_cache(cache_path)
    measurements: dict[Path, dict[str, float | int]] = {}
    missing_paths = []

    for path in all_paths:
        entry = cache.get(str(path))
        if cache_entry_is_valid(path, entry, force):
            measurements[path] = dict(entry["measurement"])
        else:
            missing_paths.append(path)

    if not missing_paths:
        print(f"All {len(measurements)} measurement(s) loaded from cache: {cache_path}", file=sys.stderr)
        return measurements

    if measurements:
        print(
            f"Resuming: {len(measurements)} cached, {len(missing_paths)} to score "
            f"(cache: {cache_path}).",
            file=sys.stderr,
        )

    completed_since_flush = 0

    def on_result(path: Path, measurement: dict[str, float | int]) -> None:
        nonlocal completed_since_flush
        measurements[path] = measurement
        completed_since_flush += 1
        if flush_interval > 0 and completed_since_flush >= flush_interval:
            write_measurement_cache(cache_path, measurements)
            completed_since_flush = 0

    # The finally block guarantees that whatever completed (even on
    # KeyboardInterrupt or a fatal scoring error) is persisted, so the next run
    # resumes from where this one stopped.
    try:
        measure_paths(
            missing_paths,
            metric_name,
            siril_path,
            timeout,
            workers,
            on_result=on_result,
        )
    finally:
        write_measurement_cache(cache_path, measurements)

    return measurements


def write_measurements_csv(
    output_path: Path,
    groups: dict[tuple[str, str, str], list[Path]],
    measurements: dict[Path, dict[str, float | int]],
) -> None:
    fieldnames = [
        "user",
        "equipment",
        "filter",
        "sub_path",
        "score",
        "star_count",
        "median_mean_star_flux",
        "background",
        "bgnoise",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for user_name, equipment_name, filter_name in sorted(groups.keys(), key=sort_group_key):
            for sub_path in sorted(groups[(user_name, equipment_name, filter_name)]):
                measurement = measurements[sub_path.resolve()]
                writer.writerow(
                    {
                        "user": user_name,
                        "equipment": equipment_name,
                        "filter": filter_name,
                        "sub_path": str(sub_path),
                        "score": measurement.get("score"),
                        "star_count": measurement.get("star_count"),
                        "median_mean_star_flux": measurement.get("median_mean_star_flux"),
                        "background": measurement.get("background"),
                        "bgnoise": measurement.get("bgnoise"),
                    }
                )


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def finite_values(values: list[object]) -> list[float]:
    return [number for value in values if (number := finite_float(value)) is not None]


def group_score_rows(
    paths: list[Path],
    measurements: dict[Path, dict[str, float | int]],
) -> list[tuple[Path, float]]:
    rows = []
    for path in paths:
        score = finite_float(measurements[path.resolve()].get("score"))
        if score is not None:
            rows.append((path, score))
    return rows


def plot_cdf(
    ax,
    values: list[object],
    label: str,
    marker_value: object | None = None,
) -> None:
    vals = np.sort(np.asarray(finite_values(values), dtype=float))
    if vals.size == 0:
        ax.set_title(f"{label} (no data)")
        return
    y = np.linspace(0, 1, vals.size)
    ax.plot(vals, y, drawstyle="steps-post")
    marker = finite_float(marker_value)
    if marker is not None:
        ax.axvline(marker, linestyle="--")
    ax.set_title(label)
    ax.set_ylabel("CDF")
    ax.grid(True, alpha=0.3)


def make_cdf_plot(
    *,
    user_name: str,
    equipment_name: str,
    filter_name: str,
    paths: list[Path],
    measurements: dict[Path, dict[str, float | int]],
    best_path: Path,
    best_score: float,
    cdf_dir: Path,
) -> Path:
    cdf_path = cdf_dir / f"{safe_name(user_name)}__{safe_name(equipment_name)}__{filter_name}.png"
    best_measurement = measurements[best_path.resolve()]
    group_measurements = [measurements[path.resolve()] for path in paths]

    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    fig.suptitle(f"{user_name} / {equipment_name} / {filter_name}  (n={len(paths)})")
    plot_cdf(axes[0, 0], [m.get("score") for m in group_measurements], "quality score", best_score)
    plot_cdf(
        axes[0, 1],
        [m.get("star_count") for m in group_measurements],
        "star_count",
        best_measurement.get("star_count"),
    )
    plot_cdf(
        axes[1, 0],
        [m.get("bgnoise") for m in group_measurements],
        "bgnoise",
        best_measurement.get("bgnoise"),
    )
    plot_cdf(
        axes[1, 1],
        [m.get("background") for m in group_measurements],
        "background",
        best_measurement.get("background"),
    )
    fig.tight_layout()
    fig.savefig(cdf_path, dpi=120)
    plt.close(fig)
    return cdf_path


def sized_image(path: Path, max_width: float) -> Image:
    image_width, image_height = ImageReader(str(path)).getSize()
    scale = max_width / float(image_width)
    return Image(str(path), width=max_width, height=image_height * scale)


def table_style() -> TableStyle:
    return TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )


def image_table_style() -> TableStyle:
    return TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
    )


def add_summary_page(
    story: list,
    *,
    styles,
    report_path: Path,
    metric_name: str,
    groups: dict[tuple[str, str, str], list[Path]],
    stats: dict[str, int],
) -> None:
    story.append(Paragraph("Multiuser Quality Report", styles["Title"]))
    story.append(Paragraph(f"Report: {escape(str(report_path))}", styles["Normal"]))
    story.append(Paragraph(f"Metric: {escape(metric_name)}", styles["Normal"]))
    story.append(
        Paragraph(
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            styles["Normal"],
        )
    )
    story.append(
        Paragraph(
            "Rows scanned: "
            f"{stats.get('total_rows', 0)}, eligible rows: {stats.get('eligible_rows', 0)}, "
            f"missing sources skipped: {stats.get('skipped_missing_source', 0)}, "
            f"outside input_dir skipped: {stats.get('skipped_outside_input_dir', 0)}",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    users = sorted({key[0] for key in groups}, key=str.lower)
    filters = [
        filter_name
        for filter_name in sorted(VALID_FILTERS, key=lambda value: FILTER_ORDER[value])
        if any(key[2] == filter_name for key in groups)
    ]
    table_data = [["User"] + filters + ["Total"]]
    column_totals = {filter_name: 0 for filter_name in filters}
    grand_total = 0
    for user_name in users:
        row = [user_name]
        row_total = 0
        for filter_name in filters:
            count = sum(
                len(paths)
                for (group_user, _equipment, group_filter), paths in groups.items()
                if group_user == user_name and group_filter == filter_name
            )
            row.append(count)
            row_total += count
            column_totals[filter_name] += count
        row.append(row_total)
        grand_total += row_total
        table_data.append(row)
    table_data.append(["TOTAL"] + [column_totals[filter_name] for filter_name in filters] + [grand_total])

    summary_table = Table(table_data, repeatRows=1)
    summary_table.setStyle(table_style())
    story.append(summary_table)
    story.append(PageBreak())


def render_preview_for_path(
    args: tuple[Path, Path, str, float],
) -> tuple[Path, Path]:
    best_path, previews_dir, siril_path, siril_timeout = args
    return best_path, previews.render_preview(best_path, previews_dir, siril_path, siril_timeout)


def render_group_previews(
    best_paths: list[Path],
    *,
    previews_dir: Path,
    siril_path: str,
    siril_timeout: float,
    workers: int,
) -> dict[Path, Path]:
    preview_map: dict[Path, Path] = {}
    total = len(best_paths)
    if total == 0:
        return preview_map

    print(f"Rendering {total} group preview(s) with {workers} worker(s)...", file=sys.stderr)

    if workers == 1 or total == 1:
        for index, best_path in enumerate(best_paths, start=1):
            print(f"Rendering preview {index:3d}/{total:3d}: {best_path.name}", file=sys.stderr)
            try:
                preview_map[best_path] = previews.render_preview(
                    best_path, previews_dir, siril_path, siril_timeout
                )
            except Exception as exc:
                print(f"Warning: failed to render preview for {best_path}: {exc}", file=sys.stderr)
        return preview_map

    max_workers = min(workers, total)
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(
                render_preview_for_path, (best_path, previews_dir, siril_path, siril_timeout)
            ): best_path
            for best_path in best_paths
        }
        for index, future in enumerate(as_completed(future_to_path), start=1):
            best_path = future_to_path[future]
            print(f"Rendered preview {index:3d}/{total:3d}: {best_path.name}", file=sys.stderr)
            try:
                _path, preview_png = future.result()
                preview_map[best_path] = preview_png
            except Exception as exc:
                print(f"Warning: failed to render preview for {best_path}: {exc}", file=sys.stderr)
    return preview_map


def add_user_sections(
    story: list,
    *,
    styles,
    groups: dict[tuple[str, str, str], list[Path]],
    measurements: dict[Path, dict[str, float | int]],
    previews_dir: Path,
    cdf_dir: Path,
    siril_path: str,
    siril_timeout: float,
    min_frames: int,
    workers: int,
) -> None:
    users = sorted({key[0] for key in groups}, key=str.lower)

    # First pass: compute per-group stats and CDF plots, and collect the unique
    # best-frame paths whose previews need rendering. This keeps the slow Siril
    # preview rendering out of the (single-threaded) story-assembly loop.
    user_blocks: dict[str, list[dict[str, object]]] = {}
    best_paths_to_render: list[Path] = []
    seen_best_paths: set[Path] = set()

    for user_name in users:
        user_group_keys = [
            key
            for key in sorted(groups.keys(), key=sort_group_key)
            if key[0] == user_name and len(groups[key]) >= min_frames
        ]
        blocks: list[dict[str, object]] = []
        for _user, equipment_name, filter_name in user_group_keys:
            paths = groups[(user_name, equipment_name, filter_name)]
            finite_score_rows = group_score_rows(paths, measurements)
            if not finite_score_rows:
                blocks.append(
                    {
                        "kind": "no_scores",
                        "equipment": equipment_name,
                        "filter": filter_name,
                        "n": len(paths),
                    }
                )
                continue

            best_path, best_score = max(finite_score_rows, key=lambda item: item[1])
            scores = [score for _path, score in finite_score_rows]
            median_score = float(np.median(scores))
            p10 = float(np.percentile(scores, 10))
            n_below_p10 = sum(1 for score in scores if score < p10)

            cdf_path = make_cdf_plot(
                user_name=user_name,
                equipment_name=equipment_name,
                filter_name=filter_name,
                paths=paths,
                measurements=measurements,
                best_path=best_path,
                best_score=best_score,
                cdf_dir=cdf_dir,
            )

            blocks.append(
                {
                    "kind": "detail",
                    "equipment": equipment_name,
                    "filter": filter_name,
                    "n": len(paths),
                    "best_path": best_path,
                    "best_score": best_score,
                    "median_score": median_score,
                    "p10": p10,
                    "n_below_p10": n_below_p10,
                    "cdf_path": cdf_path,
                }
            )
            if best_path not in seen_best_paths:
                seen_best_paths.add(best_path)
                best_paths_to_render.append(best_path)

        user_blocks[user_name] = blocks

    preview_map = render_group_previews(
        best_paths_to_render,
        previews_dir=previews_dir,
        siril_path=siril_path,
        siril_timeout=siril_timeout,
        workers=workers,
    )

    # Second pass: assemble the story using the precomputed stats and previews.
    for user_index, user_name in enumerate(users):
        if user_index > 0:
            story.append(PageBreak())
        story.append(Paragraph(escape(user_name), styles["Heading1"]))

        blocks = user_blocks[user_name]
        if not blocks:
            story.append(Paragraph("No groups meet the minimum frame count for detail pages.", styles["Normal"]))
            continue

        for block in blocks:
            equipment_name = block["equipment"]
            filter_name = block["filter"]
            if block["kind"] == "no_scores":
                story.append(
                    Paragraph(
                        f"{escape(equipment_name)} / {escape(filter_name)} - n={block['n']}: no valid scores",
                        styles["Heading2"],
                    )
                )
                story.append(Spacer(1, 0.2 * inch))
                continue

            best_path = block["best_path"]
            preview_cell: Paragraph | Image
            preview_png = preview_map.get(best_path)
            if preview_png is not None:
                preview_cell = sized_image(preview_png, 3 * inch)
            else:
                preview_cell = Paragraph("preview unavailable", styles["Normal"])

            caption = (
                f"{escape(equipment_name)} / {escape(filter_name)} - n={block['n']}, "
                f"best score={block['best_score']:.4g}, median={block['median_score']:.4g}, "
                f"p10={block['p10']:.4g}, below-p10={block['n_below_p10']}; best: {escape(best_path.name)}"
            )
            story.append(Paragraph(caption, styles["Heading2"]))
            image_table = Table(
                [[preview_cell, sized_image(block["cdf_path"], 4 * inch)]],
                colWidths=[3.2 * inch, 4.2 * inch],
            )
            image_table.setStyle(image_table_style())
            story.append(image_table)
            story.append(Spacer(1, 0.2 * inch))


def build_pdf(
    *,
    output_path: Path,
    report_path: Path,
    metric_name: str,
    groups: dict[tuple[str, str, str], list[Path]],
    stats: dict[str, int],
    measurements: dict[Path, dict[str, float | int]],
    previews_dir: Path,
    cdf_dir: Path,
    siril_path: str,
    siril_timeout: float,
    min_frames: int,
    workers: int,
) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    story = []
    add_summary_page(
        story,
        styles=styles,
        report_path=report_path,
        metric_name=metric_name,
        groups=groups,
        stats=stats,
    )
    add_user_sections(
        story,
        styles=styles,
        groups=groups,
        measurements=measurements,
        previews_dir=previews_dir,
        cdf_dir=cdf_dir,
        siril_path=siril_path,
        siril_timeout=siril_timeout,
        min_frames=min_frames,
        workers=workers,
    )
    doc.build(story)


def main() -> None:
    args = parse_args()
    report_path = args.report.expanduser().resolve()
    if not report_path.exists():
        print(f"Error: report file does not exist: {report_path}", file=sys.stderr)
        sys.exit(1)
    if args.workers < 1:
        print("Error: --workers must be at least 1", file=sys.stderr)
        sys.exit(1)
    if args.min_frames < 1:
        print("Error: --min-frames must be at least 1", file=sys.stderr)
        sys.exit(1)

    out_dir = (
        args.out_dir.expanduser().resolve()
        if args.out_dir is not None
        else (report_path.parent / "quality_report").resolve()
    )
    previews_dir = out_dir / "previews"
    cdf_dir = out_dir / "cdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)
    cdf_dir.mkdir(parents=True, exist_ok=True)

    groups, stats = parse_report(report_path)
    if not groups:
        print(f"No eligible groups found in {report_path}")
        return

    metric_name = args.metric
    all_paths = sorted({sub_path.resolve() for paths in groups.values() for sub_path in paths})
    siril_path = siril.get_siril_path(args.siril_path)
    measurements = measure_all_paths(
        all_paths,
        metric_name=metric_name,
        siril_path=siril_path,
        timeout=args.siril_timeout,
        workers=args.workers,
        cache_path=out_dir / "measurements_cache.json",
        force=args.force,
        flush_interval=args.cache_flush_interval,
    )
    write_measurements_csv(out_dir / "measurements.csv", groups, measurements)

    pdf_path = out_dir / "quality_report.pdf"
    build_pdf(
        output_path=pdf_path,
        report_path=report_path,
        metric_name=metric_name,
        groups=groups,
        stats=stats,
        measurements=measurements,
        previews_dir=previews_dir,
        cdf_dir=cdf_dir,
        siril_path=siril_path,
        siril_timeout=args.siril_timeout,
        min_frames=args.min_frames,
        workers=args.workers,
    )
    print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    main()
