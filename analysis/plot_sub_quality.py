#!/usr/bin/env python3

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import os
import platform
from pathlib import Path
import re
import shutil
import subprocess
import sys
import warnings

import matplotlib

if Path(sys.argv[0]).name == "plot_sub_quality.py" and "--show" not in sys.argv:
    matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.io import fits
from astropy.time import Time
from astropy.utils import iers
from astropy.utils.exceptions import AstropyWarning

iers.conf.auto_download = False
iers.conf.auto_max_age = None
warnings.filterwarnings(
    "ignore",
    message="Tried to get polar motions for times after IERS data is valid.*",
    category=AstropyWarning,
)


DEFAULT_SIRIL_TIMEOUT = 120.0
CSV_COLUMNS = [
    "path",
    "group",
    "sub_index",
    "time",
    "filter",
    "exposure",
    "object_altitude",
    "moon_altitude",
    "moon_illumination",
    "moon_separation",
    "background_median",
    "bgnoise",
    "star_count",
    "quality",
    "quality_norm",
    "quality_final",
]


@dataclass
class FrameRecord:
    path: Path
    filter: str
    time: Time
    exposure: float
    object_name: str
    group: str
    shape: tuple[int, int]
    object_altitude: float
    moon_altitude: float
    moon_illumination: float
    moon_separation: float
    background_median: float


@dataclass
class QualityRecord:
    frame: FrameRecord
    sub_index: int
    bgnoise: float
    star_count: int
    quality: float
    quality_norm: float
    quality_final: float


@dataclass
class FrameFeatures:
    index: int
    star_count: int
    bgnoise: float


def update_progress(current: int, total: int, prefix: str = "Progress"):
    if total <= 0:
        return
    width = 30
    ratio = current / total
    filled = int(width * ratio)
    bar = "=" * filled + "-" * (width - filled)
    print(f"\r{prefix}: [{bar}] {current}/{total}", end="", flush=True)
    if current >= total:
        print()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyze recursive FITS sub quality for faint-signal stacking."
    )
    parser.add_argument("input_dir", type=Path, help="Directory containing FITS light frames.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="Directory to write per-filter plot images into.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count(),
        help="Process count for per-file processing (default: number of processors).",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="CSV diagnostics output path (default: <out-dir>/sub_quality.csv).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Show each plot interactively before saving it.",
    )
    parser.add_argument(
        "--group-by",
        choices=("object-filter", "parent-filter", "filter"),
        default="object-filter",
        help="How to group subs for normalization and plotting (default: object-filter).",
    )
    parser.add_argument(
        "--siril-path",
        type=Path,
        default=None,
        help="Path to Siril executable (default: auto-detect).",
    )
    parser.add_argument(
        "--siril-timeout",
        type=float,
        default=DEFAULT_SIRIL_TIMEOUT,
        help=f"Timeout per Siril file analysis in seconds (default: {DEFAULT_SIRIL_TIMEOUT}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N discovered FITS files for tuning.",
    )
    return parser.parse_args()


def resolve_workers(requested_workers: int | None) -> int:
    if requested_workers is None:
        return os.cpu_count() or 1
    if requested_workers < 1:
        raise ValueError("--workers must be at least 1.")
    return requested_workers


def discover_fits_files(input_dir: Path) -> list[Path]:
    fits_files = []
    seen = set()
    for pattern in ("*.fit", "*.fits"):
        for file_path in input_dir.rglob(pattern):
            if not file_path.is_file():
                continue
            if any(part.startswith(".") for part in file_path.relative_to(input_dir).parts):
                continue
            resolved = file_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            fits_files.append(file_path)
    return sorted(fits_files)


def sanitize_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return label.strip("_") or "unknown"


def header_float(header, keys: tuple[str, ...], default: float = np.nan) -> float:
    for key in keys:
        if key in header:
            try:
                return float(header[key])
            except (TypeError, ValueError):
                continue
    return default


def robust_mad(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    median = np.median(values)
    return float(np.median(np.abs(values - median)))


def robust_sigma(values: np.ndarray) -> float:
    mad = robust_mad(values)
    return float(1.4826 * mad) if np.isfinite(mad) else float("nan")


def normalize_image_data(image_data: np.ndarray) -> np.ndarray:
    image = np.asarray(image_data, dtype=np.float32)
    image = np.squeeze(image)
    if image.ndim == 2:
        return image
    if image.ndim == 3:
        return np.nanmedian(image, axis=0).astype(np.float32)
    raise ValueError(f"Expected 2D image data, got shape {image.shape}")


def finite_image_pixels(image: np.ndarray) -> np.ndarray:
    return image[np.isfinite(image)]


def build_group_name(
    file_path: Path,
    input_dir: Path,
    object_name: str,
    filter_name: str,
    shape: tuple[int, int],
    group_by: str,
) -> str:
    if group_by == "filter":
        base = filter_name
    elif group_by == "parent-filter":
        try:
            parent = file_path.parent.relative_to(input_dir)
            parent_name = str(parent) if str(parent) != "." else file_path.parent.name
        except ValueError:
            parent_name = file_path.parent.name
        base = f"{parent_name}_{filter_name}"
    else:
        base = f"{object_name}_{filter_name}"
    return sanitize_label(f"{base}_{shape[1]}x{shape[0]}")


def parse_time(header, file_path: Path) -> Time:
    if "DATE-OBS" not in header:
        raise ValueError(f"Missing required header 'DATE-OBS' in {file_path}")
    return Time(str(header["DATE-OBS"]).strip(), format="fits")


def frame_location(header) -> EarthLocation | None:
    site_lat = header_float(header, ("SITELAT", "LAT-OBS", "OBSLAT"))
    site_lon = header_float(header, ("SITELONG", "SITELON", "LONG-OBS", "OBSLON"))
    if not np.isfinite(site_lat) or not np.isfinite(site_lon):
        return None
    elevation = header_float(header, ("ELEVATIO", "ELEVATION", "ALT-OBS"), default=0.0)
    if not np.isfinite(elevation):
        elevation = 0.0
    return EarthLocation(lat=site_lat * u.deg, lon=site_lon * u.deg, height=elevation * u.m)


def frame_object_coord(header) -> SkyCoord | None:
    ra = header_float(header, ("RA", "OBJCTRA"))
    dec = header_float(header, ("DEC", "OBJCTDEC"))
    if np.isfinite(ra) and np.isfinite(dec):
        return SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    return None


def compute_sky_context(header, obs_time: Time) -> tuple[float, float, float, float]:
    object_altitude = header_float(header, ("OBJCTALT", "ALTITUDE", "ALT-OBJ"))
    moon_altitude = float("nan")
    moon_illumination = float("nan")
    moon_separation = float("nan")
    location = frame_location(header)
    object_coord = frame_object_coord(header)

    if location is not None:
        altaz_frame = AltAz(obstime=obs_time, location=location)
        moon_topocentric = get_body("moon", obs_time, location=location)
        moon_altaz = moon_topocentric.transform_to(altaz_frame)
        moon_altitude = float(moon_altaz.alt.to_value(u.deg))
        if object_coord is not None:
            object_altaz = object_coord.transform_to(altaz_frame)
            if not np.isfinite(object_altitude):
                object_altitude = float(object_altaz.alt.to_value(u.deg))
            moon_separation = float(object_altaz.separation(moon_altaz).to_value(u.deg))

    sun = get_sun(obs_time)
    moon_geocentric = get_body("moon", obs_time)
    elongation = sun.separation(moon_geocentric)
    moon_illumination = float(50.0 * (1.0 - np.cos(elongation.to_value(u.rad))))
    return object_altitude, moon_altitude, moon_illumination, moon_separation


def read_sub_record(file_path: Path, input_dir: Path, group_by: str) -> FrameRecord:
    with fits.open(file_path, memmap=False) as hdul:
        header = hdul[0].header
        image_data = hdul[0].data
        if image_data is None:
            raise ValueError(f"Missing image data in {file_path}")

        image = normalize_image_data(image_data)
        obs_time = parse_time(header, file_path)
        filter_name = str(header.get("FILTER", "UNKNOWN")).strip().upper() or "UNKNOWN"
        object_name = str(header.get("OBJECT", file_path.parent.name)).strip() or file_path.parent.name
        exposure = header_float(header, ("EXPTIME", "EXPOSURE", "EXP_TIME"), default=1.0)
        if not np.isfinite(exposure) or exposure <= 0.0:
            exposure = 1.0
        object_altitude, moon_altitude, moon_illumination, moon_separation = compute_sky_context(
            header,
            obs_time,
        )
        shape = tuple(int(v) for v in image.shape)
        group = build_group_name(file_path, input_dir, object_name, filter_name, shape, group_by)

        return FrameRecord(
            path=file_path,
            filter=filter_name,
            time=obs_time,
            exposure=float(exposure),
            object_name=object_name,
            group=group,
            shape=shape,
            object_altitude=object_altitude,
            moon_altitude=moon_altitude,
            moon_illumination=moon_illumination,
            moon_separation=moon_separation,
            background_median=float(np.nanmedian(finite_image_pixels(image))),
        )


def get_siril_path(explicit_path: Path | None = None) -> str:
    if explicit_path is not None:
        return str(explicit_path.expanduser().resolve())

    candidates = []
    if platform.system() == "Darwin":
        candidates.extend(
            [
                "/Applications/Siril.app/Contents/MacOS/Siril",
                "/Applications/Siril.app/Contents/MacOS/siril-cli",
            ]
        )
    candidates.extend(["siril-cli", "siril"])

    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    raise RuntimeError("Siril executable not found. Pass --siril-path explicitly.")


def siril_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_siril_star_count(output: str) -> int:
    matches = re.findall(r"Found\s+([0-9]+)\s+Gaussian profile stars", output)
    if not matches:
        raise ValueError("Could not parse Siril findstar star count.")
    return max(int(match) for match in matches)


def parse_siril_bgnoise(output: str) -> float:
    matches = re.findall(r"bgnoise:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)", output)
    if not matches:
        raise ValueError("Could not parse Siril stat bgnoise.")
    return float(matches[-1])


def legacy_quality_score(star_count: int, bgnoise: float) -> float:
    if star_count > 0 and np.isfinite(bgnoise) and bgnoise > 0.0:
        return float(star_count / bgnoise)
    return float("nan")


def run_siril_quality_stats(record: FrameRecord, siril_path: str, timeout: float) -> tuple[int, float]:
    script = f"""requires 1.2.0
load {siril_quote(record.path.name)}
findstar
stat
close
"""
    result = subprocess.run(
        [siril_path, "-d", str(record.path.parent), "-s", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise RuntimeError(f"Siril failed with exit code {result.returncode}:\n{output}")
    return parse_siril_star_count(output), parse_siril_bgnoise(output)


def normalized_by_median(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    finite_positive = values[np.isfinite(values) & (values > 0.0)]
    if finite_positive.size == 0:
        return np.full(values.shape, np.nan)
    median = float(np.median(finite_positive))
    if median <= 0.0:
        return np.full(values.shape, np.nan)
    return values / median


def extract_frame_features(index: int, record: FrameRecord, siril_path: str, timeout: float) -> FrameFeatures:
    star_count, bgnoise = run_siril_quality_stats(record, siril_path, timeout)
    return FrameFeatures(index=index, star_count=star_count, bgnoise=bgnoise)


def extract_group_features(records: list[FrameRecord], args, siril_path: str) -> list[FrameFeatures]:
    features_by_index: list[FrameFeatures | None] = [None] * len(records)
    completed = 0
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        future_to_record = {
            executor.submit(extract_frame_features, index, record, siril_path, args.siril_timeout): record
            for index, record in enumerate(records)
        }
        for future in as_completed(future_to_record):
            record = future_to_record[future]
            try:
                features = future.result()
            except Exception as exc:
                raise RuntimeError(f"Failed Siril analysis for '{record.path}': {exc}") from exc
            features_by_index[features.index] = features
            completed += 1
            update_progress(completed, len(records), prefix="Siril stats")

    return [
        features if features is not None
        else FrameFeatures(
            index=index,
            star_count=0,
            bgnoise=float("nan"),
        )
        for index, features in enumerate(features_by_index)
    ]


def analyze_group(records: list[FrameRecord], args, siril_path: str) -> list[QualityRecord]:
    records = sorted(records, key=lambda record: record.time.unix)
    features = extract_group_features(records, args, siril_path)

    quality_records: list[QualityRecord] = []
    raw_quality = []
    for sub_index, (record, feature) in enumerate(zip(records, features), start=1):
        quality = legacy_quality_score(feature.star_count, feature.bgnoise)

        raw_quality.append(quality)
        quality_records.append(
            QualityRecord(
                frame=record,
                sub_index=sub_index,
                bgnoise=feature.bgnoise,
                star_count=feature.star_count,
                quality=quality,
                quality_norm=float("nan"),
                quality_final=float("nan"),
            )
        )

    quality_norm = normalized_by_median(np.asarray(raw_quality, dtype=float))
    final_quality = np.asarray(raw_quality, dtype=float).copy()
    final_quality_norm = normalized_by_median(final_quality)
    for idx, record in enumerate(quality_records):
        record.quality_norm = float(quality_norm[idx])
        record.quality_final = float(final_quality_norm[idx])
    return quality_records


def plot_group_records(
    group_name: str,
    records: list[QualityRecord],
    out_dir: Path,
    *,
    show: bool = False,
) -> Path:
    sub_numbers = np.array([record.sub_index for record in records], dtype=int)
    quality_final = np.array([record.quality_final for record in records], dtype=float)
    bgnoise = np.array([record.bgnoise for record in records], dtype=float)
    star_count = np.array([record.star_count for record in records], dtype=float)
    object_altitude = np.array([record.frame.object_altitude for record in records], dtype=float)
    moon_altitude = np.array([record.frame.moon_altitude for record in records], dtype=float)
    visible_moon_altitude = np.where(moon_altitude > 0.0, moon_altitude, np.nan)

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    fig.suptitle(f"Sub Quality - {group_name}")

    ax_quality = axes[0]
    ax_quality.plot(
        sub_numbers,
        quality_final,
        marker="o",
        linewidth=1.5,
        markersize=4,
        label="Normalized Quality",
    )
    ax_quality.set_ylabel("Quality")
    ax_quality.grid(True, alpha=0.3)
    ax_altitude = ax_quality.twinx()
    ax_altitude.plot(
        sub_numbers,
        object_altitude,
        linestyle="--",
        linewidth=1.2,
        label="Object Alt",
        color="tab:green",
    )
    ax_altitude.plot(
        sub_numbers,
        visible_moon_altitude,
        linestyle=":",
        linewidth=1.2,
        label="Moon Alt",
        color="tab:red",
    )
    ax_altitude.set_ylabel("Altitude (deg)")
    lines, labels = ax_quality.get_legend_handles_labels()
    altitude_lines, altitude_labels = ax_altitude.get_legend_handles_labels()
    ax_quality.legend(lines + altitude_lines, labels + altitude_labels, loc="best")

    axes[1].plot(sub_numbers, star_count, marker="o", linewidth=1.2, markersize=4)
    axes[1].set_ylabel("Siril Stars")
    axes[1].grid(True, alpha=0.3)

    axes[2].plot(sub_numbers, bgnoise, marker="o", linewidth=1.2, markersize=4)
    axes[2].set_ylabel("Siril bgnoise")
    axes[2].set_xlabel("Sub Index")
    axes[2].grid(True, alpha=0.3)

    fig.tight_layout()
    output_path = out_dir / f"sub_quality_{sanitize_label(group_name)}.png"
    if show:
        try:
            fig.canvas.manager.set_window_title(f"Sub Quality - {group_name}")
        except AttributeError:
            pass
        plt.show(block=True)
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def csv_value(value):
    if isinstance(value, Time):
        return value.isot
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return "" if not np.isfinite(value) else f"{value:.8g}"
    return value


def quality_record_to_row(record: QualityRecord) -> dict:
    frame = record.frame
    return {
        "path": str(frame.path),
        "group": frame.group,
        "sub_index": record.sub_index,
        "time": frame.time.isot,
        "filter": frame.filter,
        "exposure": frame.exposure,
        "object_altitude": frame.object_altitude,
        "moon_altitude": frame.moon_altitude,
        "moon_illumination": frame.moon_illumination,
        "moon_separation": frame.moon_separation,
        "background_median": frame.background_median,
        "bgnoise": record.bgnoise,
        "star_count": record.star_count,
        "quality": record.quality,
        "quality_norm": record.quality_norm,
        "quality_final": record.quality_final,
    }


def write_csv(records: list[QualityRecord], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for record in records:
            row = quality_record_to_row(record)
            writer.writerow({key: csv_value(row[key]) for key in CSV_COLUMNS})


def print_summary(records: list[QualityRecord]) -> None:
    by_group: dict[str, list[QualityRecord]] = {}
    for record in records:
        by_group.setdefault(record.frame.group, []).append(record)

    for group_name, group_records in sorted(by_group.items()):
        valid = [
            record for record in group_records
            if np.isfinite(record.quality_final)
        ]
        print(f"\n{group_name}: {len(valid)}/{len(group_records)} frames scored")
        if not valid:
            continue
        best = sorted(valid, key=lambda record: record.quality_final, reverse=True)[:3]
        worst = sorted(valid, key=lambda record: record.quality_final)[:3]
        print("  Best:")
        for record in best:
            print(
                f"    #{record.sub_index:04d} q={record.quality_final:.3f} "
                f"stars={record.star_count} bgnoise={record.bgnoise:.3g} {record.frame.path.name}"
            )
        print("  Worst:")
        for record in worst:
            print(
                f"    #{record.sub_index:04d} q={record.quality_final:.3f} "
                f"stars={record.star_count} bgnoise={record.bgnoise:.3g} {record.frame.path.name}"
            )


def read_records_parallel(fits_files: list[Path], input_dir: Path, group_by: str, workers: int) -> list[FrameRecord]:
    records = []
    completed = 0
    total_files = len(fits_files)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_path = {
            executor.submit(read_sub_record, file_path, input_dir, group_by): file_path
            for file_path in fits_files
        }
        for future in as_completed(future_to_path):
            file_path = future_to_path[future]
            try:
                records.append(future.result())
            except Exception as exc:
                print(f"Skipping '{file_path}': {exc}", file=sys.stderr)
            completed += 1
            update_progress(completed, total_files, prefix="Reading FITS")
    return records


def main():
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    csv_path = (args.csv or (out_dir / "sub_quality.csv")).expanduser().resolve()
    workers = resolve_workers(args.workers)
    try:
        siril_path = get_siril_path(args.siril_path)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    fits_files = discover_fits_files(input_dir)
    if args.limit is not None:
        fits_files = fits_files[: args.limit]
    if not fits_files:
        raise SystemExit(f"No FITS files found under {input_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    records = read_records_parallel(fits_files, input_dir, args.group_by, workers)
    if not records:
        raise SystemExit("No usable FITS files found.")

    records_by_group: dict[str, list[FrameRecord]] = {}
    for record in records:
        records_by_group.setdefault(record.group, []).append(record)

    all_quality_records: list[QualityRecord] = []
    for group_name, group_records in sorted(records_by_group.items()):
        if len(group_records) < 2:
            print(f"Skipping group '{group_name}': need at least two frames", file=sys.stderr)
            continue
        print(f"Analyzing {group_name}: {len(group_records)} frames")
        try:
            quality_records = analyze_group(group_records, args, siril_path)
        except Exception as exc:
            print(f"Skipping group '{group_name}': {exc}", file=sys.stderr)
            continue
        all_quality_records.extend(quality_records)
        output_path = plot_group_records(group_name, quality_records, out_dir, show=args.show)
        print(f"Wrote {output_path}")

    if not all_quality_records:
        raise SystemExit("No quality records were produced.")

    write_csv(all_quality_records, csv_path)
    print(f"Wrote {csv_path}")
    print_summary(all_quality_records)


if __name__ == "__main__":
    main()
