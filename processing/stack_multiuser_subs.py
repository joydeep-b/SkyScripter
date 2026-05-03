#!/usr/bin/env python3

import argparse
import csv
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_REPORT = Path("/scratch/joydeepb/astro_temp/M_40/.moonless_subs/filter_lookup_report.tsv")
VALID_FILTERS = {"L", "R", "G", "B", "H", "S", "O"}
FILTER_ORDER = {"L": 0, "R": 1, "G": 2, "B": 3, "H": 4, "S": 5, "O": 6}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create per-user LRGB master stacks using Siril from a filter lookup report."
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to filter_lookup_report.tsv",
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


def extract_user_name(source_file: Path) -> str:
    parts = source_file.parts
    if ".shared_data" in parts:
        idx = parts.index(".shared_data")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    # Fallback for paths that do not match expected layout.
    return source_file.parent.name or "unknown"


def parse_report(report_path: Path) -> tuple[dict[tuple[str, str], list[Path]], dict[str, int]]:
    stats = {
        "total_rows": 0,
        "eligible_rows": 0,
        "skipped_missing_source": 0,
    }
    groups: dict[tuple[str, str], list[Path]] = {}

    with report_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"source_file", "final_filter", "moon_status"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required column(s) in report: {sorted(missing)}")

        for row in reader:
            stats["total_rows"] += 1
            # moon_status = (row.get("moon_status") or "").strip().lower()
            final_filter = (row.get("final_filter") or "").strip().upper()
            source_file_text = (row.get("source_file") or "").strip()

            # if moon_status != "passed":
            #     continue
            if final_filter not in VALID_FILTERS:
                continue
            if not source_file_text:
                continue

            source_file = Path(source_file_text)
            if not source_file.exists():
                stats["skipped_missing_source"] += 1
                continue

            user_name = extract_user_name(source_file)
            key = (user_name, final_filter)
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


def find_stack_output(working_dir: Path, basename: str) -> Path:
    for ext in (".fit", ".fits"):
        candidate = working_dir / f"{basename}{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Expected Siril stack output not found in {working_dir}: {basename}.fit/.fits"
    )


def build_siril_script(stack_basename: str, working_dir: Path) -> str:
    return (
        "requires 1.3.5\n"
        "setcpu 192\n"
        "setmem 1\n"
        f"cd {working_dir}\n"
        "load pp_light_00001.fit\n"
        "dumpheader\n"
        "seqsubsky pp_light 2\n"
        "setfindstar reset\n"
        "register bkg_pp_light\n"
        "seqapplyreg bkg_pp_light -drizzle -scale=1.0 -pixfrac=0.9 -framing=max\n"
        "stack r_bkg_pp_light rej 3 3 -norm=addscale -output_norm "
        "-weight=wfwhm -filter-wfwhm=90% "
        f"-out={stack_basename}\n"
    )


def prepare_symlinks(working_dir: Path, source_files: list[Path]) -> None:
    if working_dir.exists():
        shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    for idx, source in enumerate(sorted(source_files), start=1):
        link_path = working_dir / f"pp_light_{idx:05d}.fit"
        link_path.symlink_to(source)


def stack_group(
    *,
    user_name: str,
    filter_name: str,
    files: list[Path],
    output_root: Path,
    siril_path: str,
    log_path: Path,
    dry_run: bool,
) -> Path:
    user_safe = safe_name(user_name)
    work_dir = output_root / "work" / user_safe / filter_name
    masters_dir = output_root / "masters" / user_safe
    destination = masters_dir / f"master_{filter_name}.fit"
    stack_basename = f"master_{filter_name}"

    if dry_run:
        print(
            f"[DRY-RUN] {user_name} / {filter_name}: "
            f"{len(files)} frame(s) -> {destination}"
        )
        return destination

    prepare_symlinks(work_dir, files)
    masters_dir.mkdir(parents=True, exist_ok=True)

    context = f"user={user_name} filter={filter_name}"
    script_text = build_siril_script(stack_basename, work_dir)
    run_siril_script(siril_path, work_dir, script_text, log_path, context)
    produced = find_stack_output(work_dir, stack_basename)

    if destination.exists():
        destination.unlink()
    shutil.move(str(produced), str(destination))
    return destination


def sort_group_key(item: tuple[str, str]) -> tuple[str, int]:
    user_name, filter_name = item
    return user_name.lower(), FILTER_ORDER.get(filter_name, 99)


def destination_for_group(output_root: Path, user_name: str, filter_name: str) -> Path:
    user_safe = safe_name(user_name)
    return output_root / "masters" / user_safe / f"master_{filter_name}.fit"


def rebuild_all_users_index(output_root: Path, dry_run: bool) -> int:
    all_users_root = output_root / "all_users"
    masters_root = output_root / "masters"
    link_count = 0

    filter_names = list(VALID_FILTERS)  
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

        for user_dir in sorted((p for p in masters_root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
            master_name = f"master_{filter_name}.fit"
            master_path = user_dir / master_name
            if not master_path.exists():
                continue

            link_name = f"{user_dir.name}_{master_name}"
            link_path = filter_dir / link_name
            if dry_run:
                print(f"[DRY-RUN] link {link_path} -> {master_path}")
            else:
                link_path.symlink_to(master_path.resolve())
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

    groups, stats = parse_report(report_path)
    sorted_keys = sorted(groups.keys(), key=sort_group_key)

    print(f"Report: {report_path}")
    print(f"Output root: {output_root}")
    print(f"Rows scanned: {stats['total_rows']}")
    print(f"Eligible rows (passed + L/R/G/B + existing files): {stats['eligible_rows']}")
    print(f"Groups discovered: {len(sorted_keys)}")

    siril_path = None
    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
        siril_path = find_siril_path(args.siril_path)
        print(f"Siril: {siril_path}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = output_root / f"stack_multiuser_subs_{timestamp}.log"
    if not args.dry_run:
        log_path.touch()
        print(f"Siril log: {log_path}")

    skipped_small_groups = 0
    skipped_existing = 0
    successful_stacks = 0
    all_users_links_created = 0
    failure_context = None

    for user_name, filter_name in sorted_keys:
        files = groups[(user_name, filter_name)]
        if len(files) < args.min_frames:
            skipped_small_groups += 1
            print(
                f"Skipping {user_name} / {filter_name}: "
                f"only {len(files)} frame(s), min={args.min_frames}"
            )
            continue

        destination = destination_for_group(output_root, user_name, filter_name)
        if destination.exists():
            skipped_existing += 1
            print(f"Skipping {user_name:30s} / {filter_name}: master exists at {destination}")
            continue

        print(f"Stacking {user_name:30s} / {filter_name} with {len(files):4d} frame(s)...")
        try:
            destination = stack_group(
                user_name=user_name,
                filter_name=filter_name,
                files=files,
                output_root=output_root,
                siril_path=siril_path or "",
                log_path=log_path,
                dry_run=args.dry_run,
            )
            successful_stacks += 1
            if not args.dry_run:
                print(f"  -> wrote {destination}")
        except Exception as exc:
            failure_context = f"{user_name} / {filter_name}: {exc}"
            print(f"ERROR: {failure_context}", file=sys.stderr)
            break

    if failure_context is None:
        all_users_links_created = rebuild_all_users_index(output_root, args.dry_run)

    print("\nSummary:")
    print(f"  Groups discovered: {len(sorted_keys)}")
    print(f"  Skipped (< min-frames): {skipped_small_groups}")
    print(f"  Skipped (master exists): {skipped_existing}")
    print(f"  Missing-source rows skipped: {stats['skipped_missing_source']}")
    print(f"  Successful stacks: {successful_stacks}")
    print(f"  all_users links created: {all_users_links_created}")
    if failure_context:
        print(f"  First failure: {failure_context}")
        sys.exit(1)
    print("  First failure: none")


if __name__ == "__main__":
    main()
