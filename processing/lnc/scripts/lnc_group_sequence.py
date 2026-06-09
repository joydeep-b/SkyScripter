#!/usr/bin/env python3
"""Run group LNC directly on a Siril sequence directory."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from lnc_registered_pair import find_siril_path, run_siril


LNC_DIR = Path(__file__).resolve().parents[1]
LNC_GROUP_BINARY = LNC_DIR / "bin" / "lnc_group_subs"
FITS_SUFFIXES = (".fit", ".fits", ".fts")
LOGGER = logging.getLogger(__name__)


@dataclass
class SequenceFrame:
    index: int
    included: bool
    path: Path
    width: int
    height: int
    siril_homography: list[float] | None = None


@dataclass
class SequenceInfo:
    path: Path
    name: str
    start_index: int
    fixed_len: int
    reference_index: int | None
    frames: list[SequenceFrame]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LNC on every included frame in a Siril sequence.")
    parser.add_argument("sequence_dir", type=Path)
    parser.add_argument("sequence_name")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--siril-path", type=Path, default=None)
    parser.add_argument("--binary", type=Path, default=None, help="Override lnc_group_subs binary path.")
    parser.add_argument("--rebuild", action="store_true", help="Run make before checking the C binary.")
    parser.add_argument("--lnc-threads", type=int, default=8)
    parser.add_argument("--lnc-workers", type=int, default=None)
    parser.add_argument(
        "--reference-index",
        type=int,
        default=None,
        help="Preferred sequence frame to use as the LNC photometric reference.",
    )
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument(
        "--background-estimator",
        choices=("trimmed-mean", "trimmed-median", "sample-median"),
        default="trimmed-median",
    )
    parser.add_argument("--scale-min", type=float, default=0.5)
    parser.add_argument("--scale-max", type=float, default=2.0)
    parser.add_argument("--grid-spacing", type=int, default=128)
    parser.add_argument("--window-size", type=int, default=256)
    parser.add_argument("--min-samples", type=int, default=2000)
    parser.add_argument("--trim-fraction", type=float, default=0.10)
    parser.add_argument("--smooth-passes", type=int, default=2)
    parser.add_argument("--min-valid-fraction", type=float, default=0.30)
    parser.add_argument("--sample-patch-size", type=int, default=25)
    parser.add_argument("--sample-stride", type=int, default=32)
    parser.add_argument("--min-patches", type=int, default=8)
    parser.add_argument("--sample-min-valid", type=float, default=0.60)
    parser.add_argument("--sample-reject-k", type=float, default=2.5)
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args()


def default_lnc_workers(lnc_threads: int, cpu_count: int | None = None) -> int:
    if lnc_threads <= 0:
        raise ValueError("--lnc-threads must be positive")
    cores = cpu_count or os.cpu_count() or lnc_threads
    return max(1, cores // lnc_threads)


def ensure_binary(binary: Path, *, rebuild: bool) -> Path:
    if rebuild or not binary.exists():
        subprocess.run(["make", "-C", str(LNC_DIR)], check=True)
    if not binary.exists():
        raise FileNotFoundError(f"LNC group binary was not built: {binary}")
    return binary


def read_fits_shape(path: Path) -> tuple[int, int]:
    header = fits.getheader(path)
    if int(header.get("NAXIS", 0)) < 2:
        raise ValueError(f"{path} has no 2D image data")
    return int(header["NAXIS1"]), int(header["NAXIS2"])


def sequence_frame_path(sequence_dir: Path, sequence_name: str, fixed_len: int, index: int) -> Path:
    stem = f"{sequence_name}{index:0{fixed_len}d}" if fixed_len > 0 else f"{sequence_name}{index}"
    for suffix in FITS_SUFFIXES:
        path = sequence_dir / f"{stem}{suffix}"
        if path.exists():
            return path
    raise FileNotFoundError(f"Could not locate frame {index} for sequence {sequence_name!r}")


def sequence_file_path(sequence_dir: Path, sequence_name: str) -> Path:
    candidates = [
        sequence_dir / f"{sequence_name}.seq",
        sequence_dir / f"{sequence_name}_.seq",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Could not find Siril sequence file for {sequence_name!r}; tried "
        + ", ".join(str(path) for path in candidates)
    )


def parse_sequence_from_files(sequence_dir: Path, sequence_name: str) -> SequenceInfo:
    pattern = re.compile(rf"^{re.escape(sequence_name)}_?(\d+)$")
    indexed_paths: list[tuple[int, Path, str]] = []
    for path in sorted(sequence_dir.iterdir()):
        if path.suffix.lower() not in FITS_SUFFIXES:
            continue
        match = pattern.match(path.stem)
        if match is None:
            continue
        digits = match.group(1)
        indexed_paths.append((int(digits), path, path.stem[: -len(digits)]))
    if not indexed_paths:
        raise FileNotFoundError(f"Could not locate FITS frames for sequence {sequence_name!r} in {sequence_dir}")

    fixed_len = max(len(path.stem) - len(prefix) for _, path, prefix in indexed_paths)
    frames = []
    for index, path, _ in indexed_paths:
        width, height = read_fits_shape(path)
        frames.append(
            SequenceFrame(
                index=index,
                included=True,
                path=path,
                width=width,
                height=height,
            )
        )
    return SequenceInfo(
        path=sequence_dir / f"{sequence_name}.seq",
        name=sequence_name,
        start_index=min(index for index, _, _ in indexed_paths),
        fixed_len=fixed_len,
        reference_index=None,
        frames=frames,
    )


def parse_sequence(sequence_dir: Path, sequence_name: str) -> SequenceInfo:
    try:
        seq_path = sequence_file_path(sequence_dir, sequence_name)
    except FileNotFoundError:
        return parse_sequence_from_files(sequence_dir, sequence_name)
    text = seq_path.read_text(encoding="utf-8", errors="replace")
    start_index = 1
    fixed_len = 0
    reference_index: int | None = None
    included_by_index: dict[int, bool] = {}
    matrices: list[list[float]] = []

    parsed_name = sequence_name
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "S" and len(parts) >= 8:
            parsed_name = parts[1].strip("'\"")
            start_index = int(parts[2])
            fixed_len = int(parts[5])
            reference_index = int(parts[6]) if int(parts[6]) > 0 else None
        elif parts[0] == "I" and len(parts) >= 3:
            included_by_index[int(parts[1])] = bool(int(parts[2]))
        elif parts[0].startswith("R"):
            try:
                h_index = parts.index("H")
            except ValueError:
                continue
            values = [float(value) for value in parts[h_index + 1 : h_index + 10]]
            if len(values) == 9:
                matrices.append(values)

    if not included_by_index:
        raise ValueError(f"Sequence file contains no image rows: {seq_path}")

    frames: list[SequenceFrame] = []
    for position, index in enumerate(sorted(included_by_index)):
        path = sequence_frame_path(sequence_dir, parsed_name, fixed_len, index)
        width, height = read_fits_shape(path)
        matrix = matrices[position] if position < len(matrices) else None
        frames.append(
            SequenceFrame(
                index=index,
                included=included_by_index[index],
                path=path,
                width=width,
                height=height,
                siril_homography=matrix,
            )
        )

    return SequenceInfo(
        path=seq_path,
        name=parsed_name,
        start_index=start_index,
        fixed_len=fixed_len,
        reference_index=reference_index,
        frames=frames,
    )


def choose_reference(sequence: SequenceInfo, preferred_index: int | None = None) -> tuple[int, str]:
    included = [frame for frame in sequence.frames if frame.included]
    if not included:
        raise ValueError("Sequence has no included frames")
    if preferred_index is not None and any(frame.index == preferred_index for frame in included):
        return preferred_index, "preferred_reference"
    if sequence.reference_index is not None and any(frame.index == sequence.reference_index for frame in included):
        return sequence.reference_index, "sequence_reference"
    return included[0].index, "first_included_fallback"


def has_usable_homography(frame: SequenceFrame) -> bool:
    if frame.siril_homography is None:
        return False
    matrix = np.array(frame.siril_homography, dtype=np.float64).reshape(3, 3)
    return bool(np.isfinite(matrix).all() and abs(np.linalg.det(matrix)) >= 1e-12)


def choose_lnc_reference(sequence: SequenceInfo, preferred_index: int) -> tuple[int, str]:
    included = [frame for frame in sequence.frames if frame.included]
    preferred = next((frame for frame in included if frame.index == preferred_index), None)
    if preferred is not None and has_usable_homography(preferred):
        return preferred.index, "preferred_reference"
    fallback = next((frame for frame in included if has_usable_homography(frame)), None)
    if fallback is None:
        raise ValueError("No included frame produced a usable registration homography")
    return fallback.index, "usable_homography_fallback"


def run_registration(sequence: SequenceInfo, reference_index: int, siril_path: str, timeout: float) -> None:
    script = f"requires 1.3.5\nregister {sequence.name} -2pass\n"
    run_siril(siril_path, sequence.path.parent, script, context=f"register {sequence.name}", timeout=timeout)


def flip_y_matrix(height: int) -> np.ndarray:
    return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, float(height - 1)], [0.0, 0.0, 1.0]])


def target_to_reference_homography(sequence: SequenceInfo, reference: SequenceFrame, target: SequenceFrame) -> list[float]:
    if reference.siril_homography is None or target.siril_homography is None:
        raise ValueError(f"Missing registration matrix for sequence index {target.index}")
    ref_matrix = np.array(reference.siril_homography, dtype=np.float64).reshape(3, 3)
    target_matrix = np.array(target.siril_homography, dtype=np.float64).reshape(3, 3)
    siril_h = np.linalg.inv(ref_matrix) @ target_matrix
    array_h = flip_y_matrix(reference.height) @ siril_h @ flip_y_matrix(target.height)
    if not np.isfinite(array_h).all():
        raise ValueError(f"Non-finite homography for sequence index {target.index}")
    return [float(value) for value in array_h.reshape(9)]


def lnc_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {
        "background_estimator": args.background_estimator,
        "scale_min": args.scale_min,
        "scale_max": args.scale_max,
        "grid_spacing": args.grid_spacing,
        "window_size": args.window_size,
        "min_samples": args.min_samples,
        "trim_fraction": args.trim_fraction,
        "smooth_passes": args.smooth_passes,
        "min_valid_fraction": args.min_valid_fraction,
    }
    if args.background_estimator == "sample-median":
        params.update(
            {
                "sample_patch_size": args.sample_patch_size,
                "sample_stride": args.sample_stride,
                "min_patches": args.min_patches,
                "sample_min_valid": args.sample_min_valid,
                "sample_reject_k": args.sample_reject_k,
            }
        )
    return params


def output_path_for(output_dir: Path, source: Path) -> Path:
    return output_dir / f"lnc_{source.stem}.fits"


def build_manifest(
    sequence: SequenceInfo,
    *,
    reference_index: int,
    reference_source: str,
    output_dir: Path,
    params: dict[str, Any],
) -> tuple[dict[str, Any], list[int]]:
    included = [frame for frame in sequence.frames if frame.included and has_usable_homography(frame)]
    skipped = [
        frame.index
        for frame in sequence.frames
        if not frame.included or not has_usable_homography(frame)
    ]
    reference = next(frame for frame in included if frame.index == reference_index)
    targets = []
    for frame in included:
        if frame.index == reference_index:
            continue
        targets.append(
            {
                "sequence_index": frame.index,
                "work_sequence_file": str(frame.path),
                "corrected_sequence_file": str(output_path_for(output_dir, frame.path)),
                "target_to_reference_homography": target_to_reference_homography(sequence, reference, frame),
                "siril_homography": frame.siril_homography,
            }
        )

    summary_path = output_dir / "lnc_group_summary.json"
    manifest = {
        "sequence_name": sequence.name,
        "params": params,
        "reference": {
            "sequence_index": reference.index,
            "work_sequence_file": str(reference.path),
            "corrected_sequence_file": str(output_path_for(output_dir, reference.path)),
            "target_to_reference_homography": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            "siril_homography": reference.siril_homography,
            "reference_source": reference_source,
        },
        "targets": targets,
        "output_summary": str(summary_path),
    }
    return manifest, skipped


def validate_lnc_science_headers(manifest: dict[str, Any]) -> dict[str, Any]:
    checked = 0
    missing_exposure: list[str] = []
    missing_mode: list[str] = []
    entries = [manifest.get("reference", {})]
    entries.extend(manifest.get("targets", []))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        output = entry.get("corrected_sequence_file")
        if not output:
            continue
        path = Path(str(output))
        header = fits.getheader(path)
        checked += 1
        if not any(key in header for key in ("LIVETIME", "EXPTIME", "EXPOSURE")):
            missing_exposure.append(str(path))
        if "LNCMODE" not in header:
            missing_mode.append(str(path))
    if missing_exposure:
        raise RuntimeError(
            "LNC output missing exposure metadata; refusing to cache/stack header-stripped FITS. "
            f"First missing: {missing_exposure[0]} ({len(missing_exposure)} total)"
        )
    if missing_mode:
        raise RuntimeError(
            "LNC output missing LNCMODE provenance. "
            f"First missing: {missing_mode[0]} ({len(missing_mode)} total)"
        )
    return {
        "checked_count": checked,
        "required_exposure_keys": ["LIVETIME", "EXPTIME", "EXPOSURE"],
        "required_lnc_keys": ["LNCMODE"],
    }


def run_group_sequence_lnc(
    sequence_dir: Path,
    sequence_name: str,
    *,
    output_dir: Path | None = None,
    siril_path: Path | None = None,
    binary: Path | None = None,
    rebuild: bool = False,
    lnc_threads: int = 8,
    lnc_workers: int | None = None,
    reference_index: int | None = None,
    diagnostics: bool = False,
    background_estimator: str = "trimmed-median",
    scale_min: float = 0.5,
    scale_max: float = 2.0,
    grid_spacing: int = 128,
    window_size: int = 256,
    min_samples: int = 2000,
    trim_fraction: float = 0.10,
    smooth_passes: int = 2,
    min_valid_fraction: float = 0.30,
    sample_patch_size: int = 25,
    sample_stride: int = 32,
    min_patches: int = 8,
    sample_min_valid: float = 0.60,
    sample_reject_k: float = 2.5,
    timeout: float = 600.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    if background_estimator not in {"trimmed-mean", "trimmed-median", "sample-median"}:
        raise ValueError(f"Unsupported background estimator: {background_estimator}")
    lnc_workers = lnc_workers or default_lnc_workers(lnc_threads)
    sequence_dir = sequence_dir.expanduser().resolve()
    output_dir = (output_dir or sequence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    binary = ensure_binary((binary or LNC_GROUP_BINARY).expanduser().resolve(), rebuild=rebuild)
    resolved_siril_path = find_siril_path(siril_path)

    LOGGER.info("Siril binary: %s", resolved_siril_path)
    LOGGER.info("Sequence directory: %s", sequence_dir)
    LOGGER.info("Sequence name: %s", sequence_name)
    LOGGER.info("Output directory: %s", output_dir)
    LOGGER.info("LNC group binary: %s", binary)
    LOGGER.info("LNC threads: %s", lnc_threads)
    LOGGER.info("LNC workers: %s", lnc_workers)
    LOGGER.info("Background estimator: %s", background_estimator)

    LOGGER.info("Parsing sequence")
    initial_sequence = parse_sequence(sequence_dir, sequence_name)
    LOGGER.info("Number of frames: %s", len(initial_sequence.frames))

    LOGGER.info("Choosing reference")
    preferred_reference_index, reference_source = choose_reference(initial_sequence, reference_index)
    LOGGER.info("Reference index: %s", preferred_reference_index)
    LOGGER.info("Reference source: %s", reference_source)

    LOGGER.info("Running registration")
    run_registration(initial_sequence, preferred_reference_index, resolved_siril_path, timeout)
    sequence = parse_sequence(sequence_dir, sequence_name)
    reference_index, reference_source = choose_lnc_reference(sequence, preferred_reference_index)
    LOGGER.info("LNC reference index: %s", reference_index)
    LOGGER.info("LNC reference source: %s", reference_source)
    params_args = argparse.Namespace(
        background_estimator=background_estimator,
        scale_min=scale_min,
        scale_max=scale_max,
        grid_spacing=grid_spacing,
        window_size=window_size,
        min_samples=min_samples,
        trim_fraction=trim_fraction,
        smooth_passes=smooth_passes,
        min_valid_fraction=min_valid_fraction,
        sample_patch_size=sample_patch_size,
        sample_stride=sample_stride,
        min_patches=min_patches,
        sample_min_valid=sample_min_valid,
        sample_reject_k=sample_reject_k,
    )
    manifest, skipped = build_manifest(
        sequence,
        reference_index=reference_index,
        reference_source=reference_source,
        output_dir=output_dir,
        params=lnc_params(params_args),
    )

    manifest_path = output_dir / "lnc_group_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    LOGGER.info("Running LNC group")
    env = os.environ.copy()
    env["LNC_WRITE_DIAGNOSTICS"] = "1" if diagnostics else "0"
    subprocess.run(
        [
            str(binary),
            "--lnc-threads",
            str(lnc_threads),
            "--lnc-workers",
            str(lnc_workers),
            str(manifest_path),
        ],
        check=True,
        env=env,
    )

    header_validation = validate_lnc_science_headers(manifest)

    LOGGER.info("Writing wrapper summary")
    wrapper_summary = {
        "sequence_dir": str(sequence_dir),
        "sequence_name": sequence.name,
        "reference_index": reference_index,
        "reference_source": reference_source,
        "skipped_sequence_indices": skipped,
        "manifest_path": str(manifest_path),
        "c_summary_path": manifest.get("output_summary"),
        "lnc_threads": lnc_threads,
        "lnc_workers": lnc_workers,
        "output_format": "float32-raw",
        "header_validation": header_validation,
        "elapsed_seconds": time.perf_counter() - started,
    }
    (output_dir / "lnc_group_sequence_report.json").write_text(
        json.dumps(wrapper_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("Done")
    return wrapper_summary


def main() -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    args = parse_args()
    run_group_sequence_lnc(
        args.sequence_dir,
        args.sequence_name,
        output_dir=args.output_dir,
        siril_path=args.siril_path,
        binary=args.binary,
        rebuild=args.rebuild,
        lnc_threads=args.lnc_threads,
        lnc_workers=args.lnc_workers,
        reference_index=args.reference_index,
        diagnostics=args.diagnostics,
        background_estimator=args.background_estimator,
        scale_min=args.scale_min,
        scale_max=args.scale_max,
        grid_spacing=args.grid_spacing,
        window_size=args.window_size,
        min_samples=args.min_samples,
        trim_fraction=args.trim_fraction,
        smooth_passes=args.smooth_passes,
        min_valid_fraction=args.min_valid_fraction,
        sample_patch_size=args.sample_patch_size,
        sample_stride=args.sample_stride,
        min_patches=args.min_patches,
        sample_min_valid=args.sample_min_valid,
        sample_reject_k=args.sample_reject_k,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
