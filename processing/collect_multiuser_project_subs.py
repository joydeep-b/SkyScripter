#!/usr/bin/env python3

import argparse
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

from astropy.io import fits


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
        "--dry-run",
        action="store_true",
        help="Print planned cleanup/link actions without modifying files",
    )
    return parser.parse_args()


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


def get_filter_and_exptime(
    fits_file: Path,
) -> tuple[str | None, float | None, str | None]:
    with fits.open(fits_file) as hdul:
        header = hdul[0].header
        raw_filter = header["FILTER"].strip() if "FILTER" in header else None
        exptime = None
        if "EXPTIME" in header:
            try:
                exptime = float(header["EXPTIME"])
            except (TypeError, ValueError):
                exptime = None
    return canonical_filter_name(raw_filter), exptime, raw_filter


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

    per_filter_count = Counter()
    per_filter_seconds = Counter()
    warning_count = 0
    missing_filter_count = 0
    filename_lookup_used_count = 0
    error_count = 0
    linked_count = 0
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
            ]
        )
    ]

    total_files = len(all_files)
    print(f"Processing {total_files} files...")

    for idx, fits_file in enumerate(all_files, start=1):
        try:
            canonical_filter, exptime, raw_filter = get_filter_and_exptime(fits_file)
        except Exception as exc:
            error_count += 1
            print(f"Warning: failed to read FITS header from {fits_file}: {exc}", file=sys.stderr)
            update_progress(idx, total_files, prefix="Processing")
            continue

        filename_lookup_hint = ""
        filename_lookup_filter = ""
        lookup_status = "fits_header"
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
                        ]
                    )
                )
                print(
                    f"Warning: unable to determine filter for sub '{fits_file}'; skipping.",
                    file=sys.stderr,
                )
                update_progress(idx, total_files, prefix="Processing")
                continue

        if exptime is not None and exptime > args.max_sub_duration:
            warning_count += 1
            print(
                f"Warning: EXPTIME={exptime:.3f}s exceeds max-sub-duration "
                f"{args.max_sub_duration:.3f}s for {fits_file}",
                file=sys.stderr,
            )

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
                ]
            )
        )

        per_filter_count[canonical_filter] += 1
        if exptime is not None:
            per_filter_seconds[canonical_filter] += exptime
        linked_count += 1
        update_progress(idx, total_files, prefix="Processing")

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
