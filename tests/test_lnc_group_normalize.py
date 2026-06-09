"""Tests for the group LNC C binary and pair processor."""

from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits


LNC_DIR = Path(__file__).resolve().parents[1] / "processing" / "lnc"


def _write_fits(path: Path, seed: float) -> None:
    rng = np.random.default_rng(int(seed * 1000))
    data = (rng.random((48, 48), dtype=np.float32) * 1000.0) + seed
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.writeto(path, data, overwrite=True)


def load_group_sequence_module():
    wrapper_path = LNC_DIR / "scripts" / "lnc_group_sequence.py"
    sys.path.insert(0, str(wrapper_path.parent))
    spec = importlib.util.spec_from_file_location("lnc_group_sequence_wrapper", wrapper_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lnc_binaries() -> dict[str, Path]:
    subprocess.run(["make", "-C", str(LNC_DIR), "bin/lnc_unregistered_pair", "bin/lnc_group_subs"], check=True)
    return {
        "pair": LNC_DIR / "bin" / "lnc_unregistered_pair",
        "group": LNC_DIR / "bin" / "lnc_group_subs",
    }


def test_lnc_group_normalize_runs(tmp_path: Path, lnc_binaries: dict[str, Path]) -> None:
    ref = tmp_path / "ref.fit"
    target = tmp_path / "target.fit"
    target2 = tmp_path / "target2.fit"
    _write_fits(ref, 1.0)
    _write_fits(target, 2.0)
    _write_fits(target2, 3.0)

    corrected_dir = tmp_path / "corrected"
    corrected_dir.mkdir(parents=True, exist_ok=True)
    corrected_ref = corrected_dir / "pp_light_00001.fit"
    corrected_target = corrected_dir / "pp_light_00002.fit"
    corrected_target2 = corrected_dir / "pp_light_00003.fit"
    manifest = {
        "sequence_name": "pp_light",
        "reference": {
            "sequence_index": 1,
            "work_sequence_file": str(ref),
            "corrected_sequence_file": str(corrected_ref),
        },
        "targets": [
            {
                "sequence_index": 2,
                "work_sequence_file": str(target),
                "corrected_sequence_file": str(corrected_target),
                "target_to_reference_homography": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            },
            {
                "sequence_index": 3,
                "work_sequence_file": str(target2),
                "corrected_sequence_file": str(corrected_target2),
                "target_to_reference_homography": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            }
        ],
        "output_summary": str(tmp_path / "summary.json"),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = subprocess.run(
        [str(lnc_binaries["group"]), "--lnc-threads", "2", "--lnc-workers", "2", str(manifest_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert corrected_ref.exists()
    assert corrected_target.exists()
    assert corrected_target2.exists()
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["lnc_threads"] == 2
    assert summary["lnc_workers"] == 2


def test_lnc_group_sequence_default_workers() -> None:
    module = load_group_sequence_module()

    assert module.default_lnc_workers(8, cpu_count=64) == 8
    assert module.default_lnc_workers(8, cpu_count=4) == 1
