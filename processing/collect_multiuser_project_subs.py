#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
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

SUPPORTED_IMAGE_SUFFIXES = {".fit", ".fits", ".xisf"}

XISF_PROPERTY_ALIASES = {
    "FILTER": (
        "FILTER",
        "INSTRUMENT:FILTER:NAME",
        "OBSERVATION:FILTER",
        "OBSERVATION:FILTER:NAME",
    ),
    "EXPTIME": (
        "EXPTIME",
        "EXPOSURE",
        "EXPOSURETIME",
        "OBSERVATION:EXPOSURETIME",
    ),
    "DATE-OBS": (
        "DATE-OBS",
        "DATEOBS",
        "OBSERVATION:TIME:START",
        "OBSERVATION:STARTTIME",
    ),
    "SITELAT": (
        "SITELAT",
        "LAT-OBS",
        "OBS-LAT",
        "OBSERVATION:LOCATION:LATITUDE",
    ),
    "SITELONG": (
        "SITELONG",
        "LONG-OBS",
        "OBS-LONG",
        "OBSERVATION:LOCATION:LONGITUDE",
    ),
    "SITEELEV": (
        "SITEELEV",
        "ELEV-OBS",
        "OBS-ELEV",
        "OBSERVATION:LOCATION:ELEVATION",
    ),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect FITS/XISF subs from multiple users/projects into filter-based symlink dirs."
    )
    parser.add_argument("input_dir", type=Path, help="Directory to search recursively for FITS/XISF files")
    parser.add_argument("output_dir", type=Path, help="Directory where grouped symlink dirs will be created")
    parser.add_argument(
        "--max-sub-duration",
        type=float,
        default=600.0,
        help="Skip subs with EXPTIME longer than this (seconds). Default: 600",
    )
    parser.add_argument(
        "--min-sub-duration",
        type=float,
        default=0.0,
        help="Skip subs with EXPTIME shorter than this (seconds). Default: 0",
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
        "--siril-path",
        type=Path,
        default=None,
        help="Optional explicit path to Siril executable for XISF-to-FITS conversion.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned cleanup/link actions without modifying files",
    )
    parser.add_argument(
        "--no-folder-filter-prompts",
        action="store_true",
        help=(
            "Do not prompt for a folder-level filter when metadata and filename "
            "lookups cannot determine one."
        ),
    )
    args = parser.parse_args()

    if args.max_moon_phase is not None and not (0.0 <= args.max_moon_phase <= 100.0):
        parser.error("--max-moon-phase must be between 0 and 100.")

    if args.min_sub_duration < 0.0:
        parser.error("--min-sub-duration must be >= 0.")

    if args.max_sub_duration <= 0.0:
        parser.error("--max-sub-duration must be > 0.")

    if args.min_sub_duration > args.max_sub_duration:
        parser.error("--min-sub-duration cannot be greater than --max-sub-duration.")

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


def format_hm_compact(total_seconds: float) -> str:
    seconds = int(round(total_seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


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


def prompt_for_folder_filter(folder: Path, source_file: Path) -> str | None:
    print()
    print(f"Unable to determine filter for: {source_file}")
    print(f"Folder: {folder}")
    print("Enter a filter for all unresolved files in this folder, or leave blank to skip them.")

    while True:
        try:
            answer = input("Filter [L/R/G/B/H/S/O or custom; blank=skip]: ")
        except EOFError:
            print("No input available; skipping unresolved files in this folder.")
            return None

        answer = answer.strip()
        if not answer:
            return None

        canonical = canonical_filter_name(answer)
        if canonical is not None:
            return canonical

        print(f"Could not use filter value '{answer}'. Please try again or leave blank to skip.")


def read_folder_filter_marker(folder: Path) -> str | None:
    markers = sorted(folder.glob(".filter_*"))
    if not markers:
        return None

    for marker in markers:
        raw_filter = marker.name.removeprefix(".filter_")
        canonical = canonical_filter_name(raw_filter)
        if canonical is not None:
            return canonical

    return None


def filter_lookup_report_row(
    *,
    source_file: Path,
    raw_filter: str | None,
    filename_lookup_hint: str,
    filename_lookup_filter: str,
    final_filter: str,
    lookup_status: str,
    moon_phase_text: str,
    moon_alt_text: str,
    moon_status: str,
    collection_file: Path | None = None,
    collection_status: str = "",
) -> dict[str, str]:
    return {
        "source_file": str(source_file),
        "collection_file": str(collection_file or ""),
        "collection_status": collection_status,
        "image_filter_raw": str(raw_filter or ""),
        "image_filter_canonical": str(canonical_filter_name(raw_filter) or ""),
        "filename_lookup_hint": filename_lookup_hint,
        "filename_lookup_canonical": filename_lookup_filter,
        "final_filter": final_filter,
        "lookup_status": lookup_status,
        "moon_phase_pct": moon_phase_text,
        "moon_alt_deg": moon_alt_text,
        "moon_status": moon_status,
    }


def write_filter_lookup_report(output_dir: Path, input_dir: Path, rows: list[dict[str, str]]) -> Path:
    report_path = output_dir / "filter_lookup_report.json"
    report = {
        "input_dir": str(input_dir),
        "rows": rows,
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report_path


def discover_image_files(input_dir: Path) -> list[Path]:
    discovered = []
    seen = set()
    for file_path in input_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
            continue
        if is_hidden_path(file_path.relative_to(input_dir)):
            continue
        resolved = file_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        discovered.append(file_path)
    return sorted(discovered)


def image_format_name(source_file: Path) -> str:
    suffix = source_file.suffix.lower()
    if suffix in {".fit", ".fits"}:
        return "fits"
    if suffix == ".xisf":
        return "xisf"
    return suffix.lstrip(".") or "unknown"


def user_name_from_relative_path(relative_path: Path) -> str:
    return relative_path.parts[0] if len(relative_path.parts) > 1 else "(root)"


def print_file_format_counts_by_user(input_dir: Path, image_files: list[Path]) -> None:
    per_user_format_counts = defaultdict(Counter)
    for source_file in image_files:
        relative_path = source_file.relative_to(input_dir)
        user_name = user_name_from_relative_path(relative_path)
        per_user_format_counts[user_name][image_format_name(source_file)] += 1

    print("\nFiles found by user:")
    header = f"{'User':<30}{'FITS':>10}{'XISF':>10}{'Total':>10}"
    print(header)
    print("-" * len(header))

    fits_total = 0
    xisf_total = 0
    for user_name in sorted(per_user_format_counts.keys(), key=str.lower):
        fits_count = int(per_user_format_counts[user_name].get("fits", 0))
        xisf_count = int(per_user_format_counts[user_name].get("xisf", 0))
        user_total = fits_count + xisf_count
        fits_total += fits_count
        xisf_total += xisf_count
        print(f"{user_name:<30}{fits_count:>10d}{xisf_count:>10d}{user_total:>10d}")

    print("-" * len(header))
    print(f"{'TOTAL':<30}{fits_total:>10d}{xisf_total:>10d}{fits_total + xisf_total:>10d}")


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


def link_name_from_relative_path(input_dir: Path, source_file: Path) -> str:
    relative = source_file.relative_to(input_dir)
    joined = "__".join(relative.parts)
    sanitized = sanitize_name(joined)
    return sanitized if sanitized is not None else source_file.name


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


def find_siril_path(explicit_path: Path | None) -> str:
    if explicit_path is not None:
        if explicit_path.exists():
            return str(explicit_path)
        raise FileNotFoundError(f"Siril path does not exist: {explicit_path}")

    if sys.platform == "darwin":
        mac_path = Path("/Applications/Siril.app/Contents/MacOS/Siril")
        if mac_path.exists():
            return str(mac_path)

    linux_candidates = [
        Path("/home/joydeepb/Siril-1.2.1-x86_64.AppImage"),
        Path("/home/joydeepb/Siril-1.2.5-x86_64.AppImage"),
    ]
    for candidate in linux_candidates:
        if candidate.exists():
            return str(candidate)

    for command_name in ("siril-cli", "siril"):
        resolved = shutil.which(command_name)
        if resolved:
            return resolved

    raise FileNotFoundError(
        "Could not find Siril executable. Use --siril-path to specify it explicitly."
    )


def write_siril_log(
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


def run_siril_script(
    siril_path: str,
    working_dir: Path,
    script_text: str,
    log_path: Path,
    context: str,
) -> None:
    command = [siril_path, "-d", str(working_dir), "-s", "-"]
    result = subprocess.run(command, input=script_text, text=True, capture_output=True)
    write_siril_log(log_path, context, command, script_text, result)
    if result.returncode != 0:
        raise RuntimeError(
            f"Siril failed for {context} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def converted_fits_path(input_dir: Path, output_dir: Path, source_file: Path) -> Path:
    relative = source_file.relative_to(input_dir)
    return output_dir / ".converted_xisf" / relative.with_suffix(".fit")


def conversion_work_dir(output_dir: Path) -> Path:
    return output_dir / ".conversion_work" / "xisf_batch"


def converted_fits_is_current(source_file: Path, destination: Path) -> bool:
    return (
        destination.exists()
        and destination.stat().st_size > 0
        and destination.stat().st_mtime >= source_file.stat().st_mtime
    )


def parse_siril_conversion_report(report_path: Path) -> dict[str, Path]:
    if not report_path.exists():
        raise FileNotFoundError(f"Siril conversion report not found: {report_path}")

    mapping: dict[str, Path] = {}
    line_pattern = re.compile(r"^'(?P<source>.*)' -> '(?P<output>.*)'(?: image \d+)?$")
    for raw_line in report_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = line_pattern.match(line)
        if match is None:
            continue
        source_path = Path(match.group("source"))
        output_path = Path(match.group("output"))
        mapping[str(source_path)] = output_path
        mapping[source_path.name] = output_path

    return mapping


def convert_xisfs_to_fits(
    *,
    conversion_jobs: list[tuple[Path, Path]],
    output_dir: Path,
    siril_path: str,
    log_path: Path,
) -> None:
    if not conversion_jobs:
        return

    work_dir = conversion_work_dir(output_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    link_paths: list[Path] = []
    for index, (source_file, _destination) in enumerate(conversion_jobs, start=1):
        link_path = work_dir / f"pp_light_{index:05d}{source_file.suffix.lower()}"
        link_path.symlink_to(source_file.resolve())
        link_paths.append(link_path)

    script_text = "requires 1.3.5\nconvert pp_light -out=.process\n"
    context = f"convert {len(conversion_jobs)} XISF file(s)"
    run_siril_script(siril_path, work_dir, script_text, log_path, context)

    report_path = work_dir / ".process" / "pp_light_conversion.txt"
    converted_outputs = parse_siril_conversion_report(report_path)
    for link_path, (_source_file, destination) in zip(link_paths, conversion_jobs, strict=True):
        produced = converted_outputs.get(str(link_path)) or converted_outputs.get(link_path.name)
        if produced is None:
            raise FileNotFoundError(
                f"Siril conversion report has no output entry for {link_path}"
            )
        if not produced.is_absolute():
            produced = work_dir / produced
        if not produced.exists():
            raise FileNotFoundError(f"Siril reported converted output does not exist: {produced}")

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        shutil.move(str(produced), str(destination))

    shutil.rmtree(work_dir)


def link_name_for_collection_target(input_dir: Path, source_file: Path, target_file: Path) -> str:
    link_name = link_name_from_relative_path(input_dir, source_file)
    if source_file.suffix.lower() == ".xisf":
        return str(Path(link_name).with_suffix(target_file.suffix.lower()))
    return link_name


def normalize_metadata_key(key: str) -> str:
    return re.sub(r"[^A-Z0-9:]", "", str(key).strip().upper())


def get_header_value(header, key: str):
    if key in header:
        return header[key]
    upper_key = key.upper()
    if upper_key in header:
        return header[upper_key]
    normalized_key = normalize_metadata_key(key)
    for existing_key, value in header.items():
        if normalize_metadata_key(existing_key) == normalized_key:
            return value
    return None


def header_float(header, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = get_header_value(header, key)
        if value is None:
            continue
        try:
            return float(value)
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


def clean_xisf_metadata_value(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        value = value[1:-1].replace("''", "'").strip()
    return value


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_xisf_header(xisf_file: Path) -> dict[str, str]:
    with xisf_file.open("rb") as handle:
        signature = handle.read(8)
        if signature != b"XISF0100":
            raise ValueError("not a supported XISF0100 file")
        header_length_bytes = handle.read(4)
        if len(header_length_bytes) != 4:
            raise ValueError("truncated XISF header length")
        header_length = int.from_bytes(header_length_bytes, byteorder="little", signed=False)
        handle.read(4)  # reserved field
        header_xml = handle.read(header_length)

    root = ET.fromstring(header_xml.decode("utf-8"))
    header: dict[str, str] = {}

    for element in root.iter():
        local_name = xml_local_name(element.tag)
        if local_name == "FITSKeyword":
            name = element.attrib.get("name")
            value = clean_xisf_metadata_value(element.attrib.get("value"))
            if name and value is not None:
                header[name.strip().upper()] = value
            continue

        if local_name != "Property":
            continue
        property_id = element.attrib.get("id") or element.attrib.get("name")
        value = (
            element.attrib.get("value")
            or element.attrib.get("Value")
            or element.text
        )
        cleaned_value = clean_xisf_metadata_value(value)
        if property_id and cleaned_value is not None:
            header[property_id.strip().upper()] = cleaned_value

    for canonical_key, aliases in XISF_PROPERTY_ALIASES.items():
        if canonical_key in header:
            continue
        for alias in aliases:
            value = get_header_value(header, alias)
            if value is not None:
                header[canonical_key] = value
                break

    return header


def read_image_header(source_file: Path):
    suffix = source_file.suffix.lower()
    if suffix in {".fit", ".fits"}:
        with fits.open(source_file) as hdul:
            return dict(hdul[0].header)
    if suffix == ".xisf":
        return read_xisf_header(source_file)
    raise ValueError(f"unsupported image suffix: {source_file.suffix}")


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
    source_file: Path,
    moon_filter_enabled: bool,
    cli_site_override: tuple[float, float] | None,
) -> dict[str, object]:
    header = read_image_header(source_file)
    raw_filter = get_header_value(header, "FILTER")
    raw_filter = str(raw_filter).strip() if raw_filter is not None else None
    exptime = None
    raw_exptime = get_header_value(header, "EXPTIME")
    if raw_exptime is not None:
        try:
            exptime = float(raw_exptime)
        except (TypeError, ValueError):
            exptime = None

    moon_key = None
    moon_error = ""
    if moon_filter_enabled:
        parsed_time = parse_date_obs(get_header_value(header, "DATE-OBS"))
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

    all_files = discover_image_files(input_dir)
    print(f"Found {len(all_files)} supported image file(s) under {input_dir}.")
    print_file_format_counts_by_user(input_dir, all_files)
    moon_filter_enabled = args.max_moon_phase is not None or args.max_moon_altitude is not None
    cli_site_override = None
    if args.site_lat is not None and args.site_lon is not None:
        cli_site_override = (float(args.site_lat), float(args.site_lon))

    per_filter_count = Counter()
    per_filter_seconds = Counter()
    per_user_filter_seconds = defaultdict(Counter)
    warning_count = 0
    missing_filter_count = 0
    filename_lookup_used_count = 0
    folder_marker_used_count = 0
    folder_prompt_used_count = 0
    error_count = 0
    linked_count = 0
    moon_filtered_out = 0
    moon_metric_errors = 0
    moon_filter_passed = 0
    converted_xisf_count = 0
    cached_xisf_count = 0
    conversion_error_count = 0
    report_rows: list[dict[str, str]] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    conversion_log_path = output_dir / f"xisf_conversion_{timestamp}.log"

    total_files = len(all_files)
    print(f"Reading headers for {total_files} files...")
    records = []
    unique_keys = []
    unique_key_seen = set()
    for idx, source_file in enumerate(all_files, start=1):
        try:
            metadata = read_sub_metadata(source_file, moon_filter_enabled, cli_site_override)
        except Exception as exc:
            error_count += 1
            print(f"Warning: failed to read image metadata from {source_file}: {exc}", file=sys.stderr)
            update_progress(idx, total_files, prefix="Headers")
            continue

        moon_key = metadata["moon_key"]
        if moon_key is not None and moon_key not in unique_key_seen:
            unique_key_seen.add(moon_key)
            unique_keys.append(moon_key)

        records.append(
            {
                "path": source_file,
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
    folder_filter_prompts_enabled = not args.no_folder_filter_prompts and sys.stdin.isatty()
    folder_filter_cache: dict[Path, str | None] = {}
    folder_filter_status_cache: dict[Path, str] = {}
    accepted_records: list[dict[str, object]] = []

    for idx, record in enumerate(records, start=1):
        source_file = record["path"]
        canonical_filter = record["canonical_filter"]
        exptime = record["exptime"]
        raw_filter = record["raw_filter"]

        filename_lookup_hint = ""
        filename_lookup_filter = ""
        lookup_status = "metadata_header"
        moon_status = "not_requested"
        moon_phase_text = ""
        moon_alt_text = ""
        if canonical_filter is None:
            inferred_filter, matched_hint = infer_filter_from_filename(source_file)
            filename_lookup_hint = matched_hint
            if inferred_filter is not None:
                canonical_filter = inferred_filter
                filename_lookup_filter = inferred_filter
                filename_lookup_used_count += 1
                lookup_status = "filename_fallback"
            else:
                folder_filter = None
                folder = source_file.parent
                if folder not in folder_filter_cache:
                    marker_filter = read_folder_filter_marker(folder)
                    if marker_filter is not None:
                        folder_filter_cache[folder] = marker_filter
                        folder_filter_status_cache[folder] = "folder_marker"
                    elif folder_filter_prompts_enabled:
                        folder_filter_cache[folder] = prompt_for_folder_filter(folder, source_file)
                        folder_filter_status_cache[folder] = "folder_prompt"
                    else:
                        folder_filter_cache[folder] = None
                        folder_filter_status_cache[folder] = "lookup_failed"
                folder_filter = folder_filter_cache[folder]
                folder_filter_status = folder_filter_status_cache[folder]

                if folder_filter is not None:
                    canonical_filter = folder_filter
                    lookup_status = folder_filter_status
                    if folder_filter_status == "folder_marker":
                        folder_marker_used_count += 1
                    else:
                        folder_prompt_used_count += 1
                else:
                    missing_filter_count += 1
                    lookup_status = "lookup_failed"
                    report_rows.append(
                        filter_lookup_report_row(
                            source_file=source_file,
                            raw_filter=raw_filter,
                            filename_lookup_hint=filename_lookup_hint,
                            filename_lookup_filter="",
                            final_filter="",
                            lookup_status=lookup_status,
                            moon_phase_text=moon_phase_text,
                            moon_alt_text=moon_alt_text,
                            moon_status=moon_status,
                        )
                    )
                    print(
                        f"Warning: unable to determine filter for sub '{source_file}'; skipping.",
                        file=sys.stderr,
                    )
                    update_progress(idx, len(records), prefix="Processing")
                    continue

        if exptime is not None and exptime < args.min_sub_duration:
            warning_count += 1
            print(
                f"Warning: EXPTIME={exptime:.3f}s is below min-sub-duration "
                f"{args.min_sub_duration:.3f}s for {source_file}; skipping.",
                file=sys.stderr,
            )
            update_progress(idx, len(records), prefix="Processing")
            continue

        if exptime is not None and exptime > args.max_sub_duration:
            warning_count += 1
            print(
                f"Warning: EXPTIME={exptime:.3f}s exceeds max-sub-duration "
                f"{args.max_sub_duration:.3f}s for {source_file}; skipping.",
                file=sys.stderr,
            )
            update_progress(idx, len(records), prefix="Processing")
            continue

        if moon_filter_enabled:
            moon_key = record["moon_key"]
            moon_error = record["moon_error"]
            moon_status = "computed"
            if moon_key is None or moon_key not in moon_metrics:
                moon_metric_errors += 1
                moon_status = f"metric_error:{moon_error or 'compute_failure'}"
                if moon_error_warning_count < moon_error_warning_limit:
                    print(
                        f"Warning: missing moon metrics for '{source_file}' "
                        f"({moon_error or 'compute_failure'}); skipping.",
                        file=sys.stderr,
                    )
                    moon_error_warning_count += 1
                report_rows.append(
                    filter_lookup_report_row(
                        source_file=source_file,
                        raw_filter=raw_filter,
                        filename_lookup_hint=filename_lookup_hint,
                        filename_lookup_filter=filename_lookup_filter,
                        final_filter=canonical_filter,
                        lookup_status=lookup_status,
                        moon_phase_text=moon_phase_text,
                        moon_alt_text=moon_alt_text,
                        moon_status=moon_status,
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
                    filter_lookup_report_row(
                        source_file=source_file,
                        raw_filter=raw_filter,
                        filename_lookup_hint=filename_lookup_hint,
                        filename_lookup_filter=filename_lookup_filter,
                        final_filter=canonical_filter,
                        lookup_status=lookup_status,
                        moon_phase_text=moon_phase_text,
                        moon_alt_text=moon_alt_text,
                        moon_status=moon_status,
                    )
                )
                update_progress(idx, len(records), prefix="Processing")
                continue

            moon_filter_passed += 1
            moon_status = "passed"

        accepted_records.append(
            {
                "source_file": source_file,
                "canonical_filter": canonical_filter,
                "exptime": exptime,
                "raw_filter": raw_filter,
                "filename_lookup_hint": filename_lookup_hint,
                "filename_lookup_filter": filename_lookup_filter,
                "lookup_status": lookup_status,
                "moon_phase_text": moon_phase_text,
                "moon_alt_text": moon_alt_text,
                "moon_status": moon_status,
            }
        )
        update_progress(idx, len(records), prefix="Processing")

    collection_targets: dict[Path, tuple[Path, str]] = {}
    conversion_jobs: list[tuple[Path, Path]] = []
    conversion_failed_sources: dict[Path, str] = {}
    for accepted_record in accepted_records:
        source_file = accepted_record["source_file"]
        if not isinstance(source_file, Path):
            continue

        if source_file.suffix.lower() != ".xisf":
            collection_targets[source_file] = (source_file, "symlink_original")
            continue

        converted_file = converted_fits_path(input_dir, output_dir, source_file)
        if converted_fits_is_current(source_file, converted_file):
            collection_targets[source_file] = (converted_file, "xisf_cached")
            cached_xisf_count += 1
            continue

        conversion_jobs.append((source_file, converted_file))

    if conversion_jobs:
        if args.dry_run:
            print(
                f"[DRY-RUN] batch convert {len(conversion_jobs)} XISF file(s) "
                f"under {output_dir / '.converted_xisf'}"
            )
            for source_file, converted_file in conversion_jobs:
                collection_targets[source_file] = (converted_file, "xisf_planned")
        else:
            try:
                siril_path = find_siril_path(args.siril_path)
                print(f"\nSiril: {siril_path}")
                print(f"Converting {len(conversion_jobs)} XISF file(s) to FITS in one Siril batch...")
                convert_xisfs_to_fits(
                    conversion_jobs=conversion_jobs,
                    output_dir=output_dir,
                    siril_path=siril_path,
                    log_path=conversion_log_path,
                )
                for source_file, converted_file in conversion_jobs:
                    collection_targets[source_file] = (converted_file, "xisf_converted")
                converted_xisf_count += len(conversion_jobs)
            except Exception as exc:
                conversion_error_count += len(conversion_jobs)
                for source_file, _converted_file in conversion_jobs:
                    conversion_failed_sources[source_file] = type(exc).__name__
                print(
                    f"Warning: failed to batch convert {len(conversion_jobs)} XISF "
                    f"file(s) to FITS: {exc}",
                    file=sys.stderr,
                )

    for accepted_record in accepted_records:
        source_file = accepted_record["source_file"]
        canonical_filter = accepted_record["canonical_filter"]
        raw_filter = accepted_record["raw_filter"]
        exptime = accepted_record["exptime"]
        filename_lookup_hint = str(accepted_record["filename_lookup_hint"])
        filename_lookup_filter = str(accepted_record["filename_lookup_filter"])
        lookup_status = str(accepted_record["lookup_status"])
        moon_phase_text = str(accepted_record["moon_phase_text"])
        moon_alt_text = str(accepted_record["moon_alt_text"])
        moon_status = str(accepted_record["moon_status"])

        if not isinstance(source_file, Path) or not isinstance(canonical_filter, str):
            continue

        if source_file in conversion_failed_sources:
            converted_file = converted_fits_path(input_dir, output_dir, source_file)
            report_rows.append(
                filter_lookup_report_row(
                    source_file=source_file,
                    raw_filter=raw_filter if isinstance(raw_filter, str) else None,
                    filename_lookup_hint=filename_lookup_hint,
                    filename_lookup_filter=filename_lookup_filter,
                    final_filter="",
                    lookup_status="conversion_failed",
                    moon_phase_text=moon_phase_text,
                    moon_alt_text=moon_alt_text,
                    moon_status=moon_status,
                    collection_file=converted_file,
                    collection_status=f"conversion_failed:{conversion_failed_sources[source_file]}",
                )
            )
            continue

        collection_file, collection_status = collection_targets[source_file]
        filter_dir = output_dir / canonical_filter
        link_name = link_name_for_collection_target(input_dir, source_file, collection_file)
        link_path = make_unique_link_path(filter_dir, link_name, collection_file.resolve())

        if not args.dry_run:
            filter_dir.mkdir(parents=True, exist_ok=True)
            link_path.symlink_to(collection_file.resolve())

        report_rows.append(
            filter_lookup_report_row(
                source_file=source_file,
                raw_filter=raw_filter if isinstance(raw_filter, str) else None,
                filename_lookup_hint=filename_lookup_hint,
                filename_lookup_filter=filename_lookup_filter,
                final_filter=canonical_filter,
                lookup_status=lookup_status,
                moon_phase_text=moon_phase_text,
                moon_alt_text=moon_alt_text,
                moon_status=moon_status,
                collection_file=collection_file,
                collection_status=collection_status,
            )
        )

        relative_path = source_file.relative_to(input_dir)
        user_name = user_name_from_relative_path(relative_path)
        exptime_seconds = float(exptime) if exptime is not None else 0.0

        per_filter_count[canonical_filter] += 1
        if exptime is not None:
            per_filter_seconds[canonical_filter] += exptime_seconds
        per_user_filter_seconds[user_name][canonical_filter] += exptime_seconds
        linked_count += 1

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
    print("\nPer-user contributions:")
    filters = sorted(per_filter_count.keys())
    users = sorted(per_user_filter_seconds.keys())
    user_col_width = max(4, len("User"), max((len(user) for user in users), default=0))
    all_time_strings = [format_hm_compact(total_seconds_all)]
    for filter_name in filters:
        all_time_strings.append(format_hm_compact(float(per_filter_seconds[filter_name])))
    for user_name in users:
        user_total_seconds = 0.0
        for filter_name in filters:
            value = float(per_user_filter_seconds[user_name].get(filter_name, 0.0))
            all_time_strings.append(format_hm_compact(value))
            user_total_seconds += value
        all_time_strings.append(format_hm_compact(user_total_seconds))
    time_col_width = max(
        8,
        len("Total"),
        max((len(filter_name) for filter_name in filters), default=0),
        max((len(text) for text in all_time_strings), default=0),
    )
    contrib_header = " ".join(
        [f"{'User':<{user_col_width}}"]
        + [f"{filter_name:>{time_col_width}}" for filter_name in filters]
        + [f"{'Total':>{time_col_width}}"]
    )
    print(contrib_header)
    print("-" * len(contrib_header))
    for user_name in users:
        user_total_seconds = 0.0
        cells = [f"{user_name:<{user_col_width}}"]
        for filter_name in filters:
            value = float(per_user_filter_seconds[user_name].get(filter_name, 0.0))
            user_total_seconds += value
            cells.append(f"{format_hm_compact(value):>{time_col_width}}")
        cells.append(f"{format_hm_compact(user_total_seconds):>{time_col_width}}")
        print(" ".join(cells))
    total_cells = [f"{'TOTAL':<{user_col_width}}"]
    for filter_name in filters:
        total_cells.append(f"{format_hm_compact(float(per_filter_seconds[filter_name])):>{time_col_width}}")
    total_cells.append(f"{format_hm_compact(total_seconds_all):>{time_col_width}}")
    print("-" * len(contrib_header))
    print(" ".join(total_cells))

    print(f"{'duration_warnings':<22}{warning_count}")
    print(f"{'missing_filter_warnings':<22}{missing_filter_count}")
    print(f"{'filename_lookup_used':<22}{filename_lookup_used_count}")
    print(f"{'folder_marker_used':<22}{folder_marker_used_count}")
    print(f"{'folder_prompt_used':<22}{folder_prompt_used_count}")
    print(f"{'header_read_errors':<22}{error_count}")
    print(f"{'xisf_converted':<22}{converted_xisf_count}")
    print(f"{'xisf_conversion_cached':<22}{cached_xisf_count}")
    print(f"{'xisf_conversion_errors':<22}{conversion_error_count}")
    if moon_filter_enabled:
        print(f"{'moon_filtered_out':<22}{moon_filtered_out}")
        print(f"{'moon_metric_errors':<22}{moon_metric_errors}")
        print(f"{'moon_filter_passed':<22}{moon_filter_passed}")
        print(f"{'moon_unique_keys':<22}{len(unique_keys)}")
    report_relpath = Path("filter_lookup_report.json")
    if not args.dry_run:
        write_filter_lookup_report(output_dir, input_dir, report_rows)
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
