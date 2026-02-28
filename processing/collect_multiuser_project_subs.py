#!/usr/bin/env python3

import argparse
import hashlib
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import AltAz, EarthLocation, get_body, get_sun
from astropy.io import fits
from astropy.time import Time


FILTER_ALIAS_MAP = {
    "H": "H",
    "HA": "H",
    "HII": "H",
    "HALPHA": "H",
    "H-ALPHA": "H",
    "HYDROGENALPHA": "H",
    
    "O": "O",
    "OIII": "O",
    "O3": "O",
    "OXYGENIII": "O",
    
    "S": "S",
    "SII": "S",
    "S2": "S",
    
    "L": "L",
    "LUM": "L",
    "LUMINANCE": "L",
    
    "R": "R",
    "RED": "R",
    
    "G": "G",
    "GREEN": "G",
    
    "B": "B",
    "BLUE": "B",
}

FILENAME_FILTER_HINTS = [
    ("HYDROGENALPHA", "H"),
    ("HALPHA", "H"),
    ("HII", "H"),
    ("OXYGENIII", "O"),
    ("OIII", "O"),
    ("O3", "O"),
    ("SULFURII", "S"),
    ("SII", "S"),
    ("S2", "S"),
    ("LUMINANCE", "L"),
    ("LUM", "L"),
    ("RED", "R"),
    ("GREEN", "G"),
    ("BLUE", "B"),
]

LAT_HEADER_KEYS = (
    "SITELAT",
    "LAT-OBS",
    "OBS-LAT",
    "LATITUDE",
    "GEOLAT",
)

LON_HEADER_KEYS = (
    "SITELONG",
    "LONG-OBS",
    "OBS-LONG",
    "LONGITUD",
    "LONGITUDE",
    "GEOLONG",
)

ELEV_HEADER_KEYS = (
    "SITEELEV",
    "ELEV-OBS",
    "OBS-ELEV",
    "ALT-OBS",
    "OBSALT",
    "ELEVATIO",
    "ELEVATION",
)

DATE_OBS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect FITS subs from multiple users/projects into filter-based symlink dirs."
    )
    parser.add_argument("input_dir", type=Path, help="Directory to search recursively for FITS files")
    parser.add_argument("output_dir", type=Path, help="Directory where grouped symlink dirs will be created")
    parser.add_argument(
        "--max-sub-duration",
        type=float,
        default=600.0,
        help="Warn if EXPTIME is longer than this (seconds). Default: 600",
    )
    parser.add_argument(
        "--max-moon-phase",
        type=float,
        default=None,
        help="Maximum allowed moon illumination percentage (0-100).",
    )
    parser.add_argument(
        "--max-moon-altitude",
        type=float,
        default=None,
        help="Maximum allowed moon altitude in degrees.",
    )
    parser.add_argument(
        "--site-lat",
        type=float,
        default=None,
        help="Override latitude (degrees) for all files.",
    )
    parser.add_argument(
        "--site-lon",
        type=float,
        default=None,
        help="Override longitude (degrees) for all files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned cleanup/link actions without modifying files",
    )
    args = parser.parse_args()

    if args.max_moon_phase is not None and not (0.0 <= args.max_moon_phase <= 100.0):
        parser.error("--max-moon-phase must be between 0 and 100.")

    if (args.site_lat is None) ^ (args.site_lon is None):
        parser.error("--site-lat and --site-lon must be provided together.")

    if args.site_lat is not None and not (-90.0 <= args.site_lat <= 90.0):
        parser.error("--site-lat must be between -90 and 90 degrees.")

    if args.site_lon is not None and not (-180.0 <= args.site_lon <= 180.0):
        parser.error("--site-lon must be between -180 and 180 degrees.")

    return args


def update_progress(current: int, total: int, prefix: str = "Progress") -> None:
    if total <= 0:
        return
    width = 30
    ratio = current / total
    filled = int(width * ratio)
    bar = "=" * filled + "-" * (width - filled)
    print(f"\r{prefix}: [{bar}] {current}/{total}", end="", flush=True)
    if current >= total:
        print()


def format_hms(total_seconds: float) -> str:
    seconds = int(round(total_seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def normalize_filter_key(filter_value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", filter_value.strip().upper())


def sanitize_name(name: str) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    cleaned = cleaned.strip("._-")
    return cleaned or None


def canonical_filter_name(raw_filter: str | None) -> str | None:
    if raw_filter is None:
        return None
    raw_filter = str(raw_filter).strip()
    if not raw_filter:
        return None
    key = normalize_filter_key(raw_filter)
    if key in FILTER_ALIAS_MAP:
        return FILTER_ALIAS_MAP[key]
    return sanitize_name(raw_filter)


def infer_filter_from_filename(file_path: Path) -> tuple[str | None, str]:
    filename_key = normalize_filter_key(file_path.stem)
    for hint, canonical in FILENAME_FILTER_HINTS:
        if hint in filename_key:
            return canonical, hint
    return None, "NO_MATCH"


def write_filter_lookup_report(output_dir: Path, rows: list[str]) -> Path:
    report_path = output_dir / "filter_lookup_report.tsv"
    report_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return report_path


def discover_fits_files(input_dir: Path) -> list[Path]:
    discovered = []
    seen = set()
    for pattern in ("*.fit", "*.fits"):
        for file_path in input_dir.rglob(pattern):
            if not file_path.is_file():
                continue
            if is_hidden_path(file_path.relative_to(input_dir)):
                continue
            resolved = file_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(file_path)
    return sorted(discovered)


def cleanup_old_symlinks(output_dir: Path, dry_run: bool) -> int:
    if not output_dir.exists():
        return 0

    removed = 0
    for child_dir in sorted(output_dir.iterdir()):
        if not child_dir.is_dir():
            continue
        for entry in sorted(child_dir.iterdir()):
            if not entry.is_symlink():
                continue
            removed += 1
            if not dry_run:
                entry.unlink()
    return removed


def link_name_from_relative_path(input_dir: Path, fits_file: Path) -> str:
    relative = fits_file.relative_to(input_dir)
    joined = "__".join(relative.parts)
    sanitized = sanitize_name(joined)
    return sanitized if sanitized is not None else fits_file.name


def make_unique_link_path(filter_dir: Path, base_name: str, target: Path) -> Path:
    candidate = filter_dir / base_name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate

    digest = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:10]
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    hashed = filter_dir / f"{stem}__{digest}{suffix}"
    if not hashed.exists() and not hashed.is_symlink():
        return hashed

    index = 2
    while True:
        indexed = filter_dir / f"{stem}__{digest}_{index}{suffix}"
        if not indexed.exists() and not indexed.is_symlink():
            return indexed
        index += 1


def header_float(header, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in header:
            continue
        try:
            return float(header[key])
        except (TypeError, ValueError):
            continue
    return None


def parse_date_obs(raw_date_obs: str | None) -> Time | None:
    if raw_date_obs is None:
        return None

    date_obs_text = str(raw_date_obs).strip()
    if not date_obs_text:
        return None

    try:
        return Time(date_obs_text, format="fits", scale="utc")
    except ValueError:
        pass

    if date_obs_text.endswith("Z"):
        date_obs_text = date_obs_text[:-1]

    for fmt in DATE_OBS_FORMATS:
        try:
            parsed_dt = datetime.strptime(date_obs_text, fmt)
            return Time(parsed_dt, scale="utc")
        except ValueError:
            continue

    return None


def extract_observer_site(
    header,
    cli_site_override: tuple[float, float] | None,
) -> tuple[float | None, float | None, float]:
    if cli_site_override is not None:
        lat, lon = cli_site_override
    else:
        lat = header_float(header, LAT_HEADER_KEYS)
        lon = header_float(header, LON_HEADER_KEYS)
    elev = header_float(header, ELEV_HEADER_KEYS)
    if elev is None:
        elev = 0.0
    return lat, lon, elev


def moon_compute_key(obs_time: Time, lat: float, lon: float, elev: float) -> tuple[int, float, float, float]:
    unix_us = int(round(float(obs_time.to_value("unix")) * 1_000_000))
    return unix_us, round(lat, 6), round(lon, 6), round(elev, 1)


def compute_moon_metrics_for_unique_keys(
    unique_keys: list[tuple[int, float, float, float]],
    chunk_size: int = 4096,
) -> dict[tuple[int, float, float, float], tuple[float, float]]:
    metrics = {}
    if not unique_keys:
        return metrics

    for start in range(0, len(unique_keys), chunk_size):
        chunk = unique_keys[start : start + chunk_size]
        unix_seconds = np.array([key[0] / 1_000_000.0 for key in chunk], dtype=float)
        latitudes = np.array([key[1] for key in chunk], dtype=float)
        longitudes = np.array([key[2] for key in chunk], dtype=float)
        elevations = np.array([key[3] for key in chunk], dtype=float)

        times = Time(unix_seconds, format="unix", scale="utc")
        locations = EarthLocation(
            lat=latitudes * u.deg,
            lon=longitudes * u.deg,
            height=elevations * u.m,
        )

        sun = get_sun(times)
        moon_topocentric = get_body("moon", times, location=locations)
        # Use geocentric moon coordinates for elongation to avoid noisy
        # NonRotationTransformationWarning output on large vectorized runs.
        moon_geocentric = get_body("moon", times)
        elongation = sun.separation(moon_geocentric)
        moon_phase_pct = 50.0 * (1.0 - np.cos(elongation.to_value(u.rad)))
        moon_altaz = moon_topocentric.transform_to(AltAz(obstime=times, location=locations))
        moon_alt_deg = moon_altaz.alt.to_value(u.deg)

        for index, key in enumerate(chunk):
            metrics[key] = (float(moon_phase_pct[index]), float(moon_alt_deg[index]))

    return metrics


def read_sub_metadata(
    fits_file: Path,
    moon_filter_enabled: bool,
    cli_site_override: tuple[float, float] | None,
) -> dict[str, object]:
    with fits.open(fits_file) as hdul:
        header = hdul[0].header
        raw_filter = header["FILTER"].strip() if "FILTER" in header else None
        exptime = None
        if "EXPTIME" in header:
            try:
                exptime = float(header["EXPTIME"])
            except (TypeError, ValueError):
                exptime = None

        moon_key = None
        moon_error = ""
        if moon_filter_enabled:
            parsed_time = parse_date_obs(header["DATE-OBS"] if "DATE-OBS" in header else None)
            lat, lon, elev = extract_observer_site(header, cli_site_override)
            if parsed_time is None:
                moon_error = "missing_or_invalid_DATE-OBS"
            elif lat is None or lon is None:
                moon_error = "missing_site_coordinates"
            else:
                moon_key = moon_compute_key(parsed_time, lat, lon, elev)

    return {
        "canonical_filter": canonical_filter_name(raw_filter),
        "exptime": exptime,
        "raw_filter": raw_filter,
        "moon_key": moon_key,
        "moon_error": moon_error,
    }


def main():
    args = parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Error: input directory does not exist: {input_dir}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("Running in DRY-RUN mode: no files will be modified.")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    removed_links = cleanup_old_symlinks(output_dir, args.dry_run)
    if args.dry_run:
        print(f"DRY-RUN cleanup summary: {removed_links} symlink(s) would be removed.")
    else:
        print(f"Cleanup summary: removed {removed_links} stale symlink(s).")

    all_files = discover_fits_files(input_dir)
    print(f"Found {len(all_files)} FITS file(s) under {input_dir}.")
    moon_filter_enabled = args.max_moon_phase is not None or args.max_moon_altitude is not None
    cli_site_override = None
    if args.site_lat is not None and args.site_lon is not None:
        cli_site_override = (float(args.site_lat), float(args.site_lon))

    per_filter_count = Counter()
    per_filter_seconds = Counter()
    warning_count = 0
    missing_filter_count = 0
    filename_lookup_used_count = 0
    error_count = 0
    linked_count = 0
    moon_filtered_out = 0
    moon_metric_errors = 0
    moon_filter_passed = 0
    report_rows = [
        "\t".join(
            [
                "source_file",
                "fits_filter_raw",
                "fits_filter_canonical",
                "filename_lookup_hint",
                "filename_lookup_canonical",
                "final_filter",
                "lookup_status",
                "moon_phase_pct",
                "moon_alt_deg",
                "moon_status",
            ]
        )
    ]

    total_files = len(all_files)
    print(f"Reading headers for {total_files} files...")
    records = []
    unique_keys = []
    unique_key_seen = set()
    for idx, fits_file in enumerate(all_files, start=1):
        try:
            metadata = read_sub_metadata(fits_file, moon_filter_enabled, cli_site_override)
        except Exception as exc:
            error_count += 1
            print(f"Warning: failed to read FITS header from {fits_file}: {exc}", file=sys.stderr)
            update_progress(idx, total_files, prefix="Headers")
            continue

        moon_key = metadata["moon_key"]
        if moon_key is not None and moon_key not in unique_key_seen:
            unique_key_seen.add(moon_key)
            unique_keys.append(moon_key)

        records.append(
            {
                "path": fits_file,
                "canonical_filter": metadata["canonical_filter"],
                "exptime": metadata["exptime"],
                "raw_filter": metadata["raw_filter"],
                "moon_key": moon_key,
                "moon_error": metadata["moon_error"],
            }
        )
        update_progress(idx, total_files, prefix="Headers")

    moon_metrics = {}
    if moon_filter_enabled and unique_keys:
        print(f"Computing moon metrics for {len(unique_keys)} unique observation key(s)...")
        moon_metrics = compute_moon_metrics_for_unique_keys(unique_keys)

    print(f"Processing {len(records)} readable files...")
    moon_error_warning_limit = 10
    moon_error_warning_count = 0

    for idx, record in enumerate(records, start=1):
        fits_file = record["path"]
        canonical_filter = record["canonical_filter"]
        exptime = record["exptime"]
        raw_filter = record["raw_filter"]

        filename_lookup_hint = ""
        filename_lookup_filter = ""
        lookup_status = "fits_header"
        moon_status = "not_requested"
        moon_phase_text = ""
        moon_alt_text = ""
        if canonical_filter is None:
            inferred_filter, matched_hint = infer_filter_from_filename(fits_file)
            filename_lookup_hint = matched_hint
            if inferred_filter is not None:
                canonical_filter = inferred_filter
                filename_lookup_filter = inferred_filter
                filename_lookup_used_count += 1
                lookup_status = "filename_fallback"
            else:
                missing_filter_count += 1
                lookup_status = "lookup_failed"
                report_rows.append(
                    "\t".join(
                        [
                            str(fits_file),
                            str(raw_filter or ""),
                            "",
                            filename_lookup_hint,
                            "",
                            "",
                            lookup_status,
                            moon_phase_text,
                            moon_alt_text,
                            moon_status,
                        ]
                    )
                )
                print(
                    f"Warning: unable to determine filter for sub '{fits_file}'; skipping.",
                    file=sys.stderr,
                )
                update_progress(idx, len(records), prefix="Processing")
                continue

        if exptime is not None and exptime > args.max_sub_duration:
            warning_count += 1
            print(
                f"Warning: EXPTIME={exptime:.3f}s exceeds max-sub-duration "
                f"{args.max_sub_duration:.3f}s for {fits_file}",
                file=sys.stderr,
            )

        if moon_filter_enabled:
            moon_key = record["moon_key"]
            moon_error = record["moon_error"]
            moon_status = "computed"
            if moon_key is None or moon_key not in moon_metrics:
                moon_metric_errors += 1
                moon_status = f"metric_error:{moon_error or 'compute_failure'}"
                if moon_error_warning_count < moon_error_warning_limit:
                    print(
                        f"Warning: missing moon metrics for '{fits_file}' "
                        f"({moon_error or 'compute_failure'}); skipping.",
                        file=sys.stderr,
                    )
                    moon_error_warning_count += 1
                report_rows.append(
                    "\t".join(
                        [
                            str(fits_file),
                            str(raw_filter or ""),
                            str(canonical_filter_name(raw_filter) or ""),
                            filename_lookup_hint,
                            filename_lookup_filter,
                            canonical_filter,
                            lookup_status,
                            moon_phase_text,
                            moon_alt_text,
                            moon_status,
                        ]
                    )
                )
                update_progress(idx, len(records), prefix="Processing")
                continue

            moon_phase_pct, moon_alt_deg = moon_metrics[moon_key]
            moon_phase_text = f"{moon_phase_pct:.3f}"
            moon_alt_text = f"{moon_alt_deg:.3f}"

            phase_pass = (
                args.max_moon_phase is not None and moon_phase_pct <= float(args.max_moon_phase)
            )
            alt_pass = (
                args.max_moon_altitude is not None and moon_alt_deg <= float(args.max_moon_altitude)
            )
            if args.max_moon_phase is not None and args.max_moon_altitude is not None:
                moon_pass = phase_pass or alt_pass
            elif args.max_moon_phase is not None:
                moon_pass = phase_pass
            else:
                moon_pass = alt_pass

            if not moon_pass:
                moon_filtered_out += 1
                moon_status = "filtered_out"
                report_rows.append(
                    "\t".join(
                        [
                            str(fits_file),
                            str(raw_filter or ""),
                            str(canonical_filter_name(raw_filter) or ""),
                            filename_lookup_hint,
                            filename_lookup_filter,
                            canonical_filter,
                            lookup_status,
                            moon_phase_text,
                            moon_alt_text,
                            moon_status,
                        ]
                    )
                )
                update_progress(idx, len(records), prefix="Processing")
                continue

            moon_filter_passed += 1
            moon_status = "passed"

        filter_dir = output_dir / canonical_filter
        link_name = link_name_from_relative_path(input_dir, fits_file)
        link_path = make_unique_link_path(filter_dir, link_name, fits_file.resolve())

        if not args.dry_run:
            filter_dir.mkdir(parents=True, exist_ok=True)
            link_path.symlink_to(fits_file.resolve())

        report_rows.append(
            "\t".join(
                [
                    str(fits_file),
                    str(raw_filter or ""),
                    str(canonical_filter_name(raw_filter) or ""),
                    filename_lookup_hint,
                    filename_lookup_filter,
                    canonical_filter,
                    lookup_status,
                    moon_phase_text,
                    moon_alt_text,
                    moon_status,
                ]
            )
        )

        per_filter_count[canonical_filter] += 1
        if exptime is not None:
            per_filter_seconds[canonical_filter] += exptime
        linked_count += 1
        update_progress(idx, len(records), prefix="Processing")

    print("\nSummary:")
    header = f"{'Filter':<12}{'Count':>10}{'Total(s)':>14}{'Total(hh:mm:ss)':>18}"
    print(header)
    print("-" * len(header))
    for filter_name in sorted(per_filter_count.keys()):
        count = per_filter_count[filter_name]
        total_seconds = float(per_filter_seconds[filter_name])
        print(
            f"{filter_name:<12}{count:>10d}{total_seconds:>14.1f}{format_hms(total_seconds):>18}"
        )

    total_seconds_all = float(sum(per_filter_seconds.values()))
    print("-" * len(header))
    print(
        f"{'TOTAL':<12}{linked_count:>10d}{total_seconds_all:>14.1f}"
        f"{format_hms(total_seconds_all):>18}"
    )
    print(f"{'duration_warnings':<22}{warning_count}")
    print(f"{'missing_filter_warnings':<22}{missing_filter_count}")
    print(f"{'filename_lookup_used':<22}{filename_lookup_used_count}")
    print(f"{'header_read_errors':<22}{error_count}")
    if moon_filter_enabled:
        print(f"{'moon_filtered_out':<22}{moon_filtered_out}")
        print(f"{'moon_metric_errors':<22}{moon_metric_errors}")
        print(f"{'moon_filter_passed':<22}{moon_filter_passed}")
        print(f"{'moon_unique_keys':<22}{len(unique_keys)}")
    report_relpath = Path("filter_lookup_report.tsv")
    if not args.dry_run:
        write_filter_lookup_report(output_dir, report_rows)
    if filename_lookup_used_count > 0 and not args.dry_run:
        print(
            f"Warning: filename-based filter lookups were used for "
            f"{filename_lookup_used_count} file(s). See report: {report_relpath}",
            file=sys.stderr,
        )
    if args.dry_run:
        print("  mode: dry-run")


if __name__ == "__main__":
    main()
