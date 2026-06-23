#!/usr/bin/env python3
"""Local normalization correction for unregistered image pairs."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from astropy.io import fits

from lnc_registered_pair import (
    FITS_ADU_MAX,
    LNC_DIR,
    StepTimer,
    add_saturation_mask,
    append_history_chunks,
    compact_json_for_history,
    default_diag_dir,
    ensure_binary,
    find_siril_path,
    format_seconds,
    log,
    mask_circle,
    normalize_unitless_fits_in_place,
    normalize_input_path,
    parse_siril_script_report,
    parse_star_list,
    read_json_if_exists,
    read_shape,
    remove_data_scaling_cards,
    run_siril,
    siril_quote,
    write_wrapper_report,
)
from lnc_star_scale import FINDSTAR_MODES, StarScaleOptions, estimate_star_scale, run_lnc_findstar


LNC_UNREGISTERED_BINARY = LNC_DIR / "bin" / "lnc_unregistered_pair"
VALUE_SCALE_ADU = "adu"
VALUE_SCALE_NORMALIZED = "normalized_float"
REGISTRATION_TRANSFORMS = ("homography", "affine", "similarity", "shift")
PHOTOMETRIC_MODELS = ("local-linear", "star-scale-additive")


def matrix_to_list(matrix: np.ndarray) -> list[float]:
    return [float(v) for v in np.asarray(matrix, dtype=np.float64).reshape(9)]


def apply_homography(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    homog = np.column_stack([points[:, 0], points[:, 1], np.ones(len(points))])
    transformed = homog @ matrix.T
    w = transformed[:, 2]
    valid = np.isfinite(w) & (np.abs(w) > 1e-12)
    out = np.full((len(points), 2), np.nan, dtype=np.float64)
    out[valid, 0] = transformed[valid, 0] / w[valid]
    out[valid, 1] = transformed[valid, 1] / w[valid]
    return out


def flip_y_matrix(height: int) -> np.ndarray:
    return np.array([[1.0, 0.0, 0.0], [0.0, -1.0, float(height - 1)], [0.0, 0.0, 1.0]])


def siril_to_array_homography(
    siril_target_to_ref: np.ndarray,
    *,
    reference_height: int,
    target_height: int,
) -> np.ndarray:
    return flip_y_matrix(reference_height) @ siril_target_to_ref @ flip_y_matrix(target_height)


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


def scaled_to_display_range(data: np.ndarray, value_scale: str, *, preserve_nan: bool = False) -> np.ndarray:
    values = np.asarray(data, dtype=np.float32).copy()
    if value_scale == VALUE_SCALE_ADU:
        upper = FITS_ADU_MAX
        divisor = FITS_ADU_MAX
    elif value_scale == VALUE_SCALE_NORMALIZED:
        upper = 1.0
        divisor = 1.0
    else:
        raise ValueError(f"Unsupported value scale: {value_scale}")

    finite = np.isfinite(values)
    if preserve_nan:
        values[finite] = np.clip(values[finite], 0.0, upper) / divisor
        values[np.isposinf(values)] = 1.0
        values[np.isneginf(values)] = 0.0
        return values.astype(np.float32)
    finite_values = np.nan_to_num(values, nan=0.0, posinf=upper, neginf=0.0)
    return (np.clip(finite_values, 0.0, upper) / divisor).astype(np.float32)


def normalize_scaled_fits_in_place(path: Path, value_scale: str) -> None:
    if not path.exists():
        return
    with fits.open(path, memmap=False) as hdul:
        data = scaled_to_display_range(np.asarray(hdul[0].data, dtype=np.float32), value_scale, preserve_nan=True)
        header = hdul[0].header.copy()
    remove_data_scaling_cards(header)
    header["LNCNORM"] = (True, "Image values normalized/clipped to [0, 1]")
    header["LNCVSCL"] = (value_scale, "Input value scale before display normalization")
    fits.writeto(path, data, header=header, overwrite=True)


def normalize_core_diagnostics(diag_dir: Path, *, save_backgrounds: bool, value_scale: str) -> dict[str, str]:
    outputs = {
        "scale_map": diag_dir / "scale_map.fits",
        "offset_map": diag_dir / "offset_map.fits",
    }
    if save_backgrounds:
        outputs.update(
            {
                "ref_background": diag_dir / "ref_background.fits",
                "target_background": diag_dir / "target_background.fits",
            }
        )
    normalize_unitless_fits_in_place(outputs["scale_map"])
    normalize_unitless_fits_in_place(outputs["offset_map"])
    if save_backgrounds:
        normalize_scaled_fits_in_place(outputs["ref_background"], value_scale)
        normalize_scaled_fits_in_place(outputs["target_background"], value_scale)
    return {key: str(path) for key, path in outputs.items() if path.exists()}


def apply_output_format_for_value_scale(output_path: Path, output_format: str, value_scale: str) -> None:
    with fits.open(output_path, memmap=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()

    normalized = scaled_to_display_range(data, value_scale)
    if output_format == "float32":
        output_data = normalized.astype(np.float32)
    elif output_format == "uint16":
        output_data = np.rint(normalized * FITS_ADU_MAX).astype(np.uint16)
    else:
        raise ValueError(f"Unsupported output format: {output_format}")

    remove_data_scaling_cards(header)
    header["LNCFMT"] = (output_format, "LNC final science image encoding")
    header["LNCVSCL"] = (value_scale, "Input value scale before final encoding")
    fits.writeto(output_path, output_data, header=header, overwrite=True, output_verify="silentfix")


def parse_sequence_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    sequence_name = path.stem
    reference_image = None
    image_sizes: list[tuple[int, int]] = []
    matrices: list[np.ndarray] = []
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
            matrices.append(np.array(values, dtype=np.float64).reshape(3, 3))
    if len(matrices) < 2:
        raise ValueError(f"No usable registration matrices found in {path}")
    return {
        "path": str(path),
        "sequence_name": sequence_name,
        "reference_image": reference_image,
        "image_sizes": image_sizes,
        "matrices": matrices,
    }


def run_two_pass_registration(
    *,
    ref_fits: Path,
    target_fits: Path,
    temp_dir: Path,
    siril_path: str,
    transform_type: str,
    timeout: float,
) -> tuple[dict, dict]:
    seq_dir = temp_dir / "registration"
    if seq_dir.exists():
        shutil.rmtree(seq_dir)
    seq_dir.mkdir(parents=True, exist_ok=True)
    ref_seq = seq_dir / "lnc_pair_00001.fit"
    target_seq = seq_dir / "lnc_pair_00002.fit"
    shutil.copy2(ref_fits, ref_seq)
    shutil.copy2(target_fits, target_seq)
    script = f"""requires 1.2.0
cd {siril_quote(seq_dir)}
setref lnc_pair 1
register lnc_pair -2pass -transf={transform_type}
"""
    result = run_siril(
        siril_path,
        seq_dir,
        script,
        context="computing two-pass registration transform",
        timeout=timeout,
    )
    candidates = sorted(seq_dir.glob("*.seq"))
    if not candidates:
        raise FileNotFoundError(f"Siril did not write a .seq file in {seq_dir}")
    seq_path = candidates[0]
    sequence = parse_sequence_file(seq_path)
    report = parse_siril_script_report(f"{result.stdout}\n{result.stderr}")
    report["sequence_file"] = str(seq_path)
    report["transform_type"] = transform_type
    return sequence, report


def median_nearest_distance(transformed: np.ndarray, reference: np.ndarray) -> float:
    transformed = transformed[np.isfinite(transformed).all(axis=1)]
    if len(transformed) == 0 or len(reference) == 0:
        return math.inf
    distances: list[float] = []
    chunk_size = 128
    for start in range(0, len(transformed), chunk_size):
        chunk = transformed[start : start + chunk_size]
        delta = chunk[:, None, :] - reference[None, :, :]
        nearest = np.sqrt(np.min(np.sum(delta * delta, axis=2), axis=1))
        distances.extend(float(v) for v in nearest if math.isfinite(float(v)))
    if not distances:
        return math.inf
    return float(np.median(distances))


def registration_transform_candidates(requested: str) -> tuple[str, ...]:
    try:
        start = REGISTRATION_TRANSFORMS.index(requested)
    except ValueError:
        start = 0
    return REGISTRATION_TRANSFORMS[start:]


def validate_target_to_reference_matrix(
    *,
    sequence: dict,
    ref_stars,
    target_stars,
    max_stars: int,
) -> tuple[np.ndarray, dict]:
    matrices: list[np.ndarray] = sequence["matrices"]
    ref_matrix = matrices[0]
    target_matrix = matrices[1]
    candidates: dict[str, np.ndarray] = {}
    for name, matrix in (
        ("image_to_internal_reference", lambda: np.linalg.inv(ref_matrix) @ target_matrix),
        ("internal_reference_to_image", lambda: ref_matrix @ np.linalg.inv(target_matrix)),
    ):
        try:
            candidate = matrix()
        except np.linalg.LinAlgError:
            continue
        if np.isfinite(candidate).all():
            candidates[name] = candidate
    if not candidates:
        raise ValueError("Siril registration produced non-invertible transform matrices")
    ref_points = np.array([[star.x, star.y] for star in ref_stars], dtype=np.float64)
    target_points = np.array([[star.x, star.y] for star in target_stars[:max_stars]], dtype=np.float64)
    scores = {}
    for name, matrix in candidates.items():
        transformed = apply_homography(matrix, target_points)
        scores[name] = median_nearest_distance(transformed, ref_points)
    finite_scores = {name: score for name, score in scores.items() if math.isfinite(score)}
    if not finite_scores:
        raise ValueError("Siril registration transform did not map target stars onto reference stars")
    convention = min(finite_scores, key=finite_scores.get)
    return candidates[convention], {
        "convention": convention,
        "median_nearest_star_distance_px": finite_scores[convention],
        "candidate_scores_px": scores,
        "target_stars_used": min(max_stars, len(target_stars)),
        "reference_stars_used": len(ref_stars),
    }


def build_single_mask(
    *,
    image_path: Path,
    stars,
    output_path: Path,
    radius_min: float,
    radius_factor: float,
    radius_max: float,
    saturation_threshold: float | None,
    saturation_dilation: int,
) -> dict[str, float | int]:
    width, height = read_shape(image_path)
    mask = np.zeros((height, width), dtype=bool)
    for star in stars:
        fwhm = star.fwhm if star.fwhm is not None and star.fwhm > 0 else radius_min / radius_factor
        radius = min(radius_max, max(radius_min, radius_factor * fwhm))
        mask_circle(mask, star.x, (height - 1) - star.y, radius)
    star_masked = int(mask.sum())
    saturated_masked = 0
    if saturation_threshold is not None:
        saturated_masked = add_saturation_mask(mask, [image_path], saturation_threshold, saturation_dilation)
    fits.writeto(output_path, mask.astype(np.uint8), overwrite=True)
    return {
        "width": width,
        "height": height,
        "stars": len(stars),
        "star_masked_pixels": star_masked,
        "saturated_masked_pixels": saturated_masked,
        "total_masked_pixels": int(mask.sum()),
        "masked_fraction": float(mask.mean()),
    }


def write_unregistered_intermediate_fits(
    *,
    reference_fits: Path,
    target_fits: Path,
    ref_mask_path: Path,
    target_mask_path: Path,
    diag_dir: Path,
    value_scale: str,
) -> dict[str, str]:
    outputs = {
        "reference_masked": diag_dir / "reference_masked.fits",
        "target_masked": diag_dir / "target_masked.fits",
    }
    write_scaled_masked_fits(reference_fits, ref_mask_path, outputs["reference_masked"], value_scale)
    write_scaled_masked_fits(target_fits, target_mask_path, outputs["target_masked"], value_scale)
    return {key: str(path) for key, path in outputs.items()}


def write_scaled_masked_fits(source_path: Path, mask_path: Path, output_path: Path, value_scale: str) -> None:
    with fits.open(source_path, memmap=False) as source_hdul:
        data = np.asarray(source_hdul[0].data, dtype=np.float32).copy()
        header = source_hdul[0].header.copy()

    mask = np.asarray(fits.getdata(mask_path, memmap=False), dtype=bool)
    if mask.shape != data.shape:
        raise ValueError(f"{mask_path} dimensions do not match {source_path}")

    data = scaled_to_display_range(data, value_scale, preserve_nan=True)
    data[mask] = np.nan
    remove_data_scaling_cards(header)
    header["LNCMASKD"] = (True, "Masked by LNC exclusion mask")
    header["LNCNORM"] = (True, "Image values normalized/clipped to [0, 1]")
    header["LNCVSCL"] = (value_scale, "Input value scale before display normalization")
    header["LNCSRC"] = (source_path.name[:68], "Source filename for masked FITS")
    fits.writeto(output_path, data, header=header, overwrite=True, output_verify="silentfix")


def preserve_target_header_and_add_lnc_metadata(
    *,
    output_path: Path,
    target_fits: Path,
    reference_source: Path,
    target_source: Path,
    reference_fits: Path,
    parameters: dict,
    ref_mask_stats: dict,
    target_mask_stats: dict,
    core_report: Path,
    transform_report: dict,
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
        source_header["LNCVRS"] = ("2-unregistered", "Local normalization correction version")
        source_header["LNCREF"] = (reference_source.name[:68], "Reference source filename")
        source_header["LNCTARG"] = (target_source.name[:68], "Target source filename")
        source_header["LNCRMSK"] = (int(ref_mask_stats["total_masked_pixels"]), "Reference masked pixels")
        source_header["LNCTMSK"] = (int(target_mask_stats["total_masked_pixels"]), "Target masked pixels")
        for key in (
            "LNCMODE",
            "LNCFMT",
            "LNCVSCL",
            "LNCBKG",
            "LNCGRID",
            "LNCWIN",
            "LNCSAMP",
            "LNCTRIM",
            "LNCSMIN",
            "LNCSMAX",
            "LNCSMTH",
            "LNCMINV",
        ):
            if key in corrected_header:
                source_header[key] = corrected_header[key]
        source_header.setdefault("LNCMODE", ("unregistered-pair", "Local normalization correction mode"))
        source_header.add_history("LNC2: corrected target preserves original target geometry")
        source_header.add_history("LNC2: correction fields are estimated in reference coordinates")
        append_history_chunks(source_header, "LNC2 ref", str(reference_source))
        append_history_chunks(source_header, "LNC2 target", str(target_source))
        append_history_chunks(source_header, "LNC2 ref FITS", str(reference_fits))
        append_history_chunks(source_header, "LNC2 target FITS", str(target_fits))
        append_history_chunks(source_header, "LNC2 report", str(core_report))
        append_history_chunks(source_header, "LNC2 params", compact_json_for_history(parameters))
        append_history_chunks(source_header, "LNC2 transform", compact_json_for_history(transform_report))
        output_hdul[0].header = source_header
        output_hdul[0].data = corrected_data
        output_hdul.flush()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local normalization correction on unregistered subs.")
    parser.add_argument("reference", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--siril-path", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the C normalizer before running.")
    parser.add_argument("--diag-dir", type=Path, help="Diagnostic output directory.")
    parser.add_argument("--registration-transform", default="homography", choices=REGISTRATION_TRANSFORMS)
    parser.add_argument(
        "--photometric-model",
        choices=PHOTOMETRIC_MODELS,
        default="local-linear",
        help="Photometric correction model: existing local scale+offset or StarScale Additive LNC.",
    )
    parser.add_argument(
        "--findstar-mode",
        choices=FINDSTAR_MODES,
        default="auto",
        help="Star detection parameters; auto uses Siril defaults for SSA-LNC and legacy LNC tuning otherwise.",
    )
    parser.add_argument(
        "--save-intermediate-fits",
        action="store_true",
        help="Save native-coordinate masked ref/target FITS and background diagnostics.",
    )
    parser.add_argument(
        "--output-format",
        choices=("uint16", "float32"),
        default="uint16",
        help="Final corrected FITS encoding: uint16 clips to [0, 65535]; float32 normalizes to [0, 1].",
    )
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--grid-spacing", type=int, default=128)
    parser.add_argument("--window-size", type=int, default=256)
    parser.add_argument("--min-samples", type=int, default=2000)
    parser.add_argument(
        "--background-estimator",
        choices=("trimmed-mean", "trimmed-median", "sample-median"),
        default="trimmed-median",
    )
    parser.add_argument("--trim-fraction", type=float, default=0.10)
    parser.add_argument("--scale-min", type=float, default=0.5)
    parser.add_argument("--scale-max", type=float, default=2.0)
    parser.add_argument("--smooth-passes", type=int, default=2)
    parser.add_argument("--min-valid-fraction", type=float, default=0.30)
    parser.add_argument("--mask-radius-min", type=float, default=8.0)
    parser.add_argument("--mask-radius-factor", type=float, default=5.0)
    parser.add_argument("--mask-radius-max", type=float, default=50.0)
    parser.add_argument("--saturation-threshold", type=float, default=65535.0)
    parser.add_argument("--saturation-dilation", type=int, default=2)
    parser.add_argument("--validation-stars", type=int, default=1000)
    parser.add_argument("--star-scale-match-radius", type=float, default=2.0)
    parser.add_argument("--star-scale-min-stars", type=int, default=20)
    parser.add_argument("--star-scale-min-r2", type=float, default=0.90)
    parser.add_argument("--star-scale-clip-sigma", type=float, default=2.5)
    return parser.parse_args()


def print_summary(
    *,
    output: Path,
    diag_dir: Path,
    timings: dict[str, float],
    ref_mask_stats: dict,
    target_mask_stats: dict,
    core_report: dict,
    transform_validation: dict,
    wrapper_report_path: Path,
) -> None:
    log("")
    log("Unregistered local normalization complete")
    log(f"  Output:      {output}")
    log(f"  Diagnostics: {diag_dir}")
    log(f"  Report:      {wrapper_report_path}")
    log("")
    log("Transform:")
    log(f"  Convention:  {transform_validation.get('convention', '?')}")
    log(
        "  Star residual: "
        f"{float(transform_validation.get('median_nearest_star_distance_px', math.nan)):.3f} px median nearest"
    )
    log("")
    log("Mask/star summary:")
    log(
        "  Reference:   "
        f"{ref_mask_stats['stars']} stars, {ref_mask_stats['total_masked_pixels']} masked "
        f"({100.0 * float(ref_mask_stats['masked_fraction']):.2f}%)"
    )
    log(
        "  Target:      "
        f"{target_mask_stats['stars']} stars, {target_mask_stats['total_masked_pixels']} masked "
        f"({100.0 * float(target_mask_stats['masked_fraction']):.2f}%)"
    )
    if core_report:
        log("")
        log("Normalization summary:")
        log(
            "  Valid grid:  "
            f"{core_report.get('initial_valid_nodes', '?')}/{core_report.get('total_nodes', '?')} "
            f"({100.0 * float(core_report.get('initial_valid_fraction', 0.0)):.1f}%)"
        )
        log(f"  Scale range: {core_report.get('scale_min', '?')} .. {core_report.get('scale_max', '?')}")
        log(f"  Offset range:{core_report.get('offset_min', '?')} .. {core_report.get('offset_max', '?')}")
    log("")
    log("Processing times:")
    for label, seconds in timings.items():
        log(f"  {label:<30} {format_seconds(seconds)}")


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
    binary_path = args.binary.expanduser().resolve() if args.binary else LNC_UNREGISTERED_BINARY
    binary = timer.run("Build/check C normalizer", lambda: ensure_binary(binary_path, rebuild=args.rebuild))
    siril_path = find_siril_path(args.siril_path)
    log(f"[LNC2] Siril: {siril_path}")
    temp_context = tempfile.TemporaryDirectory(prefix="lnc2-")
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
        log(f"[LNC2] FITS value scale: {value_scale}")

        findstar_mode = args.findstar_mode
        if findstar_mode == "auto":
            findstar_mode = "siril-default" if args.photometric_model == "star-scale-additive" else "lnc-tuned"
        log(f"[LNC2] Findstar mode: {findstar_mode}")

        ref_star_list = diag_dir / "reference_stars.lst"
        target_star_list = diag_dir / "target_stars.lst"
        siril_reports["ref stars"] = timer.run(
            "Detect reference stars",
            lambda: run_lnc_findstar(ref_fits, ref_star_list, siril_path, args.timeout, mode=findstar_mode),
        )
        siril_reports["target stars"] = timer.run(
            "Detect target stars",
            lambda: run_lnc_findstar(target_fits, target_star_list, siril_path, args.timeout, mode=findstar_mode),
        )
        ref_stars = timer.run("Parse reference stars", lambda: parse_star_list(ref_star_list))
        target_stars = timer.run("Parse target stars", lambda: parse_star_list(target_star_list))

        registration_errors: dict[str, str] = {}
        sequence = None
        registration_report = None
        siril_h = None
        transform_validation = None
        for transform_type in registration_transform_candidates(args.registration_transform):
            try:
                sequence, registration_report = timer.run(
                    f"Run Siril two-pass registration ({transform_type})",
                    lambda transform_type=transform_type: run_two_pass_registration(
                        ref_fits=ref_fits,
                        target_fits=target_fits,
                        temp_dir=temp_dir,
                        siril_path=siril_path,
                        transform_type=transform_type,
                        timeout=args.timeout,
                    ),
                )
                siril_reports[f"registration {transform_type}"] = registration_report
                siril_h, transform_validation = timer.run(
                    f"Validate transform direction ({transform_type})",
                    lambda: validate_target_to_reference_matrix(
                        sequence=sequence,
                        ref_stars=ref_stars,
                        target_stars=target_stars,
                        max_stars=args.validation_stars,
                    ),
                )
                transform_validation["registration_transform"] = transform_type
                if transform_type != args.registration_transform:
                    transform_validation["fallback_from"] = args.registration_transform
                    transform_validation["fallback_errors"] = registration_errors
                break
            except (RuntimeError, ValueError, np.linalg.LinAlgError) as exc:
                registration_errors[transform_type] = f"{type(exc).__name__}: {exc}"
                log(f"[LNC2] Registration transform {transform_type} failed: {exc}")
        if sequence is None or registration_report is None or siril_h is None or transform_validation is None:
            raise RuntimeError(f"All registration transforms failed: {registration_errors}")

        ref_width, ref_height = read_shape(ref_fits)
        target_width, target_height = read_shape(target_fits)
        array_h = siril_to_array_homography(
            siril_h,
            reference_height=ref_height,
            target_height=target_height,
        )
        transform_report = {
            "siril_target_to_reference": matrix_to_list(siril_h),
            "array_target_to_reference": matrix_to_list(array_h),
            "validation": transform_validation,
            "sequence": {
                "path": sequence["path"],
                "sequence_name": sequence["sequence_name"],
                "reference_image": sequence["reference_image"],
                "image_sizes": sequence["image_sizes"],
            },
        }
        (diag_dir / "transform_report.json").write_text(
            json.dumps(transform_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        saturation_threshold = args.saturation_threshold
        if saturation_threshold is not None and saturation_threshold < 0:
            saturation_threshold = None
        star_scale_report: dict[str, object] | None = None
        global_scale = 1.0
        if args.photometric_model == "star-scale-additive":
            star_scale_options = StarScaleOptions(
                match_radius=args.star_scale_match_radius,
                saturation_threshold=saturation_threshold,
                clip_sigma=args.star_scale_clip_sigma,
                min_fit_stars=args.star_scale_min_stars,
                min_r_squared=args.star_scale_min_r2,
            )
            star_scale_report = timer.run(
                "Estimate matched-star scale",
                lambda: estimate_star_scale(
                    reference_fits=ref_fits,
                    target_fits=target_fits,
                    reference_stars=ref_stars,
                    target_stars=target_stars,
                    target_to_reference_h=siril_h,
                    options=star_scale_options,
                ),
            )
            (diag_dir / "star_scale_report.json").write_text(
                json.dumps(star_scale_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if not star_scale_report.get("ok"):
                raise RuntimeError(f"SSA-LNC star-scale estimation failed: {star_scale_report.get('message')}")
            global_scale = float(star_scale_report["target_to_reference_scale"])
            log(
                "[LNC2] SSA-LNC global scale target->reference: "
                f"{global_scale:.8g} ({star_scale_report.get('used_stars')} matched stars used)"
            )
        ref_mask_path = diag_dir / "reference_mask.fits"
        target_mask_path = diag_dir / "target_mask.fits"
        ref_mask_stats = timer.run(
            "Build reference mask",
            lambda: build_single_mask(
                image_path=ref_fits,
                stars=ref_stars,
                output_path=ref_mask_path,
                radius_min=args.mask_radius_min,
                radius_factor=args.mask_radius_factor,
                radius_max=args.mask_radius_max,
                saturation_threshold=saturation_threshold,
                saturation_dilation=args.saturation_dilation,
            ),
        )
        target_mask_stats = timer.run(
            "Build target mask",
            lambda: build_single_mask(
                image_path=target_fits,
                stars=target_stars,
                output_path=target_mask_path,
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
                lambda: write_unregistered_intermediate_fits(
                    reference_fits=ref_fits,
                    target_fits=target_fits,
                    ref_mask_path=ref_mask_path,
                    target_mask_path=target_mask_path,
                    diag_dir=diag_dir,
                    value_scale=value_scale,
                ),
            )

        core_report = diag_dir / "local_normalize_unregistered_report.json"
        command = [
            str(binary),
            "--ref-mask",
            str(ref_mask_path),
            "--target-mask",
            str(target_mask_path),
            "--homography",
            *[f"{value:.17g}" for value in matrix_to_list(array_h)],
            "--diag-dir",
            str(diag_dir),
            *(["--save-backgrounds"] if args.save_intermediate_fits else []),
            "--report",
            str(core_report),
            "--background-estimator",
            args.background_estimator,
            "--photometric-model",
            args.photometric_model,
            "--global-scale",
            f"{global_scale:.17g}",
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
        timer.run(
            "Apply final output format",
            lambda: apply_output_format_for_value_scale(output, args.output_format, value_scale),
        )

        parameters = {
            "registration_transform": args.registration_transform,
            "photometric_model": args.photometric_model,
            "findstar_mode": findstar_mode,
            "global_scale": global_scale,
            "star_scale_report": star_scale_report,
            "background_estimator": args.background_estimator,
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
            "validation_stars": args.validation_stars,
            "star_scale_match_radius": args.star_scale_match_radius,
            "star_scale_min_stars": args.star_scale_min_stars,
            "star_scale_min_r2": args.star_scale_min_r2,
            "star_scale_clip_sigma": args.star_scale_clip_sigma,
        }
        timer.run(
            "Preserve FITS header/provenance",
            lambda: preserve_target_header_and_add_lnc_metadata(
                output_path=output,
                target_fits=target_fits,
                reference_source=reference,
                target_source=target,
                reference_fits=ref_fits,
                parameters=parameters,
                ref_mask_stats=ref_mask_stats,
                target_mask_stats=target_mask_stats,
                core_report=core_report,
                transform_report=transform_report,
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
            "reference_mask": ref_mask_stats,
            "target_mask": target_mask_stats,
            "parameters": parameters,
            "value_scale": value_scale_report,
            "core_report": str(core_report),
            "transform_report": transform_report,
            "star_scale_report": star_scale_report,
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
            ref_mask_stats=ref_mask_stats,
            target_mask_stats=target_mask_stats,
            core_report=read_json_if_exists(core_report),
            transform_validation=transform_validation,
            wrapper_report_path=wrapper_report_path,
        )
    finally:
        if args.keep_temp:
            print(f"Temporary files kept at {temp_dir}")
        else:
            temp_context.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
