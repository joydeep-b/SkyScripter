#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_REPORT = Path("/scratch/joydeepb/astro_temp/M_40/.moonless_subs/filter_lookup_report.json")
DEFAULT_SCRIPT_DIR = Path(__file__).resolve().parent / "subprocess_scripts"
DEFAULT_STACK_SCRIPT = DEFAULT_SCRIPT_DIR / "stack.ssf"
DEFAULT_BKG_STACK_SCRIPT = DEFAULT_SCRIPT_DIR / "bkg_stack.ssf"
VALID_FILTERS = {"L", "R", "G", "B", "H", "S", "O"}
FILTER_ORDER = {"L": 0, "R": 1, "G": 2, "B": 3, "H": 4, "S": 5, "O": 6}
FILTER_FOLDER_ALIASES = {
    "L": "L",
    "LUM": "L",
    "LUMINANCE": "L",
    "R": "R",
    "RED": "R",
    "G": "G",
    "GREEN": "G",
    "B": "B",
    "BLUE": "B",
    "H": "H",
    "HA": "H",
    "HALPHA": "H",
    "HYDROGENALPHA": "H",
    "S": "S",
    "SII": "S",
    "S2": "S",
    "SULFURII": "S",
    "SULPHURII": "S",
    "O": "O",
    "OIII": "O",
    "O3": "O",
    "OXYGENIII": "O",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create per-user LRGB master stacks using Siril from a filter lookup report."
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to filter_lookup_report.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Output root directory. Defaults to <report_dir>/per_user_stacks",
    )
    parser.add_argument(
        "--siril-path",
        type=Path,
        default=None,
        help="Optional explicit path to Siril executable.",
    )
    parser.add_argument(
        "--min-frames",
        type=int,
        default=3,
        help="Minimum number of subs required for a user/filter stack.",
    )
    parser.add_argument(
        "--bkg",
        action="store_true",
        help="Use the background-subtracted Siril stacking script.",
    )
    parser.add_argument(
        "--stack-script",
        type=Path,
        default=DEFAULT_STACK_SCRIPT,
        help="Path to the non-background-subtracted Siril stack script.",
    )
    parser.add_argument(
        "--bkg-stack-script",
        type=Path,
        default=DEFAULT_BKG_STACK_SCRIPT,
        help="Path to the background-subtracted Siril stack script.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without running Siril.",
    )
    return parser.parse_args()


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


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value or "unknown"


def canonical_filter_folder(value: str) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9]+", "", value).upper()
    return FILTER_FOLDER_ALIASES.get(normalized)


def extract_user_and_equipment(
    source_file: Path,
    input_dir: Path,
    filter_name: str,
) -> tuple[str, str]:
    try:
        relative_path = source_file.relative_to(input_dir)
    except ValueError as exc:
        raise ValueError(f"source file is not under input_dir: {source_file}") from exc

    if not relative_path.parts:
        raise ValueError(f"source file has no path relative to input_dir: {source_file}")
    user_name = relative_path.parts[0] or "unknown"

    if len(relative_path.parts) <= 2:
        return user_name, "default"

    top_folder = relative_path.parts[1]
    if len(relative_path.parts) == 3 and canonical_filter_folder(top_folder) == filter_name:
        return user_name, "default"

    return user_name, top_folder


def parse_report(report_path: Path) -> tuple[dict[tuple[str, str, str], list[Path]], dict[str, int]]:
    stats = {
        "total_rows": 0,
        "eligible_rows": 0,
        "skipped_missing_source": 0,
        "skipped_outside_input_dir": 0,
    }
    groups: dict[tuple[str, str, str], list[Path]] = {}

    with report_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)

    if not isinstance(report, dict):
        raise ValueError("Report must be a JSON object.")

    input_dir_text = str(report.get("input_dir") or "").strip()
    if not input_dir_text:
        raise ValueError("Report is missing required field: input_dir")
    input_dir = Path(input_dir_text).expanduser().resolve()

    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Report is missing required list field: rows")

    for row in rows:
        stats["total_rows"] += 1
        if not isinstance(row, dict):
            continue

        final_filter = str(row.get("final_filter") or "").strip().upper()
        source_file_text = str(row.get("source_file") or "").strip()

        if final_filter not in VALID_FILTERS:
            continue
        if not source_file_text:
            continue

        source_file = Path(source_file_text).expanduser()
        if not source_file.exists():
            stats["skipped_missing_source"] += 1
            continue

        try:
            user_name, equipment_name = extract_user_and_equipment(
                source_file,
                input_dir,
                final_filter,
            )
        except ValueError:
            stats["skipped_outside_input_dir"] += 1
            continue
        key = (user_name, equipment_name, final_filter)
        groups.setdefault(key, []).append(source_file)
        stats["eligible_rows"] += 1

    return groups, stats


def write_log(
    log_path: Path,
    context: str,
    command: list[str],
    script_text: str,
    result: subprocess.CompletedProcess[str],
) -> None:
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


def write_failure_log(log_path: Path, context: str, exc: Exception) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("=" * 80 + "\n")
        handle.write(f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n")
        handle.write(f"context: {context}\n")
        handle.write("failure:\n")
        handle.write(f"{type(exc).__name__}: {exc}\n")


def summarize_exception(exc: Exception) -> str:
    for line in str(exc).splitlines():
        line = line.strip()
        if line:
            return line
    return type(exc).__name__


def run_siril_script(
    siril_path: str,
    working_dir: Path,
    script_text: str,
    log_path: Path,
    context: str,
) -> None:
    command = [siril_path, "-d", str(working_dir), "-s", "-"]
    result = subprocess.run(command, input=script_text, text=True, capture_output=True)
    write_log(log_path, context, command, script_text, result)
    if result.returncode != 0:
        raise RuntimeError(
            f"Siril failed for {context} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def find_stack_output(working_dir: Path) -> Path:
    process_dir = working_dir / ".process"
    for ext in (".fit", ".fits"):
        candidate = process_dir / f"stack{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Expected Siril stack output not found in {process_dir}: stack.fit/.fits"
    )


def prepare_work_files(
    working_dir: Path,
    source_files: list[Path],
) -> None:
    if working_dir.exists():
        shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    for idx, source in enumerate(sorted(source_files), start=1):
        link_path = working_dir / f"pp_light_{idx:05d}{source.suffix.lower()}"
        link_path.symlink_to(source.resolve())


def stack_group(
    *,
    user_name: str,
    equipment_name: str,
    filter_name: str,
    files: list[Path],
    output_root: Path,
    siril_path: str,
    script_text: str,
    log_path: Path,
    dry_run: bool,
) -> Path:
    user_safe = safe_name(user_name)
    equipment_safe = safe_name(equipment_name)
    work_dir = output_root / "work" / user_safe / equipment_safe / filter_name
    masters_dir = output_root / "masters" / user_safe / equipment_safe
    destination = masters_dir / f"master_{filter_name}.fit"

    if dry_run:
        print(
            f"[DRY-RUN] {user_name} / {equipment_name} / {filter_name}: "
            f"{len(files)} frame(s) -> {destination}"
        )
        return destination

    masters_dir.mkdir(parents=True, exist_ok=True)

    context = f"user={user_name} equipment={equipment_name} filter={filter_name}"
    prepare_work_files(work_dir, files)
    run_siril_script(siril_path, work_dir, script_text, log_path, context)
    produced = find_stack_output(work_dir)

    if destination.exists():
        destination.unlink()
    shutil.move(str(produced), str(destination))
    return destination


def sort_group_key(item: tuple[str, str, str]) -> tuple[str, str, int]:
    user_name, equipment_name, filter_name = item
    return user_name.lower(), equipment_name.lower(), FILTER_ORDER.get(filter_name, 99)


def destination_for_group(
    output_root: Path,
    user_name: str,
    equipment_name: str,
    filter_name: str,
) -> Path:
    user_safe = safe_name(user_name)
    equipment_safe = safe_name(equipment_name)
    return output_root / "masters" / user_safe / equipment_safe / f"master_{filter_name}.fit"


def failure_log_path(
    output_root: Path,
    user_name: str,
    equipment_name: str,
    filter_name: str,
    timestamp: str,
) -> Path:
    user_safe = safe_name(user_name)
    equipment_safe = safe_name(equipment_name)
    return (
        output_root
        / "masters"
        / user_safe
        / equipment_safe
        / f"stack_failure_{user_safe}_{equipment_safe}_{filter_name}_{timestamp}.log"
    )


def rebuild_all_users_index(output_root: Path, dry_run: bool) -> int:
    all_users_root = output_root / "all_users"
    masters_root = output_root / "masters"
    link_count = 0

    filter_names = sorted(VALID_FILTERS, key=lambda filter_name: FILTER_ORDER.get(filter_name, 99))
    for filter_name in filter_names:
        filter_dir = all_users_root / filter_name
        if dry_run:
            print(f"[DRY-RUN] ensure directory: {filter_dir}")
        else:
            filter_dir.mkdir(parents=True, exist_ok=True)

        if filter_dir.exists():
            for existing in filter_dir.iterdir():
                if existing.is_symlink():
                    if dry_run:
                        print(f"[DRY-RUN] remove stale link: {existing}")
                    else:
                        existing.unlink()

        if not masters_root.exists():
            continue

        user_dirs = sorted((p for p in masters_root.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
        for user_dir in user_dirs:
            equipment_dirs = sorted((p for p in user_dir.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
            for equipment_dir in equipment_dirs:
                master_name = f"master_{filter_name}.fit"
                master_path = equipment_dir / master_name
                if not master_path.exists():
                    continue

                link_name = f"{user_dir.name}__{equipment_dir.name}__{master_name}"
                link_path = filter_dir / link_name
                link_target = os.path.relpath(master_path, link_path.parent)
                if dry_run:
                    print(f"[DRY-RUN] link {link_path} -> {link_target}")
                else:
                    link_path.symlink_to(link_target)
                link_count += 1

    return link_count


def main() -> None:
    args = parse_args()
    report_path = args.report.expanduser().resolve()

    if not report_path.exists():
        print(f"Error: report file does not exist: {report_path}", file=sys.stderr)
        sys.exit(1)

    if args.min_frames < 1:
        print("Error: --min-frames must be at least 1", file=sys.stderr)
        sys.exit(1)

    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root is not None
        else (report_path.parent / "per_user_stacks").resolve()
    )
    stack_script_path = (
        args.bkg_stack_script if args.bkg else args.stack_script
    ).expanduser().resolve()
    if not stack_script_path.exists():
        print(f"Error: stack script does not exist: {stack_script_path}", file=sys.stderr)
        sys.exit(1)
    script_text = stack_script_path.read_text(encoding="utf-8")

    groups, stats = parse_report(report_path)
    sorted_keys = sorted(groups.keys(), key=sort_group_key)

    print(f"Report: {report_path}")
    print(f"Output root: {output_root}")
    print(f"Stack script: {stack_script_path}")
    print(f"Rows scanned: {stats['total_rows']}")
    print(f"Eligible rows (valid filter + existing file under input_dir): {stats['eligible_rows']}")
    print(f"Groups discovered: {len(sorted_keys)}")

    siril_path = None
    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
        siril_path = find_siril_path(args.siril_path)
        print(f"Siril: {siril_path}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    skipped_small_groups = 0
    skipped_existing = 0
    successful_stacks = 0
    all_users_links_created = 0
    failed_jobs: list[dict[str, str]] = []

    for user_name, equipment_name, filter_name in sorted_keys:
        files = groups[(user_name, equipment_name, filter_name)]
        if len(files) < args.min_frames:
            skipped_small_groups += 1
            print(
                f"Skipping {user_name} / {equipment_name} / {filter_name}: "
                f"only {len(files)} frame(s), min={args.min_frames}"
            )
            continue

        destination = destination_for_group(
            output_root,
            user_name,
            equipment_name,
            filter_name,
        )
        if destination.exists():
            skipped_existing += 1
            print(
                f"Skipping {user_name:30s} / {equipment_name:30s} / {filter_name}: "
                f"master exists at {destination}"
            )
            continue

        print(
            f"Stacking {user_name:30s} / {equipment_name:30s} / {filter_name} "
            f"with {len(files):4d} frame(s)..."
        )
        job_log_path = failure_log_path(
            output_root,
            user_name,
            equipment_name,
            filter_name,
            timestamp,
        )
        try:
            destination = stack_group(
                user_name=user_name,
                equipment_name=equipment_name,
                filter_name=filter_name,
                files=files,
                output_root=output_root,
                siril_path=siril_path or "",
                script_text=script_text,
                log_path=job_log_path,
                dry_run=args.dry_run,
            )
            successful_stacks += 1
            if not args.dry_run:
                if job_log_path.exists():
                    job_log_path.unlink()
                print(f"  -> wrote {destination}")
        except Exception as exc:
            if not args.dry_run:
                write_failure_log(
                    job_log_path,
                    f"user={user_name} equipment={equipment_name} filter={filter_name}",
                    exc,
                )
            failed_jobs.append(
                {
                    "user": user_name,
                    "equipment": equipment_name,
                    "filter": filter_name,
                    "error": summarize_exception(exc),
                    "log_path": str(job_log_path),
                }
            )
            print(
                f"ERROR: {user_name} / {equipment_name} / {filter_name} failed; "
                f"log: {job_log_path}",
                file=sys.stderr,
            )

    all_users_links_created = rebuild_all_users_index(output_root, args.dry_run)

    print("\nSummary:")
    print(f"  Groups discovered: {len(sorted_keys)}")
    print(f"  Skipped (< min-frames): {skipped_small_groups}")
    print(f"  Skipped (master exists): {skipped_existing}")
    print(f"  Missing-source rows skipped: {stats['skipped_missing_source']}")
    print(f"  Outside-input_dir rows skipped: {stats['skipped_outside_input_dir']}")
    print(f"  Successful stacks: {successful_stacks}")
    print(f"  all_users links created: {all_users_links_created}")
    print(f"  Failed stacks: {len(failed_jobs)}")
    if failed_jobs:
        print("  Failed jobs:")
        for failed_job in failed_jobs:
            print(
                f"    {failed_job['user']} / {failed_job['equipment']} / "
                f"{failed_job['filter']}: "
                f"{failed_job['error']} (log: {failed_job['log_path']})"
            )
        sys.exit(1)
    print("  Failed jobs: none")


if __name__ == "__main__":
    main()
