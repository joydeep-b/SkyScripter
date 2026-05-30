#!/usr/bin/env python3
"""Local normalization correction wrapper.

This script handles file-format conversion and star masking, then delegates the
hot pixel math to processing/lnc/local_normalize.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from astropy.io import fits


REPO_ROOT = Path(__file__).resolve().parents[1]
LNC_DIR = Path(__file__).resolve().parent / "lnc"
LNC_BINARY = LNC_DIR / "local_normalize"
LNC_TRIMMED_MEDIAN_BINARY = LNC_DIR / "local_normalize_trimmed_median"
LNC_V2_BINARY = LNC_DIR / "local_normalize_v2"
FITS_SUFFIXES = {".fit", ".fits", ".fts"}
XISF_SUFFIXES = {".xisf"}
FITS_ADU_MAX = 65535.0
VALUE_SCALE_ADU = "adu"
VALUE_SCALE_NORMALIZED = "normalized_float"


@dataclass(frozen=True)
class Star:
    x: float
    y: float
    fwhm: float | None


def log(message: str) -> None:
    print(message, flush=True)


def format_seconds(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000.0:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f} s"
    minutes, remainder = divmod(seconds, 60.0)
    return f"{int(minutes)}m {remainder:.1f}s"


class StepTimer:
    def __init__(self) -> None:
        self.timings: dict[str, float] = {}

    def run(self, label: str, func):
        log(f"[LNC] {label}...")
        start = time.perf_counter()
        try:
            return func()
        finally:
            elapsed = time.perf_counter() - start
            self.timings[label] = elapsed
            log(f"[LNC] {label}: {format_seconds(elapsed)}")


def find_siril_path(explicit: Path | None) -> str:
    if explicit is not None:
        path = explicit.expanduser()
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"Siril executable not found: {path}")

    if sys.platform == "darwin":
        for mac_path in (
            Path("/Applications/Siril.app/Contents/MacOS/Siril"),
            Path("/Applications/Siril.app/Contents/MacOS/siril-cli"),
        ):
            if mac_path.exists():
                return str(mac_path)

    for name in ("siril-cli", "siril"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError("Could not find Siril. Pass --siril-path explicitly.")


def siril_quote(path: Path) -> str:
    value = str(path)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_siril(
    siril_path: str,
    work_dir: Path,
    script: str,
    *,
    context: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if not script.endswith("\n"):
        script += "\n"
    if not re.search(r"(?im)^\s*exit\s*$", script):
        script += "exit\n"
    result = subprocess.run(
        [siril_path, "-d", str(work_dir), "-s", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Siril failed while {context} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return result


def ensure_binary(binary: Path, *, rebuild: bool) -> Path:
    if rebuild or not binary.exists():
        subprocess.run(["make", "-C", str(LNC_DIR)], check=True)
    if not binary.exists():
        raise FileNotFoundError(f"local_normalize binary was not built: {binary}")
    return binary


def select_lnc_binary(args: argparse.Namespace) -> Path:
    if args.binary is not None:
        return args.binary.expanduser().resolve()
    if args.background_estimator == "trimmed-median":
        return LNC_TRIMMED_MEDIAN_BINARY
    if args.background_estimator == "sample-median":
        return LNC_V2_BINARY
    return LNC_BINARY


def read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def siril_time_to_seconds(value: str, unit: str) -> float:
    seconds = float(value)
    if unit.lower() == "ms":
        seconds /= 1000.0
    return seconds


def parse_siril_script_report(output: str) -> dict[str, float | int | None]:
    report: dict[str, float | int | None] = {
        "script_total_seconds": None,
        "command_timings_seconds": {},
    }
    current_command: str | None = None
    command_counts: dict[str, int] = {}
    command_timings: dict[str, float] = {}
    for line in output.splitlines():
        command_match = re.search(r"Running command:\s*([A-Za-z0-9_+-]+)", line)
        if command_match:
            current_command = command_match.group(1).lower()
            continue
        time_match = re.search(r"Execution time:\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s)", line, re.I)
        if time_match and current_command:
            command_counts[current_command] = command_counts.get(current_command, 0) + 1
            key = current_command
            if command_counts[current_command] > 1:
                key = f"{current_command}_{command_counts[current_command]}"
            command_timings[key] = siril_time_to_seconds(time_match.group(1), time_match.group(2))
            continue
    total_match = re.search(r"Total execution time:\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|s)", output, re.I)
    if total_match:
        report["script_total_seconds"] = siril_time_to_seconds(total_match.group(1), total_match.group(2))
    report["command_timings_seconds"] = command_timings
    return report


def parse_siril_findstar_report(output: str) -> dict[str, float | int | None]:
    report: dict[str, float | int | None] = {
        "stars": None,
        "fwhm": None,
        "findstar_seconds": None,
        "script_total_seconds": None,
    }
    matches = re.findall(
        r"Found\s+(\d+)\s+\w+\s+profile stars in image, channel #\d+ \(FWHM ([0-9]+(?:\.[0-9]+)?)\)",
        output,
    )
    if matches:
        stars, fwhm = matches[-1]
        report["stars"] = int(stars)
        report["fwhm"] = float(fwhm)

    report.update(parse_siril_script_report(output))
    command_timings = report.get("command_timings_seconds")
    if isinstance(command_timings, dict) and command_timings.get("findstar") is not None:
        report["findstar_seconds"] = command_timings["findstar"]

    return report


def convert_xisf_to_fits(source: Path, temp_dir: Path, siril_path: str, timeout: float) -> tuple[Path, dict]:
    destination = temp_dir / f"{source.stem}.fit"
    script = f"""requires 1.2.0
load {siril_quote(source)}
save {siril_quote(destination)}
close
"""
    result = run_siril(siril_path, temp_dir, script, context=f"converting {source.name}", timeout=timeout)
    candidates = [destination, destination.with_suffix(".fits"), temp_dir / f"{source.stem}.fits"]
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            report = parse_siril_script_report(f"{result.stdout}\n{result.stderr}")
            report["converted"] = True
            return candidate, report
    raise FileNotFoundError(f"Siril did not produce a FITS file for {source}")


def normalize_input_path(source: Path, temp_dir: Path, siril_path: str, timeout: float) -> tuple[Path, dict]:
    suffix = source.suffix.lower()
    if suffix in FITS_SUFFIXES:
        return source, {"converted": False, "script_total_seconds": None}
    if suffix in XISF_SUFFIXES:
        return convert_xisf_to_fits(source, temp_dir, siril_path, timeout)
    raise ValueError(f"Unsupported input format: {source}")


def run_findstar(source: Path, star_list: Path, siril_path: str, timeout: float) -> dict[str, float | int | None]:
    star_list.parent.mkdir(parents=True, exist_ok=True)
    output_name = star_list.name
    tuned_script = f"""requires 1.2.0
load {siril_quote(source)}
setfindstar -radius=3 -sigma=0.5 -roundness=0.8 -moffat -minbeta=1.5 -relax=on
findstar -out={output_name}
close
"""
    default_script = f"""requires 1.2.0
load {siril_quote(source)}
findstar -out={output_name}
close
"""
    try:
        result = run_siril(
            siril_path,
            star_list.parent,
            tuned_script,
            context=f"detecting stars in {source.name}",
            timeout=timeout,
        )
    except RuntimeError:
        if star_list.exists():
            star_list.unlink()
        result = run_siril(
            siril_path,
            star_list.parent,
            default_script,
            context=f"detecting stars in {source.name} with default parameters",
            timeout=timeout,
        )
    if not star_list.exists() or star_list.stat().st_size == 0:
        raise FileNotFoundError(f"Siril did not write a star list: {star_list}")
    return parse_siril_findstar_report(f"{result.stdout}\n{result.stderr}")


def numeric_values(line: str) -> list[float]:
    values = []
    for token in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", line):
        try:
            values.append(float(token))
        except ValueError:
            pass
    return values


def parse_header_indices(line: str) -> tuple[int, int, int | None] | None:
    normalized = re.sub(r"^[#;\s]+", "", line.strip()).lower()
    if not normalized:
        return None
    columns = [col.strip().lower() for col in re.split(r"[,;\t ]+", normalized) if col.strip()]
    if len(columns) < 2:
        return None

    def first_index(names: Iterable[str]) -> int | None:
        for i, col in enumerate(columns):
            clean = col.strip("()[]{}")
            if clean in names:
                return i
        return None

    x_idx = first_index({"x", "xpos", "x_position", "xpos[pix]", "x[pix]"})
    y_idx = first_index({"y", "ypos", "y_position", "ypos[pix]", "y[pix]"})
    fwhm_idx = first_index({"fwhm", "fwhmx", "fwhm_x", "fwhm[pix]", "fwhmx[pix]"})
    if x_idx is None or y_idx is None:
        return None
    return x_idx, y_idx, fwhm_idx


def parse_star_list(path: Path) -> list[Star]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header: tuple[int, int, int | None] | None = None
    stars: list[Star] = []

    for line in lines:
        if not line.strip():
            continue
        maybe_header = parse_header_indices(line)
        if maybe_header is not None:
            header = maybe_header
            continue

        values = numeric_values(line)
        if len(values) < 2:
            continue

        if header is not None:
            x_idx, y_idx, fwhm_idx = header
            if max(x_idx, y_idx, fwhm_idx or 0) >= len(values):
                continue
            x = values[x_idx]
            y = values[y_idx]
            fwhm = values[fwhm_idx] if fwhm_idx is not None else None
        else:
            x = values[0]
            y = values[1]
            fwhm = next((v for v in values[2:] if 0.2 <= v <= 100.0), None)

        if math.isfinite(x) and math.isfinite(y):
            stars.append(Star(x=x, y=y, fwhm=fwhm if fwhm and math.isfinite(fwhm) else None))

    if not stars:
        raise ValueError(f"No usable stars parsed from {path}")
    return stars


def mask_circle(mask: np.ndarray, x: float, y: float, radius: float) -> None:
    height, width = mask.shape
    x0 = max(0, int(math.floor(x - radius)))
    x1 = min(width - 1, int(math.ceil(x + radius)))
    y0 = max(0, int(math.floor(y - radius)))
    y1 = min(height - 1, int(math.ceil(y + radius)))
    if x0 > x1 or y0 > y1:
        return
    yy, xx = np.ogrid[y0 : y1 + 1, x0 : x1 + 1]
    local = (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius
    mask[y0 : y1 + 1, x0 : x1 + 1][local] = True


def dilate_bool(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0 or not mask.any():
        return mask
    try:
        from scipy import ndimage

        y, x = np.ogrid[-radius : radius + 1, -radius : radius + 1]
        structure = x * x + y * y <= radius * radius
        return ndimage.binary_dilation(mask, structure=structure)
    except Exception:
        result = mask.copy()
        ys, xs = np.nonzero(mask)
        for x, y in zip(xs, ys, strict=False):
            mask_circle(result, float(x), float(y), float(radius))
        return result


def read_shape(path: Path) -> tuple[int, int]:
    with fits.open(path, memmap=False) as hdul:
        data = hdul[0].data
        if data is None or data.ndim != 2:
            raise ValueError(f"{path} is not a 2D FITS image")
        return int(data.shape[1]), int(data.shape[0])


def add_saturation_mask(mask: np.ndarray, paths: list[Path], threshold: float, dilation: int) -> int:
    saturated = np.zeros(mask.shape, dtype=bool)
    for path in paths:
        with fits.open(path, memmap=False) as hdul:
            data = np.asarray(hdul[0].data)
            if data.shape != mask.shape:
                raise ValueError(f"{path} dimensions changed unexpectedly")
            saturated |= np.isfinite(data) & (data >= threshold)
    saturated = dilate_bool(saturated, dilation)
    count = int(saturated.sum())
    mask |= saturated
    return count


def build_mask(
    *,
    ref_path: Path,
    target_path: Path,
    ref_stars: list[Star],
    target_stars: list[Star],
    output_path: Path,
    radius_min: float,
    radius_factor: float,
    radius_max: float,
    saturation_threshold: float | None,
    saturation_dilation: int,
) -> dict[str, float | int]:
    width, height = read_shape(ref_path)
    target_width, target_height = read_shape(target_path)
    if (width, height) != (target_width, target_height):
        raise ValueError(
            f"Reference and target dimensions differ: {width}x{height} vs {target_width}x{target_height}"
        )

    mask = np.zeros((height, width), dtype=bool)
    for star in [*ref_stars, *target_stars]:
        fwhm = star.fwhm if star.fwhm is not None and star.fwhm > 0 else radius_min / radius_factor
        radius = min(radius_max, max(radius_min, radius_factor * fwhm))
        mask_circle(mask, star.x, (height - 1) - star.y, radius)

    star_masked = int(mask.sum())
    saturated_masked = 0
    if saturation_threshold is not None:
        saturated_masked = add_saturation_mask(
            mask, [ref_path, target_path], saturation_threshold, saturation_dilation
        )

    fits.writeto(output_path, mask.astype(np.uint8), overwrite=True)
    return {
        "width": width,
        "height": height,
        "ref_stars": len(ref_stars),
        "target_stars": len(target_stars),
        "star_masked_pixels": star_masked,
        "saturated_masked_pixels": saturated_masked,
        "total_masked_pixels": int(mask.sum()),
        "masked_fraction": float(mask.mean()),
    }


def write_masked_fits(
    source_path: Path,
    mask_path: Path,
    output_path: Path,
    value_scale: str = VALUE_SCALE_ADU,
) -> None:
    with fits.open(source_path, memmap=False) as source_hdul:
        data = np.asarray(source_hdul[0].data, dtype=np.float32).copy()
        header = source_hdul[0].header.copy()

    mask = np.asarray(fits.getdata(mask_path, memmap=False), dtype=bool)
    if mask.shape != data.shape:
        raise ValueError(f"{mask_path} dimensions do not match {source_path}")

    data = normalize_scaled_data(data, value_scale, preserve_nan=True)
    data[mask] = np.nan
    remove_data_scaling_cards(header)
    header["LNCMASKD"] = (True, "Masked by LNC exclusion mask")
    header["LNCNORM"] = (True, "Image values normalized/clipped to [0, 1]")
    header["LNCVSCL"] = (value_scale, "Input value scale before display normalization")
    header["LNCSRC"] = (source_path.name[:68], "Source filename for masked FITS")
    fits.writeto(output_path, data, header=header, overwrite=True)


def write_intermediate_masked_fits(
    *,
    reference_fits: Path,
    target_fits: Path,
    mask_path: Path,
    diag_dir: Path,
    value_scale: str = VALUE_SCALE_ADU,
) -> dict[str, str]:
    outputs = {
        "reference_masked": diag_dir / "reference_masked.fits",
        "target_masked": diag_dir / "target_masked.fits",
    }
    write_masked_fits(reference_fits, mask_path, outputs["reference_masked"], value_scale)
    write_masked_fits(target_fits, mask_path, outputs["target_masked"], value_scale)
    return {key: str(path) for key, path in outputs.items()}


def default_diag_dir(output: Path) -> Path:
    return output.with_suffix("") .with_name(output.stem + "_lnc_diagnostics")


def write_wrapper_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compact_json_for_history(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def append_history_chunks(header: fits.Header, prefix: str, text: str, chunk_size: int = 60) -> None:
    for start in range(0, len(text), chunk_size):
        chunk = text[start : start + chunk_size]
        marker = prefix if start == 0 else "LNC cont"
        header.add_history(f"{marker}: {chunk}")


def remove_data_scaling_cards(header: fits.Header) -> None:
    for key in ("BZERO", "BSCALE", "BLANK", "DATAMIN", "DATAMAX"):
        header.pop(key, None)


def infer_fits_value_scale(path: Path) -> dict[str, float | str]:
    with fits.open(path, memmap=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise ValueError(f"{path} has no finite pixels")
    median = float(np.median(finite))
    p999 = float(np.percentile(finite, 99.9))
    min_value = float(np.min(finite))
    max_value = float(np.max(finite))
    scale = VALUE_SCALE_NORMALIZED if p999 <= 1.5 and median <= 1.0 and min_value >= -0.25 else VALUE_SCALE_ADU
    return {
        "scale": scale,
        "min": min_value,
        "median": median,
        "p99_9": p999,
        "max": max_value,
    }


def choose_pipeline_value_scale(reference_fits: Path, target_fits: Path) -> dict:
    reference_scale = infer_fits_value_scale(reference_fits)
    target_scale = infer_fits_value_scale(target_fits)
    if reference_scale["scale"] != target_scale["scale"]:
        raise ValueError(
            "Reference and target appear to use different value scales: "
            f"{reference_scale['scale']} vs {target_scale['scale']}"
        )
    return {
        "scale": reference_scale["scale"],
        "reference": reference_scale,
        "target": target_scale,
    }


def normalize_scaled_data(
    data: np.ndarray,
    value_scale: str = VALUE_SCALE_ADU,
    *,
    preserve_nan: bool = False,
) -> np.ndarray:
    normalized = np.asarray(data, dtype=np.float32).copy()
    if value_scale == VALUE_SCALE_ADU:
        upper = FITS_ADU_MAX
        divisor = FITS_ADU_MAX
    elif value_scale == VALUE_SCALE_NORMALIZED:
        upper = 1.0
        divisor = 1.0
    else:
        raise ValueError(f"Unsupported value scale: {value_scale}")
    finite = np.isfinite(normalized)
    if preserve_nan:
        normalized[finite] = np.clip(normalized[finite], 0.0, upper) / divisor
        normalized[np.isposinf(normalized)] = 1.0
        normalized[np.isneginf(normalized)] = 0.0
        return normalized.astype(np.float32)
    finite_data = np.nan_to_num(normalized, nan=0.0, posinf=upper, neginf=0.0)
    return (np.clip(finite_data, 0.0, upper) / divisor).astype(np.float32)


def normalize_adu_data(data: np.ndarray, *, preserve_nan: bool = False) -> np.ndarray:
    return normalize_scaled_data(data, VALUE_SCALE_ADU, preserve_nan=preserve_nan)


def normalize_unitless_data(data: np.ndarray, *, preserve_nan: bool = False) -> np.ndarray:
    values = np.asarray(data, dtype=np.float32).copy()
    finite = np.isfinite(values)
    if finite.any():
        finite_values = values[finite]
        vmin = float(np.min(finite_values))
        vmax = float(np.max(finite_values))
        if vmax > vmin:
            values[finite] = (finite_values - vmin) / (vmax - vmin)
        else:
            values[finite] = np.clip(finite_values, 0.0, 1.0)
    if preserve_nan:
        values[np.isposinf(values)] = 1.0
        values[np.isneginf(values)] = 0.0
        return values.astype(np.float32)
    return np.nan_to_num(values, nan=0.0, posinf=1.0, neginf=0.0).astype(np.float32)


def normalize_scaled_fits_in_place(path: Path, value_scale: str = VALUE_SCALE_ADU) -> None:
    if not path.exists():
        return
    with fits.open(path, memmap=False) as hdul:
        data = normalize_scaled_data(
            np.asarray(hdul[0].data, dtype=np.float32),
            value_scale,
            preserve_nan=True,
        )
        header = hdul[0].header.copy()

    remove_data_scaling_cards(header)
    header["LNCNORM"] = (True, "Image values normalized/clipped to [0, 1]")
    header["LNCVSCL"] = (value_scale, "Input value scale before display normalization")
    fits.writeto(path, data, header=header, overwrite=True)


def normalize_adu_fits_in_place(path: Path) -> None:
    normalize_scaled_fits_in_place(path, VALUE_SCALE_ADU)


def normalize_unitless_fits_in_place(path: Path) -> None:
    if not path.exists():
        return
    with fits.open(path, memmap=False) as hdul:
        raw = np.asarray(hdul[0].data, dtype=np.float32)
        data = normalize_unitless_data(raw, preserve_nan=True)
        header = hdul[0].header.copy()
        finite = raw[np.isfinite(raw)]
        raw_min = float(np.min(finite)) if finite.size else math.nan
        raw_max = float(np.max(finite)) if finite.size else math.nan

    remove_data_scaling_cards(header)
    header["LNCNORM"] = (True, "Unitless values normalized to [0, 1]")
    header["LNCVMIN"] = (raw_min, "Original finite minimum before normalization")
    header["LNCVMAX"] = (raw_max, "Original finite maximum before normalization")
    fits.writeto(path, data, header=header, overwrite=True)


def normalize_core_diagnostics(
    diag_dir: Path,
    *,
    save_backgrounds: bool,
    value_scale: str = VALUE_SCALE_ADU,
) -> dict[str, str]:
    outputs = {
        "scale_map": diag_dir / "scale_map.fits",
        "offset_map": diag_dir / "offset_map.fits",
        "accepted_patch_fraction": diag_dir / "accepted_patch_fraction.fits",
    }
    if save_backgrounds:
        outputs.update(
            {
                "ref_background": diag_dir / "ref_background.fits",
                "target_background": diag_dir / "target_background.fits",
            }
        )
    normalize_unitless_fits_in_place(outputs["scale_map"])
    normalize_unitless_fits_in_place(outputs["accepted_patch_fraction"])
    normalize_unitless_fits_in_place(outputs["offset_map"])
    if save_backgrounds:
        normalize_scaled_fits_in_place(outputs["ref_background"], value_scale)
        normalize_scaled_fits_in_place(outputs["target_background"], value_scale)
    return {key: str(path) for key, path in outputs.items() if path.exists()}


def normalize_core_adu_diagnostics(diag_dir: Path, *, save_backgrounds: bool) -> dict[str, str]:
    return normalize_core_diagnostics(diag_dir, save_backgrounds=save_backgrounds, value_scale=VALUE_SCALE_ADU)


def apply_output_format(
    output_path: Path,
    output_format: str,
    value_scale: str = VALUE_SCALE_ADU,
) -> None:
    with fits.open(output_path, memmap=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()

    normalized = normalize_scaled_data(data, value_scale)
    if output_format == "uint16":
        output_data = np.rint(normalized * FITS_ADU_MAX).astype(np.uint16)
    elif output_format == "float32":
        output_data = normalized.astype(np.float32)
    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    remove_data_scaling_cards(header)
    header["LNCFMT"] = (output_format, "LNC final science image encoding")
    header["LNCVSCL"] = (value_scale, "Input value scale before final encoding")
    fits.writeto(output_path, output_data, header=header, overwrite=True)


def preserve_header_and_add_lnc_metadata(
    *,
    output_path: Path,
    target_fits: Path,
    reference_source: Path,
    target_source: Path,
    reference_fits: Path,
    parameters: dict,
    mask_stats: dict,
    core_report: Path,
) -> None:
    with fits.open(output_path, mode="update", memmap=False) as output_hdul:
        corrected_data = output_hdul[0].data
        corrected_header = output_hdul[0].header.copy()

        with fits.open(target_fits, memmap=False) as target_hdul:
            source_header = target_hdul[0].header.copy()

        remove_data_scaling_cards(source_header)
        for key in ("SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "EXTEND"):
            if key in corrected_header:
                source_header[key] = corrected_header[key]

        source_header["LNCVRS"] = ("1", "Local normalization correction version")
        source_header["LNCREF"] = (reference_source.name[:68], "Reference source filename")
        source_header["LNCTARG"] = (target_source.name[:68], "Target source filename")
        source_header["LNCMASK"] = (int(mask_stats["total_masked_pixels"]), "LNC masked pixels")
        source_header["LNCMFRAC"] = (float(mask_stats["masked_fraction"]), "LNC masked fraction")
        if "LNCFMT" in corrected_header:
            source_header["LNCFMT"] = corrected_header["LNCFMT"]
        if "LNCVSCL" in corrected_header:
            source_header["LNCVSCL"] = corrected_header["LNCVSCL"]
        source_header.add_history("LNC: corrected = scale(x,y) * target + offset(x,y)")
        append_history_chunks(source_header, "LNC ref", str(reference_source))
        append_history_chunks(source_header, "LNC target", str(target_source))
        append_history_chunks(source_header, "LNC ref FITS", str(reference_fits))
        append_history_chunks(source_header, "LNC target FITS", str(target_fits))
        append_history_chunks(source_header, "LNC report", str(core_report))
        append_history_chunks(source_header, "LNC params", compact_json_for_history(parameters))
        append_history_chunks(source_header, "LNC mask", compact_json_for_history(mask_stats))

        output_hdul[0].header = source_header
        output_hdul[0].data = corrected_data
        output_hdul.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local normalization correction on registered subs.")
    parser.add_argument("reference", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--siril-path", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the C normalizer before running.")
    parser.add_argument("--diag-dir", type=Path, help="Diagnostic output directory.")
    parser.add_argument(
        "--background-estimator",
        choices=("trimmed-mean", "trimmed-median", "sample-median"),
        default="trimmed-mean",
        help=(
            "C-core estimator: V1 trimmed-mean, V1 trimmed-median, "
            "or experimental Siril-inspired sample-median."
        ),
    )
    parser.add_argument(
        "--save-intermediate-fits",
        action="store_true",
        help="Save ref/target background maps and masked ref/target FITS in the diagnostic directory.",
    )
    parser.add_argument(
        "--output-format",
        choices=("uint16", "float32"),
        default="uint16",
        help=(
            "Final corrected FITS encoding: uint16 clips to [0, 65535]; "
            "float32 clips and normalizes to [0, 1]."
        ),
    )
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--grid-spacing", type=int, default=128)
    parser.add_argument("--window-size", type=int, default=256)
    parser.add_argument("--min-samples", type=int, default=2000)
    parser.add_argument("--trim-fraction", type=float, default=0.10)
    parser.add_argument("--scale-min", type=float, default=0.5)
    parser.add_argument("--scale-max", type=float, default=2.0)
    parser.add_argument("--smooth-passes", type=int, default=2)
    parser.add_argument("--min-valid-fraction", type=float, default=0.30)
    parser.add_argument("--sample-patch-size", type=int, default=25)
    parser.add_argument("--sample-stride", type=int, default=32)
    parser.add_argument("--min-patches", type=int, default=8)
    parser.add_argument("--sample-min-valid", type=float, default=0.60)
    parser.add_argument("--sample-reject-k", type=float, default=2.5)
    parser.add_argument("--mask-radius-min", type=float, default=8.0)
    parser.add_argument("--mask-radius-factor", type=float, default=5.0)
    parser.add_argument("--mask-radius-max", type=float, default=50.0)
    parser.add_argument(
        "--saturation-threshold",
        type=float,
        default=65535.0,
        help="Mask pixels at or above this value; use negative value to disable.",
    )
    parser.add_argument("--saturation-dilation", type=int, default=2)
    return parser.parse_args()


def print_summary(
    *,
    output: Path,
    diag_dir: Path,
    timings: dict[str, float],
    mask_stats: dict,
    core_report: dict,
    wrapper_report_path: Path,
    siril_reports: dict[str, dict],
    intermediate_outputs: dict[str, str],
) -> None:
    measured_steps = {key: value for key, value in timings.items() if key != "Total wall time"}
    total = sum(measured_steps.values())
    log("")
    log("Local normalization complete")
    log(f"  Output:      {output}")
    log(f"  Diagnostics: {diag_dir}")
    log(f"  Report:      {wrapper_report_path}")
    if intermediate_outputs:
        log("  Intermediate FITS:")
        for name, path in intermediate_outputs.items():
            log(f"    {name}: {path}")
    log("")
    log("Processing times:")
    for label, seconds in timings.items():
        log(f"  {label:<28} {format_seconds(seconds)}")
    if "Total wall time" not in timings:
        log(f"  {'Total measured':<28} {format_seconds(total)}")
    if core_report.get("elapsed_seconds") is not None:
        log(f"  {'C core reported':<28} {format_seconds(float(core_report['elapsed_seconds']))}")
    log("")
    log("Mask/star summary:")
    log(f"  Reference stars: {mask_stats['ref_stars']}")
    log(f"  Target stars:    {mask_stats['target_stars']}")
    log(
        "  Masked pixels:   "
        f"{mask_stats['total_masked_pixels']} ({100.0 * float(mask_stats['masked_fraction']):.2f}%)"
    )
    if siril_reports:
        log("")
        log("Siril internal timings:")
        for name, report in siril_reports.items():
            parts = []
            if report.get("stars") is not None:
                parts.append(f"{report['stars']} stars")
            if report.get("fwhm") is not None:
                parts.append(f"FWHM {float(report['fwhm']):.3f}")
            command_timings = report.get("command_timings_seconds")
            if isinstance(command_timings, dict):
                for command, seconds in command_timings.items():
                    parts.append(f"{command} {format_seconds(float(seconds))}")
            if report.get("script_total_seconds") is not None:
                parts.append(f"script total {format_seconds(float(report['script_total_seconds']))}")
            log(f"  {name:<10} " + ", ".join(parts))
    if core_report:
        log("")
        log("Normalization summary:")
        log(
            "  Valid grid:      "
            f"{core_report.get('initial_valid_nodes', '?')}/{core_report.get('total_nodes', '?')} "
            f"({100.0 * float(core_report.get('initial_valid_fraction', 0.0)):.1f}%)"
        )
        log(f"  Scale range:     {core_report.get('scale_min', '?')} .. {core_report.get('scale_max', '?')}")
        log(f"  Offset range:    {core_report.get('offset_min', '?')} .. {core_report.get('offset_max', '?')}")


def main() -> int:
    total_start = time.perf_counter()
    timer = StepTimer()
    args = parse_args()
    reference = args.reference.expanduser().resolve()
    target = args.target.expanduser().resolve()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    diag_dir = (args.diag_dir or default_diag_dir(output)).expanduser().resolve()
    diag_dir.mkdir(parents=True, exist_ok=True)

    selected_binary = select_lnc_binary(args)
    binary = timer.run(
        "Build/check C normalizer",
        lambda: ensure_binary(selected_binary, rebuild=args.rebuild),
    )
    siril_path = find_siril_path(args.siril_path)
    log(f"[LNC] Siril: {siril_path}")
    temp_context = tempfile.TemporaryDirectory(prefix="lnc-")
    temp_dir = Path(temp_context.name)
    wrapper_report_path = diag_dir / "wrapper_report.json"
    siril_reports: dict[str, dict] = {}
    intermediate_outputs: dict[str, str] = {}
    normalized_diagnostic_outputs: dict[str, str] = {}

    try:
        ref_fits, ref_prepare_report = timer.run(
            "Prepare reference input",
            lambda: normalize_input_path(reference, temp_dir, siril_path, args.timeout),
        )
        siril_reports["ref input"] = ref_prepare_report
        target_fits, target_prepare_report = timer.run(
            "Prepare target input",
            lambda: normalize_input_path(target, temp_dir, siril_path, args.timeout),
        )
        siril_reports["target input"] = target_prepare_report
        value_scale_report = timer.run(
            "Infer FITS value scale",
            lambda: choose_pipeline_value_scale(ref_fits, target_fits),
        )
        value_scale = str(value_scale_report["scale"])
        log(f"[LNC] FITS value scale: {value_scale}")

        ref_star_list = diag_dir / "reference_stars.lst"
        target_star_list = diag_dir / "target_stars.lst"
        ref_findstar_report = timer.run(
            "Detect reference stars",
            lambda: run_findstar(ref_fits, ref_star_list, siril_path, args.timeout),
        )
        siril_reports["ref stars"] = ref_findstar_report
        target_findstar_report = timer.run(
            "Detect target stars",
            lambda: run_findstar(target_fits, target_star_list, siril_path, args.timeout),
        )
        siril_reports["target stars"] = target_findstar_report
        ref_stars = timer.run("Parse reference stars", lambda: parse_star_list(ref_star_list))
        target_stars = timer.run("Parse target stars", lambda: parse_star_list(target_star_list))

        mask_path = diag_dir / "star_mask.fits"
        saturation_threshold = args.saturation_threshold
        if saturation_threshold is not None and saturation_threshold < 0:
            saturation_threshold = None
        mask_stats = timer.run(
            "Build star/saturation mask",
            lambda: build_mask(
                ref_path=ref_fits,
                target_path=target_fits,
                ref_stars=ref_stars,
                target_stars=target_stars,
                output_path=mask_path,
                radius_min=args.mask_radius_min,
                radius_factor=args.mask_radius_factor,
                radius_max=args.mask_radius_max,
                saturation_threshold=saturation_threshold,
                saturation_dilation=args.saturation_dilation,
            ),
        )
        if args.save_intermediate_fits:
            intermediate_outputs = timer.run(
                "Save intermediate FITS",
                lambda: write_intermediate_masked_fits(
                    reference_fits=ref_fits,
                    target_fits=target_fits,
                    mask_path=mask_path,
                    diag_dir=diag_dir,
                    value_scale=value_scale,
                ),
            )

        core_report = diag_dir / "local_normalize_report.json"
        command = [
            str(binary),
            "--mask",
            str(mask_path),
            "--diag-dir",
            str(diag_dir),
            *(["--save-backgrounds"] if args.save_intermediate_fits else []),
            "--report",
            str(core_report),
            "--grid-spacing",
            str(args.grid_spacing),
            "--window-size",
            str(args.window_size),
            "--min-samples",
            str(args.min_samples),
            "--trim-fraction",
            str(args.trim_fraction),
            "--scale-min",
            str(args.scale_min),
            "--scale-max",
            str(args.scale_max),
            "--smooth-passes",
            str(args.smooth_passes),
            "--min-valid-fraction",
            str(args.min_valid_fraction),
            *(
                [
                    "--sample-patch-size",
                    str(args.sample_patch_size),
                    "--sample-stride",
                    str(args.sample_stride),
                    "--min-patches",
                    str(args.min_patches),
                    "--sample-min-valid",
                    str(args.sample_min_valid),
                    "--sample-reject-k",
                    str(args.sample_reject_k),
                ]
                if args.background_estimator == "sample-median"
                else []
            ),
            str(ref_fits),
            str(target_fits),
            str(output),
        ]
        timer.run("Run C normalization core", lambda: subprocess.run(command, check=True))
        normalized_diagnostic_outputs = timer.run(
            "Normalize diagnostics",
            lambda: normalize_core_diagnostics(
                diag_dir,
                save_backgrounds=args.save_intermediate_fits,
                value_scale=value_scale,
            ),
        )
        timer.run("Apply final output format", lambda: apply_output_format(output, args.output_format, value_scale))

        parameters = {
            "grid_spacing": args.grid_spacing,
            "window_size": args.window_size,
            "min_samples": args.min_samples,
            "trim_fraction": args.trim_fraction,
            "scale_min": args.scale_min,
            "scale_max": args.scale_max,
            "smooth_passes": args.smooth_passes,
            "min_valid_fraction": args.min_valid_fraction,
            "mask_radius_min": args.mask_radius_min,
            "mask_radius_factor": args.mask_radius_factor,
            "mask_radius_max": args.mask_radius_max,
            "saturation_threshold": saturation_threshold,
            "saturation_dilation": args.saturation_dilation,
            "save_intermediate_fits": args.save_intermediate_fits,
            "output_format": args.output_format,
            "value_scale": value_scale,
            "value_scale_report": value_scale_report,
            "background_estimator": args.background_estimator,
            "sample_patch_size": args.sample_patch_size,
            "sample_stride": args.sample_stride,
            "min_patches": args.min_patches,
            "sample_min_valid": args.sample_min_valid,
            "sample_reject_k": args.sample_reject_k,
        }
        timer.run(
            "Preserve FITS header/provenance",
            lambda: preserve_header_and_add_lnc_metadata(
                output_path=output,
                target_fits=target_fits,
                reference_source=reference,
                target_source=target,
                reference_fits=ref_fits,
                parameters=parameters,
                mask_stats=mask_stats,
                core_report=core_report,
            ),
        )
        timer.timings["Total wall time"] = time.perf_counter() - total_start

        wrapper_report = {
            "reference": str(reference),
            "target": str(target),
            "reference_fits": str(ref_fits),
            "target_fits": str(target_fits),
            "output": str(output),
            "diagnostics": str(diag_dir),
            "mask": mask_stats,
            "parameters": parameters,
            "value_scale": value_scale_report,
            "core_report": str(core_report),
            "intermediate_outputs": intermediate_outputs,
            "normalized_diagnostic_outputs": normalized_diagnostic_outputs,
            "command": command,
            "timings_seconds": timer.timings,
            "siril_reports": siril_reports,
        }
        write_wrapper_report(wrapper_report_path, wrapper_report)
        print_summary(
            output=output,
            diag_dir=diag_dir,
            timings=timer.timings,
            mask_stats=mask_stats,
            core_report=read_json_if_exists(core_report),
            wrapper_report_path=wrapper_report_path,
            siril_reports=siril_reports,
            intermediate_outputs=intermediate_outputs,
        )
    finally:
        if args.keep_temp:
            print(f"Temporary files kept at {temp_dir}", file=sys.stderr)
        else:
            temp_context.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
