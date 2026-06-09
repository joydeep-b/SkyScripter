#!/usr/bin/env python3
"""Multi-user LNC normalization and stacking pipeline."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from astropy.io import fits

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processing.multiuser_lnc_common import (
    DEFAULT_DROP_MAX_FRACTION,
    DEFAULT_SEQUENCE_NAME,
    IO_WORKERS,
    GroupRow,
    PreparedGroup,
    atomic_replace_corrected_files,
    group_planned_summary,
    find_siril_path,
    parse_sequence_file,
    plan_all_groups,
    rebuild_all_users_index,
    render_siril_template,
    run_siril_script,
    summarize_exception,
    verify_fits_geometry,
    write_failure_log,
    write_json,
)

DEFAULT_REPORT = Path("/scratch/joydeepb/astro_temp/M_40/.moonless_subs/filter_lookup_report.json")
DEFAULT_SCRIPT_DIR = Path(__file__).resolve().parent / "subprocess_scripts"
DEFAULT_STACK_SCRIPT = DEFAULT_SCRIPT_DIR / "lnc_stack_registered_group.ssf"
DEFAULT_LNC_DIR = Path(__file__).resolve().parent / "lnc"
DEFAULT_LNC_SCRIPT_DIR = DEFAULT_LNC_DIR / "scripts"

TOTAL_CPUS = os.cpu_count() or 1
DEFAULT_RESERVE_CPUS = 8
DEFAULT_COPY_WORKERS = max(1, min(64, TOTAL_CPUS - DEFAULT_RESERVE_CPUS))
LOGGER = logging.getLogger("multiuser_lnc_stack")


def load_group_sequence_runner():
    module_path = DEFAULT_LNC_SCRIPT_DIR / "lnc_group_sequence.py"
    spec = importlib.util.spec_from_file_location("lnc_group_sequence", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load LNC group sequence wrapper: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("lnc_group_sequence", module)
    spec.loader.exec_module(module)
    return module.run_group_sequence_lnc


run_group_sequence_lnc = load_group_sequence_runner()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multi-user LNC normalization and Siril stacking per user/equipment/filter group."
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="filter_lookup_report.json")
    parser.add_argument(
        "--measurements",
        type=Path,
        default=None,
        help="quality_report/measurements.csv (default: <report_dir>/quality_report/measurements.csv)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Output root (default: <report_dir>/per_user_lnc_stacks)",
    )
    parser.add_argument("--siril-path", type=Path, default=None)
    parser.add_argument("--stack-script", type=Path, default=DEFAULT_STACK_SCRIPT)
    parser.add_argument("--lnc-dir", type=Path, default=DEFAULT_LNC_DIR)
    parser.add_argument("--min-frames", type=int, default=3)
    parser.add_argument("--drop-max-fraction", type=float, default=DEFAULT_DROP_MAX_FRACTION)
    parser.add_argument(
        "--lnc-threads",
        type=int,
        default=8,
        help="OpenMP threads used inside each LNC target normalization (default: 8)",
    )
    parser.add_argument(
        "--lnc-workers",
        type=int,
        default=None,
        help="Number of LNC targets normalized concurrently (default: auto = CPUs / --lnc-threads)",
    )
    parser.add_argument(
        "--copy-workers",
        type=int,
        default=DEFAULT_COPY_WORKERS,
        help=f"Parallel file materialization workers per group (default: {DEFAULT_COPY_WORKERS})",
    )
    parser.add_argument(
        "--work-file-mode",
        choices=("symlink", "copy"),
        default="symlink",
        help="How to materialize top-level Siril sequence inputs before convert (default: symlink)",
    )
    parser.add_argument("--rebuild-lnc", action="store_true", help="Rebuild the LNC group binary before running")
    parser.add_argument(
        "--background-estimator",
        choices=("trimmed-mean", "trimmed-median", "sample-median"),
        default="trimmed-median",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help=(
            "Keep per-group intermediate files (.process/, corrected_sequence/, materialized inputs) "
            "after a successful stack. By default these are deleted on success to save disk."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Accepted for compatibility; groups now rerun unless the master already exists.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "Write per-target LNC diagnostics (scale_map/offset_map FITS + report). "
            "Disabled by default because it roughly triples per-target write I/O."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N planned groups (useful for a smoke test)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help="Only process groups whose 'user / equipment filter / filter' label contains this substring",
    )
    return parser.parse_args()


def default_measurements_path(report_path: Path) -> Path:
    return report_path.parent / "quality_report" / "measurements.csv"


def default_output_root(report_path: Path) -> Path:
    return report_path.parent / "per_user_lnc_stacks"


def template_values(args: argparse.Namespace) -> dict[str, str]:
    return {
        "SEQUENCE_NAME": DEFAULT_SEQUENCE_NAME,
        "REFERENCE_INDEX": "1",
    }


def group_summary_path(prepared: PreparedGroup) -> Path:
    return prepared.work_dir / "manifests" / "group_summary.json"


def load_group_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_group_summary(prepared: PreparedGroup, payload: dict[str, Any]) -> None:
    write_json(group_summary_path(prepared), payload)


def is_master_complete(prepared: PreparedGroup) -> bool:
    master = prepared.reference_row.master_file
    return master is not None and master.exists()


def sequence_file_path(prepared: PreparedGroup) -> Path:
    process_dir = prepared.work_dir / ".process"
    preferred = [
        process_dir / f"{DEFAULT_SEQUENCE_NAME}.seq",
        process_dir / f"{DEFAULT_SEQUENCE_NAME}_.seq",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate
    matches = sorted(process_dir.glob(f"{DEFAULT_SEQUENCE_NAME}*.seq"))
    if matches:
        return matches[0]
    return preferred[0]


def validate_registration_sequence(
    prepared: PreparedGroup,
    *,
    expected_reference_index: int | None = None,
    allow_internal_reference: bool = False,
) -> dict[str, Any]:
    seq_path = sequence_file_path(prepared)
    if not seq_path.exists():
        raise FileNotFoundError(f"Expected Siril registration sequence not found: {seq_path}")

    seq_info = parse_sequence_file(seq_path)
    actual_reference = int(seq_info["reference_image"])
    if expected_reference_index is None:
        expected_reference_index = int(prepared.reference_row.sequence_index or 1)

    if actual_reference != expected_reference_index and not allow_internal_reference:
        raise ValueError(
            "Siril changed the registration reference "
            f"from {expected_reference_index} to {actual_reference} in {seq_path}"
        )

    fill_missing_sequence_sizes(prepared, seq_info)
    return seq_info


def parse_stack_script_sequences(rendered_script: str) -> tuple[str | None, str | None]:
    """Extract the (registered, stacked) sequence base names from a stack script.

    A stack script may register the base sequence in place (``register pp_light_``)
    or a sequence derived from it, such as the background-extracted one produced by
    ``seqsubsky`` (``register bkg_pp_light_``). The ``register`` target is the
    sequence Siril writes registration matrices into; the ``stack`` target is the
    drizzled ``r_`` sequence it produces. Reading these from the rendered script
    lets post-stack validation inspect whatever sequence Siril actually wrote,
    instead of assuming the base ``pp_light_`` naming.

    Returns ``(register_base, stack_base)``; either is ``None`` if the script does
    not contain the corresponding command.
    """
    register_base: str | None = None
    stack_base: str | None = None
    for line in rendered_script.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "register" and len(parts) >= 2:
            register_base = parts[1]
        elif parts[0] == "stack" and len(parts) >= 2:
            stack_base = parts[1]
    return register_base, stack_base


def configure_logging(output_root: Path) -> Path:
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)
    LOGGER.info("Writing run log to %s", log_path)
    return log_path


def log_stage(message: str) -> None:
    LOGGER.info(message)


def format_eta(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def log_run_progress(completed: int, total: int, failures: int, run_started: float) -> None:
    """Emit a single at-a-glance progress line as each group finishes."""
    elapsed = time.monotonic() - run_started
    avg = elapsed / completed if completed else 0.0
    eta = avg * (total - completed)
    log_stage(
        f"PROGRESS {completed}/{total} groups done "
        f"({completed - failures} ok, {failures} failed); "
        f"avg {avg:.0f}s/group, elapsed {format_eta(elapsed)}, ETA {format_eta(eta)}"
    )


def print_lnc_configuration(args: argparse.Namespace) -> None:
    LOGGER.info(
        "\n".join(
            [
                "LNC configuration:",
                f"  detected CPUs: {TOTAL_CPUS}",
                "  group workers: 1 (serial groups)",
                f"  LNC threads per target: {args.lnc_threads}",
                f"  LNC workers: {args.lnc_workers or 'auto'}",
                f"  background estimator: {args.background_estimator}",
                f"  copy/link workers per group: {args.copy_workers}",
                f"  work file mode: {args.work_file_mode}",
            ]
        )
    )


def group_label(prepared: PreparedGroup) -> str:
    return f"{prepared.user} / {prepared.equipment} / {prepared.filter}"


def cleanup_group_intermediates(prepared: PreparedGroup) -> None:
    """Delete a successfully-stacked group's large intermediates to reclaim disk.

    Removes the Siril working sequence (.process: converted + drizzle r_ frames +
    masks), the redundant corrected_sequence/ (the same frames already persist in
    normalized_subs/), and the top-level materialized inputs. Keeps manifests/ and
    logs/ for provenance. Only call after the group's master is verified written;
    never on failure, so a failed group retains everything for debugging.
    """
    work_dir = prepared.work_dir
    log_stage(f"CLEANUP intermediates {group_label(prepared)}")
    shutil.rmtree(work_dir / ".process", ignore_errors=True)
    shutil.rmtree(work_dir / "corrected_sequence", ignore_errors=True)
    for stale in work_dir.glob(f"{DEFAULT_SEQUENCE_NAME}_*.fit*"):
        if stale.is_file() or stale.is_symlink():
            try:
                stale.unlink()
            except OSError:
                pass


def materialize_sequence_input(row, destination: Path, mode: str) -> int:
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    if mode == "copy":
        shutil.copy2(row.sequence_input_file, destination)
    elif mode == "symlink":
        destination.symlink_to(row.sequence_input_file.resolve())
    else:
        raise ValueError(f"Unsupported work-file mode: {mode}")
    return destination.stat().st_size


def materialize_group_inputs(prepared: PreparedGroup, *, copy_workers: int, mode: str) -> None:
    total = len(prepared.kept_rows)
    prepared.work_dir.mkdir(parents=True, exist_ok=True)
    for stale in prepared.work_dir.glob(f"{DEFAULT_SEQUENCE_NAME}_*.fit*"):
        if stale.is_file() or stale.is_symlink():
            stale.unlink()

    destinations: list[tuple[Any, Path]] = []
    total_bytes = 0
    for row in prepared.kept_rows:
        ext = row.sequence_input_file.suffix.lower() or ".fit"
        destination = prepared.work_dir / f"{row.sequence_name}_{row.sequence_index:05d}{ext}"
        destinations.append((row, destination))
        try:
            total_bytes += row.sequence_input_file.stat().st_size
        except OSError:
            pass

    action = "copy" if mode == "copy" else "link"
    log_stage(
        f"START {action} {group_label(prepared)}: {total} file(s), "
        f"{total_bytes / (1024 ** 3):.1f} GiB referenced, workers={max(1, copy_workers)}"
    )
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, copy_workers)) as executor:
        futures = {
            executor.submit(materialize_sequence_input, row, destination, mode): (row, destination)
            for row, destination in destinations
        }
        for future in as_completed(futures):
            row, destination = futures[future]
    log_stage(f"DONE {action} {group_label(prepared)} in {time.monotonic() - started:.0f}s")


def fill_missing_sequence_sizes(prepared: PreparedGroup, seq_info: dict[str, Any]) -> None:
    if len(seq_info["image_sizes"]) == len(prepared.kept_rows):
        return
    rows = sorted(prepared.kept_rows, key=lambda item: item.sequence_index or 0)
    for row in rows:
        if row.work_sequence_file is None:
            raise ValueError("work_sequence_file missing while filling sequence image sizes")
    # FITS header reads are I/O-bound; fan them out so groups with hundreds/thousands
    # of frames don't pay a long serial stall here. pool.map preserves input order.
    workers = max(1, min(IO_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        seq_info["image_sizes"] = list(
            pool.map(lambda row: verify_and_read_fits_shape(row.work_sequence_file), rows)
        )


def verify_and_read_fits_shape(path: Path) -> tuple[int, int]:
    from processing.multiuser_lnc_common import read_fits_shape
    return read_fits_shape(path)


def mark_registration_failed(
    prepared: PreparedGroup,
    sequence_indices: list[int],
    seq_path: Path,
    *,
    reason: str,
) -> None:
    if not sequence_indices:
        return
    index_set = set(sequence_indices)
    marked = []
    for row in prepared.kept_rows:
        if row.sequence_index in index_set and row.drop_status != "registration_failed":
            row.drop_status = "registration_failed"
            marked.append(int(row.sequence_index or 0))
    if marked:
        LOGGER.warning(
            "Dropping %s frame(s) from subsequent processing for %s because %s in %s: seq#%s",
            len(marked),
            group_label(prepared),
            reason,
            seq_path,
            marked,
        )


def convert_group_inputs(
    prepared: PreparedGroup,
    *,
    siril_path: str,
    values: dict[str, str],
    dry_run: bool,
) -> dict[str, Any]:
    log_path = prepared.work_dir / "logs" / f"convert_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    if dry_run:
        LOGGER.info("[DRY-RUN] Siril convert: %s", prepared.work_dir)
        return {"dry_run": True}
    log_stage(f"START convert sequence {group_label(prepared)}")
    sequence_name = values.get("SEQUENCE_NAME", DEFAULT_SEQUENCE_NAME)
    script = f"requires 1.3.5\nconvert {sequence_name} -out=.process\n"
    run_siril_script(siril_path, prepared.work_dir, script, log_path, f"convert {prepared.key}")
    seq_path = sequence_file_path(prepared)
    process_dir = prepared.work_dir / ".process"
    converted_frames = sorted(process_dir.glob(f"{sequence_name}_*.fit*"))
    if not seq_path.exists() and not converted_frames:
        raise ValueError(
            f"Missing converted frames after convert in {process_dir}"
        )
    log_stage(f"DONE convert sequence {group_label(prepared)}")
    return {
        "sequence_file": str(seq_path) if seq_path.exists() else "",
        "converted_count": len(converted_frames),
    }


def wrapper_corrected_path(row: GroupRow, corrected_dir: Path) -> Path:
    if row.work_sequence_file is None:
        raise ValueError("work_sequence_file missing while mapping LNC wrapper output")
    return corrected_dir / f"lnc_{row.work_sequence_file.stem}.fits"


def apply_lnc_wrapper_results(prepared: PreparedGroup, wrapper_summary: dict[str, Any]) -> None:
    reference_index = int(wrapper_summary.get("reference_index") or prepared.reference_row.sequence_index or 1)
    reference = next((row for row in prepared.kept_rows if row.sequence_index == reference_index), None)
    if reference is None:
        raise ValueError(f"LNC wrapper selected unknown reference index {reference_index}")
    if reference is not prepared.reference_row:
        prepared.reference_row.reference = False
        reference.reference = True
        prepared.reference_row = reference
        log_stage(f"LNC reference fallback {group_label(prepared)}: seq#{reference_index}")

    skipped_indices = {int(index) for index in wrapper_summary.get("skipped_sequence_indices", [])}
    corrected_dir = prepared.work_dir / "corrected_sequence"
    for row in prepared.kept_rows:
        if row.sequence_index in skipped_indices:
            row.drop_status = "registration_failed"
        row.corrected_sequence_file = wrapper_corrected_path(row, corrected_dir)


def run_lnc_for_group(
    prepared: PreparedGroup,
    *,
    args: argparse.Namespace,
    siril_path: str,
    dry_run: bool,
) -> dict[str, Any]:
    manifests_dir = prepared.work_dir / "manifests"
    corrected_dir = prepared.work_dir / "corrected_sequence"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    corrected_dir.mkdir(parents=True, exist_ok=True)
    write_json(manifests_dir / "sequence_manifest.json", prepared.row_manifest_records())

    if dry_run:
        LOGGER.info("[DRY-RUN] LNC group sequence: %s -> %s", prepared.work_dir / ".process", corrected_dir)
        return {"dry_run": True, "output_dir": str(corrected_dir)}

    log_stage(f"START LNC correction {group_label(prepared)}")
    wrapper_summary = run_group_sequence_lnc(
        prepared.work_dir / ".process",
        DEFAULT_SEQUENCE_NAME,
        output_dir=corrected_dir,
        siril_path=Path(siril_path),
        binary=args.lnc_dir / "bin" / "lnc_group_subs",
        rebuild=args.rebuild_lnc,
        lnc_threads=args.lnc_threads,
        lnc_workers=args.lnc_workers,
        reference_index=prepared.reference_row.sequence_index,
        diagnostics=args.diagnostics,
        background_estimator=args.background_estimator,
    )
    apply_lnc_wrapper_results(prepared, wrapper_summary)

    rows_to_verify = [
        row
        for row in prepared.kept_rows
        if row.drop_status != "registration_failed"
        and row.corrected_sequence_file is not None
        and row.work_sequence_file is not None
    ]

    def _verify(row: GroupRow) -> None:
        if not row.corrected_sequence_file.exists():
            raise FileNotFoundError(f"Missing corrected output: {row.corrected_sequence_file}")
        verify_fits_geometry(row.work_sequence_file, row.corrected_sequence_file)

    if rows_to_verify:
        verify_workers = max(1, min(IO_WORKERS, len(rows_to_verify)))
        with ThreadPoolExecutor(max_workers=verify_workers) as pool:
            for _ in pool.map(_verify, rows_to_verify):
                pass

    atomic_replace_corrected_files(prepared, corrected_dir, prepared.work_dir / ".process")
    c_summary_path = Path(str(wrapper_summary.get("c_summary_path") or ""))
    if c_summary_path.exists():
        c_summary = json.loads(c_summary_path.read_text(encoding="utf-8"))
        wrapper_summary["c_summary"] = c_summary
        if isinstance(c_summary.get("targets"), list):
            wrapper_summary["targets"] = c_summary["targets"]
    write_json(manifests_dir / "lnc_group_sequence_report.json", wrapper_summary)
    write_json(manifests_dir / "lnc_group_summary.json", wrapper_summary)
    log_stage(f"DONE LNC correction {group_label(prepared)}")
    return wrapper_summary


def restore_lnc_from_normalized_subs(prepared: PreparedGroup) -> dict[str, Any] | None:
    manifests_dir = prepared.work_dir / "manifests"
    required_rows = [row for row in prepared.kept_rows if row.drop_status != "registration_failed"]
    if not required_rows:
        LOGGER.info("LNC cache miss %s: no reusable rows", group_label(prepared))
        return None

    incomplete_rows = [
        row.sequence_index
        for row in required_rows
        if row.work_sequence_file is None or row.normalized_sub_file is None
    ]
    if incomplete_rows:
        LOGGER.info(
            "LNC cache miss %s: %s row(s) missing cache path metadata; first seq#%s",
            group_label(prepared),
            len(incomplete_rows),
            incomplete_rows[0],
        )
        return None

    rows = required_rows

    missing = [row.normalized_sub_file for row in rows if not row.normalized_sub_file.exists()]
    if missing:
        LOGGER.info(
            "LNC cache miss %s: %s normalized sub(s) missing; first missing: %s",
            group_label(prepared),
            len(missing),
            missing[0],
        )
        return None

    def _verify(row: GroupRow) -> None:
        verify_fits_geometry(row.work_sequence_file, row.normalized_sub_file)
        header = fits.getheader(row.normalized_sub_file)
        if not any(key in header for key in ("LIVETIME", "EXPTIME", "EXPOSURE")):
            raise ValueError(
                f"normalized sub is missing exposure metadata needed for Siril LIVETIME: {row.normalized_sub_file}"
            )
        if "LNCMODE" not in header:
            raise ValueError(f"normalized sub is missing LNC provenance metadata: {row.normalized_sub_file}")

    try:
        verify_workers = max(1, min(IO_WORKERS, len(rows)))
        with ThreadPoolExecutor(max_workers=verify_workers) as pool:
            for _ in pool.map(_verify, rows):
                pass
    except Exception as exc:
        LOGGER.info(
            "LNC cache miss %s: normalized sub geometry validation failed: %s",
            group_label(prepared),
            summarize_exception(exc),
        )
        return None

    log_stage(f"START restore LNC cache {group_label(prepared)}: {len(rows)} file(s)")

    def _restore(row: GroupRow) -> None:
        src = row.normalized_sub_file
        dst = row.work_sequence_file
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        row.corrected_sequence_file = src

    restore_workers = max(1, min(IO_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=restore_workers) as pool:
        for _ in pool.map(_restore, rows):
            pass

    targets = [
        {
            "sequence_index": row.sequence_index,
            "work_sequence_file": str(row.work_sequence_file),
            "corrected_sequence_file": str(row.corrected_sequence_file),
            "normalized_sub_file": str(row.normalized_sub_file),
            "status": "reused",
            "elapsed_seconds": 0,
        }
        for row in rows
    ]
    reference_index = prepared.reference_row.sequence_index or 1
    summary = {
        "sequence_dir": str(prepared.work_dir / ".process"),
        "sequence_name": DEFAULT_SEQUENCE_NAME,
        "reference_index": reference_index,
        "reference_source": "normalized_subs_cache",
        "skipped_sequence_indices": [],
        "manifest_path": "",
        "c_summary_path": str(manifests_dir / "lnc_group_summary.json"),
        "reused_from_normalized_subs": True,
        "cache_source": "normalized_subs",
        "output_dir": str(prepared.work_dir / "corrected_sequence"),
        "reused_count": len(rows),
        "computed_count": 0,
        "target_count": len(rows),
        "failures": 0,
        "targets": targets,
        "c_summary": {
            "target_count": len(rows),
            "failures": 0,
            "targets": targets,
            "reused_from_normalized_subs": True,
        },
    }
    manifests_dir.mkdir(parents=True, exist_ok=True)
    write_json(manifests_dir / "sequence_manifest.json", prepared.row_manifest_records())
    write_json(manifests_dir / "lnc_group_sequence_report.json", summary)
    write_json(manifests_dir / "lnc_group_summary.json", summary)
    log_stage(f"DONE restore LNC cache {group_label(prepared)}")
    return summary


def stack_group(
    prepared: PreparedGroup,
    *,
    siril_path: str,
    stack_script: Path,
    values: dict[str, str],
    dry_run: bool,
) -> Path:
    master = prepared.reference_row.master_file
    if master is None:
        raise ValueError("master_file not set for group")
    log_path = prepared.work_dir / "logs" / f"stack_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    if dry_run:
        LOGGER.info("[DRY-RUN] Siril stack: %s -> %s", prepared.work_dir, master)
        return master

    master.parent.mkdir(parents=True, exist_ok=True)
    log_stage(f"START final registration/stack {group_label(prepared)}")
    script = render_siril_template(stack_script, values)
    run_siril_script(siril_path, prepared.work_dir, script, log_path, f"stack {prepared.key}")

    # The stack script decides which sequence carries the final registration: the
    # base sequence in place (``register pp_light_``) or a derived one such as the
    # background-extracted sequence (``register bkg_pp_light_``). Validate whichever
    # the script actually registered/stacked, rather than assuming the base name.
    register_base, stack_base = parse_stack_script_sequences(script)
    if register_base is None or stack_base is None:
        raise ValueError(
            f"Stack script {stack_script} did not contain both a 'register' and a "
            f"'stack' command; cannot determine which sequence to validate."
        )
    
    output_file = prepared.work_dir / ".process" / "stack.fit"
    if not output_file.exists():
        raise FileNotFoundError(f"Expected Siril stack output not found in {prepared.work_dir / '.process'}: stack.fit")
    if master.exists():
        master.unlink()
    shutil.move(str(output_file), str(master))
    log_stage(f"DONE final registration/stack {group_label(prepared)} -> {master}")
    return master


def process_group(
    prepared: PreparedGroup,
    *,
    args: argparse.Namespace,
    siril_path: str,
    values: dict[str, str],
) -> dict[str, Any]:
    log_stage(f"GROUP start {group_label(prepared)} ({len(prepared.kept_rows)} kept, {len(prepared.dropped_rows)} dropped)")
    summary = load_group_summary(group_summary_path(prepared))
    if is_master_complete(prepared) and not args.force:
        log_stage(f"GROUP skip existing master {group_label(prepared)}")
        summary.update(
            {
                "key": prepared.key,
                "user": prepared.user,
                "equipment": prepared.equipment,
                "filter": prepared.filter,
                "state": "master_written",
                "status": "success",
                "kept_count": len(prepared.kept_rows),
                "dropped_count": len(prepared.dropped_rows),
                "drop_summary": prepared.drop_summary,
                "master_file": str(prepared.reference_row.master_file),
            }
        )
        save_group_summary(prepared, summary)
        if not args.dry_run and not args.keep_intermediates:
            cleanup_group_intermediates(prepared)
        return summary

    started_at = datetime.now().isoformat(timespec="seconds")
    state = "planned"
    summary = {
        "key": prepared.key,
        "user": prepared.user,
        "equipment": prepared.equipment,
        "filter": prepared.filter,
        "state": state,
        "status": "running",
        "started_at": started_at,
        "kept_count": len(prepared.kept_rows),
        "dropped_count": len(prepared.dropped_rows),
        "drop_summary": prepared.drop_summary,
        "lnc_threads": args.lnc_threads,
        "lnc_workers": args.lnc_workers,
    }
    save_group_summary(prepared, summary)

    try:
        if not args.dry_run:
            shutil.rmtree(prepared.work_dir / ".process", ignore_errors=True)
            shutil.rmtree(prepared.work_dir / "corrected_sequence", ignore_errors=True)
        materialize_group_inputs(
            prepared,
            copy_workers=args.copy_workers,
            mode=args.work_file_mode,
        )
        state = "sequence_prepared"
        save_group_summary(prepared, {**summary, "state": state})
        convert_summary = convert_group_inputs(
            prepared,
            siril_path=siril_path,
            values=values,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            summary.update({"state": "sequence_prepared", "status": "dry_run", "convert_summary": convert_summary})
            save_group_summary(prepared, summary)
            return summary
        summary["state"] = state
        summary["convert_summary"] = convert_summary
        save_group_summary(prepared, summary)

        lnc_summary = None if args.force else restore_lnc_from_normalized_subs(prepared)
        if lnc_summary is None:
            lnc_summary = run_lnc_for_group(
                prepared,
                args=args,
                siril_path=siril_path,
                dry_run=args.dry_run,
            )
        if args.dry_run:
            summary.update({"state": "lnc_corrected", "status": "dry_run", "lnc_summary": lnc_summary})
            save_group_summary(prepared, summary)
            return summary

        state = "sequence_replaced"
        summary.update({"state": state, "lnc_summary": lnc_summary})
        summary["registration_failed_count"] = len(
            [row for row in prepared.kept_rows if row.drop_status == "registration_failed"]
        )
        summary["registration_failed_indices"] = [
            int(row.sequence_index or 0)
            for row in prepared.kept_rows
            if row.drop_status == "registration_failed"
        ]
        summary["reference_changed"] = int(lnc_summary.get("reference_index") or 1) != 1
        save_group_summary(prepared, summary)

        stack_values = {
            **values,
            "REFERENCE_INDEX": str(prepared.reference_row.sequence_index or 1),
        }
        master = stack_group(
            prepared,
            siril_path=siril_path,
            stack_script=args.stack_script,
            values=stack_values,
            dry_run=args.dry_run,
        )
        summary.update(
            {
                "state": "master_written",
                "status": "success" if not args.dry_run else "dry_run",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "master_file": str(master),
            }
        )
        save_group_summary(prepared, summary)
        if not args.dry_run and not args.keep_intermediates:
            cleanup_group_intermediates(prepared)
        return summary
    except Exception as exc:
        summary.update(
            {
                "status": "failed",
                "failed_stage": state,
                "error": summarize_exception(exc),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        save_group_summary(prepared, summary)
        log_dir = prepared.work_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        write_failure_log(log_dir / "group_failure.log", str(prepared.key), exc)
        raise


def write_global_manifests(
    output_root: Path,
    prepared_groups: list[PreparedGroup],
    group_summaries: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    stats: dict[str, int],
) -> None:
    summary_by_key = {
        tuple(summary.get("key", ())): summary
        for summary in group_summaries
        if isinstance(summary.get("key"), (list, tuple))
    }
    row_records: list[dict[str, Any]] = []
    for prepared in prepared_groups:
        group_summary = summary_by_key.get(prepared.key, {})
        row_records.extend(prepared.row_manifest_records(group_summary=group_summary))
    for skipped_group in skipped:
        row_records.extend(skipped_group.get("rows", []))

    manifest_json = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": stats,
        "skipped_groups": skipped,
        "groups": group_summaries,
        "rows": row_records,
    }
    write_json(output_root / "lnc_manifest.json", manifest_json)

    csv_path = output_root / "lnc_manifest.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "user",
                "equipment",
                "filter",
                "source_file",
                "sequence_input_file",
                "score",
                "reference",
                "drop_status",
                "sequence_index",
                "work_sequence_file",
                "corrected_sequence_file",
                "normalized_sub_file",
                "status",
                "state",
                "master_file",
                "error",
            ],
        )
        writer.writeheader()
        for row in row_records:
            writer.writerow({field: row.get(field) for field in writer.fieldnames})

    runtime_rows = []
    for summary in group_summaries:
        lnc_summary = summary.get("lnc_summary")
        if isinstance(lnc_summary, dict) and isinstance(lnc_summary.get("targets"), list):
            for target in lnc_summary["targets"]:
                runtime_rows.append(
                    {
                        "user": summary.get("user"),
                        "equipment": summary.get("equipment"),
                        "filter": summary.get("filter"),
                        "status": target.get("status", summary.get("status")),
                        "sequence_index": target.get("sequence_index"),
                        "elapsed_seconds": target.get("elapsed_seconds"),
                        "valid_grid_fraction": target.get("valid_fraction"),
                        "openmp_threads": summary.get("lnc_threads"),
                        "output_file": target.get("corrected_sequence_file"),
                    }
                )
    runtime_path = output_root / "lnc_runtime_summary.csv"
    with runtime_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "user",
                "equipment",
                "filter",
                "status",
                "sequence_index",
                "elapsed_seconds",
                "valid_grid_fraction",
                "openmp_threads",
                "output_file",
            ],
        )
        writer.writeheader()
        writer.writerows(runtime_rows)

    write_json(output_root / "lnc_group_summary.json", {"groups": group_summaries, "skipped": skipped})


def main() -> int:
    args = parse_args()
    report_path = args.report.expanduser().resolve()
    measurements_path = (args.measurements or default_measurements_path(report_path)).expanduser().resolve()
    output_root = (args.output_root or default_output_root(report_path)).expanduser().resolve()
    configure_logging(output_root)

    if not report_path.exists():
        LOGGER.error("Report not found: %s", report_path)
        return 2
    if not measurements_path.exists():
        LOGGER.error("Measurements not found: %s", measurements_path)
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    log_stage(f"Planning groups from report: {report_path}")
    prepared_groups, skipped, stats = plan_all_groups(
        report_path,
        measurements_path,
        output_root,
        min_frames=args.min_frames,
        drop_max_fraction=args.drop_max_fraction,
        materialize=False,
    )
    log_stage(f"Planned {len(prepared_groups)} group(s); skipped {len(skipped)} group(s)")

    if args.only:
        prepared_groups = [g for g in prepared_groups if args.only in group_label(g)]
        log_stage(f"Filtered to {len(prepared_groups)} group(s) matching --only={args.only!r}")
    if args.limit is not None:
        prepared_groups = prepared_groups[: max(0, args.limit)]
        log_stage(f"Limited to first {len(prepared_groups)} group(s) via --limit")

    print_lnc_configuration(args)

    if args.dry_run:
        LOGGER.info("Planned %s group(s); skipped %s", len(prepared_groups), len(skipped))
        group_summaries = [group_planned_summary(prepared, status="dry_run") for prepared in prepared_groups]
        rebuild_all_users_index(output_root, dry_run=True)
        write_global_manifests(output_root, prepared_groups, group_summaries, skipped, stats)
        return 0

    siril_path = find_siril_path(args.siril_path)
    values = template_values(args)
    group_summaries: list[dict[str, Any]] = []
    failures = 0
    total_groups = len(prepared_groups)
    completed = 0
    run_started = time.monotonic()

    for prepared in prepared_groups:
        try:
            group_summaries.append(process_group(prepared, args=args, siril_path=siril_path, values=values))
        except Exception as exc:
            failures += 1
            LOGGER.exception("FAILED %s: %s", prepared.key, exc)
        completed += 1
        log_run_progress(completed, total_groups, failures, run_started)

    rebuild_all_users_index(output_root, dry_run=False)
    write_global_manifests(output_root, prepared_groups, group_summaries, skipped, stats)
    LOGGER.info(
        f"Completed {len(group_summaries)} group(s) with {failures} failure(s); "
        f"skipped {len(skipped)} group(s) during planning"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
