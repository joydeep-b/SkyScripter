#!/usr/bin/env python3

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.io import fits
from astropy.time import Time


REQUIRED_HEADER_KEYS = (
    "DATE-OBS",
    "FILTER",
    "SITELAT",
    "SITELONG",
    "OBJCTALT",
    "RA",
    "DEC",
)


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
        description="Plot FITS sub quality metrics vs. sub number per filter."
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


def require_header_value(header, key: str, file_path: Path):
    if key not in header:
        raise ValueError(f"Missing required header '{key}' in {file_path}")
    return header[key]


def read_sub_record(file_path: Path) -> dict:
    with fits.open(file_path) as hdul:
        header = hdul[0].header
        for key in REQUIRED_HEADER_KEYS:
            require_header_value(header, key, file_path)

        image_data = hdul[0].data
        if image_data is None:
            raise ValueError(f"Missing image data in {file_path}")

        obs_time = Time(str(header["DATE-OBS"]).strip(), format="fits")
        filter_name = str(header["FILTER"]).strip().upper()
        if not filter_name:
            raise ValueError(f"Empty FILTER value in {file_path}")

        site_lat = float(header["SITELAT"])
        site_lon = float(header["SITELONG"])
        altitude = float(header["OBJCTALT"])
        object_coord = SkyCoord(ra=float(header["RA"]) * u.deg, dec=float(header["DEC"]) * u.deg)

        location = EarthLocation(lat=site_lat * u.deg, lon=site_lon * u.deg, height=0.0 * u.m)
        altaz_frame = AltAz(obstime=obs_time, location=location)
        moon_topocentric = get_body("moon", obs_time, location=location)
        moon_altaz = moon_topocentric.transform_to(altaz_frame)
        moon_altitude = moon_altaz.alt.to_value(u.deg)
        object_altaz = object_coord.transform_to(altaz_frame)

        sun = get_sun(obs_time)
        moon_geocentric = get_body("moon", obs_time)
        elongation = sun.separation(moon_geocentric)
        moon_illumination = 50.0 * (1.0 - np.cos(elongation.to_value(u.rad)))
        moon_separation = (
            float(object_altaz.separation(moon_altaz).to_value(u.deg))
            if moon_altitude >= 0.0
            else np.nan
        )

        return {
            "path": file_path,
            "filter": filter_name,
            "time": obs_time,
            "altitude": altitude,
            "median": float(np.median(image_data)),
            "moon_illumination": float(moon_illumination),
            "moon_separation": moon_separation,
        }


def plot_filter_records(filter_name: str, records: list[dict], out_dir: Path) -> Path:
    sub_numbers = np.arange(1, len(records) + 1)
    altitude = np.array([record["altitude"] for record in records], dtype=float)
    median = np.array([record["median"] for record in records], dtype=float)
    moon_illumination = np.array(
        [record["moon_illumination"] for record in records],
        dtype=float,
    )
    moon_separation = np.array(
        [record["moon_separation"] for record in records],
        dtype=float,
    )

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    fig.suptitle(f"Sub Quality - {filter_name}")

    panels = [
        ("Altitude (deg)", altitude),
        ("Median Value", median),
        ("Moon Illumination (%)", moon_illumination),
        ("Moon Separation (deg)", moon_separation),
    ]

    for ax, (title, values) in zip(axes.flat, panels):
        ax.plot(sub_numbers, values, marker="o", linewidth=1.5, markersize=4)
        ax.set_title(title)
        ax.set_xlabel("Sub Number")
        ax.grid(True, alpha=0.3)

    fig.tight_layout()
    output_path = out_dir / f"sub_quality_{filter_name}.png"
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def main():
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    workers = resolve_workers(args.workers)

    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input directory does not exist: {input_dir}")

    fits_files = discover_fits_files(input_dir)
    if not fits_files:
        raise SystemExit(f"No FITS files found under {input_dir}")

    records_by_filter: dict[str, list[dict]] = {}
    total_files = len(fits_files)
    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_to_path = {
            executor.submit(read_sub_record, file_path): file_path for file_path in fits_files
        }
        for future in as_completed(future_to_path):
            file_path = future_to_path[future]
            try:
                record = future.result()
            except Exception as exc:
                raise SystemExit(f"Failed processing '{file_path}': {exc}") from exc
            records_by_filter.setdefault(record["filter"], []).append(record)
            completed += 1
            update_progress(completed, total_files, prefix="Reading FITS")

    out_dir.mkdir(parents=True, exist_ok=True)

    for filter_name, records in sorted(records_by_filter.items()):
        records.sort(key=lambda record: record["time"].unix)
        output_path = plot_filter_records(filter_name, records, out_dir)
        print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
