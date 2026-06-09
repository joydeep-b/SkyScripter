"""Mocked Siril integration tests for the LNC stacker."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from processing.stack_multiuser_lnc_subs import main


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "processing" / "stack_multiuser_lnc_subs.py"


def _write_fits(path: Path, value: float) -> None:
    fits.writeto(path, np.full((16, 16), value, dtype=np.float32), overwrite=True)


def test_direct_script_can_run_from_other_cwd(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_dry_run_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_dir = tmp_path / "input" / "user" / "rig"
    input_dir.mkdir(parents=True)
    subs = []
    for index, score in enumerate([9.0, 8.0, 7.0], start=1):
        sub = input_dir / f"frame_{index}.fit"
        _write_fits(sub, score)
        subs.append(
            {
                "source_file": str(sub),
                "collection_file": str(sub),
                "final_filter": "L",
            }
        )
    report_path = tmp_path / "filter_lookup_report.json"
    report_path.write_text(json.dumps({"input_dir": str(input_dir.parent.parent), "rows": subs}), encoding="utf-8")

    import csv

    measurements = tmp_path / "measurements.csv"
    with measurements.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["sub_path", "score"])
        writer.writeheader()
        for sub in subs:
            writer.writerow({"sub_path": sub["source_file"], "score": 9.0})

    output_root = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        [
            "stack_multiuser_lnc_subs",
            "--report",
            str(report_path),
            "--measurements",
            str(measurements),
            "--output-root",
            str(output_root),
            "--dry-run",
        ],
    )
    assert main() == 0
    manifest_path = output_root / "lnc_manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["rows"]) == 3
    assert not list((output_root / "work").glob("**/pp_light_*.fit"))
    assert (output_root / "work" / "user" / "rig" / "L" / "manifests" / "group_manifest.json").exists()
