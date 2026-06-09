"""Shared helpers for multi-user LNC stacking pipeline."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any

import numpy as np
from astropy.io import fits

# Re-export stack grouping constants for tests and callers
VALID_FILTERS = {"L", "R", "G", "B", "H", "S", "O"}
LOGGER = logging.getLogger(__name__)
FILTER_ORDER = {"L": 0, "R": 1, "G": 2, "B": 3, "H": 4, "S": 5, "O": 6}
DEFAULT_SEQUENCE_NAME = "pp_light"
# Siril's `convert` normalizes output frames to com.pref.ext (default ".fit"),
# regardless of the source extension (.fits/.fts/.fit). Files inside .process and
# everything derived from them therefore use this extension, not the source one.
SIRIL_CONVERTED_EXT = ".fit"
DEFAULT_DROP_MAX_FRACTION = 0.15
# Thread pool size for I/O-bound per-frame work (FITS header reads, file copies).
# Capped well below the core count: these are storage-bound, so a handful of
# concurrent streams saturates NVMe and more just adds contention.
IO_WORKERS = max(1, min(32, os.cpu_count() or 1))


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._-") or "unknown"


def canonical_filter_folder(value: str) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9]+", "", value).upper()
    aliases = {
        "L": "L",
        "LUM": "L",
        "LUMINANCE": "L",
        "R": "R",
        "RED": "R",
        "G": "G",
        "GREEN": "G",
        "B": "B",
        "BLUE": "B",
        "H": "H",
        "HA": "H",
        "HALPHA": "H",
        "HYDROGENALPHA": "H",
        "S": "S",
        "SII": "S",
        "S2": "S",
        "O": "O",
        "OIII": "O",
        "O3": "O",
    }
    return aliases.get(normalized)


def extract_user_and_equipment(source_file: Path, input_dir: Path, filter_name: str) -> tuple[str, str]:
    try:
        relative_path = source_file.resolve().relative_to(input_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"source file is not under input_dir: {source_file}") from exc
    if not relative_path.parts:
        raise ValueError(f"source file has no path relative to input_dir: {source_file}")
    user_name = relative_path.parts[0] or "unknown"
    if len(relative_path.parts) <= 2:
        return user_name, "default"
    top_folder = relative_path.parts[1]
    if len(relative_path.parts) == 3 and canonical_filter_folder(top_folder) == filter_name:
        return user_name, "default"
    return user_name, top_folder


def sort_group_key(item: tuple[str, str, str]) -> tuple[str, str, int]:
    user_name, equipment_name, filter_name = item
    return user_name.lower(), equipment_name.lower(), FILTER_ORDER.get(filter_name, 99)


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def percentile_sorted(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    index = round(q * (len(sorted_values) - 1))
    return sorted_values[index]


def find_siril_path(explicit_path: Path | None) -> str:
    if explicit_path is not None:
        path = explicit_path.expanduser()
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"Siril path does not exist: {explicit_path}")

    if sys.platform == "darwin":
        mac_path = Path("/Applications/Siril.app/Contents/MacOS/Siril")
        if mac_path.exists():
            return str(mac_path)

    for candidate in (
        Path("/home/joydeepb/Siril-1.2.1-x86_64.AppImage"),
        Path("/home/joydeepb/Siril-1.2.5-x86_64.AppImage"),
    ):
        if candidate.exists():
            return str(candidate)

    for command_name in ("siril-cli", "siril"):
        resolved = shutil.which(command_name)
        if resolved:
            return resolved

    raise FileNotFoundError(
        "Could not find Siril executable. Use --siril-path to specify it explicitly."
    )


def run_siril_script(
    siril_path: str,
    working_dir: Path,
    script_text: str,
    log_path: Path,
    context: str,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if not script_text.endswith("\n"):
        script_text += "\n"
    if not re.search(r"(?im)^\s*exit\s*$", script_text):
        script_text += "exit\n"
    command = [siril_path, "-d", str(working_dir), "-s", "-"]
    LOGGER.info("START Siril: %s", context)
    start = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(script_text)
    process.stdin.close()
    process.stdin = None
    heartbeat_seconds = int(timeout) if timeout is not None and timeout < 30 else 30
    while True:
        try:
            stdout, stderr = process.communicate(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            LOGGER.info("Siril still running (%.0fs): %s", elapsed, context)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    write_log(log_path, context, command, script_text, result)
    if result.returncode != 0:
        raise RuntimeError(
            f"Siril failed for {context} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    elapsed = time.monotonic() - start
    LOGGER.info("DONE Siril (%.0fs): %s", elapsed, context)
    return result


def write_log(
    log_path: Path,
    context: str,
    command: list[str],
    script_text: str,
    result: subprocess.CompletedProcess[str],
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("=" * 80 + "\n")
        handle.write(f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n")
        handle.write(f"context: {context}\n")
        handle.write(f"command: {' '.join(command)}\n")
        handle.write("-" * 80 + "\n")
        handle.write("script:\n")
        handle.write(script_text)
        if not script_text.endswith("\n"):
            handle.write("\n")
        handle.write("-" * 80 + "\n")
        handle.write("stdout:\n")
        handle.write(result.stdout)
        if not result.stdout.endswith("\n"):
            handle.write("\n")
        handle.write("-" * 80 + "\n")
        handle.write("stderr:\n")
        handle.write(result.stderr)
        if not result.stderr.endswith("\n"):
            handle.write("\n")


def write_failure_log(log_path: Path, context: str, exc: Exception) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("=" * 80 + "\n")
        handle.write(f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n")
        handle.write(f"context: {context}\n")
        handle.write("failure:\n")
        handle.write(f"{type(exc).__name__}: {exc}\n")


def summarize_exception(exc: Exception) -> str:
    for line in str(exc).splitlines():
        line = line.strip()
        if line:
            return line
    return type(exc).__name__


def rebuild_all_users_index(output_root: Path, dry_run: bool) -> int:
    all_users_root = output_root / "all_users"
    masters_root = output_root / "masters"
    link_count = 0
    for filter_name in sorted(VALID_FILTERS, key=lambda f: FILTER_ORDER.get(f, 99)):
        filter_dir = all_users_root / filter_name
        if dry_run:
            LOGGER.info("[DRY-RUN] ensure directory: %s", filter_dir)
        else:
            filter_dir.mkdir(parents=True, exist_ok=True)
            for existing in filter_dir.iterdir():
                if existing.is_symlink():
                    existing.unlink()
        if not masters_root.exists():
            continue
        user_dirs = sorted((p for p in masters_root.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
        for user_dir in user_dirs:
            equipment_dirs = sorted((p for p in user_dir.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
            for equipment_dir in equipment_dirs:
                master_path = equipment_dir / f"master_{filter_name}.fit"
                if not master_path.exists():
                    continue
                link_name = f"{user_dir.name}__{equipment_dir.name}__master_{filter_name}.fit"
                link_path = filter_dir / link_name
                link_target = os.path.relpath(master_path, link_path.parent)
                if dry_run:
                    LOGGER.info("[DRY-RUN] link %s -> %s", link_path, link_target)
                else:
                    link_path.symlink_to(link_target)
                link_count += 1
    return link_count


def resolve_path_key(path: Path) -> str:
    return str(path.expanduser().resolve())


def load_measurements_csv(path: Path) -> dict[str, dict[str, Any]]:
    import csv

    measurements: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            sub_path = str(row.get("sub_path") or "").strip()
            if not sub_path:
                continue
            key = resolve_path_key(Path(sub_path))
            measurements[key] = row
    return measurements


def sequence_input_path(row: dict[str, Any]) -> Path:
    collection_file = str(row.get("collection_file") or "").strip()
    source_file = str(row.get("source_file") or "").strip()
    if collection_file:
        path = Path(collection_file).expanduser()
        if path.exists() and path.suffix.lower() in {".fit", ".fits", ".fts"}:
            return path
    if source_file:
        return Path(source_file).expanduser()
    raise ValueError("row has no usable sequence input path")


@dataclass
class GroupRow:
    user: str
    equipment: str
    filter: str
    source_file: Path
    sequence_input_file: Path
    score: float | None = None
    star_count: float | None = None
    median_mean_star_flux: float | None = None
    background: float | None = None
    bgnoise: float | None = None
    score_rank: int = 0
    reference: bool = False
    drop_status: str = "kept"
    sequence_index: int | None = None
    sequence_name: str = DEFAULT_SEQUENCE_NAME
    work_sequence_file: Path | None = None
    corrected_sequence_file: Path | None = None
    normalized_sub_file: Path | None = None
    master_file: Path | None = None
    target_to_reference_homography: list[float] | None = None
    siril_homography: list[float] | None = None
    transform_validation_status: str = ""


@dataclass
class PreparedGroup:
    key: tuple[str, str, str]
    user: str
    equipment: str
    filter: str
    work_dir: Path
    reference_row: GroupRow
    kept_rows: list[GroupRow]
    dropped_rows: list[GroupRow]
    all_rows: list[GroupRow] = field(default_factory=list)
    drop_summary: dict[str, Any] = field(default_factory=dict)
    sequence_manifest: list[dict[str, Any]] = field(default_factory=list)

    def row_manifest_records(self, *, group_summary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        group_summary = group_summary or {}
        rows = self.all_rows or [*self.kept_rows, *self.dropped_rows]
        return [
            row_to_manifest_record(
                row,
                status=str(group_summary.get("status") or ""),
                state=str(group_summary.get("state") or ""),
                error=str(group_summary.get("error") or ""),
            )
            for row in rows
        ]


def row_to_manifest_record(
    row: GroupRow,
    *,
    status: str = "",
    state: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "user": row.user,
        "equipment": row.equipment,
        "filter": row.filter,
        "source_file": str(row.source_file),
        "sequence_input_file": str(row.sequence_input_file),
        "score": row.score,
        "star_count": row.star_count,
        "median_mean_star_flux": row.median_mean_star_flux,
        "background": row.background,
        "bgnoise": row.bgnoise,
        "score_rank": row.score_rank,
        "reference": row.reference,
        "drop_status": row.drop_status,
        "sequence_index": row.sequence_index,
        "sequence_name": row.sequence_name,
        "work_sequence_file": str(row.work_sequence_file) if row.work_sequence_file else "",
        "corrected_sequence_file": str(row.corrected_sequence_file) if row.corrected_sequence_file else "",
        "normalized_sub_file": str(row.normalized_sub_file) if row.normalized_sub_file else "",
        "master_file": str(row.master_file) if row.master_file else "",
        "target_to_reference_homography": row.target_to_reference_homography,
        "siril_homography": row.siril_homography,
        "transform_validation_status": row.transform_validation_status,
        "status": status,
        "state": state,
        "error": error,
    }


def parse_report_rows(
    report_path: Path,
    measurements_path: Path,
) -> tuple[dict[tuple[str, str, str], list[GroupRow]], dict[str, int]]:
    with report_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    input_dir = Path(str(report["input_dir"])).expanduser().resolve()
    measurements = load_measurements_csv(measurements_path)

    groups: dict[tuple[str, str, str], list[GroupRow]] = defaultdict(list)
    stats = {
        "total_rows": 0,
        "eligible_rows": 0,
        "skipped_missing_source": 0,
        "skipped_outside_input_dir": 0,
    }

    for row in report.get("rows", []):
        stats["total_rows"] += 1
        if not isinstance(row, dict):
            continue
        final_filter = str(row.get("final_filter") or "").strip().upper()
        source_file_text = str(row.get("source_file") or "").strip()
        if final_filter not in VALID_FILTERS:
            continue
        if not source_file_text:
            continue
        source_file = Path(source_file_text).expanduser()
        if not source_file.exists():
            stats["skipped_missing_source"] += 1
            continue
        try:
            user_name, equipment_name = extract_user_and_equipment(source_file, input_dir, final_filter)
        except ValueError:
            stats["skipped_outside_input_dir"] += 1
            continue

        stats["eligible_rows"] += 1
        try:
            sequence_input = sequence_input_path(row)
        except ValueError:
            continue
        if not sequence_input.exists():
            continue

        meas_key = resolve_path_key(source_file)
        meas = measurements.get(meas_key, {})
        gr = GroupRow(
            user=user_name,
            equipment=equipment_name,
            filter=final_filter,
            source_file=source_file,
            sequence_input_file=sequence_input,
            score=finite_float(meas.get("score")),
            star_count=finite_float(meas.get("star_count")),
            median_mean_star_flux=finite_float(meas.get("median_mean_star_flux")),
            background=finite_float(meas.get("background")),
            bgnoise=finite_float(meas.get("bgnoise")),
        )
        groups[(user_name, equipment_name, final_filter)].append(gr)

    return dict(groups), stats


def apply_drop_policy(
    rows: list[GroupRow],
    *,
    min_frames: int,
    drop_max_fraction: float,
) -> tuple[list[GroupRow], list[GroupRow], dict[str, Any]]:
    valid = [row for row in rows if row.score is not None and math.isfinite(row.score)]
    if len(valid) < min_frames:
        return [], rows, {"reason": "too_few_valid_scores", "valid_count": len(valid)}

    sorted_valid = sorted(valid, key=lambda r: (-r.score, str(r.source_file)))
    reference = sorted_valid[0]
    reference.reference = True
    reference.drop_status = "kept"
    scores = [row.score for row in sorted_valid]
    q1 = percentile_sorted(scores, 0.25)
    median = percentile_sorted(scores, 0.5)
    q3 = percentile_sorted(scores, 0.75)
    iqr = q3 - q1
    threshold = q1 - 1.5 * iqr

    candidates = [row for row in sorted_valid if row is not reference and row.score < threshold]
    max_drops = int(math.floor(len(sorted_valid) * drop_max_fraction))
    candidates.sort(key=lambda r: r.score)
    n_drop = min(len(candidates), max_drops)
    dropped = candidates[:n_drop]

    kept = [reference] + [row for row in sorted_valid if row is not reference and row not in dropped]
    if len(kept) < min_frames:
        need = min_frames - len(kept)
        restore = sorted(dropped, key=lambda r: (-r.score, str(r.source_file)))[:need]
        for row in restore:
            dropped.remove(row)
        kept = [reference] + [row for row in sorted_valid if row is not reference and row not in dropped]

    non_reference = [row for row in kept if not row.reference]
    non_reference.sort(key=lambda r: (-r.score, str(r.source_file)))
    kept = [reference] + non_reference
    for rank, row in enumerate(kept, start=1):
        row.score_rank = rank
        row.reference = row is reference
        row.drop_status = "kept"

    for row in dropped:
        row.drop_status = "dropped_quality_outlier"

    for row in rows:
        if row not in kept and row not in dropped:
            if row.score is None or not math.isfinite(row.score):
                row.drop_status = "skipped_invalid_score"
            elif not row.sequence_input_file.exists():
                row.drop_status = "skipped_missing_file"
            else:
                row.drop_status = "skipped_missing_measurement"

    summary = {
        "q1": q1,
        "median": median,
        "q3": q3,
        "iqr": iqr,
        "low_outlier_threshold": threshold,
        "candidate_drop_count": len(candidates),
        "actual_drop_count": len(dropped),
        "kept_count": len(kept),
        "reference_score": reference.score,
        "valid_count": len(valid),
    }
    return kept, dropped, summary


def assign_sequence_indices(kept_rows: list[GroupRow]) -> None:
    reference = next(row for row in kept_rows if row.reference)
    others = [row for row in kept_rows if not row.reference]
    others.sort(key=lambda r: (-r.score, str(r.source_file)))
    ordered = [reference] + others
    for index, row in enumerate(ordered, start=1):
        row.sequence_index = index


def prepare_group(
    key: tuple[str, str, str],
    rows: list[GroupRow],
    output_root: Path,
    drop_summary: dict[str, Any],
    *,
    all_rows: list[GroupRow] | None = None,
    materialize: bool = True,
) -> PreparedGroup:
    user, equipment, filter_name = key
    work_dir = output_root / "work" / safe_name(user) / safe_name(equipment) / filter_name
    manifests_dir = work_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)

    reference_row = next(row for row in rows if row.reference)
    assign_sequence_indices(rows)

    sequence_manifest = []
    for row in rows:
        sequence_manifest.append(
            {
                "sequence_index": row.sequence_index,
                "source_file": str(row.source_file),
                "sequence_input_file": str(row.sequence_input_file),
                "score": row.score,
                "reference": row.reference,
                "drop_status": row.drop_status,
            }
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    if materialize:
        for row in rows:
            dest_name = f"{row.sequence_name}_{row.sequence_index:05d}{row.sequence_input_file.suffix.lower()}"
            dest = work_dir / dest_name
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            shutil.copy2(row.sequence_input_file, dest)

    normalized_sub_dir = output_root / "normalized_subs" / safe_name(user) / safe_name(equipment) / filter_name
    if materialize:
        normalized_sub_dir.mkdir(parents=True, exist_ok=True)
    master_file = output_root / "masters" / safe_name(user) / safe_name(equipment) / f"master_{filter_name}.fit"

    for row in rows:
        # These all live downstream of Siril's convert, which emits SIRIL_CONVERTED_EXT
        # files, so they must not inherit the (possibly .fits/.fts) source extension.
        ext = SIRIL_CONVERTED_EXT
        row.normalized_sub_file = normalized_sub_dir / f"{row.sequence_name}_{row.sequence_index:05d}{ext}"
        row.master_file = master_file
        row.work_sequence_file = work_dir / ".process" / f"{row.sequence_name}_{row.sequence_index:05d}{ext}"
        row.corrected_sequence_file = work_dir / "corrected_sequence" / f"{row.sequence_name}_{row.sequence_index:05d}{ext}"

    return PreparedGroup(
        key=key,
        user=user,
        equipment=equipment,
        filter=filter_name,
        work_dir=work_dir,
        reference_row=reference_row,
        kept_rows=rows,
        dropped_rows=[],
        all_rows=all_rows or rows,
        drop_summary=drop_summary,
        sequence_manifest=sequence_manifest,
    )


def write_group_planning_manifests(prepared: PreparedGroup) -> None:
    manifests_dir = prepared.work_dir / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    write_json(manifests_dir / "sequence_manifest.json", prepared.sequence_manifest)
    write_json(
        manifests_dir / "group_manifest.json",
        {
            "key": list(prepared.key),
            "user": prepared.user,
            "equipment": prepared.equipment,
            "filter": prepared.filter,
            "state": "planned",
            "reference": row_to_manifest_record(prepared.reference_row),
            "kept_count": len(prepared.kept_rows),
            "dropped_count": len(prepared.dropped_rows),
            "drop_summary": prepared.drop_summary,
            "rows": prepared.row_manifest_records(),
        },
    )


def group_planned_summary(prepared: PreparedGroup, *, status: str = "planned") -> dict[str, Any]:
    return {
        "key": list(prepared.key),
        "user": prepared.user,
        "equipment": prepared.equipment,
        "filter": prepared.filter,
        "state": "planned",
        "status": status,
        "kept_count": len(prepared.kept_rows),
        "dropped_count": len(prepared.dropped_rows),
        "drop_summary": prepared.drop_summary,
        "reference": str(prepared.reference_row.source_file),
        "master_file": str(prepared.reference_row.master_file or ""),
    }


ALLOWED_TEMPLATE_PLACEHOLDERS = frozenset(
    {
        "SIRIL_THREADS",
        "SEQUENCE_NAME",
        "REFERENCE_INDEX",
    }
)


def render_siril_template(template_path: Path, values: dict[str, str]) -> str:
    template_text = template_path.read_text(encoding="utf-8")
    placeholders = set(re.findall(r"\$\{([A-Z0-9_]+)\}", template_text))
    unknown = placeholders - ALLOWED_TEMPLATE_PLACEHOLDERS
    if unknown:
        raise ValueError(f"Unknown template placeholders: {sorted(unknown)}")
    missing = placeholders - set(values.keys())
    if missing:
        raise ValueError(f"Missing template values: {sorted(missing)}")
    for key, value in values.items():
        if "\n" in value:
            raise ValueError(f"Placeholder {key} must not contain newlines")
    return Template(template_text).substitute(values)


def flip_y_matrix(height: int) -> np.ndarray:
    return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, float(height - 1)], [0.0, 0.0, 1.0]], dtype=np.float64)


def siril_to_array_homography(
    siril_target_to_ref: np.ndarray,
    *,
    reference_height: int,
    target_height: int,
) -> np.ndarray:
    return flip_y_matrix(reference_height) @ siril_target_to_ref @ flip_y_matrix(target_height)


def parse_sequence_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    sequence_name = path.stem
    reference_image: int | None = None
    image_sizes: list[tuple[int, int]] = []
    matrices: list[list[float]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "S" and len(parts) >= 8:
            sequence_name = parts[1].strip("'\"")
            reference_image = int(parts[6])
            continue
        if parts[0] == "I" and len(parts) >= 4:
            width_text, height_text = parts[3].split(",", 1)
            image_sizes.append((int(width_text), int(height_text)))
            continue
        if parts[0].startswith("R"):
            try:
                h_index = parts.index("H")
            except ValueError:
                continue
            values = [float(value) for value in parts[h_index + 1 : h_index + 10]]
            if len(values) != 9:
                continue
            matrices.append(values)
    if reference_image is None:
        raise ValueError(f"Sequence file missing S line: {path}")
    return {
        "path": str(path),
        "sequence_name": sequence_name,
        "reference_image": reference_image,
        "image_sizes": image_sizes,
        "matrices": matrices,
    }


def matrix_to_list(matrix: np.ndarray) -> list[float]:
    return [float(v) for v in matrix.reshape(9)]


def extract_target_to_reference_homography(
    sequence: dict[str, Any],
    *,
    reference_index: int,
    target_index: int,
) -> tuple[list[float], dict[str, Any]]:
    matrices = [np.array(values, dtype=np.float64).reshape(3, 3) for values in sequence["matrices"]]
    if reference_index < 1 or target_index < 1:
        raise ValueError("Sequence indices are one-based")
    if reference_index > len(matrices) or target_index > len(matrices):
        raise ValueError("Sequence index exceeds available registration matrices")

    ref_matrix = matrices[reference_index - 1]
    target_matrix = matrices[target_index - 1]
    ref_height = sequence["image_sizes"][reference_index - 1][1]
    target_height = sequence["image_sizes"][target_index - 1][1]

    candidates: dict[str, np.ndarray] = {}
    for name, builder in (
        ("image_to_internal_reference", lambda: np.linalg.inv(ref_matrix) @ target_matrix),
        ("internal_reference_to_image", lambda: ref_matrix @ np.linalg.inv(target_matrix)),
    ):
        try:
            siril_h = builder()
        except np.linalg.LinAlgError:
            continue
        if np.isfinite(siril_h).all():
            candidates[name] = siril_h

    if not candidates:
        raise ValueError("Siril registration produced non-invertible transform matrices")

    convention = "image_to_internal_reference"
    if convention not in candidates:
        convention = next(iter(candidates))
    siril_h = candidates[convention]
    array_h = siril_to_array_homography(
        siril_h,
        reference_height=ref_height,
        target_height=target_height,
    )
    return matrix_to_list(array_h), {
        "convention": convention,
        "transform_validation_status": "not_available",
        "note": "Star-list residual validation was not available; using the existing wrapper's default convention.",
        "reference_index": reference_index,
        "target_index": target_index,
    }


def build_lnc_group_manifest(prepared: PreparedGroup, seq_info: dict[str, Any]) -> dict[str, Any]:
    ref_row = prepared.reference_row
    ref_index = int(ref_row.sequence_index or 1)
    identity = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

    reference = {
        "sequence_index": ref_index,
        "work_sequence_file": str(ref_row.work_sequence_file or ""),
        "corrected_sequence_file": str(ref_row.corrected_sequence_file or ""),
        "target_to_reference_homography": identity,
        "siril_homography": seq_info["matrices"][ref_index - 1],
        "transform_validation": {"transform_validation_status": "reference"},
    }

    targets = []
    for row in prepared.kept_rows:
        if row.reference:
            continue
        if row.drop_status == "registration_failed":
            # Siril could not register this frame (zero/non-invertible homography);
            # reconcile_registration flagged it so we never hand the LNC binary a
            # singular transform. It stays in the sequence but Siril excludes it
            # (incl=0) from the final stack.
            continue
        target_index = int(row.sequence_index or 0)
        if target_index < 1 or target_index > len(seq_info["matrices"]):
            raise ValueError(f"Missing registration matrix for sequence index {target_index}")
        tgt_array_h, tgt_meta = extract_target_to_reference_homography(
            seq_info,
            reference_index=ref_index,
            target_index=target_index,
        )
        row.target_to_reference_homography = tgt_array_h
        row.siril_homography = seq_info["matrices"][target_index - 1]
        row.transform_validation_status = str(tgt_meta.get("transform_validation_status") or "")
        targets.append(
            {
                "sequence_index": target_index,
                "work_sequence_file": str(row.work_sequence_file or ""),
                "corrected_sequence_file": str(row.corrected_sequence_file or ""),
                "target_to_reference_homography": tgt_array_h,
                "siril_homography": row.siril_homography,
                "transform_validation": tgt_meta,
            }
        )

    return {
        "sequence_name": DEFAULT_SEQUENCE_NAME,
        "reference": reference,
        "targets": targets,
        "output_summary": str(prepared.work_dir / "manifests" / "lnc_group_summary.json"),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_lnc_binary(lnc_dir: Path, *, group: bool = False) -> Path:
    target = "lnc_group_subs" if group else "lnc_unregistered_pair"
    binary = lnc_dir / "bin" / target
    if not binary.exists():
        raise FileNotFoundError(
            f"LNC binary is not built: {binary}. "
            f"Build it before running this script with: make -C {lnc_dir} {target}"
        )
    return binary


def create_empty_mask(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros((height, width), dtype=np.uint8)
    fits.writeto(path, data, overwrite=True)


def read_fits_shape(path: Path) -> tuple[int, int]:
    # Read only the header (NAXIS1/NAXIS2) instead of loading pixel data. For large
    # mono frames this is ~1000x cheaper, and the dimensions are all we need for
    # the sequence-size and geometry checks. Returns (width, height).
    header = fits.getheader(path)
    if int(header.get("NAXIS", 0)) < 2:
        raise ValueError(f"{path} has no image data")
    return int(header["NAXIS1"]), int(header["NAXIS2"])


def verify_fits_geometry(expected: Path, actual: Path) -> None:
    if read_fits_shape(expected) != read_fits_shape(actual):
        raise ValueError(f"Geometry mismatch between {expected} and {actual}")

def _count_fresh_outputs(paths: list[Path], since_wall: float) -> int:
    """Count output files written during this run (mtime at/after launch).

    Counting plain existence is misleading because corrected files from a prior
    (cancelled) run persist on disk, which would make progress jump to 100%.
    """
    count = 0
    threshold = since_wall - 2.0  # small tolerance for clock granularity
    for path in paths:
        try:
            if path.stat().st_mtime >= threshold:
                count += 1
        except OSError:
            continue
    return count


def run_lnc_group_normalize(
    manifest_path: Path,
    lnc_dir: Path,
    omp_threads: int,
    *,
    diagnostics: bool = False,
) -> dict[str, Any]:
    binary = ensure_lnc_binary(lnc_dir, group=True)
    env = os.environ.copy()
    env["LNC_WRITE_DIAGNOSTICS"] = "1" if diagnostics else "0"
    command = [
        str(binary),
        "--lnc-threads",
        str(max(1, omp_threads)),
        "--lnc-workers",
        "1",
        str(manifest_path),
    ]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    targets = manifest.get("targets") if isinstance(manifest.get("targets"), list) else []
    target_outputs = [
        Path(str(target.get("corrected_sequence_file")))
        for target in targets
        if isinstance(target, dict) and target.get("corrected_sequence_file")
    ]
    LOGGER.info("START LNC group: %s", manifest_path)
    start = time.monotonic()
    start_wall = time.time()
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    heartbeat_seconds = 30
    while True:
        try:
            stdout, stderr = process.communicate(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            completed = _count_fresh_outputs(target_outputs, start_wall)
            LOGGER.info(
                "LNC group still running (%.0fs, %s/%s targets corrected): %s",
                elapsed,
                completed,
                len(target_outputs),
                manifest_path,
            )
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"lnc_group_subs failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    elapsed = time.monotonic() - start
    LOGGER.info("DONE LNC group (%.0fs): %s", elapsed, manifest_path)
    summary_path = Path(str(manifest.get("output_summary") or ""))
    if summary_path.exists():
        return json.loads(summary_path.read_text(encoding="utf-8"))
    return {"stdout": result.stdout, "stderr": result.stderr}


def plan_all_groups(
    report_path: Path,
    measurements_path: Path,
    output_root: Path,
    *,
    min_frames: int,
    drop_max_fraction: float,
    materialize: bool = True,
) -> tuple[list[PreparedGroup], list[dict[str, Any]], dict[str, int]]:
    LOGGER.info("Parsing")
    raw_groups, stats = parse_report_rows(report_path, measurements_path)
    LOGGER.info("Parsed report rows")
    prepared_groups: list[PreparedGroup] = []
    skipped: list[dict[str, Any]] = []

    for key in sorted(raw_groups.keys(), key=sort_group_key):
        rows = raw_groups[key]
        LOGGER.info("PLANNING GROUP: %s", key)
        kept, dropped, drop_summary = apply_drop_policy(
            rows,
            min_frames=min_frames,
            drop_max_fraction=drop_max_fraction,
        )
        if len(kept) < min_frames:
            skipped.append(
                {
                    "key": key,
                    "reason": drop_summary.get("reason", "too_few_frames_after_drop"),
                    "kept_count": len(kept),
                    "dropped_count": len(dropped),
                    "drop_summary": drop_summary,
                    "rows": [row_to_manifest_record(row, status="skipped", state="planned") for row in rows],
                }
            )
            continue
        assign_sequence_indices(kept)
        prepared = prepare_group(
            key,
            kept,
            output_root,
            drop_summary,
            all_rows=rows,
            materialize=materialize,
        )
        prepared.dropped_rows = dropped
        write_group_planning_manifests(prepared)
        prepared_groups.append(prepared)

    return prepared_groups, skipped, stats


def atomic_replace_corrected_files(
    prepared: PreparedGroup,
    corrected_dir: Path,
    process_dir: Path,
    *,
    workers: int = IO_WORKERS,
) -> None:
    rows = [
        row
        for row in prepared.kept_rows
        if row.drop_status != "registration_failed"
        and row.work_sequence_file is not None
        and row.corrected_sequence_file is not None
    ]
    if not rows:
        return

    # Pre-create the (shared) destination directories once so the worker threads
    # don't race on mkdir for every frame.
    for parent in {row.work_sequence_file.parent for row in rows}:
        parent.mkdir(parents=True, exist_ok=True)
    for parent in {row.normalized_sub_file.parent for row in rows if row.normalized_sub_file is not None}:
        parent.mkdir(parents=True, exist_ok=True)

    def _replace(row: GroupRow) -> None:
        src = row.corrected_sequence_file
        dst = row.work_sequence_file
        if not src.exists():
            raise FileNotFoundError(f"Missing corrected file: {src}")
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        if row.normalized_sub_file is not None:
            shutil.copy2(dst, row.normalized_sub_file)

    pool_workers = max(1, min(workers, len(rows)))
    with ThreadPoolExecutor(max_workers=pool_workers) as pool:
        # Consume the iterator so any worker exception propagates.
        for _ in pool.map(_replace, rows):
            pass
