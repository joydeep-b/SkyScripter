import math
from dataclasses import dataclass
from pathlib import Path
import tempfile

from astropy.io import fits
import numpy as np

from analysis import plot_sub_quality as psq
from analysis.sub_quality_scoring import siril


DEFAULT_STAR_APERTURE_RADIUS_SCALE = 1.5
SIRIL_FLOAT_STAT_SCALE = 65535.0
STAR_BACKGROUND_FEATURE_KEYS = ("star_count", "median_mean_star_flux", "background", "bgnoise")
FeatureMeasurement = dict[str, float | int]
FITS_SUFFIXES = {".fit", ".fits", ".fts"}


@dataclass(frozen=True)
class SirilStar:
    x: float
    y: float
    fwhm: float


def parse_star_list_text(text: str) -> list[SirilStar]:
    stars = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split()
        if len(columns) < 9:
            continue
        try:
            x = float(columns[5])
            y = float(columns[6])
            fwhm_x = float(columns[7])
            fwhm_y = float(columns[8])
        except ValueError:
            continue
        fwhm_values = [
            value for value in (fwhm_x, fwhm_y)
            if math.isfinite(value) and value > 0.0
        ]
        if not math.isfinite(x) or not math.isfinite(y) or not fwhm_values:
            continue
        stars.append(SirilStar(x=x, y=y, fwhm=float(np.mean(fwhm_values))))
    return stars


def run_star_background_stats(
    sub_path: Path,
    siril_path: str,
    timeout: float,
) -> tuple[list[SirilStar], float, float]:
    with tempfile.TemporaryDirectory() as tmpdirname:
        tmpdir = Path(tmpdirname)
        star_list_path = tmpdir / "stars.lst"
        script = f"""requires 1.2.0
setcpu 1
load {siril.quote(str(sub_path))}
findstar -out={star_list_path.name}
stat
close
"""
        output = siril.run_siril_script(
            script,
            tmpdir,
            siril_path,
            timeout,
            failure_context=f"Siril star/background measurement for {sub_path}",
        )
        # Siril's findstar does not write the star list file when it detects no
        # stars (e.g. saturated or empty frames). The script itself still exits
        # successfully (run_siril_script raises on non-zero exit codes), so a
        # missing file here means zero stars, not a failure.
        if star_list_path.exists():
            stars = parse_star_list_text(star_list_path.read_text(encoding="utf-8"))
        else:
            stars = []
        return stars, siril.parse_background(output), siril.parse_bgnoise(output)


def star_aperture_slice_mask(
    shape: tuple[int, int],
    star: SirilStar,
    *,
    radius_scale: float = DEFAULT_STAR_APERTURE_RADIUS_SCALE,
) -> tuple[tuple[slice, slice], np.ndarray] | None:
    height, width = shape
    radius = radius_scale * star.fwhm
    if not math.isfinite(radius) or radius <= 0.0:
        return None

    x_min = max(int(math.floor(star.x - radius)), 0)
    x_max = min(int(math.ceil(star.x + radius)) + 1, width)
    y_min = max(int(math.floor(star.y - radius)), 0)
    y_max = min(int(math.ceil(star.y + radius)) + 1, height)
    if x_min >= x_max or y_min >= y_max:
        return None

    yy, xx = np.ogrid[y_min:y_max, x_min:x_max]
    local_mask = ((xx - star.x) ** 2 + (yy - star.y) ** 2) <= radius**2
    if not np.any(local_mask):
        return None
    return (slice(y_min, y_max), slice(x_min, x_max)), local_mask


def median_star_mean_flux(
    image: np.ndarray,
    stars: list[SirilStar],
    background: float,
    *,
    radius_scale: float = DEFAULT_STAR_APERTURE_RADIUS_SCALE,
) -> float:
    image = np.asarray(image, dtype=np.float32)
    if image.ndim != 2:
        raise ValueError(f"Expected 2D image data, got shape {image.shape}")
    if not stars or not math.isfinite(background):
        return float("nan")

    per_star_means = []
    for star in stars:
        aperture = star_aperture_slice_mask(image.shape, star, radius_scale=radius_scale)
        if aperture is None:
            continue
        slices, local_mask = aperture
        local_image = image[slices]
        aperture_pixels = local_image[local_mask & np.isfinite(local_image)]
        if aperture_pixels.size:
            per_star_means.append(float(np.mean(aperture_pixels - background)))

    if not per_star_means:
        return float("nan")
    return float(np.median(per_star_means))


def image_data_to_siril_stat_scale(image: np.ndarray) -> np.ndarray:
    image = np.asarray(image, dtype=np.float32)
    finite_pixels = image[np.isfinite(image)]
    if finite_pixels.size == 0:
        return image
    if np.nanmin(finite_pixels) >= -0.1 and np.nanmax(finite_pixels) <= 1.1:
        return image * SIRIL_FLOAT_STAT_SCALE
    return image


def median_star_mean_flux_auto_orient(
    image: np.ndarray,
    stars: list[SirilStar],
    background: float,
    *,
    radius_scale: float = DEFAULT_STAR_APERTURE_RADIUS_SCALE,
) -> float:
    # Siril's findstar reports star Y from the bottom of the frame, while astropy
    # reads FITS rows top-first. Whether a vertical flip is required depends on the
    # file's ROWORDER keyword, which is frequently missing or inconsistent across
    # the various capture/processing pipelines feeding this report. Rather than
    # trust that keyword, detect the orientation empirically: the correct vertical
    # orientation places the detected stars on real signal (large positive median
    # flux), while the mirrored one samples background (~0, often slightly
    # negative). Pick whichever yields the larger median star flux.
    upright = median_star_mean_flux(image, stars, background, radius_scale=radius_scale)
    flipped = median_star_mean_flux(np.flipud(image), stars, background, radius_scale=radius_scale)
    candidates = [value for value in (upright, flipped) if math.isfinite(value)]
    if not candidates:
        return float("nan")
    return max(candidates)


def read_fits_image_data(image_path: Path) -> np.ndarray:
    with fits.open(image_path, memmap=False) as hdul:
        return image_data_to_siril_stat_scale(psq.normalize_image_data(hdul[0].data))


def convert_xisf_to_temporary_fits(sub_path: Path, tmpdir: Path, siril_path: str, timeout: float) -> Path:
    output_stem = tmpdir / "measurement_input"
    script = f"""requires 1.2.0
setcpu 1
load {siril.quote(str(sub_path))}
save {siril.quote(str(output_stem))}
close
"""
    output = siril.run_siril_script(
        script,
        tmpdir,
        siril_path,
        timeout,
        failure_context=f"Siril XISF-to-FITS conversion for {sub_path}",
    )
    for suffix in (".fit", ".fits", ".fts"):
        output_path = output_stem.with_suffix(suffix)
        if output_path.exists():
            return output_path
    raise RuntimeError(f"Siril did not create expected FITS conversion for {sub_path}\n{output}")


def read_measurement_image_data(sub_path: Path, siril_path: str, timeout: float) -> np.ndarray:
    if sub_path.suffix.lower() in FITS_SUFFIXES:
        return read_fits_image_data(sub_path)
    if sub_path.suffix.lower() == ".xisf":
        with tempfile.TemporaryDirectory() as tmpdirname:
            converted_path = convert_xisf_to_temporary_fits(
                sub_path,
                Path(tmpdirname),
                siril_path,
                timeout,
            )
            return read_fits_image_data(converted_path)
    raise ValueError(f"Unsupported image format for measurement: {sub_path}")


def extract_star_background_features(sub_path: Path, siril_path: str, timeout: float) -> FeatureMeasurement:
    stars, background, bgnoise = run_star_background_stats(sub_path, siril_path, timeout)
    image = read_measurement_image_data(sub_path, siril_path, timeout)
    return {
        "star_count": len(stars),
        "median_mean_star_flux": median_star_mean_flux_auto_orient(image, stars, background),
        "background": background,
        "bgnoise": bgnoise,
    }
