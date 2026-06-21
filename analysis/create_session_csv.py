#!/usr/bin/env python3

# This script takes a directory name, and checks all the fits files under the sub-directories. It
# then creates a csv file with the astrophotography session information based on the files found.

import csv
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astropy.io import fits
from astropy.time import Time
from tqdm import tqdm

default_values = {
    'bortle': '1',
    'darks': '100',
    'flats': '100',
    'bias': '100',
}

default_filter_lookup = {
    'L': 4452,
    'R': 4457,
    'G': 4447,
    'B': 4442,
    'H': 23761,
    'S': 23763,
    'O': 23762,
}
# This global can be overridden at runtime via CLI flags.
filter_lookup = default_filter_lookup.copy()

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
    "GAIN": ("GAIN", "CCD:GAIN"),
    "CCD-TEMP": ("CCD-TEMP", "CCD:TEMPERATURE"),
    "FOCUSTEM": ("FOCUSTEM", "FOCUS:TEMPERATURE", "TEMPERATURE:FOCUS"),
}


def is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


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


def normalize_metadata_key(key: str) -> str:
    return re.sub(r"[^A-Z0-9:]", "", str(key).strip().upper())


def lookup_header_value(header, key: str):
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


def get_header_value(header, key: str):
    value = lookup_header_value(header, key)
    if value is not None:
        return value

    for alias in XISF_PROPERTY_ALIASES.get(key.upper(), ()):
        value = lookup_header_value(header, alias)
        if value is not None:
            return value
    return None


def parse_date_obs(raw_date_obs: str | None) -> datetime | None:
    if raw_date_obs is None:
        return None

    date_obs_text = str(raw_date_obs).strip()
    if not date_obs_text:
        return None

    try:
        return Time(date_obs_text, format="fits", scale="utc").to_datetime()
    except (TypeError, ValueError):
        pass

    try:
        parsed_dt = datetime.fromisoformat(date_obs_text.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is not None:
            return parsed_dt.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed_dt
    except ValueError:
        pass

    if date_obs_text.endswith("Z"):
        date_obs_text = date_obs_text[:-1]

    for fmt in DATE_OBS_FORMATS:
        try:
            return datetime.strptime(date_obs_text, fmt)
        except ValueError:
            continue

    return None


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


class Session:
    def __init__(self, date, filter, duration, gain, sensorCooling, darks, flats, bias, bortle, temperature):
        self.date = date
        self.filter = filter_lookup[filter]
        self.number = 1
        self.duration = duration
        self.gain = gain
        self.sensorCooling = int(sensorCooling +0.5)
        self.darks = darks
        self.flats = flats
        self.bias = bias
        self.bortle = bortle
        self.temperature = temperature

    # Static method to display the header of the csv file.
    @staticmethod
    def header():
        return ['date', 'filter', 'number', 'duration', 'gain', 'sensorCooling', 'darks', 'flats', 'bias', 'bortle', 'temperature']

    def __str__(self):
        datestr = self.date.strftime('%Y-%m-%d')
        return f'{datestr},{self.filter},{self.number:04},{self.duration:06.1f},{self.gain},{self.sensorCooling:02},{self.darks},{self.flats},{self.bias},{self.bortle},{self.temperature:04.2f}'

    # Add an increment oparator to increment the number of images taken.
    def __iadd__(self, other):
        self.number += other
        return self

    # Comparison operators to sort the sessions by date.
    def __lt__(self, other):
        return self.date < other.date

    # Equality operator to compare the sessions.
    def __eq__(self, other):
        return self.date == other.date and \
          self.filter == other.filter and \
          self.duration == other.duration and \
          self.gain == other.gain

    def __iter__(self):
        return iter([self.date, self.filter, self.number, self.duration, self.gain, self.sensorCooling, self.darks, self.flats, self.bias, self.bortle, self.temperature])

def save_session_csv(sessions, output_file):
    for header in Session.header():
        print(header, end='')
        if header != Session.header()[-1]:
            print(',', end='')
        else:
            print()
    for session in sessions:
        print(session)
    return

def get_session_data(directory):
    sessions = []
    i = 0
    progress = ['|', '/', '-', '\\']
    
    # Recursively search for supported image files in all subdirectories.
    print("Recursively searching for FITS and XISF files...")
    all_files = discover_image_files(directory)
    print(f"Found {len(all_files)} FITS/XISF files")
    
    # Use TQDM to show a progress bar.
    for file in all_files:
        print(f'\r{progress[i % 4]}', end='')
        i += 1
        # print(f'Processing {file}')
        try:
            header = read_image_header(file)
            dt = parse_date_obs(get_header_value(header, "DATE-OBS"))
            if dt is None:
                raise ValueError("missing or unsupported DATE-OBS")

            date = dt.date()
            raw_filter = get_header_value(header, "FILTER")
            if raw_filter is not None:
                filter = str(raw_filter).strip()
            else:
                filter = 'Unknown'

            raw_duration = get_header_value(header, "EXPTIME")
            if raw_duration is None:
                raise KeyError("EXPTIME")
            duration = float(raw_duration)

            gain = get_header_value(header, "GAIN")
            if gain is None:
                raise KeyError("GAIN")

            raw_sensor_cooling = get_header_value(header, "CCD-TEMP")
            if raw_sensor_cooling is not None:
                sensorCooling = float(raw_sensor_cooling)
            else:
                sensorCooling = 0

            raw_temperature = get_header_value(header, "FOCUSTEM")
            if raw_temperature is not None:
                temperature = float(raw_temperature)
            else:
                temperature = -1
            
            # Handle case where filter might not be in lookup
            if filter not in filter_lookup:
                # Try matching just the first letter
                if filter and filter[0] in filter_lookup:
                    filter = filter[0]
                    # print(f"\nInfo: Using filter '{filter}' for {file}")
                else:
                    print(f"\nWarning: Unknown filter '{filter}' in {file}, using 'H' as default")
                    filter = 'H'
            session = Session(date, filter, duration, gain, sensorCooling,
                              default_values['darks'], default_values['flats'],
                              default_values['bias'], default_values['bortle'], temperature)
            if session in sessions:
                index = sessions.index(session)
                sessions[index] += 1
            else:
                sessions.append(session)
        except Exception as e:
            print(f"\nError processing {file}: {e}")
            # raise e
            continue
    
    print('\r', end='')
    sessions.sort()
    return sessions

def seconds_to_hms(seconds):
    hours = seconds // 3600
    seconds -= hours * 3600
    minutes = seconds // 60
    seconds -= minutes * 60
    return hours, minutes, seconds

def show_totals(sessions):
    totals = {}
    for session in sessions:
        if session.filter not in totals:
            totals[session.filter] = 0
        totals[session.filter] += int(session.number * session.duration)

    for filter, total in totals.items():
        # Look up the filter name.
        for key, value in filter_lookup.items():
            if value == filter:
                filter = key
                break
        # Get total in hours, minutes, and seconds.
        # duration = timedelta(seconds=total)
        h, m, s = seconds_to_hms(total)
        print(f'{filter:5}: {total:7} seconds ({h:3}:{m:02}:{s:02})')

    h, m, s = seconds_to_hms(sum(totals.values()))
    print(f'Total: {sum(totals.values()):7} seconds ({h:3}:{m:02}:{s:02})')

def main():
    # Parse the command line arguments.
    import argparse
    parser = argparse.ArgumentParser(description='Create a csv file with astrophotography session information.')
    parser.add_argument('dir', type=str, help='Directory containing the fits files.')
    parser.add_argument('-out', type=str, help='Output csv file.')
    # Allow per-filter ID overrides (e.g. -H 9999).
    for filter_name in default_filter_lookup.keys():
        parser.add_argument(f'-{filter_name}', dest=filter_name, type=int,
                            help=f'Override filter ID for {filter_name} filter.')
    args = parser.parse_args()

    # Apply filter overrides if provided.
    global filter_lookup
    filter_lookup = default_filter_lookup.copy()
    for name, default_id in default_filter_lookup.items():
        override = getattr(args, name)
        if override is not None:
            filter_lookup[name] = override
    print(f'Using filter lookup override: {filter_lookup}')

    print(f'Creating csv file with session data from {args.dir}.')
    # Get the session data.
    directory = Path(args.dir)
    sessions = get_session_data(directory)
    save_session_csv(sessions, args.out)
    show_totals(sessions)

if __name__ == '__main__':
    main()