#!/usr/bin/env python3

"""
Analyze Ekos .analyze log files to summarize captured subs.

Features:
- Recursively scan a directory for Ekos Analyze logs (.analyze).
- Parse CaptureComplete entries, classify sub type (light/flat/dark/bias/darkflat/other).
- Summarize total hours per type by month and overall.
- Optional --max-months to limit how far back to include (in whole months, inclusive).
- Optional --csv-out to write a CSV of all light frames.
"""

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional


CAPTURE_PREFIX = "CaptureComplete,"
START_PREFIX = "AnalyzeStartTime,"


@dataclass
class Capture:
    timestamp: datetime
    exposure_s: float
    filter: str
    hfr: Optional[float]
    filepath: str
    kind: str  # light/flat/dark/bias/darkflat/other
    log_path: Path
    elapsed_s: float


def parse_start_time(line: str) -> Optional[datetime]:
    """
    Example: AnalyzeStartTime,2026-01-16 18:56:21.779,CST
    """
    parts = line.strip().split(",")
    if len(parts) < 2:
        return None
    ts = parts[1]
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def classify_filepath(path_str: str) -> str:
    """
    Infer frame type from path/name.
    Priority handles dark flats first to avoid matching "flat" substring.
    """
    lower = path_str.lower()
    if "darkflat" in lower or "dark-flat" in lower or "/darkflat/" in lower:
        return "darkflat"
    if "light" in lower:
        return "light"
    if "flat" in lower:
        return "flat"
    if "dark" in lower:
        return "dark"
    if "bias" in lower:
        return "bias"
    return "other"


def month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def months_ago(dt: datetime, now: datetime) -> int:
    return (now.year - dt.year) * 12 + (now.month - dt.month)


def parse_capture_line(line: str, start_ts: datetime, log_path: Path) -> Optional[Capture]:
    """
    CaptureComplete,<elapsed>,<exposure>,<filter>,<hfr>,<filepath>,...
    """
    parts = line.strip().split(",")
    if len(parts) < 6:
        return None
    try:
        elapsed = float(parts[1])
        exposure = float(parts[2])
        filt = parts[3]
    except ValueError:
        return None
    hfr = None
    try:
        hfr = float(parts[4])
    except ValueError:
        pass
    filepath = parts[5]
    ts = start_ts + timedelta(seconds=elapsed)
    kind = classify_filepath(filepath)
    return Capture(
        timestamp=ts,
        exposure_s=exposure,
        filter=filt,
        hfr=hfr,
        filepath=filepath,
        kind=kind,
        log_path=log_path,
        elapsed_s=elapsed,
    )


def iter_log_captures(log_path: Path) -> Iterable[Capture]:
    start_ts: Optional[datetime] = None
    capture_lines: List[str] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(START_PREFIX) and start_ts is None:
                    start_ts = parse_start_time(line)
                elif line.startswith(CAPTURE_PREFIX):
                    capture_lines.append(line)
    except OSError as exc:
        print(f"Warning: could not read {log_path}: {exc}", file=sys.stderr)
        return

    if start_ts is None:
        print(f"Warning: missing AnalyzeStartTime in {log_path}, skipping captures", file=sys.stderr)
        return

    for line in capture_lines:
        cap = parse_capture_line(line, start_ts, log_path)
        if cap:
            yield cap


def scan_logs(root: Path) -> List[Capture]:
    captures: List[Capture] = []
    for log_path in sorted(root.rglob("*.analyze")):
        if "/." in str(log_path):
            continue
        captures.extend(iter_log_captures(log_path))
    return captures


def summarize(captures: Iterable[Capture], max_months: Optional[int]) -> Dict[str, Dict[str, float]]:
    now = datetime.now()
    totals: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for cap in captures:
        if max_months is not None and months_ago(cap.timestamp, now) > max_months:
            continue
        mkey = month_key(cap.timestamp)
        totals[mkey][cap.kind] += cap.exposure_s
        totals["__total__"][cap.kind] += cap.exposure_s
    return totals


def print_summary(totals: Dict[str, Dict[str, float]]) -> None:
    """
    Print a horizontal summary: one line per month, one line for total.
    The total row is preceded by a delimiter line for clarity.
    """

    def fmt_hours(seconds: float) -> str:
        return f"{seconds / 3600.0:6.1f}"

    month_keys = sorted(k for k in totals.keys() if k != "__total__")
    kinds = sorted({kind for bucket in totals.values() for kind in bucket.keys()})

    # Header
    month_width = max(len("month"), max((len(k) for k in month_keys), default=0), len("total"))
    val_width = max(6, max((len(k) for k in kinds), default=0))

    def fmt_header(name: str) -> str:
        return f"{name:>{val_width}}"

    print(f"{'month':<{month_width}} " + " ".join(fmt_header(k) for k in kinds))

    # Per-month rows
    for mkey in month_keys:
        row_vals = [f"{totals[mkey].get(kind, 0.0) / 3600.0:>{val_width}.1f}" for kind in kinds]
        print(f"{mkey:<{month_width}} " + " ".join(row_vals))

    # Delimiter and total row
    if "__total__" in totals:
        total_line_len = month_width + 1 + len(kinds) * (val_width + 1) - 1
        print("-" * total_line_len)
        total_vals = [f"{totals['__total__'].get(kind, 0.0) / 3600.0:>{val_width}.1f}" for kind in kinds]
        print(f"{'total':<{month_width}} " + " ".join(total_vals))


def write_lights_csv(captures: Iterable[Capture], out_path: Path, max_months: Optional[int]) -> None:
    now = datetime.now()
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "capture_datetime",
                "elapsed_seconds",
                "exposure_seconds",
                "filter",
                "hfr",
                "filepath",
                "logfile",
            ]
        )
        for cap in captures:
            if cap.kind != "light":
                continue
            if max_months is not None and months_ago(cap.timestamp, now) > max_months:
                continue
            writer.writerow(
                [
                    cap.timestamp.isoformat(),
                    f"{cap.elapsed_s:.3f}",
                    f"{cap.exposure_s:.3f}",
                    cap.filter,
                    "" if cap.hfr is None else f"{cap.hfr:.3f}",
                    cap.filepath,
                    str(cap.log_path),
                ]
            )
    print(f"Wrote lights CSV to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Ekos Analyze logs.")
    parser.add_argument("directory", type=Path, help="Root directory to search for .analyze logs.")
    parser.add_argument(
        "--max-months",
        type=int,
        default=None,
        help="Only include captures within this many whole months ago (inclusive).",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Path to write CSV of light frames.",
    )
    args = parser.parse_args()

    if not args.directory.exists():
        print(f"Error: directory {args.directory} does not exist", file=sys.stderr)
        sys.exit(1)

    captures = scan_logs(args.directory)
    if not captures:
        print("No captures found.")
        return

    totals = summarize(captures, args.max_months)
    print_summary(totals)

    if args.csv_out:
        write_lights_csv(captures, args.csv_out, args.max_months)


if __name__ == "__main__":
    main()
