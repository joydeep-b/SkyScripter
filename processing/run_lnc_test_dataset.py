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
    parser.add_argument("--siril-path", type=Path, default=None)
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
    group_dir: str
    output_dir: Path
    corrected_path: Path
    diag_dir: Path
    log_path: Path

    @classmethod
    def from_row(cls, dataset_root: Path, row: dict[str, str]) -> PairJob:
        group_dir = row["group_dir"]
        pair_key = pair_id_from_row(row)
        output_dir = dataset_root / "subs" / group_dir / "lnc_outputs" / pair_key
        return cls(
            row=row,
            pair_id=pair_key,
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
    wrapper = read_json(job.diag_dir / "wrapper_report.json")
    core = read_json(job.diag_dir / "local_normalize_unregistered_report.json")
    transform = wrapper.get("transform_report", {})
    validation = transform.get("validation", {}) if isinstance(transform, dict) else {}
    ref_mask = wrapper.get("reference_mask", {})
    tgt_mask = wrapper.get("target_mask", {})
    timings = wrapper.get("timings_seconds", {}) if isinstance(wrapper.get("timings_seconds"), dict) else {}
    wall_seconds = finite_float(timings.get("Total wall time"))

    return {
        "pair_id": job.pair_id,
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
        "offset_min": finite_float(core.get("offset_min")),
        "offset_max": finite_float(core.get("offset_max")),
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
    mapping = {
        "reference": Path(job.row["reference_file"]),
        "target_original": Path(job.row["target_file"]),
        "target_lnc": job.corrected_path,
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
    allowed_suffixes = ("__reference.jpg", "__target_original.jpg", "__target_lnc.jpg")
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

        for summary in sorted(group_rows, key=lambda row: str(row.get("pair_id", ""))):
            previews_map = summary.get("preview_paths", {})
            story.append(
                Paragraph(
                    f"Reference: {escape(role_label(summary.get('reference_roles')))} | "
                    f"Target: {escape(role_label(summary.get('target_roles')))} | "
                    f"Status: {escape(str(summary.get('status', '')))}",
                    styles["Heading2"],
                )
            )
            diag_lines = [
                f"registration {fmt_num(summary.get('transform_median_residual_px'))} px",
                f"valid grid {fmt_pct(summary.get('initial_valid_fraction'))}",
                f"target score {fmt_num(summary.get('target_score'))}",
                f"runtime {fmt_num(summary.get('wall_seconds'), 2, ' s')}",
                f"CPU eff {fmt_num(summary.get('cpu_efficiency'), 2)}",
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
                sized_image(Path(previews_map["reference"]) if previews_map.get("reference") else Path(), 3.15 * inch),
                sized_image(
                    Path(previews_map["target_original"]) if previews_map.get("target_original") else Path(),
                    3.15 * inch,
                ),
                sized_image(Path(previews_map["target_lnc"]) if previews_map.get("target_lnc") else Path(), 3.15 * inch),
            ]
            story.append(
                Table(
                    [["Reference", "Original target", "LNC corrected"], images],
                    colWidths=[3.25 * inch, 3.25 * inch, 3.25 * inch],
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


def run_pair_worker(payload: dict[str, Any]) -> dict[str, Any]:
    job = PairJob(
        row=payload["row"],
        pair_id=payload["pair_id"],
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
    resolved_siril = host.get("siril_path")
    if not resolved_siril and not args.skip_lnc:
        print("Error: could not resolve Siril path", file=sys.stderr)
        return 1
    if not args.skip_lnc:
        ensure_lnc_binary()

    jobs = [PairJob.from_row(dataset_root, row) for row in pair_rows]
    payloads = [
        {
            "row": job.row,
            "pair_id": job.pair_id,
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
        "offset_min",
        "offset_max",
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

    orchestration = build_orchestration_recommendation(
        summaries=summaries,
        host=host,
        full_measurements_path=args.full_measurements.expanduser().resolve(),
    )
    write_json(dataset_root / "lnc_orchestration_recommendation.json", orchestration)

    manifest_rows = load_selection_manifest(dataset_root)
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
    print(f"Wrote {dataset_root / 'lnc_orchestration_recommendation.json'}")
    return 0 if complete_count == len(jobs) else 1


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
