#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
from pathlib import Path
import re
from typing import Any

from astropy.io import fits


DEFAULT_REPORT = Path("/scratch/joydeepb/astro_temp/markarians/moony_subs/filter_lookup_report.json")
DEFAULT_MEASUREMENTS = Path(
    "/scratch/joydeepb/astro_temp/markarians/moony_subs/quality_report/measurements.csv"
)
DEFAULT_OUTPUT_ROOT = Path("/scratch/joydeepb/astro_temp/markarians/markarians_LNC_test")

METRIC_FIELDS = ("score", "star_count", "median_mean_star_flux", "background", "bgnoise")
MEASUREMENT_FIELDS = (
    "user",
    "equipment",
    "filter",
    "sub_path",
    "score",
    "star_count",
    "median_mean_star_flux",
    "background",
    "bgnoise",
)

SAMPLE_GROUPS = (
    ("Andy Weeks", "default", "L"),
    ("Andy Weeks", "default", "H"),
    ("Russell Schlapper", "default", "L"),
    ("Russell Schlapper", "default", "H"),
    ("bray", "Ha RASA1", "H"),
    ("bray", "RASA G", "G"),
    ("Max Kuster", "Esprit", "L"),
    ("Max Kuster", "Redcat", "L"),
    ("SteveG ibagman0414", "default", "O"),
    ("joydeepb", "calibrated_subs", "H"),
    ("jdh_astro", "default", "L"),
)

ROLE_PRIORITY = {
    "reference_rank_1": 0,
    "reference_rank_2": 1,
    "reference_rank_3": 2,
    "score_median": 3,
    "score_p10": 4,
    "score_p25": 5,
    "score_p75": 6,
    "max_bgnoise": 7,
    "max_background": 8,
    "min_star_count": 9,
    "max_star_count": 10,
}
SMOKE_ROLE_PREFIXES = (
    "reference_rank_1",
    "reference_rank_2",
    "score_median",
    "max_bgnoise",
    "max_background",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a portable copied Markarians LNC test dataset from the large quality report."
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Source filter_lookup_report.json.")
    parser.add_argument(
        "--measurements",
        type=Path,
        default=DEFAULT_MEASUREMENTS,
        help="Source quality_report/measurements.csv.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Output dataset root.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned sample without copying or writing files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate --output-root if it already exists.",
    )
    return parser.parse_args()


def safe_name(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value or "unknown"


def group_dir_name(user: str, equipment: str, filter_name: str) -> str:
    return f"{safe_name(user)}__{safe_name(equipment)}__{safe_name(filter_name)}"


def finite_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def metric_sort_value(row: dict[str, Any], metric_name: str) -> float:
    value = finite_float(row.get(metric_name))
    return value if value is not None else -math.inf


def load_report(report_path: Path) -> dict[str, Any]:
    with report_path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    if not isinstance(report, dict):
        raise ValueError(f"Report must be a JSON object: {report_path}")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Report is missing a rows list: {report_path}")
    return report


def load_measurements(measurements_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with measurements_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            parsed: dict[str, Any] = dict(row)
            for field in METRIC_FIELDS:
                parsed[field] = finite_float(row.get(field))
            rows.append(parsed)
    return rows


def add_selection(selections: dict[str, dict[str, Any]], row: dict[str, Any] | None, role: str) -> None:
    if row is None:
        return
    key = str(row["sub_path"])
    existing = selections.get(key)
    if existing is None:
        selections[key] = {"measurement": row, "roles": [role]}
    elif role not in existing["roles"]:
        existing["roles"].append(role)


def quantile_row(rows: list[dict[str, Any]], metric_name: str, quantile: float) -> dict[str, Any] | None:
    finite_rows = [row for row in rows if finite_float(row.get(metric_name)) is not None]
    finite_rows.sort(key=lambda row: float(row[metric_name]))
    if not finite_rows:
        return None
    index = round(quantile * (len(finite_rows) - 1))
    return finite_rows[index]


def select_rows_for_group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selections: dict[str, dict[str, Any]] = {}
    score_rows = [row for row in rows if finite_float(row.get("score")) is not None]
    score_rows.sort(key=lambda row: (float(row["score"]), str(row["sub_path"])), reverse=True)
    for rank, row in enumerate(score_rows[:3], start=1):
        add_selection(selections, row, f"reference_rank_{rank}")

    add_selection(selections, quantile_row(rows, "score", 0.50), "score_median")
    add_selection(selections, quantile_row(rows, "score", 0.10), "score_p10")
    add_selection(selections, quantile_row(rows, "score", 0.25), "score_p25")
    add_selection(selections, quantile_row(rows, "score", 0.75), "score_p75")

    for metric_name, role in (
        ("bgnoise", "max_bgnoise"),
        ("background", "max_background"),
        ("star_count", "min_star_count"),
        ("star_count", "max_star_count"),
    ):
        candidates = [row for row in rows if finite_float(row.get(metric_name)) is not None]
        if not candidates:
            continue
        if role.startswith("min_"):
            selected = min(candidates, key=lambda row: (float(row[metric_name]), str(row["sub_path"])))
        else:
            selected = max(candidates, key=lambda row: (float(row[metric_name]), str(row["sub_path"])))
        add_selection(selections, selected, role)

    def selected_sort_key(item: dict[str, Any]) -> tuple[int, str]:
        primary_role = primary_selection_role(item["roles"])
        return ROLE_PRIORITY.get(primary_role, 99), str(item["measurement"]["sub_path"])

    return sorted(selections.values(), key=selected_sort_key)


def primary_selection_role(roles: list[str]) -> str:
    return min(roles, key=lambda role: ROLE_PRIORITY.get(role, 99))


def read_image_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "image_width": None,
        "image_height": None,
        "bitpix": None,
        "fits_filter": None,
        "instrument": None,
        "exposure": None,
    }
    try:
        with fits.open(path, memmap=True) as hdul:
            header = hdul[0].header
            info.update(
                {
                    "image_width": header.get("NAXIS1"),
                    "image_height": header.get("NAXIS2"),
                    "bitpix": header.get("BITPIX"),
                    "fits_filter": header.get("FILTER"),
                    "instrument": header.get("INSTRUME"),
                    "exposure": header.get("EXPTIME") or header.get("EXPOSURE"),
                }
            )
    except Exception as exc:
        info["image_error"] = f"{type(exc).__name__}: {exc}"
    return info


def unique_destination_path(group_dir: Path, base_name: str, used_paths: set[Path]) -> Path:
    destination = group_dir / base_name
    if destination not in used_paths and not destination.exists():
        used_paths.add(destination)
        return destination
    stem = destination.stem
    suffix = destination.suffix
    index = 2
    while True:
        candidate = group_dir / f"{stem}_{index}{suffix}"
        if candidate not in used_paths and not candidate.exists():
            used_paths.add(candidate)
            return candidate
        index += 1


def source_to_copy(report_row: dict[str, Any], original_source: Path) -> Path:
    collection_file = str(report_row.get("collection_file") or "").strip()
    if collection_file:
        collection_path = Path(collection_file).expanduser()
        if collection_path.exists():
            return collection_path
    return original_source


def build_selected_records(
    *,
    report: dict[str, Any],
    measurements: list[dict[str, Any]],
    output_root: Path,
) -> list[dict[str, Any]]:
    report_rows = [row for row in report["rows"] if isinstance(row, dict)]
    report_by_source = {str(Path(str(row.get("source_file", ""))).expanduser()): row for row in report_rows}

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in measurements:
        key = (str(row["user"]), str(row["equipment"]), str(row["filter"]))
        groups.setdefault(key, []).append(row)

    records: list[dict[str, Any]] = []
    used_destinations: set[Path] = set()
    subs_dir = output_root / "subs"
    for user_name, equipment_name, filter_name in SAMPLE_GROUPS:
        key = (user_name, equipment_name, filter_name)
        group_rows = groups.get(key)
        if not group_rows:
            raise ValueError(f"No measurement rows found for requested group: {key}")

        selected_rows = select_rows_for_group(group_rows)
        group_dirname = group_dir_name(user_name, equipment_name, filter_name)
        group_dir = subs_dir / group_dirname
        for index, selected in enumerate(selected_rows, start=1):
            measurement = selected["measurement"]
            roles = selected["roles"]
            primary_role = primary_selection_role(roles)
            original_source = Path(str(measurement["sub_path"])).expanduser()
            report_row = report_by_source.get(str(original_source))
            if report_row is None:
                raise ValueError(f"Could not find report row for measurement path: {original_source}")

            copy_source = source_to_copy(report_row, original_source)
            if not copy_source.exists():
                raise FileNotFoundError(f"Selected source does not exist: {copy_source}")

            destination_name = (
                f"{index:03d}_{safe_name(primary_role)}_{safe_name(original_source.stem)}"
                f"{copy_source.suffix.lower()}"
            )
            destination = unique_destination_path(group_dir, destination_name, used_destinations)
            image_info = read_image_info(copy_source)
            records.append(
                {
                    "user": user_name,
                    "equipment": equipment_name,
                    "filter": filter_name,
                    "group_dir": group_dirname,
                    "roles": roles,
                    "primary_role": primary_role,
                    "measurement": measurement,
                    "report_row": report_row,
                    "original_source_file": str(original_source),
                    "original_collection_file": str(report_row.get("collection_file") or ""),
                    "original_collection_status": str(report_row.get("collection_status") or ""),
                    "copy_source_file": str(copy_source),
                    "copied_file": str(destination),
                    "relative_copied_file": str(destination.relative_to(output_root)),
                    "source_suffix": original_source.suffix.lower(),
                    "copied_suffix": copy_source.suffix.lower(),
                    **image_info,
                }
            )
    return records


def record_sort_key(record: dict[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        str(record["user"]).lower(),
        str(record["equipment"]).lower(),
        str(record["filter"]),
        ROLE_PRIORITY.get(str(record["primary_role"]), 99),
        str(record["copied_file"]),
    )


def smoke_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_group.setdefault(str(record["group_dir"]), []).append(record)

    output: list[dict[str, Any]] = []
    for group_records in by_group.values():
        selected: list[dict[str, Any]] = []
        for role in SMOKE_ROLE_PREFIXES:
            for record in group_records:
                if role in record["roles"] and record not in selected:
                    selected.append(record)
                    break
        for record in group_records:
            if len(selected) >= 4:
                break
            if record not in selected:
                selected.append(record)
        output.extend(selected)
    return sorted(output, key=record_sort_key)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "group_dir",
        "user",
        "equipment",
        "filter",
        "roles",
        "primary_role",
        "original_source_file",
        "original_collection_file",
        "original_collection_status",
        "copy_source_file",
        "copied_file",
        "relative_copied_file",
        "source_suffix",
        "copied_suffix",
        "image_width",
        "image_height",
        "bitpix",
        "fits_filter",
        "instrument",
        "exposure",
        *METRIC_FIELDS,
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            measurement = record["measurement"]
            writer.writerow(
                {
                    **{field: record.get(field) for field in fieldnames},
                    "roles": ";".join(record["roles"]),
                    **{field: measurement.get(field) for field in METRIC_FIELDS},
                }
            )


def manifest_json_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for record in records:
        measurement = record["measurement"]
        output.append(
            {
                "group_dir": record["group_dir"],
                "user": record["user"],
                "equipment": record["equipment"],
                "filter": record["filter"],
                "roles": record["roles"],
                "primary_role": record["primary_role"],
                "original_source_file": record["original_source_file"],
                "original_collection_file": record["original_collection_file"],
                "original_collection_status": record["original_collection_status"],
                "copy_source_file": record["copy_source_file"],
                "copied_file": record["copied_file"],
                "relative_copied_file": record["relative_copied_file"],
                "source_suffix": record["source_suffix"],
                "copied_suffix": record["copied_suffix"],
                "image": {
                    "width": record.get("image_width"),
                    "height": record.get("image_height"),
                    "bitpix": record.get("bitpix"),
                    "filter": record.get("fits_filter"),
                    "instrument": record.get("instrument"),
                    "exposure": record.get("exposure"),
                    "error": record.get("image_error"),
                },
                "metrics": {field: measurement.get(field) for field in METRIC_FIELDS},
            }
        )
    return output


def report_for_records(
    *,
    source_report: dict[str, Any],
    records: list[dict[str, Any]],
    output_root: Path,
) -> dict[str, Any]:
    rows = []
    for record in records:
        row = dict(record["report_row"])
        row["original_source_file"] = record["original_source_file"]
        row["original_collection_file"] = record["original_collection_file"]
        row["original_collection_status"] = record["original_collection_status"]
        row["source_file"] = record["copied_file"]
        row["collection_file"] = record["copied_file"]
        row["collection_status"] = "copied_for_lnc_test"
        row["final_filter"] = record["filter"]
        row["lnc_group_dir"] = record["group_dir"]
        row["lnc_selection_roles"] = ";".join(record["roles"])
        rows.append(row)

    report = {key: value for key, value in source_report.items() if key != "rows"}
    report["input_dir"] = str((output_root / "subs").resolve())
    report["rows"] = rows
    report["lnc_test_dataset"] = {
        "source_report": str(DEFAULT_REPORT),
        "description": "Portable copied subset for Markarians LNC testing.",
    }
    return report


def write_measurements_csv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MEASUREMENT_FIELDS)
        writer.writeheader()
        for record in records:
            measurement = record["measurement"]
            writer.writerow(
                {
                    "user": record["group_dir"],
                    "equipment": "default",
                    "filter": record["filter"],
                    "sub_path": record["copied_file"],
                    **{field: measurement.get(field) for field in METRIC_FIELDS},
                }
            )


def pair_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_group: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_group.setdefault(str(record["group_dir"]), []).append(record)

    pairs = []
    for group_dir, group_records in sorted(by_group.items()):
        references = [
            record
            for record in group_records
            if any(role.startswith("reference_rank_") for role in record["roles"])
        ]
        targets = [record for record in group_records if record not in references]
        for reference in references:
            for target in targets:
                pairs.append(
                    {
                        "group_dir": group_dir,
                        "user": reference["user"],
                        "equipment": reference["equipment"],
                        "filter": reference["filter"],
                        "reference_roles": ";".join(reference["roles"]),
                        "reference_file": reference["copied_file"],
                        "reference_relative_file": reference["relative_copied_file"],
                        "reference_score": reference["measurement"].get("score"),
                        "target_roles": ";".join(target["roles"]),
                        "target_file": target["copied_file"],
                        "target_relative_file": target["relative_copied_file"],
                        "target_score": target["measurement"].get("score"),
                    }
                )
    return pairs


def write_pairs_csv(path: Path, records: list[dict[str, Any]]) -> None:
    rows = pair_rows(records)
    fieldnames = [
        "group_dir",
        "user",
        "equipment",
        "filter",
        "reference_roles",
        "reference_file",
        "reference_relative_file",
        "reference_score",
        "target_roles",
        "target_file",
        "target_relative_file",
        "target_score",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_readme(path: Path, records: list[dict[str, Any]], smoke: list[dict[str, Any]]) -> None:
    groups = {}
    for record in records:
        groups.setdefault(record["group_dir"], []).append(record)

    lines = [
        "# Markarians LNC Test Dataset",
        "",
        "Portable copied subset generated from the large Markarians quality report.",
        "",
        "## Contents",
        "",
        "- `subs/`: copied FITS files, grouped one level deep by `user__equipment__filter`.",
        "- `filter_lookup_report.json`: full reduced report for stacking/LNC integration tests.",
        "- `filter_lookup_report_smoke.json`: smaller report for quick checks.",
        "- `quality_report/measurements.csv`: quality metrics for copied files.",
        "- `selection_manifest.csv` / `.json`: source paths, roles, image metadata, and metrics.",
        "- `lnc_pairs_full.csv` / `lnc_pairs_smoke.csv`: reference-target LNC pairs.",
        "",
        "## Selected Groups",
        "",
    ]
    for group_dir in sorted(groups):
        group_records = groups[group_dir]
        sample = group_records[0]
        refs = sum(1 for record in group_records if record["primary_role"].startswith("reference_rank_"))
        lines.append(
            f"- `{group_dir}`: {len(group_records)} frames, {refs} reference candidates, "
            f"{sample.get('image_width')}x{sample.get('image_height')}, "
            f"{sample.get('instrument') or 'unknown instrument'}"
        )

    first_pair = pair_rows(smoke)[:1]
    lines.extend(
        [
            "",
            "## Example Commands",
            "",
            "Stack dry-run:",
            "",
            "```bash",
            "python processing/stack_multiuser_subs.py \\",
            f"  --report {path.parent / 'filter_lookup_report_smoke.json'} \\",
            "  --dry-run",
            "```",
            "",
        ]
    )
    if first_pair:
        pair = first_pair[0]
        lines.extend(
            [
                "Single LNC smoke pair:",
                "",
                "```bash",
                "python processing/local_normalize_unregistered.py \\",
                f"  {json.dumps(pair['reference_file'])} \\",
                f"  {json.dumps(pair['target_file'])} \\",
                f"  {json.dumps(str(path.parent / 'lnc_smoke_output.fit'))} \\",
                f"  --diag-dir {json.dumps(str(path.parent / 'lnc_smoke_diag'))} \\",
                "  --save-intermediate-fits \\",
                "  --output-format float32",
                "```",
                "",
            ]
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_records(records: list[dict[str, Any]]) -> None:
    for record in records:
        source = Path(record["copy_source_file"])
        destination = Path(record["copied_file"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if destination.is_symlink():
            raise RuntimeError(f"Copied file unexpectedly remained a symlink: {destination}")


def write_outputs(
    *,
    output_root: Path,
    source_report: dict[str, Any],
    records: list[dict[str, Any]],
) -> None:
    records = sorted(records, key=record_sort_key)
    smoke = smoke_records(records)
    (output_root / "quality_report").mkdir(parents=True, exist_ok=True)

    copy_records(records)
    write_json(output_root / "filter_lookup_report.json", report_for_records(
        source_report=source_report,
        records=records,
        output_root=output_root,
    ))
    write_json(output_root / "filter_lookup_report_smoke.json", report_for_records(
        source_report=source_report,
        records=smoke,
        output_root=output_root,
    ))
    write_measurements_csv(output_root / "quality_report" / "measurements.csv", records)
    write_manifest_csv(output_root / "selection_manifest.csv", records)
    write_json(output_root / "selection_manifest.json", manifest_json_records(records))
    write_pairs_csv(output_root / "lnc_pairs_full.csv", records)
    write_pairs_csv(output_root / "lnc_pairs_smoke.csv", smoke)
    write_readme(output_root / "README.md", records, smoke)


def print_summary(records: list[dict[str, Any]], output_root: Path) -> None:
    by_group: dict[str, list[dict[str, Any]]] = {}
    total_bytes = 0
    for record in records:
        by_group.setdefault(str(record["group_dir"]), []).append(record)
        total_bytes += Path(record["copy_source_file"]).stat().st_size

    print(f"Output root: {output_root}")
    print(f"Selected groups: {len(by_group)}")
    print(f"Selected frames: {len(records)}")
    print(f"Copy size: {total_bytes / (1024 ** 3):.2f} GiB")
    for group_dir in sorted(by_group):
        group_records = by_group[group_dir]
        refs = [record for record in group_records if record["primary_role"].startswith("reference_rank_")]
        sample = group_records[0]
        star_counts = [
            value
            for record in group_records
            if (value := finite_float(record["measurement"].get("star_count"))) is not None
        ]
        if star_counts:
            star_summary = f"stars {min(star_counts):.0f}..{max(star_counts):.0f}"
        else:
            star_summary = "stars unavailable"
        print(
            f"  {group_dir}: {len(group_records)} frame(s), {len(refs)} refs, "
            f"{sample.get('image_width')}x{sample.get('image_height')}, {star_summary}"
        )


def main() -> None:
    args = parse_args()
    report_path = args.report.expanduser().resolve()
    measurements_path = args.measurements.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()

    source_report = load_report(report_path)
    measurements = load_measurements(measurements_path)
    records = build_selected_records(
        report=source_report,
        measurements=measurements,
        output_root=output_root,
    )
    print_summary(records, output_root)

    if args.dry_run:
        print("Dry run only; no files written.")
        return

    if output_root.exists():
        if output_root.is_dir() and not any(output_root.iterdir()):
            pass
        elif args.force:
            if output_root.is_dir():
                shutil.rmtree(output_root)
            else:
                output_root.unlink()
            output_root.mkdir(parents=True, exist_ok=True)
        else:
            raise FileExistsError(f"Output root already exists and is not empty: {output_root}")
    else:
        output_root.mkdir(parents=True, exist_ok=True)
    write_outputs(output_root=output_root, source_report=source_report, records=records)
    print(f"Wrote portable LNC test dataset: {output_root}")


if __name__ == "__main__":
    main()
