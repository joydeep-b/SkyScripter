"""Matched-star global scale estimation for LNC variants."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from astropy.io import fits

from lnc_registered_pair import parse_siril_findstar_report, run_siril, siril_quote


FINDSTAR_MODES = ("auto", "siril-default", "lnc-tuned")
LNC_TUNED_FINDSTAR_COMMAND = "setfindstar -radius=3 -sigma=0.5 -roundness=0.8 -moffat -minbeta=1.5 -relax=on"


@dataclass(frozen=True)
class ApertureRadii:
    aperture: float
    annulus_inner: float
    annulus_outer: float


@dataclass(frozen=True)
class PhotometryResult:
    flux: float | None
    background: float | None
    peak: float | None
    snr: float | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class StarMatch:
    reference_index: int
    target_index: int
    x_a: float
    y_a: float
    x_b: float
    y_b: float
    x_b_registered: float
    y_b_registered: float
    distance_px: float
    fwhm_a: float | None
    fwhm_b: float | None


@dataclass(frozen=True)
class StarScaleOptions:
    match_radius: float = 2.0
    aperture_radius: float | None = None
    aperture_fwhm_factor: float = 2.5
    aperture_radius_min: float = 3.0
    aperture_radius_max: float = 12.0
    annulus_inner_radius: float | None = None
    annulus_outer_radius: float | None = None
    annulus_inner_fwhm_factor: float = 4.0
    annulus_outer_fwhm_factor: float = 6.0
    saturation_threshold: float | None = 65535.0
    isolation_radius: float | None = None
    isolation_radius_factor: float = 6.0
    min_flux: float = 0.0
    max_flux: float | None = None
    min_snr: float = 5.0
    clip_sigma: float = 2.5
    clip_iterations: int = 6
    min_fit_stars: int = 20
    min_r_squared: float = 0.90


@dataclass(frozen=True)
class RobustRatioFit:
    ok: bool
    scale_b_over_a: float | None
    ratio_mad: float | None
    log_ratio_sigma: float | None
    n_initial: int
    n_used: int
    clipped: int
    kept_mask: np.ndarray
    message: str | None = None

    def to_report(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "scale_b_over_a": self.scale_b_over_a,
            "ratio_mad": self.ratio_mad,
            "log_ratio_sigma": self.log_ratio_sigma,
            "n_initial": self.n_initial,
            "n_used": self.n_used,
            "clipped": self.clipped,
            "message": self.message,
        }


def run_lnc_findstar(
    source: Path,
    star_list: Path,
    siril_path: str,
    timeout: float,
    *,
    mode: str,
) -> dict[str, object]:
    if mode == "auto":
        mode = "siril-default"
    if mode not in FINDSTAR_MODES:
        raise ValueError(f"Unknown findstar mode: {mode}")

    star_list.parent.mkdir(parents=True, exist_ok=True)
    if star_list.exists():
        star_list.unlink()
    output_name = star_list.name
    command_line = LNC_TUNED_FINDSTAR_COMMAND if mode == "lnc-tuned" else None
    setfindstar_block = f"{command_line}\n" if command_line is not None else ""
    script = f"""requires 1.2.0
load {siril_quote(source)}
{setfindstar_block}findstar -out={output_name}
close
"""
    result = run_siril(
        siril_path,
        star_list.parent,
        script,
        context=f"detecting stars in {source.name} ({mode})",
        timeout=timeout,
    )
    if not star_list.exists() or star_list.stat().st_size == 0:
        raise FileNotFoundError(f"Siril did not write a star list: {star_list}")
    report: dict[str, object] = dict(parse_siril_findstar_report(f"{result.stdout}\n{result.stderr}"))
    report["findstar_mode"] = mode
    report["setfindstar_command"] = command_line
    return report


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_fits_data(path: Path) -> np.ndarray:
    with fits.open(path, memmap=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError(f"{path} is not a 2D FITS image")
    return data


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


def star_xy(star: object) -> tuple[float, float]:
    return float(getattr(star, "x")), float(getattr(star, "y"))


def siril_to_array_y(y: float, height: int) -> float:
    return float((height - 1) - y)


def median_star_fwhm(stars: list[object], fallback: float = 3.0) -> float:
    values = []
    for star in stars:
        value = finite_float(getattr(star, "fwhm", None))
        if value is not None and value > 0.0:
            values.append(value)
    if not values:
        return fallback
    return float(np.median(np.asarray(values, dtype=np.float64)))


def resolve_radii(
    *,
    fwhm: float | None,
    fallback_fwhm: float,
    options: StarScaleOptions,
) -> ApertureRadii:
    fwhm_value = fwhm if fwhm is not None and fwhm > 0.0 else fallback_fwhm
    if options.aperture_radius is None:
        aperture = options.aperture_fwhm_factor * fwhm_value
        aperture = min(options.aperture_radius_max, max(options.aperture_radius_min, aperture))
    else:
        aperture = options.aperture_radius
    inner = (
        options.annulus_inner_radius
        if options.annulus_inner_radius is not None
        else options.annulus_inner_fwhm_factor * fwhm_value
    )
    outer = (
        options.annulus_outer_radius
        if options.annulus_outer_radius is not None
        else options.annulus_outer_fwhm_factor * fwhm_value
    )
    inner = max(inner, aperture + 1.0)
    outer = max(outer, inner + 1.0)
    return ApertureRadii(aperture=float(aperture), annulus_inner=float(inner), annulus_outer=float(outer))


def measure_aperture_flux(
    data: np.ndarray,
    *,
    x: float,
    y: float,
    radii: ApertureRadii,
    saturation_threshold: float | None,
) -> PhotometryResult:
    height, width = data.shape
    flags: list[str] = []
    outer = radii.annulus_outer
    x0 = int(math.floor(x - outer))
    x1 = int(math.ceil(x + outer))
    y0 = int(math.floor(y - outer))
    y1 = int(math.ceil(y + outer))
    if x0 < 0 or y0 < 0 or x1 >= width or y1 >= height:
        return PhotometryResult(None, None, None, None, ("edge",))

    yy, xx = np.ogrid[y0 : y1 + 1, x0 : x1 + 1]
    radius2 = (xx - x) ** 2 + (yy - y) ** 2
    aperture_mask = radius2 <= radii.aperture * radii.aperture
    annulus_mask = (radius2 >= radii.annulus_inner * radii.annulus_inner) & (
        radius2 <= radii.annulus_outer * radii.annulus_outer
    )
    aperture_pixels = np.asarray(data[y0 : y1 + 1, x0 : x1 + 1][aperture_mask], dtype=np.float64)
    annulus_pixels = np.asarray(data[y0 : y1 + 1, x0 : x1 + 1][annulus_mask], dtype=np.float64)
    if aperture_pixels.size == 0:
        flags.append("empty_aperture")
    if annulus_pixels.size == 0:
        flags.append("empty_annulus")
    if not flags and (not np.isfinite(aperture_pixels).all() or not np.isfinite(annulus_pixels).all()):
        flags.append("nonfinite_pixel")
    if saturation_threshold is not None and not flags:
        if np.max(aperture_pixels) >= saturation_threshold or np.max(annulus_pixels) >= saturation_threshold:
            flags.append("saturated")
    if flags:
        peak = finite_float(np.nanmax(aperture_pixels)) if aperture_pixels.size else None
        return PhotometryResult(None, None, peak, None, tuple(flags))

    background = float(np.median(annulus_pixels))
    flux = float(np.sum(aperture_pixels - background))
    peak = float(np.max(aperture_pixels))
    noise_per_pixel = float(np.std(annulus_pixels, ddof=1)) if annulus_pixels.size > 1 else 0.0
    if noise_per_pixel > 0.0:
        snr = flux / (noise_per_pixel * math.sqrt(float(aperture_pixels.size)))
    else:
        snr = math.inf if flux > 0.0 else 0.0
    return PhotometryResult(flux=flux, background=background, peak=peak, snr=float(snr), flags=())


def closest_distance_to_other_star(stars: list[object], index: int) -> float:
    if len(stars) <= 1:
        return math.inf
    x, y = star_xy(stars[index])
    best = math.inf
    for other_index, star in enumerate(stars):
        if other_index == index:
            continue
        ox, oy = star_xy(star)
        best = min(best, math.hypot(ox - x, oy - y))
    return best


def match_star_centers(
    reference_stars: list[object],
    target_stars: list[object],
    target_to_reference_h: np.ndarray,
    *,
    match_radius: float,
) -> list[StarMatch]:
    if not reference_stars or not target_stars:
        return []
    reference_points = np.array([star_xy(star) for star in reference_stars], dtype=np.float64)
    target_points = np.array([star_xy(star) for star in target_stars], dtype=np.float64)
    transformed = apply_homography(target_to_reference_h, target_points)
    candidates: list[tuple[float, int, int]] = []
    for target_index, point in enumerate(transformed):
        if not np.isfinite(point).all():
            continue
        deltas = reference_points - point
        distances = np.sqrt(np.sum(deltas * deltas, axis=1))
        reference_index = int(np.argmin(distances))
        distance = float(distances[reference_index])
        if distance <= match_radius:
            candidates.append((distance, reference_index, target_index))

    matches: list[StarMatch] = []
    used_reference: set[int] = set()
    used_target: set[int] = set()
    for distance, reference_index, target_index in sorted(candidates, key=lambda item: item[0]):
        if reference_index in used_reference or target_index in used_target:
            continue
        used_reference.add(reference_index)
        used_target.add(target_index)
        ref_star = reference_stars[reference_index]
        target_star = target_stars[target_index]
        x_a, y_a = star_xy(ref_star)
        x_b, y_b = star_xy(target_star)
        matches.append(
            StarMatch(
                reference_index=reference_index,
                target_index=target_index,
                x_a=x_a,
                y_a=y_a,
                x_b=x_b,
                y_b=y_b,
                x_b_registered=float(transformed[target_index, 0]),
                y_b_registered=float(transformed[target_index, 1]),
                distance_px=distance,
                fwhm_a=finite_float(getattr(ref_star, "fwhm", None)),
                fwhm_b=finite_float(getattr(target_star, "fwhm", None)),
            )
        )
    return matches


def coefficient_of_determination(observed: np.ndarray, predicted: np.ndarray) -> float | None:
    observed = np.asarray(observed, dtype=np.float64)
    predicted = np.asarray(predicted, dtype=np.float64)
    valid = np.isfinite(observed) & np.isfinite(predicted)
    if int(valid.sum()) < 2:
        return None
    y = observed[valid]
    y_hat = predicted[valid]
    total = float(np.sum((y - float(np.mean(y))) ** 2))
    if total <= 0.0:
        return None
    residual = float(np.sum((y - y_hat) ** 2))
    return float(1.0 - residual / total)


def robust_ratio_fit(
    flux_a: Iterable[float],
    flux_b: Iterable[float],
    *,
    clip_sigma: float,
    max_iterations: int,
    min_points: int,
) -> RobustRatioFit:
    a = np.asarray(list(flux_a), dtype=np.float64)
    b = np.asarray(list(flux_b), dtype=np.float64)
    valid = np.isfinite(a) & np.isfinite(b) & (a > 0.0) & (b > 0.0)
    a = a[valid]
    b = b[valid]
    n_initial = int(a.size)
    if n_initial < min_points:
        return RobustRatioFit(False, None, None, None, n_initial, 0, 0, np.zeros(n_initial, dtype=bool), "too_few_points")

    log_ratio = np.log(b / a)
    kept = np.ones(n_initial, dtype=bool)
    for _ in range(max_iterations):
        current = log_ratio[kept]
        center = float(np.median(current))
        mad = float(np.median(np.abs(current - center)))
        sigma = 1.4826 * mad if mad > 0.0 else float(np.std(current))
        if sigma <= 0.0 or not math.isfinite(sigma):
            break
        next_kept = np.abs(log_ratio - center) <= clip_sigma * sigma
        if int(next_kept.sum()) < min_points:
            break
        if np.array_equal(next_kept, kept):
            break
        kept = next_kept

    used = int(kept.sum())
    if used < min_points:
        return RobustRatioFit(False, None, None, None, n_initial, used, n_initial - used, kept, "too_few_after_clipping")

    ratios = b[kept] / a[kept]
    scale = float(np.median(ratios))
    ratio_mad = float(np.median(np.abs(ratios - scale)))
    kept_log = log_ratio[kept]
    log_sigma = float(1.4826 * np.median(np.abs(kept_log - float(np.median(kept_log)))))
    return RobustRatioFit(True, scale, ratio_mad, log_sigma, n_initial, used, n_initial - used, kept, None)


def estimate_star_scale(
    *,
    reference_fits: Path,
    target_fits: Path,
    reference_stars: list[object],
    target_stars: list[object],
    target_to_reference_h: np.ndarray,
    options: StarScaleOptions,
) -> dict[str, object]:
    data_a = read_fits_data(reference_fits)
    data_b = read_fits_data(target_fits)
    height_a, _ = data_a.shape
    height_b, _ = data_b.shape
    matches = match_star_centers(reference_stars, target_stars, target_to_reference_h, match_radius=options.match_radius)
    fallback_fwhm_a = median_star_fwhm(reference_stars)
    fallback_fwhm_b = median_star_fwhm(target_stars)

    rows: list[dict[str, object]] = []
    fit_flux_a: list[float] = []
    fit_flux_b: list[float] = []
    candidate_match_ids: list[int] = []
    for match_id, match in enumerate(matches, start=1):
        fwhm_a = match.fwhm_a if match.fwhm_a is not None else fallback_fwhm_a
        fwhm_b = match.fwhm_b if match.fwhm_b is not None else fallback_fwhm_b
        radii_a = resolve_radii(fwhm=fwhm_a, fallback_fwhm=fallback_fwhm_a, options=options)
        radii_b = resolve_radii(fwhm=fwhm_b, fallback_fwhm=fallback_fwhm_b, options=options)
        phot_a = measure_aperture_flux(
            data_a,
            x=match.x_a,
            y=siril_to_array_y(match.y_a, height_a),
            radii=radii_a,
            saturation_threshold=options.saturation_threshold,
        )
        phot_b = measure_aperture_flux(
            data_b,
            x=match.x_b,
            y=siril_to_array_y(match.y_b, height_b),
            radii=radii_b,
            saturation_threshold=options.saturation_threshold,
        )
        flags = [f"a_{flag}" for flag in phot_a.flags] + [f"b_{flag}" for flag in phot_b.flags]
        isolation_radius_a = options.isolation_radius or options.isolation_radius_factor * fwhm_a
        isolation_radius_b = options.isolation_radius or options.isolation_radius_factor * fwhm_b
        if closest_distance_to_other_star(reference_stars, match.reference_index) < isolation_radius_a:
            flags.append("a_close_neighbor")
        if closest_distance_to_other_star(target_stars, match.target_index) < isolation_radius_b:
            flags.append("b_close_neighbor")
        for label, phot in (("a", phot_a), ("b", phot_b)):
            if phot.flux is None:
                continue
            if phot.flux <= options.min_flux:
                flags.append(f"{label}_low_flux")
            if options.max_flux is not None and phot.flux >= options.max_flux:
                flags.append(f"{label}_high_flux")
            if phot.snr is not None and phot.snr < options.min_snr:
                flags.append(f"{label}_low_snr")
        if not flags and phot_a.flux is not None and phot_b.flux is not None:
            candidate_match_ids.append(match_id)
            fit_flux_a.append(float(phot_a.flux))
            fit_flux_b.append(float(phot_b.flux))
        rows.append(
            {
                "match_id": match_id,
                "flux_a": phot_a.flux,
                "flux_b": phot_b.flux,
                "flags": flags,
                "distance_px": match.distance_px,
            }
        )

    fit = robust_ratio_fit(
        fit_flux_a,
        fit_flux_b,
        clip_sigma=options.clip_sigma,
        max_iterations=options.clip_iterations,
        min_points=options.min_fit_stars,
    )
    used_flux_a = np.asarray(fit_flux_a, dtype=np.float64)
    used_flux_b = np.asarray(fit_flux_b, dtype=np.float64)
    kept_ids: list[int] = []
    r_squared = None
    target_to_reference_scale = None
    if fit.ok and fit.scale_b_over_a is not None:
        kept_mask = fit.kept_mask
        kept_ids = [candidate_match_ids[i] for i, kept in enumerate(kept_mask) if kept]
        used_flux_a = used_flux_a[kept_mask]
        used_flux_b = used_flux_b[kept_mask]
        r_squared = coefficient_of_determination(used_flux_b, float(fit.scale_b_over_a) * used_flux_a)
        if r_squared is not None and r_squared < options.min_r_squared:
            fit = RobustRatioFit(False, fit.scale_b_over_a, fit.ratio_mad, fit.log_ratio_sigma, fit.n_initial, fit.n_used, fit.clipped, fit.kept_mask, "low_r_squared")
        else:
            target_to_reference_scale = float(1.0 / fit.scale_b_over_a)

    rejection_counts: dict[str, int] = {}
    for row in rows:
        for flag in row["flags"]:
            rejection_counts[str(flag)] = rejection_counts.get(str(flag), 0) + 1
    for row in rows:
        if row["match_id"] in kept_ids:
            row["used_for_scale"] = True
        elif not row["flags"] and fit.ok:
            row["used_for_scale"] = False
            row["flags"] = [*row["flags"], "ratio_outlier"]

    report = fit.to_report()
    report["r_squared"] = r_squared
    return {
        "ok": fit.ok,
        "message": fit.message,
        "scale_b_over_a": fit.scale_b_over_a,
        "target_to_reference_scale": target_to_reference_scale,
        "matched_stars": len(matches),
        "measured_stars": len(rows),
        "candidate_stars": len(candidate_match_ids),
        "used_stars": fit.n_used if fit.ok else 0,
        "rejected_stars": len(rows) - (fit.n_used if fit.ok else 0),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "robust_fit": report,
        "options": {
            "match_radius": options.match_radius,
            "clip_sigma": options.clip_sigma,
            "clip_iterations": options.clip_iterations,
            "min_fit_stars": options.min_fit_stars,
            "min_r_squared": options.min_r_squared,
        },
    }
