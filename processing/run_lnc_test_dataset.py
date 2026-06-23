#!/usr/bin/env python3
"""Run LNC verification on the Markarians LNC test dataset and produce review artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analysis.sub_quality_scoring import previews
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DEFAULT_DATASET_ROOT = Path("/scratch/joydeepb/astro_temp/markarians/markarians_LNC_test")
DEFAULT_PAIRS = "lnc_pairs_smoke.csv"
DEFAULT_FULL_MEASUREMENTS = Path(
    "/scratch/joydeepb/astro_temp/markarians/moony_subs/quality_report/measurements.csv"
)
LNC_WRAPPER = REPO_ROOT / "processing" / "lnc" / "scripts" / "lnc_unregistered_pair.py"
SATELLITE_TRAIL_GROUPS = frozenset({"Max_Kuster__Redcat__L", "jdh_astro__default__L"})
PHOTOMETRIC_MODELS = ("local-linear", "star-scale-additive")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LNC verification on the copied LNC test dataset.")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument(
        "--pairs",
        type=str,
        default=DEFAULT_PAIRS,
        help="CSV filename under dataset-root or absolute path.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="Parallel LNC pair workers. Default: min(8, CPU count).",
    )
    parser.add_argument("--resume", action="store_true", help="Skip pairs with existing successful outputs.")
    parser.add_argument("--force", action="store_true", help="Rerun even if outputs exist.")
    parser.add_argument("--max-jobs", type=int, default=None, help="Limit number of pairs to run.")
    parser.add_argument("--skip-lnc", action="store_true", help="Only rebuild previews/PDF from existing outputs.")
    parser.add_argument("--skip-previews", action="store_true")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-html", action="store_true")
    parser.add_argument("--siril-path", type=Path, default=None)
    parser.add_argument(
        "--photometric-models",
        nargs="+",
        choices=PHOTOMETRIC_MODELS,
        default=list(PHOTOMETRIC_MODELS),
        help="LNC photometric models to run. Default: local-linear and star-scale-additive.",
    )
    parser.add_argument(
        "--omp-threads",
        type=int,
        default=4,
        help="OMP_NUM_THREADS for each LNC C-core process. Default: 4.",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="Per-pair Siril/LNC timeout seconds.")
    parser.add_argument(
        "--full-measurements",
        type=Path,
        default=DEFAULT_FULL_MEASUREMENTS,
        help="Full-dataset measurements.csv for orchestration estimates.",
    )
    return parser.parse_args()


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value.strip("._") or "unknown"


def pairs_path(dataset_root: Path, pairs_arg: str) -> Path:
    path = Path(pairs_arg)
    if path.is_absolute():
        return path
    return dataset_root / pairs_arg


def load_pairs(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def primary_role(roles_text: str) -> str:
    roles = [part.strip() for part in roles_text.split(";") if part.strip()]
    priority = {
        "reference_rank_1": 0,
        "reference_rank_2": 1,
        "reference_rank_3": 2,
        "score_median": 3,
        "score_p10": 4,
        "score_p25": 5,
        "score_p75": 6,
        "max_bgnoise": 7,
        "max_background": 8,
        "min_star_count": 9,
        "max_star_count": 10,
        "illum_reference_rank_1": 20,
        "illum_reference_rank_2": 21,
        "illum_reference_rank_3": 22,
        "illum_gradient_high": 30,
        "illum_patchy_high": 31,
        "illum_range_high": 32,
        "bright_gradient": 33,
        "cloud_suspect": 34,
    }
    return min(roles, key=lambda role: priority.get(role, 99))


def pair_id_from_row(row: dict[str, str]) -> str:
    ref_role = primary_role(row["reference_roles"])
    tgt_role = primary_role(row["target_roles"])
    ref_stem = safe_name(Path(row["reference_file"]).stem)[:36]
    tgt_stem = safe_name(Path(row["target_file"]).stem)[:36]
    return f"{safe_name(ref_role)}_{ref_stem}__{safe_name(tgt_role)}_{tgt_stem}"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def image_shape(path: Path) -> tuple[int | None, int | None]:
    try:
        with fits.open(path, memmap=True) as hdul:
            header = hdul[0].header
            return int(header.get("NAXIS1", 0)) or None, int(header.get("NAXIS2", 0)) or None
    except Exception:
        return None, None


def background_flatness_metrics(path: Path, *, tiles: int = 8) -> dict[str, float | None]:
    if not path.exists():
        return {}
    try:
        with fits.open(path, memmap=True) as hdul:
            data = np.asarray(hdul[0].data, dtype=np.float32)
        if data.ndim != 2:
            return {}
        height, width = data.shape
        medians: list[float] = []
        for gy in range(tiles):
            y0 = round(gy * height / tiles)
            y1 = round((gy + 1) * height / tiles)
            for gx in range(tiles):
                x0 = round(gx * width / tiles)
                x1 = round((gx + 1) * width / tiles)
                tile = data[y0:y1, x0:x1]
                finite = tile[np.isfinite(tile)]
                if finite.size:
                    medians.append(float(np.median(finite)))
        if not medians:
            return {}
        values = np.asarray(medians, dtype=np.float64)
        median = float(np.median(values))
        p05 = float(np.percentile(values, 5))
        p95 = float(np.percentile(values, 95))
        mad = float(np.median(np.abs(values - median)))
        return {
            "background_tile_median": median,
            "background_tile_p05": p05,
            "background_tile_p95": p95,
            "background_tile_p95_p05": p95 - p05,
            "background_tile_mad": mad,
        }
    except Exception:
        return {}


def ratio_or_none(numerator: object, denominator: object) -> float | None:
    n = finite_float(numerator)
    d = finite_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def host_context(workers: int, siril_path: str | None) -> dict[str, Any]:
    memory_total_gb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    memory_total_gb = round(int(line.split()[1]) / (1024 * 1024), 2)
                    break
    except OSError:
        pass
    resolved_siril = None
    if siril_path:
        resolved_siril = str(siril_path)
    else:
        try:
            sys.path.insert(0, str(REPO_ROOT / "processing" / "lnc" / "scripts"))
            from lnc_registered_pair import find_siril_path

            resolved_siril = find_siril_path(None)
        except Exception:
            resolved_siril = None
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "configured_workers": workers,
        "memory_total_gb": memory_total_gb,
        "python_executable": sys.executable,
        "siril_path": resolved_siril,
        "lnc_wrapper": str(LNC_WRAPPER),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def ensure_lnc_binary() -> None:
    subprocess.run(
        ["make", "-C", str(REPO_ROOT / "processing" / "lnc"), "bin/lnc_unregistered_pair"],
        check=True,
    )


@dataclass
class PairJob:
    row: dict[str, str]
    pair_id: str
    photometric_model: str
    group_dir: str
    output_dir: Path
    corrected_path: Path
    diag_dir: Path
    log_path: Path

    @classmethod
    def from_row(cls, dataset_root: Path, row: dict[str, str], photometric_model: str) -> PairJob:
        group_dir = row["group_dir"]
        pair_key = pair_id_from_row(row)
        output_dir = dataset_root / "subs" / group_dir / "lnc_outputs" / photometric_model / pair_key
        return cls(
            row=row,
            pair_id=pair_key,
            photometric_model=photometric_model,
            group_dir=group_dir,
            output_dir=output_dir,
            corrected_path=output_dir / "corrected.fit",
            diag_dir=output_dir / "diag",
            log_path=output_dir / "run.log",
        )


def pair_is_complete(job: PairJob) -> bool:
    wrapper_report = job.diag_dir / "wrapper_report.json"
    return job.corrected_path.exists() and wrapper_report.exists()


def child_usage_snapshot() -> resource.struct_rusage:
    return resource.getrusage(resource.RUSAGE_CHILDREN)


def child_usage_delta(
    before: resource.struct_rusage,
    after: resource.struct_rusage,
    wall_seconds: float,
) -> dict[str, float | int | None]:
    user_seconds = max(0.0, after.ru_utime - before.ru_utime)
    system_seconds = max(0.0, after.ru_stime - before.ru_stime)
    cpu_seconds = user_seconds + system_seconds
    return {
        "cpu_user_seconds": user_seconds,
        "cpu_system_seconds": system_seconds,
        "cpu_total_seconds": cpu_seconds,
        "cpu_efficiency": (cpu_seconds / wall_seconds) if wall_seconds > 0 else None,
        "max_rss_kb": max(after.ru_maxrss, before.ru_maxrss),
    }


def run_lnc_pair(
    job: PairJob,
    *,
    siril_path: Path | None,
    timeout: float,
    omp_threads: int,
    force: bool,
    resume: bool,
) -> dict[str, Any]:
    if resume and not force and pair_is_complete(job):
        summary = summarize_existing_pair(job, status="success")
        summary["resume_skipped_run"] = True
        return summary

    job.output_dir.mkdir(parents=True, exist_ok=True)
    job.diag_dir.mkdir(parents=True, exist_ok=True)
    if force and job.corrected_path.exists():
        job.corrected_path.unlink()

    command = [
        sys.executable,
        str(LNC_WRAPPER),
        str(Path(job.row["reference_file"]).resolve()),
        str(Path(job.row["target_file"]).resolve()),
        str(job.corrected_path),
        "--diag-dir",
        str(job.diag_dir),
        "--save-intermediate-fits",
        "--output-format",
        "float32",
        "--photometric-model",
        job.photometric_model,
    ]
    if siril_path is not None:
        command.extend(["--siril-path", str(siril_path)])

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    usage_before = child_usage_snapshot()
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(max(1, omp_threads))
    with job.log_path.open("w", encoding="utf-8") as log_handle:
        log_handle.write(f"command: {' '.join(command)}\n")
        log_handle.write(f"started_at: {started_at}\n\n")
        log_handle.flush()
        try:
            result = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                env=env,
            )
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            log_handle.write(f"\nTimed out after {timeout:.1f} seconds.\n")
            returncode = 124
    wall_seconds = time.perf_counter() - t0
    usage_after = child_usage_snapshot()
    finished_at = datetime.now(timezone.utc).isoformat()

    summary = summarize_existing_pair(job, status="success" if returncode == 0 else "failed")
    summary.update(
        {
            "started_at": started_at,
            "finished_at": finished_at,
            "wall_seconds": wall_seconds,
            "exit_code": returncode,
            "command": command,
            "omp_threads": max(1, omp_threads),
            **child_usage_delta(usage_before, usage_after, wall_seconds),
        }
    )
    if returncode != 0:
        summary["error"] = tail_log(job.log_path)
    return summary


def tail_log(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def summarize_existing_pair(job: PairJob, *, status: str) -> dict[str, Any]:
    ref_path = Path(job.row["reference_file"])
    tgt_path = Path(job.row["target_file"])
    width, height = image_shape(tgt_path)
    ref_flatness = background_flatness_metrics(ref_path)
    target_flatness = background_flatness_metrics(tgt_path)
    corrected_flatness = background_flatness_metrics(job.corrected_path)
    wrapper = read_json(job.diag_dir / "wrapper_report.json")
    core = read_json(job.diag_dir / "local_normalize_unregistered_report.json")
    transform = wrapper.get("transform_report", {})
    validation = transform.get("validation", {}) if isinstance(transform, dict) else {}
    ref_mask = wrapper.get("reference_mask", {})
    tgt_mask = wrapper.get("target_mask", {})
    parameters = wrapper.get("parameters", {}) if isinstance(wrapper.get("parameters"), dict) else {}
    star_scale = wrapper.get("star_scale_report") or parameters.get("star_scale_report")
    if not isinstance(star_scale, dict):
        star_scale = {}
    timings = wrapper.get("timings_seconds", {}) if isinstance(wrapper.get("timings_seconds"), dict) else {}
    wall_seconds = finite_float(timings.get("Total wall time"))

    return {
        "pair_id": job.pair_id,
        "photometric_model": job.photometric_model,
        "group_dir": job.group_dir,
        "user": job.row.get("user", ""),
        "equipment": job.row.get("equipment", ""),
        "filter": job.row.get("filter", ""),
        "reference_roles": job.row.get("reference_roles", ""),
        "target_roles": job.row.get("target_roles", ""),
        "reference_file": str(ref_path),
        "target_file": str(tgt_path),
        "corrected_file": str(job.corrected_path),
        "output_dir": str(job.output_dir),
        "log_path": str(job.log_path),
        "status": status,
        "reference_score": finite_float(job.row.get("reference_score")),
        "target_score": finite_float(job.row.get("target_score")),
        "image_width": width,
        "image_height": height,
        "reference_bytes": ref_path.stat().st_size if ref_path.exists() else None,
        "target_bytes": tgt_path.stat().st_size if tgt_path.exists() else None,
        "corrected_bytes": job.corrected_path.stat().st_size if job.corrected_path.exists() else None,
        "transform_median_residual_px": finite_float(validation.get("median_nearest_star_distance_px")),
        "transform_convention": validation.get("convention"),
        "ref_mask_fraction": finite_float(ref_mask.get("masked_fraction")),
        "target_mask_fraction": finite_float(tgt_mask.get("masked_fraction")),
        "ref_star_count": ref_mask.get("stars"),
        "target_star_count": tgt_mask.get("stars"),
        "initial_valid_fraction": finite_float(core.get("initial_valid_fraction")),
        "scale_min": finite_float(core.get("scale_min")),
        "scale_max": finite_float(core.get("scale_max")),
        "global_scale": finite_float(core.get("global_scale") or parameters.get("global_scale")),
        "star_scale_used_stars": star_scale.get("used_stars"),
        "star_scale_r_squared": finite_float((star_scale.get("robust_fit") or {}).get("r_squared"))
        if isinstance(star_scale.get("robust_fit"), dict)
        else finite_float(star_scale.get("r_squared")),
        "offset_min": finite_float(core.get("offset_min")),
        "offset_max": finite_float(core.get("offset_max")),
        "reference_background_range": ref_flatness.get("background_tile_p95_p05"),
        "target_original_background_range": target_flatness.get("background_tile_p95_p05"),
        "corrected_background_range": corrected_flatness.get("background_tile_p95_p05"),
        "corrected_background_mad": corrected_flatness.get("background_tile_mad"),
        "corrected_to_target_background_range_ratio": ratio_or_none(
            corrected_flatness.get("background_tile_p95_p05"),
            target_flatness.get("background_tile_p95_p05"),
        ),
        "corrected_to_reference_background_range_ratio": ratio_or_none(
            corrected_flatness.get("background_tile_p95_p05"),
            ref_flatness.get("background_tile_p95_p05"),
        ),
        "value_scale": (wrapper.get("value_scale") or {}).get("scale")
        if isinstance(wrapper.get("value_scale"), dict)
        else wrapper.get("parameters", {}).get("value_scale")
        if isinstance(wrapper.get("parameters"), dict)
        else None,
        "wall_seconds": wall_seconds,
        "wrapper_timings_seconds": timings,
        "preview_paths": {},
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "command": None,
        "omp_threads": None,
        "cpu_user_seconds": None,
        "cpu_system_seconds": None,
        "cpu_total_seconds": None,
        "cpu_efficiency": None,
        "max_rss_kb": None,
        "resume_skipped_run": False,
        "error": None,
    }


def render_preview(path: Path, output_path: Path, siril_path: str, timeout: float) -> Path | None:
    if not path.exists():
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rendered = previews.render_preview(path, output_path.parent, siril_path, timeout)
        if rendered.resolve() != output_path.resolve():
            shutil.copy2(rendered, output_path)
        return output_path
    except Exception:
        return None


def render_pair_previews(
    job: PairJob,
    dataset_root: Path,
    siril_path: str,
    timeout: float,
) -> dict[str, str]:
    review_dir = dataset_root / "review_jpegs" / job.group_dir
    outputs: dict[str, str] = {}
    model_label = "target_ssa_lnc" if job.photometric_model == "star-scale-additive" else "target_local_linear_lnc"
    mapping = {
        "reference": Path(job.row["reference_file"]),
        "target_original": Path(job.row["target_file"]),
        model_label: job.corrected_path,
    }
    for label, fits_path in mapping.items():
        if not fits_path.exists():
            continue
        dest = review_dir / f"{job.pair_id}__{label}.jpg"
        rendered = render_preview(fits_path, dest, siril_path, timeout)
        if rendered is not None:
            outputs[label] = str(rendered)
    return outputs


def cleanup_review_jpegs(dataset_root: Path) -> None:
    review_root = dataset_root / "review_jpegs"
    if not review_root.exists():
        return
    allowed_suffixes = (
        "__reference.jpg",
        "__target_original.jpg",
        "__target_local_linear_lnc.jpg",
        "__target_ssa_lnc.jpg",
    )
    for path in review_root.rglob("*.jpg"):
        if not path.name.endswith(allowed_suffixes):
            path.unlink()


def load_selection_manifest(dataset_root: Path) -> list[dict[str, str]]:
    path = dataset_root / "selection_manifest.csv"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat = dict(row)
            if "wrapper_timings_seconds" in flat and isinstance(flat["wrapper_timings_seconds"], dict):
                flat["wrapper_timings_json"] = json.dumps(flat.pop("wrapper_timings_seconds"), sort_keys=True)
            if "command" in flat and isinstance(flat["command"], list):
                flat["command_json"] = json.dumps(flat.pop("command"))
            if "preview_paths" in flat and isinstance(flat["preview_paths"], dict):
                flat["preview_paths_json"] = json.dumps(flat.pop("preview_paths"), sort_keys=True)
            writer.writerow(flat)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round(q * (len(ordered) - 1))
    return ordered[int(index)]


def group_dir_name(user: str, equipment: str, filter_name: str) -> str:
    return f"{safe_name(user)}__{safe_name(equipment)}__{safe_name(filter_name)}"


def build_orchestration_recommendation(
    *,
    summaries: list[dict[str, Any]],
    host: dict[str, Any],
    full_measurements_path: Path,
) -> dict[str, Any]:
    successful = [row for row in summaries if row.get("status") == "success" and finite_float(row.get("wall_seconds"))]
    wall_times = [float(row["wall_seconds"]) for row in successful]
    workers = int(host.get("configured_workers") or 1)
    cpu_count = host.get("cpu_count") or workers

    by_size: dict[str, list[float]] = defaultdict(list)
    by_group: dict[str, list[float]] = defaultdict(list)
    for row in successful:
        wall = float(row["wall_seconds"])
        size_key = f"{row.get('image_width')}x{row.get('image_height')}"
        by_size[size_key].append(wall)
        by_group[str(row["group_dir"])].append(wall)

    group_frame_counts: dict[str, int] = defaultdict(int)
    if full_measurements_path.exists():
        with full_measurements_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = group_dir_name(row["user"], row["equipment"], row["filter"])
                group_frame_counts[key] += 1

    total_full_frames = sum(group_frame_counts.values()) if group_frame_counts else 15476
    estimated_production_jobs = max(total_full_frames - len(group_frame_counts), total_full_frames * 0.95)

    median_wall = percentile(wall_times, 0.5) or 0.0
    p90_wall = percentile(wall_times, 0.9) or median_wall
    serial_hours = (estimated_production_jobs * median_wall) / 3600.0
    parallel_hours = serial_hours / max(workers, 1)

    cpu_efficiencies = [
        float(row["cpu_efficiency"])
        for row in successful
        if finite_float(row.get("cpu_efficiency")) is not None
    ]
    for row in successful:
        timings = row.get("wrapper_timings_seconds")
        if isinstance(timings, dict):
            total = finite_float(timings.get("Total wall time"))
            if total and total > 0:
                accounted = sum(
                    float(v)
                    for k, v in timings.items()
                    if k != "Total wall time" and finite_float(v) is not None
                )
                cpu_efficiencies.append(accounted / total)
    median_cpu_eff = percentile(cpu_efficiencies, 0.5) if cpu_efficiencies else None

    omp_threads = int(host.get("omp_threads_per_worker") or 1)
    if cpu_count and cpu_count >= 32:
        recommended_workers = min(16, max(workers, cpu_count // max(omp_threads * 3, 1)))
    elif cpu_count and cpu_count >= 8:
        recommended_workers = min(8, max(workers, cpu_count // max(omp_threads * 2, 1)))
    else:
        recommended_workers = max(1, workers)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_pairs": len(summaries),
        "benchmark_successes": len(successful),
        "host": host,
        "timing": {
            "median_wall_seconds_per_pair": median_wall,
            "p90_wall_seconds_per_pair": p90_wall,
            "min_wall_seconds": min(wall_times) if wall_times else None,
            "max_wall_seconds": max(wall_times) if wall_times else None,
            "median_cpu_efficiency": median_cpu_eff,
            "by_image_size": {
                key: {
                    "count": len(vals),
                    "median_wall_seconds": percentile(vals, 0.5),
                    "p90_wall_seconds": percentile(vals, 0.9),
                }
                for key, vals in sorted(by_size.items())
            },
            "by_group_dir": {
                key: {
                    "count": len(vals),
                    "median_wall_seconds": percentile(vals, 0.5),
                    "p90_wall_seconds": percentile(vals, 0.9),
                }
                for key, vals in sorted(by_group.items())
            },
        },
        "full_dataset_estimate": {
            "measurements_path": str(full_measurements_path),
            "total_measured_frames": total_full_frames,
            "estimated_production_lnc_jobs_one_ref_per_frame": int(estimated_production_jobs),
            "serial_runtime_hours_median": round(serial_hours, 2),
            "parallel_runtime_hours_median_workers": {
                str(workers): round(parallel_hours, 2),
                str(recommended_workers): round(
                    (estimated_production_jobs * median_wall) / 3600.0 / max(recommended_workers, 1),
                    2,
                ),
            },
            "serial_runtime_hours_p90": round((estimated_production_jobs * p90_wall) / 3600.0, 2),
        },
        "recommendations": {
            "orchestrator": "resumable_global_job_queue",
            "recommended_workers": recommended_workers,
            "reference_strategy": "one reference_rank_1 per user/equipment/filter; allow alternate refs for artifact-sensitive groups",
            "artifact_sensitive_groups": sorted(SATELLITE_TRAIL_GROUPS),
            "checkpoint": "skip pair when corrected.fit and wrapper_report.json exist unless --force",
            "output_layout": "lnc_corrected_subs/<group_dir>/<target_stem>_lnc.fit with per-pair diag/ and run.log",
            "notes": [
                f"Benchmark used workers={workers} and OMP_NUM_THREADS={omp_threads}.",
                "Increase workers only if memory remains comfortable and CPU utilization stays below saturation.",
                "Use global queue rather than strict group-by-group to balance long and short groups.",
                "Flush lnc_runtime_summary.csv periodically during production runs.",
            ],
        },
    }


def build_ab_comparisons(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in summaries:
        key = (str(row.get("group_dir", "")), str(row.get("pair_id", "")))
        grouped[key][str(row.get("photometric_model", "local-linear"))] = row

    comparisons: list[dict[str, Any]] = []
    for (group_dir, pair_id), by_model in sorted(grouped.items()):
        local = by_model.get("local-linear", {})
        ssa = by_model.get("star-scale-additive", {})
        comparisons.append(
            {
                "pair_id": pair_id,
                "group_dir": group_dir,
                "user": (local or ssa).get("user", ""),
                "equipment": (local or ssa).get("equipment", ""),
                "filter": (local or ssa).get("filter", ""),
                "reference_roles": (local or ssa).get("reference_roles", ""),
                "target_roles": (local or ssa).get("target_roles", ""),
                "local_linear_status": local.get("status"),
                "ssa_lnc_status": ssa.get("status"),
                "local_linear_valid_fraction": local.get("initial_valid_fraction"),
                "ssa_lnc_valid_fraction": ssa.get("initial_valid_fraction"),
                "local_linear_scale_min": local.get("scale_min"),
                "local_linear_scale_max": local.get("scale_max"),
                "ssa_lnc_scale_min": ssa.get("scale_min"),
                "ssa_lnc_scale_max": ssa.get("scale_max"),
                "ssa_lnc_global_scale": ssa.get("global_scale"),
                "ssa_lnc_used_stars": ssa.get("star_scale_used_stars"),
                "ssa_lnc_scale_r_squared": ssa.get("star_scale_r_squared"),
                "reference_background_range": (local or ssa).get("reference_background_range"),
                "target_original_background_range": (local or ssa).get("target_original_background_range"),
                "local_linear_corrected_background_range": local.get("corrected_background_range"),
                "ssa_lnc_corrected_background_range": ssa.get("corrected_background_range"),
                "local_linear_background_range_ratio": local.get("corrected_to_target_background_range_ratio"),
                "ssa_lnc_background_range_ratio": ssa.get("corrected_to_target_background_range_ratio"),
                "local_linear_wall_seconds": local.get("wall_seconds"),
                "ssa_lnc_wall_seconds": ssa.get("wall_seconds"),
                "local_linear_corrected_file": local.get("corrected_file"),
                "ssa_lnc_corrected_file": ssa.get("corrected_file"),
                "local_linear_output_dir": local.get("output_dir"),
                "ssa_lnc_output_dir": ssa.get("output_dir"),
                "local_linear_error": local.get("error"),
                "ssa_lnc_error": ssa.get("error"),
            }
        )
    return comparisons


def sized_image(path: Path, max_width: float) -> Image | Paragraph:
    if not path or not str(path).strip() or not path.exists() or not path.is_file():
        return Paragraph("missing", getSampleStyleSheet()["Normal"])
    image_width, image_height = ImageReader(str(path)).getSize()
    scale = max_width / float(image_width)
    return Image(str(path), width=max_width, height=image_height * scale)


def fmt_num(value: object, digits: int = 3, suffix: str = "") -> str:
    number = finite_float(value)
    if number is None:
        return "n/a"
    if abs(number) >= 100:
        text = f"{number:.0f}"
    elif abs(number) >= 10:
        text = f"{number:.1f}"
    else:
        text = f"{number:.{digits}g}"
    return f"{text}{suffix}"


def fmt_pct(value: object) -> str:
    number = finite_float(value)
    if number is None:
        return "n/a"
    return f"{100.0 * number:.1f}%"


def role_label(roles: object) -> str:
    text = str(roles or "")
    replacements = {
        "reference_rank_1": "Reference #1",
        "reference_rank_2": "Reference #2",
        "reference_rank_3": "Reference #3",
        "score_p10": "Bad score (p10)",
        "score_p25": "Low score (p25)",
        "score_median": "Median score",
        "score_p75": "Good score (p75)",
        "max_bgnoise": "Worst noise",
        "max_background": "Brightest sky",
        "min_star_count": "Fewest stars",
        "max_star_count": "Most stars",
        "illum_reference_rank_1": "Flat reference #1",
        "illum_reference_rank_2": "Flat reference #2",
        "illum_reference_rank_3": "Flat reference #3",
        "illum_gradient_high": "Strong gradient",
        "illum_patchy_high": "Patchy/cloud structure",
        "illum_range_high": "High illumination range",
        "bright_gradient": "Bright gradient",
        "cloud_suspect": "Cloud/haze suspect",
    }
    labels = [replacements.get(part, part) for part in text.split(";") if part]
    return " + ".join(labels) if labels else "unknown"


def compact_filename(path_text: object) -> str:
    name = Path(str(path_text or "")).name
    return name if len(name) <= 52 else f"{name[:24]}...{name[-24:]}"


def build_pdf(
    *,
    dataset_root: Path,
    summaries: list[dict[str, Any]],
    manifest_rows: list[dict[str, str]],
    orchestration: dict[str, Any],
    host: dict[str, Any],
) -> Path:
    pdf_path = dataset_root / "lnc_test_report.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(letter),
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )
    story: list[Any] = []

    successes = [row for row in summaries if row.get("status") == "success"]
    failures = [row for row in summaries if row.get("status") not in {"success", "skipped_resume"}]
    wall_times = [float(row["wall_seconds"]) for row in successes if finite_float(row.get("wall_seconds"))]
    residuals = [
        float(row["transform_median_residual_px"])
        for row in successes
        if finite_float(row.get("transform_median_residual_px"))
    ]

    story.append(Paragraph("LNC Test Dataset Verification Report", styles["Title"]))
    story.append(Paragraph(f"Dataset: {escape(str(dataset_root))}", styles["Normal"]))
    story.append(Paragraph(f"Generated: {datetime.now().isoformat(timespec='seconds')}", styles["Normal"]))
    story.append(
        Paragraph(
            f"Pairs: {len(summaries)} attempted, {len(successes)} success, {len(failures)} failed, "
            f"{sum(1 for row in summaries if row.get('status') == 'skipped_resume')} resumed/skipped",
            styles["Normal"],
        )
    )
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph("Runtime summary", styles["Heading2"]))
    story.append(
        Paragraph(
            f"Host CPUs: {host.get('cpu_count')}, workers: {host.get('configured_workers')}, "
            f"memory: {host.get('memory_total_gb')} GiB, Siril: {escape(str(host.get('siril_path') or '?'))}",
            styles["Normal"],
        )
    )
    if wall_times:
        story.append(
            Paragraph(
                f"Pair wall time (wrapper total): median {percentile(wall_times, 0.5):.1f}s, "
                f"p90 {percentile(wall_times, 0.9):.1f}s, max {max(wall_times):.1f}s",
                styles["Normal"],
            )
        )
    estimate = orchestration.get("full_dataset_estimate", {})
    story.append(
        Paragraph(
            "Estimated full-dataset runtime (one LNC job per frame, median timing): "
            f"{estimate.get('serial_runtime_hours_median')} h serial, "
            f"{estimate.get('parallel_runtime_hours_median_workers')} parallel",
            styles["Normal"],
        )
    )
    rec = orchestration.get("recommendations", {})
    story.append(
        Paragraph(
            f"Recommended production workers: {rec.get('recommended_workers')}; "
            f"orchestrator: {rec.get('orchestrator')}",
            styles["Normal"],
        )
    )
    story.append(PageBreak())

    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summaries:
        by_group[str(row["group_dir"])].append(row)

    refs_by_group: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in manifest_rows:
        if "reference_rank" in row.get("primary_role", ""):
            refs_by_group[row["group_dir"]].append(row)

    review_root = dataset_root / "review_jpegs"
    for group_dir in sorted(by_group):
        group_rows = by_group[group_dir]
        story.append(Paragraph(escape(group_dir), styles["Heading1"]))
        sample = group_rows[0]
        story.append(
            Paragraph(
                f"Filter {escape(str(sample.get('filter', '')))} | "
                f"size {sample.get('image_width')}x{sample.get('image_height')} | "
                f"pairs {len(group_rows)} | "
                f"score range {fmt_num(min(finite_float(r.get('target_score')) or 0 for r in group_rows))}"
                f" to {fmt_num(max(finite_float(r.get('target_score')) or 0 for r in group_rows))}",
                styles["Normal"],
            )
        )
        if group_dir in SATELLITE_TRAIL_GROUPS:
            story.append(Paragraph("Satellite-trail / artifact-sensitive group", styles["Normal"]))

        ref_rows = sorted(refs_by_group.get(group_dir, []), key=lambda row: row.get("primary_role", ""))
        if ref_rows:
            story.append(Paragraph("Reference candidates", styles["Heading2"]))
            ref_images = []
            siril_for_pdf = host.get("siril_path") or ""
            for ref_row in ref_rows[:3]:
                role = ref_row.get("primary_role", "")
                copied = Path(ref_row.get("copied_file", ""))
                preview = review_root / group_dir / f"{safe_name(role)}__reference.jpg"
                if siril_for_pdf and copied.exists() and not preview.exists():
                    render_preview(copied, preview, siril_for_pdf, 300.0)
                cell = (
                    sized_image(preview, 1.8 * inch)
                    if preview.exists()
                    else Paragraph(role, styles["Normal"])
                )
                ref_images.append(cell)
            if ref_images:
                story.append(Table([ref_images], colWidths=[2.25 * inch] * len(ref_images)))

        by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for summary in group_rows:
            by_pair[str(summary.get("pair_id", ""))][str(summary.get("photometric_model", "local-linear"))] = summary

        for pair_id in sorted(by_pair):
            model_rows = by_pair[pair_id]
            local = model_rows.get("local-linear", {})
            ssa = model_rows.get("star-scale-additive", {})
            summary = local or ssa
            previews_map: dict[str, str] = {}
            for row in (local, ssa):
                if isinstance(row.get("preview_paths"), dict):
                    previews_map.update(row["preview_paths"])
            story.append(
                Paragraph(
                    f"Reference: {escape(role_label(summary.get('reference_roles')))} | "
                    f"Target: {escape(role_label(summary.get('target_roles')))} | "
                    f"Local-linear: {escape(str(local.get('status', 'missing')))} | "
                    f"SSA-LNC: {escape(str(ssa.get('status', 'missing')))}",
                    styles["Heading2"],
                )
            )
            diag_lines = [
                f"registration local {fmt_num(local.get('transform_median_residual_px'))} px / "
                f"SSA {fmt_num(ssa.get('transform_median_residual_px'))} px",
                f"valid grid local {fmt_pct(local.get('initial_valid_fraction'))} / "
                f"SSA {fmt_pct(ssa.get('initial_valid_fraction'))}",
                f"target score {fmt_num(summary.get('target_score'))}",
                f"bg range target {fmt_num(summary.get('target_original_background_range'))}, "
                f"local {fmt_num(local.get('corrected_background_range'))}, "
                f"SSA {fmt_num(ssa.get('corrected_background_range'))}",
                f"scale local {fmt_num(local.get('scale_min'))}-{fmt_num(local.get('scale_max'))}, "
                f"SSA {fmt_num(ssa.get('scale_min'))}-{fmt_num(ssa.get('scale_max'))}",
                f"SSA star scale {fmt_num(ssa.get('global_scale'))}, "
                f"stars {fmt_num(ssa.get('star_scale_used_stars'))}, "
                f"R2 {fmt_num(ssa.get('star_scale_r_squared'))}",
            ]
            story.append(Paragraph(", ".join(diag_lines), styles["Normal"]))
            story.append(
                Paragraph(
                    f"Ref file: {escape(compact_filename(summary.get('reference_file')))} | "
                    f"Target file: {escape(compact_filename(summary.get('target_file')))}",
                    styles["Normal"],
                )
            )
            if summary.get("error"):
                story.append(Paragraph(escape(str(summary["error"])[:500]), styles["Normal"]))
            images = [
                sized_image(Path(previews_map["reference"]) if previews_map.get("reference") else Path(), 2.35 * inch),
                sized_image(
                    Path(previews_map["target_original"]) if previews_map.get("target_original") else Path(),
                    2.35 * inch,
                ),
                sized_image(
                    Path(previews_map["target_local_linear_lnc"])
                    if previews_map.get("target_local_linear_lnc")
                    else Path(),
                    2.35 * inch,
                ),
                sized_image(
                    Path(previews_map["target_ssa_lnc"]) if previews_map.get("target_ssa_lnc") else Path(),
                    2.35 * inch,
                ),
            ]
            story.append(
                Table(
                    [["Reference", "Original target", "Old LNC", "SSA-LNC"], images],
                    colWidths=[2.4 * inch, 2.4 * inch, 2.4 * inch, 2.4 * inch],
                    style=TableStyle(
                        [
                            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ]
                    ),
                )
            )
            story.append(Spacer(1, 0.12 * inch))
            story.append(PageBreak())
        story.append(PageBreak())

    doc.build(story)
    return pdf_path


def rel_report_path(dataset_root: Path, path_text: object) -> str:
    if not path_text:
        return ""
    try:
        path = Path(str(path_text))
        if not path.is_absolute():
            return path.as_posix()
        return os.path.relpath(path, dataset_root)
    except Exception:
        return str(path_text)


def html_data_value(value: object) -> float | str | None:
    number = finite_float(value)
    if number is not None:
        return number
    if value is None:
        return None
    return str(value)


def pair_link_set(dataset_root: Path, row: dict[str, Any]) -> dict[str, str]:
    output_dir = Path(str(row.get("output_dir") or ""))
    diag_dir = output_dir / "diag" if output_dir else Path()
    return {
        "corrected_fits": rel_report_path(dataset_root, row.get("corrected_file")),
        "wrapper_report": rel_report_path(dataset_root, diag_dir / "wrapper_report.json"),
        "core_report": rel_report_path(dataset_root, diag_dir / "local_normalize_unregistered_report.json"),
        "scale_map": rel_report_path(dataset_root, diag_dir / "scale_map.fits"),
        "offset_map": rel_report_path(dataset_root, diag_dir / "offset_map.fits"),
        "run_log": rel_report_path(dataset_root, row.get("log_path")),
    }


def html_summary_stats(ab_rows: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [
        row
        for row in ab_rows
        if row.get("local_linear_status") == "success" and row.get("ssa_lnc_status") == "success"
    ]
    failed = [row for row in ab_rows if row not in complete]

    def values(key: str) -> list[float]:
        return [float(v) for row in complete if (v := finite_float(row.get(key))) is not None]

    def median(key: str) -> float | None:
        vals = sorted(values(key))
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else 0.5 * (vals[mid - 1] + vals[mid])

    ssa_better = sum(
        1
        for row in complete
        if finite_float(row.get("ssa_lnc_corrected_background_range")) is not None
        and finite_float(row.get("local_linear_corrected_background_range")) is not None
        and float(row["ssa_lnc_corrected_background_range"]) < float(row["local_linear_corrected_background_range"])
    )
    local_better = sum(
        1
        for row in complete
        if finite_float(row.get("ssa_lnc_corrected_background_range")) is not None
        and finite_float(row.get("local_linear_corrected_background_range")) is not None
        and float(row["local_linear_corrected_background_range"]) < float(row["ssa_lnc_corrected_background_range"])
    )
    return {
        "total_pairs": len(ab_rows),
        "complete_pairs": len(complete),
        "failed_pairs": len(failed),
        "ssa_better_flatness_pairs": ssa_better,
        "local_better_flatness_pairs": local_better,
        "median_target_bg_range": median("target_original_background_range"),
        "median_local_bg_range": median("local_linear_corrected_background_range"),
        "median_ssa_bg_range": median("ssa_lnc_corrected_background_range"),
        "median_local_bg_ratio": median("local_linear_background_range_ratio"),
        "median_ssa_bg_ratio": median("ssa_lnc_background_range_ratio"),
        "median_ssa_r2": median("ssa_lnc_scale_r_squared"),
        "median_ssa_used_stars": median("ssa_lnc_used_stars"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_interactive_html_report(
    *,
    dataset_root: Path,
    summaries: list[dict[str, Any]],
    ab_comparisons: list[dict[str, Any]],
    host: dict[str, Any],
) -> Path:
    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in summaries:
        key = (str(row.get("group_dir", "")), str(row.get("pair_id", "")))
        by_pair[key][str(row.get("photometric_model", "local-linear"))] = row

    ab_by_key = {(str(row.get("group_dir", "")), str(row.get("pair_id", ""))): row for row in ab_comparisons}
    pairs: list[dict[str, Any]] = []
    for key, ab_row in sorted(ab_by_key.items()):
        model_rows = by_pair.get(key, {})
        local = model_rows.get("local-linear", {})
        ssa = model_rows.get("star-scale-additive", {})
        preview_paths: dict[str, str] = {}
        for row in (local, ssa):
            if isinstance(row.get("preview_paths"), dict):
                preview_paths.update({k: rel_report_path(dataset_root, v) for k, v in row["preview_paths"].items()})
        local_bg = finite_float(ab_row.get("local_linear_corrected_background_range"))
        ssa_bg = finite_float(ab_row.get("ssa_lnc_corrected_background_range"))
        if local.get("status") != "success" or ssa.get("status") != "success":
            status = "failed"
        elif local_bg is not None and ssa_bg is not None and ssa_bg < local_bg:
            status = "ssa_better"
        elif local_bg is not None and ssa_bg is not None and local_bg < ssa_bg:
            status = "local_better"
        else:
            status = "complete"
        pairs.append(
            {
                "pair_id": ab_row.get("pair_id"),
                "group_dir": ab_row.get("group_dir"),
                "user": ab_row.get("user"),
                "equipment": ab_row.get("equipment"),
                "filter": ab_row.get("filter"),
                "reference_roles": ab_row.get("reference_roles"),
                "target_roles": ab_row.get("target_roles"),
                "status": status,
                "search_text": " ".join(
                    str(ab_row.get(key_name) or "")
                    for key_name in ("pair_id", "group_dir", "user", "equipment", "filter", "reference_roles", "target_roles")
                ).lower(),
                "reference_file": rel_report_path(dataset_root, local.get("reference_file") or ssa.get("reference_file")),
                "target_file": rel_report_path(dataset_root, local.get("target_file") or ssa.get("target_file")),
                "images": {
                    "reference": preview_paths.get("reference", ""),
                    "target_original": preview_paths.get("target_original", ""),
                    "local_linear": preview_paths.get("target_local_linear_lnc", ""),
                    "ssa_lnc": preview_paths.get("target_ssa_lnc", ""),
                },
                "metrics": {key_name: html_data_value(value) for key_name, value in ab_row.items()},
                "local": {
                    "links": pair_link_set(dataset_root, local),
                    "residual_px": html_data_value(local.get("transform_median_residual_px")),
                    "valid_fraction": html_data_value(local.get("initial_valid_fraction")),
                    "runtime_seconds": html_data_value(local.get("wall_seconds")),
                    "error": local.get("error"),
                },
                "ssa": {
                    "links": pair_link_set(dataset_root, ssa),
                    "residual_px": html_data_value(ssa.get("transform_median_residual_px")),
                    "valid_fraction": html_data_value(ssa.get("initial_valid_fraction")),
                    "runtime_seconds": html_data_value(ssa.get("wall_seconds")),
                    "error": ssa.get("error"),
                },
            }
        )

    report_data = {
        "summary": html_summary_stats(ab_comparisons),
        "host": host,
        "pairs": pairs,
        "groups": sorted({str(pair["group_dir"]) for pair in pairs if pair.get("group_dir")}),
    }
    data_json = json.dumps(report_data, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    html_path = dataset_root / "lnc_ab_comparison_report.html"
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LNC A/B Interactive Report</title>
<style>
:root {{
  --bg:#f7f7f5; --panel:#ffffff; --ink:#1f2328; --muted:#60656f; --line:#d7d9dd;
  --soft:#eef0f3; --good:#1f7a3f; --warn:#9a6700; --bad:#b42318; --accent:#1f5fbf;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Arial,Helvetica,sans-serif; background:var(--bg); color:var(--ink); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
header {{ position:sticky; top:0; z-index:5; background:var(--panel); border-bottom:1px solid var(--line); padding:14px 18px; }}
h1 {{ margin:0 0 4px; font-size:22px; }}
h2 {{ margin:22px 0 10px; }}
.subtle {{ color:var(--muted); font-size:13px; }}
.layout {{ display:grid; grid-template-columns:250px minmax(0,1fr); gap:18px; padding:18px; }}
.sidebar {{ position:sticky; top:138px; align-self:start; max-height:calc(100vh - 156px); overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; }}
.group-link {{ display:block; width:100%; border:0; background:transparent; text-align:left; padding:6px 8px; border-radius:6px; color:var(--ink); cursor:pointer; font-size:13px; }}
.group-link:hover {{ background:var(--soft); }}
.stats {{ display:grid; grid-template-columns:repeat(7,minmax(110px,1fr)); gap:10px; margin-top:12px; }}
.stat {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:9px 11px; }}
.stat b {{ display:block; font-size:20px; margin-bottom:2px; }}
.controls {{ display:grid; grid-template-columns:2fr repeat(4, minmax(150px,1fr)); gap:10px; margin-top:12px; }}
input,select {{ width:100%; border:1px solid var(--line); border-radius:7px; background:#fff; padding:8px; color:var(--ink); }}
.results-meta {{ margin:0 0 12px; color:var(--muted); font-size:13px; }}
.pair {{ background:var(--panel); border:1px solid var(--line); border-left:6px solid var(--line); border-radius:10px; padding:14px; margin:0 0 16px; }}
.pair.ssa_better {{ border-left-color:var(--good); }}
.pair.local_better {{ border-left-color:var(--warn); }}
.pair.failed {{ border-left-color:var(--bad); }}
.pair-head {{ display:flex; gap:12px; justify-content:space-between; align-items:flex-start; }}
.pair h3 {{ margin:0 0 6px; font-size:17px; }}
.badge-row {{ display:flex; flex-wrap:wrap; gap:7px; margin:10px 0; }}
.badge {{ display:inline-block; background:var(--soft); border-radius:999px; padding:4px 8px; font-size:12px; color:#30343a; }}
.badge.good {{ color:var(--good); }}
.badge.warn {{ color:var(--warn); }}
.badge.bad {{ color:var(--bad); }}
.images {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:10px; }}
.image-panel {{ min-width:0; }}
.image-title {{ font-weight:bold; font-size:12px; margin:0 0 5px; color:#30343a; }}
.image-panel img {{ width:100%; height:auto; border:1px solid var(--line); background:white; cursor:zoom-in; display:block; }}
.missing {{ border:1px dashed var(--line); min-height:120px; display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:13px; }}
.links {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; font-size:12px; }}
.error {{ margin-top:8px; background:#fff2f0; border:1px solid #f0b8b0; color:#7a1d14; border-radius:7px; padding:8px; font-size:12px; white-space:pre-wrap; }}
.lightbox {{ position:fixed; inset:0; display:none; z-index:20; background:rgba(0,0,0,.82); padding:28px; }}
.lightbox.open {{ display:flex; flex-direction:column; align-items:center; justify-content:center; }}
.lightbox img {{ max-width:96vw; max-height:88vh; border:1px solid #555; background:white; }}
.lightbox button {{ margin-top:10px; padding:8px 12px; border-radius:7px; border:1px solid #ccc; cursor:pointer; }}
@media (max-width:1100px) {{
  .layout {{ grid-template-columns:1fr; }}
  .sidebar {{ position:static; max-height:none; }}
  .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
  .controls {{ grid-template-columns:1fr 1fr; }}
  .images {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
}}
</style>
</head>
<body>
<header>
  <h1>LNC A/B Interactive Report</h1>
  <div class="subtle">Old local-linear LNC vs StarScale Additive LNC. Generated from <code>lnc_pairs_full.csv</code>.</div>
  <div id="stats" class="stats"></div>
  <div class="controls">
    <input id="search" placeholder="Search user, group, filter, role, pair id">
    <select id="statusFilter">
      <option value="all">All statuses</option>
      <option value="complete">Both succeeded</option>
      <option value="ssa_better">SSA flatter</option>
      <option value="local_better">Old LNC flatter</option>
      <option value="failed">Registration failed</option>
    </select>
    <select id="groupFilter"><option value="all">All groups</option></select>
    <select id="sortBy">
      <option value="pair_id">Sort by pair id</option>
      <option value="ssa_advantage">Sort by SSA flatness advantage</option>
      <option value="ssa_r2">Sort by SSA R²</option>
      <option value="ssa_stars">Sort by SSA stars used</option>
      <option value="registration">Sort by registration residual</option>
      <option value="runtime">Sort by runtime</option>
    </select>
    <select id="density">
      <option value="full">Full cards</option>
      <option value="compact">Compact metrics</option>
    </select>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <b>Groups</b>
    <div id="groupLinks"></div>
    <h2>Artifacts</h2>
    <div class="links">
      <a href="lnc_test_report.pdf">PDF report</a>
      <a href="lnc_ab_comparison_summary.csv">A/B CSV</a>
      <a href="lnc_runtime_summary.csv">Runtime CSV</a>
      <a href="lnc_ab_comparison_summary.json">A/B JSON</a>
    </div>
  </aside>
  <main>
    <div id="resultsMeta" class="results-meta"></div>
    <div id="pairs"></div>
  </main>
</div>
<div id="lightbox" class="lightbox"><img id="lightboxImg" alt=""><button id="closeLightbox">Close</button></div>
<script id="report-data" type="application/json">{data_json}</script>
<script>
const data = JSON.parse(document.getElementById('report-data').textContent);
const state = {{ search:'', status:'all', group:'all', sortBy:'pair_id', density:'full' }};
const fmt = new Intl.NumberFormat(undefined, {{ maximumSignificantDigits: 4 }});
const pct = v => Number.isFinite(v) ? (100*v).toFixed(1)+'%' : 'n/a';
const num = v => Number.isFinite(v) ? fmt.format(v) : 'n/a';
const role = s => String(s || '').replaceAll('_',' ');
function metric(pair, key) {{
  const value = pair.metrics[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}}
function labelForStatus(status) {{
  if (status === 'ssa_better') return 'SSA flatter';
  if (status === 'local_better') return 'Old LNC flatter';
  if (status === 'failed') return 'Registration failed';
  return 'Both succeeded';
}}
function linkList(title, links) {{
  const entries = Object.entries(links || {{}}).filter(([,v]) => v);
  if (!entries.length) return '';
  return '<span class="badge">'+title+'</span>' + entries.map(([k,v]) => `<a href="${{v}}">${{k.replaceAll('_',' ')}}</a>`).join('');
}}
function imagePanel(title, src) {{
  if (!src) return `<div class="image-panel"><div class="image-title">${{title}}</div><div class="missing">missing preview</div></div>`;
  return `<div class="image-panel"><div class="image-title">${{title}}</div><img loading="lazy" src="${{src}}" alt="${{title}}" data-full="${{src}}"></div>`;
}}
function pairSortValue(pair) {{
  if (state.sortBy === 'ssa_advantage') return (metric(pair,'local_linear_corrected_background_range') ?? 0) - (metric(pair,'ssa_lnc_corrected_background_range') ?? 0);
  if (state.sortBy === 'ssa_r2') return metric(pair,'ssa_lnc_scale_r_squared') ?? -Infinity;
  if (state.sortBy === 'ssa_stars') return metric(pair,'ssa_lnc_used_stars') ?? -Infinity;
  if (state.sortBy === 'registration') return Math.max(pair.local.residual_px ?? Infinity, pair.ssa.residual_px ?? Infinity);
  if (state.sortBy === 'runtime') return Math.max(pair.local.runtime_seconds ?? -Infinity, pair.ssa.runtime_seconds ?? -Infinity);
  return pair.pair_id || '';
}}
function filteredPairs() {{
  let rows = data.pairs.filter(pair => {{
    if (state.status !== 'all') {{
      if (state.status === 'complete' && pair.status === 'failed') return false;
      if (state.status !== 'complete' && pair.status !== state.status) return false;
    }}
    if (state.group !== 'all' && pair.group_dir !== state.group) return false;
    if (state.search && !pair.search_text.includes(state.search.toLowerCase())) return false;
    return true;
  }});
  rows.sort((a,b) => {{
    const av = pairSortValue(a), bv = pairSortValue(b);
    if (typeof av === 'string') return av.localeCompare(String(bv));
    if (state.sortBy === 'registration') return av - bv;
    return bv - av;
  }});
  return rows;
}}
function renderStats() {{
  const s = data.summary;
  const stats = [
    [s.total_pairs, 'Pairs'],
    [s.complete_pairs, 'Both succeeded'],
    [s.failed_pairs, 'Registration failures'],
    [s.ssa_better_flatness_pairs, 'SSA flatter'],
    [s.local_better_flatness_pairs, 'Old LNC flatter'],
    [num(s.median_ssa_r2), 'Median SSA R²'],
    [num(s.median_ssa_used_stars), 'Median SSA stars'],
  ];
  document.getElementById('stats').innerHTML = stats.map(([v,l]) => `<div class="stat"><b>${{v}}</b>${{l}}</div>`).join('');
}}
function renderControls() {{
  const groupSelect = document.getElementById('groupFilter');
  groupSelect.innerHTML = '<option value="all">All groups</option>' + data.groups.map(g => `<option value="${{g}}">${{g}}</option>`).join('');
  document.getElementById('groupLinks').innerHTML = data.groups.map(g => `<button class="group-link" data-group="${{g}}">${{g}}</button>`).join('');
}}
function renderPairs() {{
  const rows = filteredPairs();
  document.getElementById('resultsMeta').textContent = `Showing ${{rows.length}} of ${{data.pairs.length}} pairs`;
  document.getElementById('pairs').innerHTML = rows.map(pair => {{
    const m = pair.metrics;
    const compact = state.density === 'compact';
    const badges = [
      `<span class="badge ${{pair.status === 'failed' ? 'bad' : 'good'}}">${{labelForStatus(pair.status)}}</span>`,
      `<span class="badge">Target bg ${{num(m.target_original_background_range)}}</span>`,
      `<span class="badge">Old bg ${{num(m.local_linear_corrected_background_range)}} (${{pct(m.local_linear_background_range_ratio)}})</span>`,
      `<span class="badge">SSA bg ${{num(m.ssa_lnc_corrected_background_range)}} (${{pct(m.ssa_lnc_background_range_ratio)}})</span>`,
      `<span class="badge">Old grid ${{pct(m.local_linear_valid_fraction)}}</span>`,
      `<span class="badge">SSA grid ${{pct(m.ssa_lnc_valid_fraction)}}</span>`,
      `<span class="badge">SSA scale ${{num(m.ssa_lnc_global_scale)}}</span>`,
      `<span class="badge">SSA stars ${{num(m.ssa_lnc_used_stars)}}</span>`,
      `<span class="badge">SSA R² ${{num(m.ssa_lnc_scale_r_squared)}}</span>`,
    ].join('');
    const errors = [pair.local.error, pair.ssa.error].filter(Boolean).map(e => `<div class="error">${{String(e).slice(-1600)}}</div>`).join('');
    return `<section class="pair ${{pair.status}}" id="${{pair.pair_id}}">
      <div class="pair-head">
        <div><h3>${{pair.user || ''}} · ${{pair.filter || ''}} · ${{role(pair.target_roles)}} </h3>
        <div class="subtle">${{pair.group_dir}} · ${{pair.pair_id}}</div></div>
        <a href="#top" onclick="scrollTo({{top:0,behavior:'smooth'}});return false;">top</a>
      </div>
      <div class="badge-row">${{badges}}</div>
      ${{compact ? '' : `<div class="images">
        ${{imagePanel('Reference', pair.images.reference)}}
        ${{imagePanel('Original target', pair.images.target_original)}}
        ${{imagePanel('Old local-linear LNC', pair.images.local_linear)}}
        ${{imagePanel('SSA-LNC', pair.images.ssa_lnc)}}
      </div>`}}
      <div class="links">
        <span class="badge">Source</span><a href="${{pair.reference_file}}">reference FITS</a><a href="${{pair.target_file}}">target FITS</a>
        ${{linkList('Old LNC', pair.local.links)}}
        ${{linkList('SSA-LNC', pair.ssa.links)}}
      </div>
      ${{errors}}
    </section>`;
  }}).join('');
}}
function update() {{ renderPairs(); }}
document.getElementById('search').addEventListener('input', e => {{ state.search = e.target.value.trim(); update(); }});
document.getElementById('statusFilter').addEventListener('change', e => {{ state.status = e.target.value; update(); }});
document.getElementById('groupFilter').addEventListener('change', e => {{ state.group = e.target.value; update(); }});
document.getElementById('sortBy').addEventListener('change', e => {{ state.sortBy = e.target.value; update(); }});
document.getElementById('density').addEventListener('change', e => {{ state.density = e.target.value; update(); }});
document.getElementById('groupLinks').addEventListener('click', e => {{
  const group = e.target && e.target.dataset ? e.target.dataset.group : null;
  if (!group) return;
  state.group = group;
  document.getElementById('groupFilter').value = group;
  update();
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}});
document.addEventListener('click', e => {{
  if (e.target && e.target.matches('img[data-full]')) {{
    document.getElementById('lightboxImg').src = e.target.dataset.full;
    document.getElementById('lightbox').classList.add('open');
  }}
}});
document.getElementById('closeLightbox').addEventListener('click', () => document.getElementById('lightbox').classList.remove('open'));
document.getElementById('lightbox').addEventListener('click', e => {{ if (e.target.id === 'lightbox') e.currentTarget.classList.remove('open'); }});
renderStats();
renderControls();
renderPairs();
</script>
</body>
</html>
"""
    html_path.write_text(html, encoding="utf-8")
    return html_path


def run_pair_worker(payload: dict[str, Any]) -> dict[str, Any]:
    job = PairJob(
        row=payload["row"],
        pair_id=payload["pair_id"],
        photometric_model=payload["photometric_model"],
        group_dir=payload["group_dir"],
        output_dir=Path(payload["output_dir"]),
        corrected_path=Path(payload["corrected_path"]),
        diag_dir=Path(payload["diag_dir"]),
        log_path=Path(payload["log_path"]),
    )
    if payload.get("skip_lnc"):
        if pair_is_complete(job):
            summary = summarize_existing_pair(job, status="success")
        else:
            summary = summarize_existing_pair(job, status="failed")
    else:
        summary = run_lnc_pair(
            job,
            siril_path=Path(payload["siril_path"]) if payload.get("siril_path") else None,
            timeout=float(payload["timeout"]),
            omp_threads=int(payload.get("omp_threads") or 1),
            force=bool(payload.get("force")),
            resume=bool(payload.get("resume")),
        )
    if not payload.get("skip_previews") and summary.get("status") in {"success", "skipped_resume", "skipped_lnc"}:
        siril_path = payload.get("resolved_siril")
        if siril_path:
            summary["preview_paths"] = render_pair_previews(
                job,
                Path(payload["dataset_root"]),
                siril_path,
                float(payload["timeout"]),
            )
    return summary


def main() -> int:
    args = parse_args()
    dataset_root = args.dataset_root.expanduser().resolve()
    pairs_csv = pairs_path(dataset_root, args.pairs)
    if not pairs_csv.exists():
        print(f"Error: pairs file not found: {pairs_csv}", file=sys.stderr)
        return 1
    if args.workers < 1:
        print("Error: --workers must be at least 1", file=sys.stderr)
        return 1

    pair_rows = load_pairs(pairs_csv)
    if args.max_jobs is not None:
        pair_rows = pair_rows[: args.max_jobs]

    host = host_context(args.workers, args.siril_path)
    host["omp_threads_per_worker"] = max(1, args.omp_threads)
    host["photometric_models"] = list(args.photometric_models)
    resolved_siril = host.get("siril_path")
    if not resolved_siril and not args.skip_lnc:
        print("Error: could not resolve Siril path", file=sys.stderr)
        return 1
    if not args.skip_lnc:
        ensure_lnc_binary()

    jobs = [
        PairJob.from_row(dataset_root, row, photometric_model)
        for row in pair_rows
        for photometric_model in args.photometric_models
    ]
    payloads = [
        {
            "row": job.row,
            "pair_id": job.pair_id,
            "photometric_model": job.photometric_model,
            "group_dir": job.group_dir,
            "output_dir": str(job.output_dir),
            "corrected_path": str(job.corrected_path),
            "diag_dir": str(job.diag_dir),
            "log_path": str(job.log_path),
            "dataset_root": str(dataset_root),
            "siril_path": str(args.siril_path) if args.siril_path else None,
            "resolved_siril": resolved_siril,
            "timeout": args.timeout,
            "omp_threads": max(1, args.omp_threads),
            "force": args.force,
            "resume": args.resume,
            "skip_lnc": args.skip_lnc,
            "skip_previews": args.skip_previews,
        }
        for job in jobs
    ]

    summaries: list[dict[str, Any]] = []
    if args.workers == 1 or len(payloads) == 1:
        for index, payload in enumerate(payloads, start=1):
            print(f"LNC pair {index}/{len(payloads)}: {payload['pair_id']}", file=sys.stderr)
            summaries.append(run_pair_worker(payload))
    else:
        with ProcessPoolExecutor(max_workers=min(args.workers, len(payloads))) as executor:
            future_map = {executor.submit(run_pair_worker, payload): payload for payload in payloads}
            for index, future in enumerate(as_completed(future_map), start=1):
                payload = future_map[future]
                print(f"LNC pair {index}/{len(payloads)} done: {payload['pair_id']}", file=sys.stderr)
                summaries.append(future.result())

    summaries.sort(key=lambda row: (str(row.get("group_dir", "")), str(row.get("pair_id", ""))))

    run_fields = [
        "pair_id",
        "photometric_model",
        "group_dir",
        "user",
        "equipment",
        "filter",
        "status",
        "reference_roles",
        "target_roles",
        "reference_score",
        "target_score",
        "transform_median_residual_px",
        "transform_convention",
        "initial_valid_fraction",
        "scale_min",
        "scale_max",
        "global_scale",
        "star_scale_used_stars",
        "star_scale_r_squared",
        "offset_min",
        "offset_max",
        "reference_background_range",
        "target_original_background_range",
        "corrected_background_range",
        "corrected_background_mad",
        "corrected_to_target_background_range_ratio",
        "corrected_to_reference_background_range_ratio",
        "ref_mask_fraction",
        "target_mask_fraction",
        "wall_seconds",
        "exit_code",
        "error",
        "reference_file",
        "target_file",
        "corrected_file",
        "output_dir",
        "log_path",
    ]
    runtime_fields = run_fields + [
        "image_width",
        "image_height",
        "reference_bytes",
        "target_bytes",
        "corrected_bytes",
        "value_scale",
        "wrapper_timings_json",
        "command_json",
        "preview_paths_json",
        "started_at",
        "finished_at",
        "omp_threads",
        "cpu_user_seconds",
        "cpu_system_seconds",
        "cpu_total_seconds",
        "cpu_efficiency",
        "max_rss_kb",
        "resume_skipped_run",
    ]
    write_csv(dataset_root / "lnc_run_summary.csv", summaries, run_fields)
    write_csv(dataset_root / "lnc_runtime_summary.csv", summaries, runtime_fields)
    write_json(dataset_root / "lnc_run_summary.json", summaries)
    write_json(dataset_root / "lnc_runtime_summary.json", summaries)
    ab_comparisons = build_ab_comparisons(summaries)
    ab_fields = [
        "pair_id",
        "group_dir",
        "user",
        "equipment",
        "filter",
        "reference_roles",
        "target_roles",
        "local_linear_status",
        "ssa_lnc_status",
        "local_linear_valid_fraction",
        "ssa_lnc_valid_fraction",
        "local_linear_scale_min",
        "local_linear_scale_max",
        "ssa_lnc_scale_min",
        "ssa_lnc_scale_max",
        "ssa_lnc_global_scale",
        "ssa_lnc_used_stars",
        "ssa_lnc_scale_r_squared",
        "reference_background_range",
        "target_original_background_range",
        "local_linear_corrected_background_range",
        "ssa_lnc_corrected_background_range",
        "local_linear_background_range_ratio",
        "ssa_lnc_background_range_ratio",
        "local_linear_wall_seconds",
        "ssa_lnc_wall_seconds",
        "local_linear_corrected_file",
        "ssa_lnc_corrected_file",
        "local_linear_output_dir",
        "ssa_lnc_output_dir",
        "local_linear_error",
        "ssa_lnc_error",
    ]
    write_csv(dataset_root / "lnc_ab_comparison_summary.csv", ab_comparisons, ab_fields)
    write_json(dataset_root / "lnc_ab_comparison_summary.json", ab_comparisons)

    orchestration = build_orchestration_recommendation(
        summaries=summaries,
        host=host,
        full_measurements_path=args.full_measurements.expanduser().resolve(),
    )
    write_json(dataset_root / "lnc_orchestration_recommendation.json", orchestration)

    manifest_rows = load_selection_manifest(dataset_root)
    if not args.skip_html:
        html_path = build_interactive_html_report(
            dataset_root=dataset_root,
            summaries=summaries,
            ab_comparisons=ab_comparisons,
            host=host,
        )
        print(f"Wrote {html_path}")
    if not args.skip_pdf:
        pdf_path = build_pdf(
            dataset_root=dataset_root,
            summaries=summaries,
            manifest_rows=manifest_rows,
            orchestration=orchestration,
            host=host,
        )
        print(f"Wrote {pdf_path}")
    if not args.skip_previews:
        cleanup_review_jpegs(dataset_root)

    complete_count = sum(1 for job in jobs if pair_is_complete(job))
    success_count = sum(1 for row in summaries if row.get("status") == "success")
    print(f"Completed {len(summaries)} pair(s): {success_count} success, {complete_count} with corrected outputs")
    print(f"Wrote {dataset_root / 'lnc_runtime_summary.csv'}")
    print(f"Wrote {dataset_root / 'lnc_ab_comparison_summary.csv'}")
    if not args.skip_html:
        print(f"Wrote {dataset_root / 'lnc_ab_comparison_report.html'}")
    print(f"Wrote {dataset_root / 'lnc_orchestration_recommendation.json'}")
    return 0 if complete_count == len(jobs) else 1


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
