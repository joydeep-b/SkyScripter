"""Tests for multi-user LNC stacking helpers and planning."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from processing.multiuser_lnc_common import (
    apply_drop_policy,
    build_lnc_group_manifest,
    extract_target_to_reference_homography,
    parse_report_rows,
    parse_sequence_file,
    plan_all_groups,
    prepare_group,
    PreparedGroup,
    render_siril_template,
    safe_name,
)
from processing.stack_multiuser_lnc_subs import default_output_root
from processing.stack_multiuser_lnc_subs import validate_registration_sequence


def _write_fits(path: Path, value: float = 100.0) -> None:
    data = np.full((32, 32), value, dtype=np.float32)
    fits.writeto(path, data, overwrite=True)


def _write_report(tmp_path: Path, rows: list[dict]) -> Path:
    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True)
    report = {"input_dir": str(input_dir), "rows": rows}
    report_path = tmp_path / "filter_lookup_report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path, input_dir


def _write_measurements(tmp_path: Path, entries: list[tuple[str, float]]) -> Path:
    import csv

    path = tmp_path / "measurements.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sub_path", "score", "star_count"])
        writer.writeheader()
        for sub_path, score in entries:
            writer.writerow({"sub_path": sub_path, "score": score, "star_count": 50})
    return path


def test_safe_name() -> None:
    assert safe_name("User One/Rig A") == "User_One_Rig_A"


def test_apply_drop_policy_keeps_reference_and_caps_drops() -> None:
    from processing.multiuser_lnc_common import GroupRow

    rows = [
        GroupRow("u", "e", "L", Path("/a.fit"), Path("/a.fit"), score=10.0),
        GroupRow("u", "e", "L", Path("/b.fit"), Path("/b.fit"), score=9.5),
        GroupRow("u", "e", "L", Path("/c.fit"), Path("/c.fit"), score=9.0),
        GroupRow("u", "e", "L", Path("/d.fit"), Path("/d.fit"), score=1.0),
        GroupRow("u", "e", "L", Path("/e.fit"), Path("/e.fit"), score=0.5),
    ]
    kept, dropped, summary = apply_drop_policy(rows, min_frames=3, drop_max_fraction=0.15)
    assert len(kept) >= 3
    assert kept[0].reference
    assert kept[0].score == 10.0
    assert summary["actual_drop_count"] <= 1


def test_render_registration_template(tmp_path: Path) -> None:
    template = tmp_path / "register.ssf"
    template.write_text(
        "setcpu ${SIRIL_THREADS}\nsetref ${SEQUENCE_NAME} ${REFERENCE_INDEX}\n",
        encoding="utf-8",
    )
    rendered = render_siril_template(
        template,
        {
            "SIRIL_THREADS": "4",
            "SEQUENCE_NAME": "pp_light",
            "REFERENCE_INDEX": "1",
        },
    )
    assert "setref pp_light 1" in rendered


def test_plan_all_groups(tmp_path: Path) -> None:
    report_path, input_dir = _write_report(tmp_path, [])
    user_dir = input_dir / "alice" / "rig"
    user_dir.mkdir(parents=True)
    subs = []
    scores = [8.0, 7.5, 7.0]
    for index, score in enumerate(scores, start=1):
        sub = user_dir / f"sub_{index}.fit"
        _write_fits(sub, value=score)
        subs.append(
            {
                "source_file": str(sub),
                "collection_file": str(sub),
                "final_filter": "L",
            }
        )
    report_path.write_text(json.dumps({"input_dir": str(input_dir), "rows": subs}), encoding="utf-8")
    measurements = _write_measurements(tmp_path, [(str(sub.resolve()), score) for sub, score in zip(
        [user_dir / f"sub_{i}.fit" for i in range(1, 4)],
        scores,
    )])
    output_root = tmp_path / "out"
    prepared, skipped, stats = plan_all_groups(
        report_path,
        measurements,
        output_root,
        min_frames=3,
        drop_max_fraction=0.15,
    )
    assert stats["eligible_rows"] == 3
    assert len(prepared) == 1
    assert len(skipped) == 0
    group = prepared[0]
    assert group.reference_row.reference
    assert group.reference_row.sequence_index == 1
    assert (group.work_dir / "pp_light_00001.fit").exists()


def test_parse_sequence_and_homography(tmp_path: Path) -> None:
    seq_text = "\n".join(
        [
            "S pp_light 0 2 2 0 0 0",
            "I 0 0 64,64",
            "I 1 0 64,64",
            "R0 H 1 0 0 0 1 0 0 0 1",
            "R1 H 1 0 1 0 1 0 0 0 1",
        ]
    )
    seq_path = tmp_path / "pp_light.seq"
    seq_path.write_text(seq_text, encoding="utf-8")
    seq_info = parse_sequence_file(seq_path)
    array_h, meta = extract_target_to_reference_homography(
        seq_info,
        reference_index=1,
        target_index=2,
    )
    assert len(array_h) == 9
    assert meta["transform_validation_status"] == "not_available"


def test_build_lnc_group_manifest(tmp_path: Path) -> None:
    from processing.multiuser_lnc_common import GroupRow, assign_sequence_indices

    work_dir = tmp_path / "work"
    ref = work_dir / ".process" / "pp_light_00001.fit"
    tgt = work_dir / ".process" / "pp_light_00002.fit"
    ref.parent.mkdir(parents=True)
    _write_fits(ref)
    _write_fits(tgt)
    rows = [
        GroupRow("u", "e", "L", ref, ref, score=10.0, reference=True),
        GroupRow("u", "e", "L", tgt, tgt, score=9.0),
    ]
    assign_sequence_indices(rows)
    for row in rows:
        row.work_sequence_file = work_dir / ".process" / f"pp_light_{row.sequence_index:05d}.fit"
        row.corrected_sequence_file = work_dir / "corrected_sequence" / f"pp_light_{row.sequence_index:05d}.fit"
    prepared = prepare_group(("u", "e", "L"), rows, tmp_path / "out", {})
    seq_path = tmp_path / "pp_light.seq"
    seq_path.write_text(
        "\n".join(
            [
                "S pp_light 0 2 2 0 0 0",
                "I 0 0 32,32",
                "I 1 0 32,32",
                "R0 H 1 0 0 0 1 0 0 0 1",
                "R1 H 1 0 0 0 1 0 0 0 1",
            ]
        ),
        encoding="utf-8",
    )
    manifest = build_lnc_group_manifest(prepared, parse_sequence_file(seq_path))
    assert manifest["reference"]["sequence_index"] == 1
    assert len(manifest["targets"]) == 1


def _prepared_group_with_seq(tmp_path: Path, reference_field: int) -> PreparedGroup:
    from processing.multiuser_lnc_common import GroupRow, assign_sequence_indices

    rows = [
        GroupRow("u", "e", "L", tmp_path / "a.fit", tmp_path / "a.fit", score=10.0, reference=True),
        GroupRow("u", "e", "L", tmp_path / "b.fit", tmp_path / "b.fit", score=9.0),
    ]
    _write_fits(tmp_path / "a.fit")
    _write_fits(tmp_path / "b.fit")
    assign_sequence_indices(rows)
    prepared = prepare_group(("u", "e", "L"), rows, tmp_path / "out", {}, materialize=False)
    process_dir = prepared.work_dir / ".process"
    process_dir.mkdir(parents=True)
    for row in prepared.kept_rows:
        _write_fits(process_dir / f"pp_light_{row.sequence_index:05d}.fit")
    (process_dir / "pp_light_.seq").write_text(
        "\n".join(
            [
                f"S 'pp_light_' 1 2 2 5 {reference_field} 6 0 0 0",
                "I 1 1",
                "I 2 1",
                "R0 H 1 0 0 0 1 0 0 0 1",
                "R0 H 1 0 0 0 1 0 0 0 1",
            ]
        ),
        encoding="utf-8",
    )
    return prepared


def test_final_stack_fails_when_siril_changes_reference(tmp_path: Path) -> None:
    # The S line's reference field (index 6) is 1, but the final stack expects 0.
    prepared = _prepared_group_with_seq(tmp_path, reference_field=1)
    with pytest.raises(ValueError, match="Siril changed the registration reference"):
        validate_registration_sequence(prepared, expected_reference_index=0)


def test_initial_pass_keeps_reference_despite_siril_reselection(tmp_path: Path) -> None:
    # `register -2pass` picks its own internal reference (field index 6 = 1 here),
    # but the transforms-only pass must keep our quality-selected reference and not fail.
    prepared = _prepared_group_with_seq(tmp_path, reference_field=1)
    reference_before = prepared.reference_row
    seq_info = validate_registration_sequence(prepared, allow_internal_reference=True)
    assert prepared.reference_row is reference_before
    assert len(seq_info["matrices"]) == 2


def test_default_output_root(tmp_path: Path) -> None:
    report = tmp_path / "filter_lookup_report.json"
    report.write_text("{}", encoding="utf-8")
    assert default_output_root(report) == tmp_path / "per_user_lnc_stacks"
