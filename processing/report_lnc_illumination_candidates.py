#!/usr/bin/env python3
"""Find and curate large-scale illumination stress targets for LNC testing."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import resource
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import features, previews, siril

DEFAULT_REPORT = Path("/scratch/joydeepb/astro_temp/markarians/moony_subs/filter_lookup_report.json")
DEFAULT_MEASUREMENTS = Path("/scratch/joydeepb/astro_temp/markarians/moony_subs/quality_report/measurements.csv")
DEFAULT_DATASET_ROOT = Path("/scratch/joydeepb/astro_temp/markarians/markarians_LNC_test")
DEFAULT_OUTPUT_DIR = Path("/scratch/joydeepb/astro_temp/markarians/moony_subs/quality_report/lnc_illumination")

QUALITY_FIELDS = ("score", "star_count", "median_mean_star_flux", "background", "bgnoise")
ILLUM_FIELDS = (
    "illum_median",
    "illum_range_frac",
    "illum_mad_frac",
    "plane_gradient_frac",
    "plane_r2",
    "patchiness_frac",
    "corner_delta_frac",
    "valid_tile_fraction",
)
ROLE_PRIORITY = {
    "illum_reference_rank_1": 0,
    "illum_reference_rank_2": 1,
    "illum_reference_rank_3": 2,
    "illum_gradient_high": 10,
    "illum_patchy_high": 11,
    "illum_range_high": 12,
    "bright_gradient": 13,
    "cloud_suspect": 14,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure spatial background variation and create targeted LNC illumination pairs."
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--measurements", type=Path, default=DEFAULT_MEASUREMENTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--siril-path", type=Path, default=None)
    parser.add_argument("--siril-timeout", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=min(164, os.cpu_count() or 1))
    parser.add_argument("--preview-workers", type=int, default=min(164, os.cpu_count() or 1))
    parser.add_argument("--tile-grid", type=int, default=24)
    parser.add_argument("--min-valid-tile-fraction", type=float, default=0.45)
    parser.add_argument("--cache-flush-interval", type=int, default=100)
    parser.add_argument("--max-groups", type=int, default=12)
    parser.add_argument("--refs-per-group", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="Recompute cached illumination metrics.")
    parser.add_argument("--skip-scan", action="store_true", help="Use existing lnc_illumination_metrics.csv.")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--skip-curation", action="store_true")
    parser.add_argument("--compare-lnc", action="store_true", help="Compare target originals to LNC outputs.")
    parser.add_argument("--pairs", default="lnc_pairs_illumination.csv")
    parser.add_argument("--run-summary", type=Path, default=None)
    parser.add_argument("--max-compare-workers", type=int, default=164)
    return parser.parse_args()


def safe_name(value: str) -> str:
    return psq.sanitize_label(str(value))


def group_dir_name(user: str, equipment: str, filter_name: str) -> str:
    return f"{safe_name(user)}__{safe_name(equipment)}__{safe_name(filter_name)}"


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def finite_or_nan(value: object) -> float:
    number = finite_float(value)
    return float("nan") if number is None else number


def percentile(values: list[float], q: float) -> float | None:
    finite = sorted(v for v in values if math.isfinite(v))
    if not finite:
        return None
    index = round(q * (len(finite) - 1))
    return finite[index]


def percentile_rank(sorted_values: list[float], value: object) -> float | None:
    number = finite_float(value)
    if number is None or not sorted_values:
        return None
    below_or_equal = sum(1 for item in sorted_values if item <= number)
    if len(sorted_values) == 1:
        return 1.0
    return (below_or_equal - 1) / (len(sorted_values) - 1)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_measurements(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = dict(row)
            for field in QUALITY_FIELDS:
                parsed[field] = finite_float(row.get(field))
            rows.append(parsed)
    return rows


def load_report_rows(path: Path) -> dict[str, dict[str, Any]]:
    report = read_json(path)
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Report is missing rows list: {path}")
    return {
        str(Path(str(row.get("source_file", ""))).expanduser()): row
        for row in rows
        if isinstance(row, dict) and row.get("source_file")
    }


def source_to_process(report_row: dict[str, Any] | None, original_source: Path) -> Path:
    if report_row:
        collection_file = str(report_row.get("collection_file") or "").strip()
        if collection_file:
            collection_path = Path(collection_file).expanduser()
            if collection_path.exists():
                return collection_path
    return original_source


def image_info(path: Path) -> dict[str, Any]:
    info = {"image_width": None, "image_height": None, "bitpix": None, "exposure": None}
    try:
        with fits.open(path, memmap=True) as hdul:
            header = hdul[0].header
            info.update(
                {
                    "image_width": header.get("NAXIS1"),
                    "image_height": header.get("NAXIS2"),
                    "bitpix": header.get("BITPIX"),
                    "exposure": header.get("EXPTIME") or header.get("EXPOSURE"),
                }
            )
    except Exception as exc:
        info["image_error"] = f"{type(exc).__name__}: {exc}"
    return info


def build_work_rows(
    measurements: list[dict[str, Any]],
    report_rows: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for measurement in measurements:
        original = Path(str(measurement["sub_path"])).expanduser()
        report_row = report_rows.get(str(original))
        process_path = source_to_process(report_row, original)
        if not process_path.exists():
            continue
        row = {
            **measurement,
            "original_source_file": str(original),
            "process_file": str(process_path),
            "group_dir": group_dir_name(str(measurement["user"]), str(measurement["equipment"]), str(measurement["filter"])),
        }
        row.update(image_info(process_path))
        rows.append(row)
    return rows


def cache_key(row: dict[str, Any]) -> str:
    return str(Path(row["process_file"]).resolve())


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = read_json(path)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_cache(path: Path, cache: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    write_json(tmp, cache)
    os.replace(tmp, path)


def star_mask(shape: tuple[int, int], stars: list[features.SirilStar], radius_scale: float = 4.0) -> np.ndarray:
    height, width = shape
    mask = np.zeros(shape, dtype=bool)
    for star in stars:
        radius = max(8.0, min(45.0, radius_scale * float(star.fwhm)))
        for y_center in (float(star.y), float(height - 1) - float(star.y)):
            x_min = max(int(math.floor(star.x - radius)), 0)
            x_max = min(int(math.ceil(star.x + radius)) + 1, width)
            y_min = max(int(math.floor(y_center - radius)), 0)
            y_max = min(int(math.ceil(y_center + radius)) + 1, height)
            if x_min >= x_max or y_min >= y_max:
                continue
            yy, xx = np.ogrid[y_min:y_max, x_min:x_max]
            local = ((xx - star.x) ** 2 + (yy - y_center) ** 2) <= radius**2
            mask[y_min:y_max, x_min:x_max] |= local
    return mask


def tile_background_surface(
    image: np.ndarray,
    stars: list[features.SirilStar],
    *,
    tile_grid: int,
    min_valid_fraction: float,
) -> np.ndarray:
    image = np.asarray(image, dtype=np.float32)
    if image.ndim != 2:
        raise ValueError(f"Expected 2D image, got {image.shape}")
    height, width = image.shape
    mask = star_mask(image.shape, stars)
    tile_values = np.full((tile_grid, tile_grid), np.nan, dtype=np.float32)
    y_edges = np.linspace(0, height, tile_grid + 1, dtype=int)
    x_edges = np.linspace(0, width, tile_grid + 1, dtype=int)
    for yi in range(tile_grid):
        y0, y1 = y_edges[yi], y_edges[yi + 1]
        for xi in range(tile_grid):
            x0, x1 = x_edges[xi], x_edges[xi + 1]
            pixels = image[y0:y1, x0:x1]
            local_mask = mask[y0:y1, x0:x1]
            valid = pixels[np.isfinite(pixels) & ~local_mask]
            if valid.size < max(64, int(min_valid_fraction * pixels.size)):
                continue
            lo, hi = np.nanpercentile(valid, [10, 90])
            clipped = valid[(valid >= lo) & (valid <= hi)]
            if clipped.size:
                tile_values[yi, xi] = float(np.nanmedian(clipped))
    return tile_values


def fit_plane(tile_values: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    yy, xx = np.indices(tile_values.shape, dtype=np.float64)
    z = tile_values.astype(np.float64)
    valid = np.isfinite(z)
    if int(valid.sum()) < 3:
        return np.full(tile_values.shape, np.nan), {"plane_gradient_frac": math.nan, "plane_r2": math.nan}
    x = xx[valid] / max(tile_values.shape[1] - 1, 1)
    y = yy[valid] / max(tile_values.shape[0] - 1, 1)
    values = z[valid]
    design = np.column_stack([x, y, np.ones_like(x)])
    coeffs, *_ = np.linalg.lstsq(design, values, rcond=None)
    plane = coeffs[0] * (xx / max(tile_values.shape[1] - 1, 1)) + coeffs[1] * (
        yy / max(tile_values.shape[0] - 1, 1)
    ) + coeffs[2]
    corners = np.array([coeffs[2], coeffs[0] + coeffs[2], coeffs[1] + coeffs[2], coeffs[0] + coeffs[1] + coeffs[2]])
    residual = values - (design @ coeffs)
    sst = float(np.sum((values - np.mean(values)) ** 2))
    sse = float(np.sum(residual**2))
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    median = float(np.nanmedian(values))
    denom = abs(median) if abs(median) > 1.0e-9 else 1.0
    return plane.astype(np.float32), {
        "plane_gradient_frac": float((np.nanmax(corners) - np.nanmin(corners)) / denom),
        "plane_r2": float(max(0.0, min(1.0, r2))),
    }


def robust_mad(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return math.nan
    med = float(np.nanmedian(values))
    return float(np.nanmedian(np.abs(values - med)))


def corner_delta(tile_values: np.ndarray) -> float:
    h, w = tile_values.shape
    corners = [
        tile_values[: h // 2, : w // 2],
        tile_values[: h // 2, w // 2 :],
        tile_values[h // 2 :, : w // 2],
        tile_values[h // 2 :, w // 2 :],
    ]
    medians = [float(np.nanmedian(corner)) for corner in corners if np.isfinite(corner).any()]
    if len(medians) < 2:
        return math.nan
    return float(max(medians) - min(medians))


def illumination_metrics_from_tiles(tile_values: np.ndarray) -> dict[str, float]:
    valid = tile_values[np.isfinite(tile_values)]
    if valid.size == 0:
        return {field: math.nan for field in ILLUM_FIELDS}
    median = float(np.nanmedian(valid))
    denom = abs(median) if abs(median) > 1.0e-9 else 1.0
    p5, p95 = np.nanpercentile(valid, [5, 95])
    plane, plane_metrics = fit_plane(tile_values)
    residual = tile_values - plane
    return {
        "illum_median": median,
        "illum_range_frac": float((p95 - p5) / denom),
        "illum_mad_frac": float(1.4826 * robust_mad(valid) / denom),
        "plane_gradient_frac": plane_metrics["plane_gradient_frac"],
        "plane_r2": plane_metrics["plane_r2"],
        "patchiness_frac": float(1.4826 * robust_mad(residual) / denom),
        "corner_delta_frac": float(corner_delta(tile_values) / denom),
        "valid_tile_fraction": float(np.isfinite(tile_values).sum() / tile_values.size),
    }


def measure_one(payload: dict[str, Any]) -> dict[str, Any]:
    row = payload["row"]
    path = Path(row["process_file"])
    start = time.perf_counter()
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    try:
        stars, background, bgnoise = features.run_star_background_stats(path, payload["siril_path"], float(payload["timeout"]))
        image = features.read_measurement_image_data(path, payload["siril_path"], float(payload["timeout"]))
        tile_values = tile_background_surface(
            image,
            stars,
            tile_grid=int(payload["tile_grid"]),
            min_valid_fraction=float(payload["min_valid_tile_fraction"]),
        )
        metrics = illumination_metrics_from_tiles(tile_values)
        status = "success"
        error = ""
        height, width = image.shape
    except Exception as exc:
        metrics = {field: math.nan for field in ILLUM_FIELDS}
        stars = []
        background = math.nan
        bgnoise = math.nan
        width = row.get("image_width")
        height = row.get("image_height")
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    return {
        **row,
        **metrics,
        "measured_star_count": len(stars),
        "measured_background": background,
        "measured_bgnoise": bgnoise,
        "image_width": width,
        "image_height": height,
        "illum_status": status,
        "illum_error": error,
        "illum_wall_seconds": time.perf_counter() - start,
        "illum_cpu_user_seconds": max(0.0, usage_after.ru_utime - usage_before.ru_utime),
        "illum_cpu_system_seconds": max(0.0, usage_after.ru_stime - usage_before.ru_stime),
        "illum_max_rss_kb": usage_after.ru_maxrss,
    }


def cache_entry_valid(row: dict[str, Any], entry: object, force: bool) -> bool:
    if force or not isinstance(entry, dict):
        return False
    path = Path(row["process_file"])
    try:
        return abs(float(entry["mtime"]) - path.stat().st_mtime) < 1.0e-6 and isinstance(entry.get("row"), dict)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def measure_rows(
    rows: list[dict[str, Any]],
    *,
    output_dir: Path,
    siril_path: str,
    timeout: float,
    workers: int,
    tile_grid: int,
    min_valid_tile_fraction: float,
    force: bool,
    flush_interval: int,
) -> list[dict[str, Any]]:
    cache_path = output_dir / "lnc_illumination_metric_cache.json"
    cache = load_cache(cache_path)
    results_by_key: dict[str, dict[str, Any]] = {}
    pending = []
    for row in rows:
        key = cache_key(row)
        entry = cache.get(key)
        if cache_entry_valid(row, entry, force):
            results_by_key[key] = dict(entry["row"])
        else:
            pending.append(row)

    if pending:
        print(f"Illumination scan: {len(results_by_key)} cached, {len(pending)} to measure.", file=sys.stderr)
    else:
        print(f"Illumination scan: all {len(results_by_key)} rows loaded from cache.", file=sys.stderr)

    completed_since_flush = 0

    def store_result(result: dict[str, Any]) -> None:
        nonlocal completed_since_flush
        key = cache_key(result)
        results_by_key[key] = result
        cache[key] = {"mtime": Path(result["process_file"]).stat().st_mtime, "row": result}
        completed_since_flush += 1
        if flush_interval > 0 and completed_since_flush >= flush_interval:
            write_cache(cache_path, cache)
            completed_since_flush = 0

    payloads = [
        {
            "row": row,
            "siril_path": siril_path,
            "timeout": timeout,
            "tile_grid": tile_grid,
            "min_valid_tile_fraction": min_valid_tile_fraction,
        }
        for row in pending
    ]
    if payloads:
        if workers == 1 or len(payloads) == 1:
            for index, payload in enumerate(payloads, start=1):
                print(f"Illumination {index}/{len(payloads)}: {Path(payload['row']['process_file']).name}", file=sys.stderr)
                store_result(measure_one(payload))
        else:
            with ProcessPoolExecutor(max_workers=min(workers, len(payloads))) as executor:
                future_map = {executor.submit(measure_one, payload): payload for payload in payloads}
                for index, future in enumerate(as_completed(future_map), start=1):
                    payload = future_map[future]
                    print(
                        f"Illumination {index}/{len(payloads)} done: {Path(payload['row']['process_file']).name}",
                        file=sys.stderr,
                    )
                    store_result(future.result())
        write_cache(cache_path, cache)
    return [results_by_key[cache_key(row)] for row in rows if cache_key(row) in results_by_key]


def add_group_percentiles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, object, object], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row["user"]), str(row["equipment"]), str(row["filter"]), row.get("image_width"), row.get("image_height"))
        grouped[key].append(row)
        row["selection_group_key"] = "::".join(str(part) for part in key)

    fields = list(QUALITY_FIELDS) + list(ILLUM_FIELDS)
    for group_rows in grouped.values():
        sorted_by_field = {
            field: sorted(float(row[field]) for row in group_rows if finite_float(row.get(field)) is not None)
            for field in fields
        }
        for row in group_rows:
            for field in fields:
                row[f"{field}_pct"] = percentile_rank(sorted_by_field[field], row.get(field))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def metrics_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    base = [
        "user",
        "equipment",
        "filter",
        "group_dir",
        "selection_group_key",
        "sub_path",
        "original_source_file",
        "process_file",
        "image_width",
        "image_height",
        "bitpix",
        "exposure",
        *QUALITY_FIELDS,
        *ILLUM_FIELDS,
        *(f"{field}_pct" for field in (*QUALITY_FIELDS, *ILLUM_FIELDS)),
        "measured_star_count",
        "measured_background",
        "measured_bgnoise",
        "illum_status",
        "illum_error",
        "illum_wall_seconds",
        "illum_cpu_user_seconds",
        "illum_cpu_system_seconds",
        "illum_max_rss_kb",
    ]
    extra = sorted({key for row in rows for key in row.keys()} - set(base))
    return base + extra


def load_metrics_csv(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = dict(row)
            for field in (*QUALITY_FIELDS, *ILLUM_FIELDS):
                parsed[field] = finite_float(row.get(field))
            for field in [key for key in row if key.endswith("_pct")]:
                parsed[field] = finite_float(row.get(field))
            rows.append(parsed)
    return rows


def role_score(row: dict[str, Any], role: str) -> tuple[float, str]:
    if role == "illum_gradient_high":
        score = (finite_or_nan(row.get("plane_gradient_frac_pct")) + finite_or_nan(row.get("plane_r2_pct"))) / 2.0
    elif role == "illum_patchy_high":
        score = finite_or_nan(row.get("patchiness_frac_pct"))
    elif role == "illum_range_high":
        score = finite_or_nan(row.get("illum_range_frac_pct"))
    elif role == "bright_gradient":
        score = 0.5 * finite_or_nan(row.get("background_pct")) + 0.5 * finite_or_nan(row.get("plane_gradient_frac_pct"))
    elif role == "cloud_suspect":
        star_low = 1.0 - (finite_float(row.get("star_count_pct")) or 0.5)
        flux_low = 1.0 - (finite_float(row.get("median_mean_star_flux_pct")) or 0.5)
        score = 0.55 * finite_or_nan(row.get("patchiness_frac_pct")) + 0.225 * star_low + 0.225 * flux_low
    else:
        score = -math.inf
    if not math.isfinite(score):
        score = -math.inf
    return score, str(row.get("process_file", ""))


def reference_score(row: dict[str, Any]) -> tuple[float, str]:
    score_pct = finite_float(row.get("score_pct")) or 0.0
    flatness = 1.0 - max(
        finite_float(row.get("illum_range_frac_pct")) or 0.5,
        finite_float(row.get("plane_gradient_frac_pct")) or 0.5,
        finite_float(row.get("patchiness_frac_pct")) or 0.5,
    )
    valid = finite_float(row.get("valid_tile_fraction")) or 0.0
    star_pct = finite_float(row.get("star_count_pct")) or 0.0
    return 0.55 * score_pct + 0.30 * flatness + 0.10 * valid + 0.05 * star_pct, str(row.get("process_file", ""))


def target_strength(row: dict[str, Any]) -> float:
    return max(
        finite_float(row.get("illum_range_frac_pct")) or 0.0,
        finite_float(row.get("plane_gradient_frac_pct")) or 0.0,
        finite_float(row.get("patchiness_frac_pct")) or 0.0,
        0.5 * (finite_float(row.get("background_pct")) or 0.0)
        + 0.5 * (finite_float(row.get("plane_gradient_frac_pct")) or 0.0),
    )


def add_role(selection: dict[str, dict[str, Any]], row: dict[str, Any], role: str) -> None:
    key = str(row["process_file"])
    if key not in selection:
        selection[key] = {**row, "roles": [role]}
    elif role not in selection[key]["roles"]:
        selection[key]["roles"].append(role)


def select_group_records(rows: list[dict[str, Any]], refs_per_group: int) -> list[dict[str, Any]]:
    usable = [
        row
        for row in rows
        if row.get("illum_status") == "success"
        and (finite_float(row.get("valid_tile_fraction")) or 0.0) >= 0.45
        and finite_float(row.get("score")) is not None
    ]
    if len(usable) < 3:
        return []
    selection: dict[str, dict[str, Any]] = {}
    refs = sorted(usable, key=reference_score, reverse=True)[:refs_per_group]
    for index, row in enumerate(refs, start=1):
        add_role(selection, row, f"illum_reference_rank_{index}")
    ref_paths = {row["process_file"] for row in refs}
    targets = [row for row in usable if row["process_file"] not in ref_paths]
    for role in ("illum_gradient_high", "illum_patchy_high", "illum_range_high", "bright_gradient", "cloud_suspect"):
        if not targets:
            continue
        row = max(targets, key=lambda candidate, role=role: role_score(candidate, role))
        if role_score(row, role)[0] > -math.inf:
            add_role(selection, row, role)
    return list(selection.values())


def select_targeted_records(
    rows: list[dict[str, Any]],
    *,
    refs_per_group: int,
    max_groups: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("selection_group_key") or row.get("group_dir"))].append(row)

    candidates: list[tuple[float, str, list[dict[str, Any]]]] = []
    for key, group_rows in grouped.items():
        selected = select_group_records(group_rows, refs_per_group)
        target_rows = [row for row in selected if not any(role.startswith("illum_reference") for role in row["roles"])]
        if not target_rows:
            continue
        group_strength = max(target_strength(row) for row in target_rows)
        candidates.append((group_strength, key, selected))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    output: list[dict[str, Any]] = []
    for _strength, _key, selected in candidates[:max_groups]:
        output.extend(selected)
    for row in output:
        row["primary_role"] = min(row["roles"], key=lambda role: ROLE_PRIORITY.get(role, 99))
    return sorted(
        output,
        key=lambda row: (
            str(row["group_dir"]),
            str(row.get("selection_group_key", "")),
            ROLE_PRIORITY.get(str(row.get("primary_role")), 99),
            str(row["process_file"]),
        ),
    )


def unique_destination_path(group_dir: Path, base_name: str, used: set[Path]) -> Path:
    candidate = group_dir / base_name
    if candidate not in used and not candidate.exists():
        used.add(candidate)
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while True:
        next_candidate = group_dir / f"{stem}_{index}{suffix}"
        if next_candidate not in used and not next_candidate.exists():
            used.add(next_candidate)
            return next_candidate
        index += 1


def copy_selected_records(records: list[dict[str, Any]], dataset_root: Path) -> list[dict[str, Any]]:
    output = []
    used: set[Path] = set()
    for index, row in enumerate(records, start=1):
        source = Path(row["process_file"])
        role = str(row["primary_role"])
        group_dir = dataset_root / "subs" / str(row["group_dir"])
        destination = unique_destination_path(group_dir, f"illum_{index:03d}_{safe_name(role)}_{safe_name(source.stem)}{source.suffix.lower()}", used)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.stat().st_size != source.stat().st_size:
            shutil.copy2(source, destination)
        output.append(
            {
                **row,
                "copied_file": str(destination),
                "relative_copied_file": str(destination.relative_to(dataset_root)),
            }
        )
    return output


def write_manifest(records: list[dict[str, Any]], dataset_root: Path) -> None:
    fieldnames = [
        "group_dir",
        "selection_group_key",
        "user",
        "equipment",
        "filter",
        "roles",
        "primary_role",
        "original_source_file",
        "process_file",
        "copied_file",
        "relative_copied_file",
        "image_width",
        "image_height",
        *QUALITY_FIELDS,
        *ILLUM_FIELDS,
        *(f"{field}_pct" for field in (*QUALITY_FIELDS, *ILLUM_FIELDS)),
    ]
    rows = [{**row, "roles": ";".join(row["roles"])} for row in records]
    write_csv(dataset_root / "selection_manifest_illumination.csv", rows, fieldnames)
    write_json(dataset_root / "selection_manifest_illumination.json", rows)


def write_pairs(records: list[dict[str, Any]], dataset_root: Path) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row["selection_group_key"])].append(row)
    pairs = []
    for _key, group_records in sorted(grouped.items()):
        refs = [row for row in group_records if any(role.startswith("illum_reference") for role in row["roles"])]
        targets = [row for row in group_records if row not in refs]
        for ref in refs:
            for target in targets:
                pairs.append(
                    {
                        "group_dir": ref["group_dir"],
                        "user": ref["user"],
                        "equipment": ref["equipment"],
                        "filter": ref["filter"],
                        "reference_roles": ";".join(ref["roles"]),
                        "reference_file": ref["copied_file"],
                        "reference_relative_file": ref["relative_copied_file"],
                        "reference_score": ref.get("score"),
                        "target_roles": ";".join(target["roles"]),
                        "target_file": target["copied_file"],
                        "target_relative_file": target["relative_copied_file"],
                        "target_score": target.get("score"),
                        "target_illum_range_frac": target.get("illum_range_frac"),
                        "target_plane_gradient_frac": target.get("plane_gradient_frac"),
                        "target_patchiness_frac": target.get("patchiness_frac"),
                    }
                )
    fieldnames = [
        "group_dir",
        "user",
        "equipment",
        "filter",
        "reference_roles",
        "reference_file",
        "reference_relative_file",
        "reference_score",
        "target_roles",
        "target_file",
        "target_relative_file",
        "target_score",
        "target_illum_range_frac",
        "target_plane_gradient_frac",
        "target_patchiness_frac",
    ]
    write_csv(dataset_root / "lnc_pairs_illumination.csv", pairs, fieldnames)
    return pairs


def render_source_preview(source: Path, dest: Path, siril_path: str, timeout: float) -> Path | None:
    if not source.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        rendered = previews.render_preview(source, dest.parent, siril_path, timeout)
        if rendered.resolve() != dest.resolve():
            shutil.copy2(rendered, dest)
        return dest
    except Exception:
        return None


def save_map(path: Path, values: np.ndarray, title: str, cmap: str = "viridis") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(5.2, 4.2))
    image = ax.imshow(values, origin="lower", cmap=cmap)
    ax.set_title(title)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.colorbar(image, ax=ax, shrink=0.82)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def artifact_worker(payload: dict[str, Any]) -> dict[str, str]:
    row = payload["row"]
    source = Path(row["process_file"])
    role = str(row["primary_role"])
    group = str(row["group_dir"])
    base = f"{safe_name(role)}__{safe_name(source.stem)}"
    output_dir = Path(payload["output_dir"])
    preview_path = output_dir / "candidate_jpegs" / group / f"{base}.jpg"
    background_map = output_dir / "background_maps" / group / f"{base}.png"
    residual_map = output_dir / "residual_maps" / group / f"{base}.png"
    render_source_preview(source, preview_path, payload["siril_path"], float(payload["timeout"]))
    try:
        stars, _background, _bgnoise = features.run_star_background_stats(source, payload["siril_path"], float(payload["timeout"]))
        image = features.read_measurement_image_data(source, payload["siril_path"], float(payload["timeout"]))
        tile_values = tile_background_surface(
            image,
            stars,
            tile_grid=int(payload["tile_grid"]),
            min_valid_fraction=float(payload["min_valid_tile_fraction"]),
        )
        plane, _plane_metrics = fit_plane(tile_values)
        save_map(background_map, tile_values, f"{role} background")
        save_map(residual_map, tile_values - plane, f"{role} residual", cmap="coolwarm")
    except Exception as exc:
        return {"preview": str(preview_path), "background_map": "", "residual_map": "", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "preview": str(preview_path),
        "background_map": str(background_map),
        "residual_map": str(residual_map),
        "error": "",
    }


def generate_artifacts(
    records: list[dict[str, Any]],
    *,
    output_dir: Path,
    siril_path: str,
    timeout: float,
    preview_workers: int,
    tile_grid: int,
    min_valid_tile_fraction: float,
) -> list[dict[str, Any]]:
    payloads = [
        {
            "row": row,
            "output_dir": str(output_dir),
            "siril_path": siril_path,
            "timeout": timeout,
            "tile_grid": tile_grid,
            "min_valid_tile_fraction": min_valid_tile_fraction,
        }
        for row in records
    ]
    artifacts_by_path: dict[str, dict[str, str]] = {}
    if preview_workers == 1 or len(payloads) <= 1:
        for index, payload in enumerate(payloads, start=1):
            print(f"Artifact {index}/{len(payloads)}: {Path(payload['row']['process_file']).name}", file=sys.stderr)
            artifacts_by_path[payload["row"]["process_file"]] = artifact_worker(payload)
    else:
        with ProcessPoolExecutor(max_workers=min(preview_workers, len(payloads))) as executor:
            future_map = {executor.submit(artifact_worker, payload): payload for payload in payloads}
            for index, future in enumerate(as_completed(future_map), start=1):
                payload = future_map[future]
                print(f"Artifact {index}/{len(payloads)} done: {Path(payload['row']['process_file']).name}", file=sys.stderr)
                artifacts_by_path[payload["row"]["process_file"]] = future.result()
    output = []
    for row in records:
        output.append({**row, **{f"artifact_{key}": value for key, value in artifacts_by_path.get(row["process_file"], {}).items()}})
    return output


def sized_image(path: Path, max_width: float) -> Image | Paragraph:
    if not path.exists() or not path.is_file():
        return Paragraph("missing", getSampleStyleSheet()["Normal"])
    width, height = ImageReader(str(path)).getSize()
    scale = max_width / float(width)
    return Image(str(path), width=max_width, height=height * scale)


def fmt(value: object, digits: int = 3) -> str:
    number = finite_float(value)
    if number is None:
        return "n/a"
    if abs(number) >= 100:
        return f"{number:.0f}"
    if abs(number) >= 10:
        return f"{number:.1f}"
    return f"{number:.{digits}g}"


def build_candidate_pdf(records: list[dict[str, Any]], output_dir: Path) -> Path:
    pdf_path = output_dir / "lnc_illumination_candidates.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(letter),
        leftMargin=0.35 * inch,
        rightMargin=0.35 * inch,
        topMargin=0.35 * inch,
        bottomMargin=0.35 * inch,
    )
    story: list[Any] = []
    story.append(Paragraph("LNC Illumination Candidate Report", styles["Title"]))
    story.append(Paragraph(f"Generated: {datetime.now().isoformat(timespec='seconds')}", styles["Normal"]))
    story.append(Paragraph(f"Selected frames: {len(records)}", styles["Normal"]))
    story.append(PageBreak())

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[str(row["selection_group_key"])].append(row)
    for key, group_rows in sorted(grouped.items()):
        sample = group_rows[0]
        story.append(Paragraph(escape(str(sample["group_dir"])), styles["Heading1"]))
        story.append(
            Paragraph(
                f"Selection group: {escape(key)} | size {sample.get('image_width')}x{sample.get('image_height')}",
                styles["Normal"],
            )
        )
        for row in sorted(group_rows, key=lambda item: ROLE_PRIORITY.get(str(item.get("primary_role")), 99)):
            story.append(Paragraph(escape(" + ".join(row["roles"])), styles["Heading2"]))
            metric_line = (
                f"range={fmt(row.get('illum_range_frac'))}, gradient={fmt(row.get('plane_gradient_frac'))}, "
                f"patchiness={fmt(row.get('patchiness_frac'))}, background={fmt(row.get('background'))}, "
                f"stars={fmt(row.get('star_count'))}, score={fmt(row.get('score'))}"
            )
            story.append(Paragraph(metric_line, styles["Normal"]))
            preview = Path(str(row.get("artifact_preview") or ""))
            background_map = Path(str(row.get("artifact_background_map") or ""))
            residual_map = Path(str(row.get("artifact_residual_map") or ""))
            story.append(
                Table(
                    [["Source preview", "Background surface", "Plane residual"], [
                        sized_image(preview, 3.0 * inch),
                        sized_image(background_map, 3.0 * inch),
                        sized_image(residual_map, 3.0 * inch),
                    ]],
                    colWidths=[3.15 * inch, 3.15 * inch, 3.15 * inch],
                    style=TableStyle([
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ]),
                )
            )
            story.append(Spacer(1, 0.12 * inch))
        story.append(PageBreak())
    doc.build(story)
    return pdf_path


def read_pairs(dataset_root: Path, pairs_arg: str) -> list[dict[str, str]]:
    path = Path(pairs_arg)
    if not path.is_absolute():
        path = dataset_root / path
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def pair_id(row: dict[str, str]) -> str:
    def primary(roles: str) -> str:
        parts = [part for part in roles.split(";") if part]
        return min(parts, key=lambda role: ROLE_PRIORITY.get(role, 99)) if parts else "unknown"

    ref_stem = safe_name(Path(row["reference_file"]).stem)[:36]
    target_stem = safe_name(Path(row["target_file"]).stem)[:36]
    return f"{safe_name(primary(row['reference_roles']))}_{ref_stem}__{safe_name(primary(row['target_roles']))}_{target_stem}"


def measure_image_path_for_compare(payload: dict[str, Any]) -> dict[str, Any]:
    path = Path(payload["path"])
    start = time.perf_counter()
    try:
        stars, _background, _bgnoise = features.run_star_background_stats(path, payload["siril_path"], float(payload["timeout"]))
        image = features.read_measurement_image_data(path, payload["siril_path"], float(payload["timeout"]))
        tiles = tile_background_surface(
            image,
            stars,
            tile_grid=int(payload["tile_grid"]),
            min_valid_fraction=float(payload["min_valid_tile_fraction"]),
        )
        metrics = illumination_metrics_from_tiles(tiles)
        status = "success"
        error = ""
    except Exception as exc:
        metrics = {field: math.nan for field in ILLUM_FIELDS}
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    return {
        "path": str(path),
        "status": status,
        "error": error,
        "wall_seconds": time.perf_counter() - start,
        **metrics,
    }


def compare_lnc_outputs(
    *,
    dataset_root: Path,
    pairs_arg: str,
    siril_path: str,
    timeout: float,
    workers: int,
    tile_grid: int,
    min_valid_tile_fraction: float,
) -> list[dict[str, Any]]:
    pairs = read_pairs(dataset_root, pairs_arg)
    payloads = []
    path_to_label: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for row in pairs:
        pid = pair_id(row)
        corrected = dataset_root / "subs" / row["group_dir"] / "lnc_outputs" / pid / "corrected.fit"
        for label, path in (("original", Path(row["target_file"])), ("corrected", corrected)):
            path_to_label[str(path)].append((label, row))
            payloads.append(
                {
                    "path": str(path),
                    "siril_path": siril_path,
                    "timeout": timeout,
                    "tile_grid": tile_grid,
                    "min_valid_tile_fraction": min_valid_tile_fraction,
                }
            )
    unique_payloads = {payload["path"]: payload for payload in payloads}
    metrics_by_path = {}
    payload_list = list(unique_payloads.values())
    if workers == 1 or len(payload_list) <= 1:
        for index, payload in enumerate(payload_list, start=1):
            print(f"Compare metrics {index}/{len(payload_list)}: {Path(payload['path']).name}", file=sys.stderr)
            metrics_by_path[payload["path"]] = measure_image_path_for_compare(payload)
    else:
        with ProcessPoolExecutor(max_workers=min(workers, len(payload_list))) as executor:
            future_map = {executor.submit(measure_image_path_for_compare, payload): payload for payload in payload_list}
            for index, future in enumerate(as_completed(future_map), start=1):
                payload = future_map[future]
                print(f"Compare metrics {index}/{len(payload_list)} done: {Path(payload['path']).name}", file=sys.stderr)
                metrics_by_path[payload["path"]] = future.result()

    output = []
    for row in pairs:
        pid = pair_id(row)
        original = metrics_by_path.get(str(Path(row["target_file"])), {})
        corrected_path = dataset_root / "subs" / row["group_dir"] / "lnc_outputs" / pid / "corrected.fit"
        corrected = metrics_by_path.get(str(corrected_path), {})
        record = {
            "pair_id": pid,
            "group_dir": row["group_dir"],
            "target_roles": row["target_roles"],
            "target_file": row["target_file"],
            "corrected_file": str(corrected_path),
            "original_status": original.get("status"),
            "corrected_status": corrected.get("status"),
        }
        for field in ILLUM_FIELDS:
            before = finite_float(original.get(field))
            after = finite_float(corrected.get(field))
            record[f"original_{field}"] = before
            record[f"corrected_{field}"] = after
            record[f"delta_{field}"] = (after - before) if before is not None and after is not None else None
            record[f"reduction_{field}"] = ((before - after) / before) if before not in (None, 0.0) and after is not None else None
        output.append(record)
    return output


def host_context(workers: int, preview_workers: int, siril_path: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "workers": workers,
        "preview_workers": preview_workers,
        "siril_path": siril_path,
    }


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    dataset_root = args.dataset_root.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.workers < 1 or args.preview_workers < 1:
        print("Error: worker counts must be >= 1", file=sys.stderr)
        return 1

    siril_path = siril.get_siril_path(args.siril_path)
    metrics_path = output_dir / "lnc_illumination_metrics.csv"
    dataset_metrics_path = dataset_root / "lnc_illumination_metrics.csv"

    if args.skip_scan:
        rows = load_metrics_csv(metrics_path)
    else:
        report_rows = load_report_rows(args.report.expanduser().resolve())
        measurements = load_measurements(args.measurements.expanduser().resolve())
        work_rows = build_work_rows(measurements, report_rows)
        print(f"Prepared {len(work_rows)} illumination work row(s).", file=sys.stderr)
        rows = measure_rows(
            work_rows,
            output_dir=output_dir,
            siril_path=siril_path,
            timeout=args.siril_timeout,
            workers=args.workers,
            tile_grid=args.tile_grid,
            min_valid_tile_fraction=args.min_valid_tile_fraction,
            force=args.force,
            flush_interval=args.cache_flush_interval,
        )
        rows = add_group_percentiles(rows)
        write_csv(metrics_path, rows, metrics_fieldnames(rows))
        dataset_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metrics_path, dataset_metrics_path)
        write_json(
            output_dir / "lnc_illumination_scan_summary.json",
            {
                "host": host_context(args.workers, args.preview_workers, siril_path),
                "rows": len(rows),
                "successes": sum(1 for row in rows if row.get("illum_status") == "success"),
                "failures": sum(1 for row in rows if row.get("illum_status") != "success"),
            },
        )

    selected: list[dict[str, Any]] = []
    if not args.skip_curation:
        selected = select_targeted_records(rows, refs_per_group=args.refs_per_group, max_groups=args.max_groups)
        if not args.skip_artifacts:
            selected = generate_artifacts(
                selected,
                output_dir=output_dir,
                siril_path=siril_path,
                timeout=args.siril_timeout,
                preview_workers=args.preview_workers,
                tile_grid=args.tile_grid,
                min_valid_tile_fraction=args.min_valid_tile_fraction,
            )
            pdf_path = build_candidate_pdf(selected, output_dir)
            print(f"Wrote {pdf_path}")
        copied = copy_selected_records(selected, dataset_root)
        write_manifest(copied, dataset_root)
        pairs = write_pairs(copied, dataset_root)
        write_json(output_dir / "lnc_illumination_selection.json", copied)
        print(f"Selected {len(copied)} frame(s) and wrote {len(pairs)} LNC illumination pair(s).")

    if args.compare_lnc:
        compare_workers = min(args.max_compare_workers, args.workers)
        comparisons = compare_lnc_outputs(
            dataset_root=dataset_root,
            pairs_arg=args.pairs,
            siril_path=siril_path,
            timeout=args.siril_timeout,
            workers=compare_workers,
            tile_grid=args.tile_grid,
            min_valid_tile_fraction=args.min_valid_tile_fraction,
        )
        fields = [
            "pair_id",
            "group_dir",
            "target_roles",
            "target_file",
            "corrected_file",
            "original_status",
            "corrected_status",
        ]
        for field in ILLUM_FIELDS:
            fields.extend([f"original_{field}", f"corrected_{field}", f"delta_{field}", f"reduction_{field}"])
        write_csv(dataset_root / "lnc_illumination_lnc_comparison.csv", comparisons, fields)
        write_json(dataset_root / "lnc_illumination_lnc_comparison.json", comparisons)
        print(f"Wrote {dataset_root / 'lnc_illumination_lnc_comparison.csv'}")

    print(f"Wrote {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

