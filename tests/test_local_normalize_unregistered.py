from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "processing" / "lnc" / "scripts" / "lnc_unregistered_pair.py"
GROUP_WRAPPER_PATH = REPO_ROOT / "processing" / "lnc" / "scripts" / "lnc_group_sequence.py"
LNC_DIR = REPO_ROOT / "processing" / "lnc"
LNC_UNREGISTERED_BINARY = LNC_DIR / "bin" / "lnc_unregistered_pair"
LNC_GROUP_BINARY = LNC_DIR / "bin" / "lnc_group_subs"
STAR_SCALE_PATH = REPO_ROOT / "processing" / "lnc" / "scripts" / "lnc_star_scale.py"


def load_wrapper_module():
    sys.path.insert(0, str(REPO_ROOT / "processing" / "lnc" / "scripts"))
    spec = importlib.util.spec_from_file_location("local_normalize_unregistered_wrapper", WRAPPER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_star_scale_module():
    sys.path.insert(0, str(REPO_ROOT / "processing" / "lnc" / "scripts"))
    spec = importlib.util.spec_from_file_location("lnc_star_scale_test", STAR_SCALE_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_group_module():
    sys.path.insert(0, str(REPO_ROOT / "processing" / "lnc" / "scripts"))
    spec = importlib.util.spec_from_file_location("lnc_group_sequence_test", GROUP_WRAPPER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_sequence_file_extracts_homographies(tmp_path: Path):
    module = load_wrapper_module()
    seq = tmp_path / "lnc_pair_.seq"
    seq.write_text(
        "# Siril sequence file\n"
        "S 'lnc_pair_' 1 2 2 5 1 6 1 0 0\n"
        "I 1 1 100,80\n"
        "I 2 1 100,90\n"
        "R0 0 0 0 0 0 0 H 1 0 4 0 1 -3 0 0 1\n"
        "R0 0 0 0 0 0 0 H 1 0 0 0 1 0 0 0 1\n",
        encoding="utf-8",
    )

    parsed = module.parse_sequence_file(seq)

    assert parsed["sequence_name"] == "lnc_pair_"
    assert parsed["reference_image"] == 1
    assert parsed["image_sizes"] == [(100, 80), (100, 90)]
    assert np.allclose(parsed["matrices"][0], [[1, 0, 4], [0, 1, -3], [0, 0, 1]])


def test_siril_to_array_homography_flips_y_coordinates():
    module = load_wrapper_module()
    siril_h = np.eye(3)

    array_h = module.siril_to_array_homography(
        siril_h,
        reference_height=5,
        target_height=7,
    )

    transformed = module.apply_homography(array_h, np.array([[10.0, 6.0]]))
    assert transformed[0, 0] == pytest.approx(10.0)
    assert transformed[0, 1] == pytest.approx(4.0)


def test_transform_validation_uses_full_reference_catalog():
    module = load_wrapper_module()
    ref_stars = [
        SimpleNamespace(x=100.0, y=100.0),
        SimpleNamespace(x=200.0, y=200.0),
        SimpleNamespace(x=10.0, y=20.0),
    ]
    target_stars = [SimpleNamespace(x=7.0, y=24.0)]
    target_to_ref = np.array([[1.0, 0.0, 3.0], [0.0, 1.0, -4.0], [0.0, 0.0, 1.0]])
    sequence = {"matrices": [np.eye(3), target_to_ref]}

    matrix, report = module.validate_target_to_reference_matrix(
        sequence=sequence,
        ref_stars=ref_stars,
        target_stars=target_stars,
        max_stars=1,
    )

    assert np.allclose(matrix, target_to_ref)
    assert report["median_nearest_star_distance_px"] == pytest.approx(0.0)
    assert report["reference_stars_used"] == 3
    assert report["target_stars_used"] == 1


def test_value_scale_detection_and_float32_output_preserve_normalized_data(tmp_path: Path):
    module = load_wrapper_module()
    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    output_path = tmp_path / "corrected.fit"
    data = np.array([[0.0, 0.25, 0.75], [1.0, -0.01, 0.5]], dtype=np.float32)
    fits.writeto(ref_path, data, overwrite=True)
    fits.writeto(target_path, data + 0.001, overwrite=True)
    fits.writeto(output_path, data, overwrite=True)

    report = module.choose_pipeline_value_scale(ref_path, target_path)
    module.apply_output_format_for_value_scale(output_path, "float32", report["scale"])

    assert report["scale"] == module.VALUE_SCALE_NORMALIZED
    with fits.open(output_path, memmap=False) as hdul:
        assert hdul[0].data[0, 1] == pytest.approx(0.25)
        assert hdul[0].data[0, 2] == pytest.approx(0.75)
        assert hdul[0].data[1, 0] == pytest.approx(1.0)
        assert hdul[0].header["LNCVSCL"] == module.VALUE_SCALE_NORMALIZED


def test_value_scale_detection_and_float32_output_normalize_adu_data(tmp_path: Path):
    module = load_wrapper_module()
    ref_path = tmp_path / "ref_adu.fit"
    target_path = tmp_path / "target_adu.fit"
    output_path = tmp_path / "corrected_adu.fit"
    data = np.array([[0.0, 32767.5, 65535.0], [70000.0, -10.0, 1000.0]], dtype=np.float32)
    fits.writeto(ref_path, data, overwrite=True)
    fits.writeto(target_path, data + 10.0, overwrite=True)
    fits.writeto(output_path, data, overwrite=True)

    report = module.choose_pipeline_value_scale(ref_path, target_path)
    module.apply_output_format_for_value_scale(output_path, "float32", report["scale"])

    assert report["scale"] == module.VALUE_SCALE_ADU
    with fits.open(output_path, memmap=False) as hdul:
        assert hdul[0].data[0, 1] == pytest.approx(32767.5 / 65535.0)
        assert hdul[0].data[0, 2] == pytest.approx(1.0)
        assert hdul[0].data[1, 0] == pytest.approx(1.0)
        assert hdul[0].data[1, 1] == pytest.approx(0.0)
        assert hdul[0].header["LNCVSCL"] == module.VALUE_SCALE_ADU


def test_unregistered_c_core_corrects_translated_synthetic_image(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR), "bin/lnc_unregistered_pair"], check=True)

    height = 256
    width = 256
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ref = 1000.0 + 0.2 * xx + 0.12 * yy + 12.0 * np.sin(xx / 31.0)
    scale = 1.16
    offset = -53.0
    dx = 4
    dy = -3
    target = np.full((height, width), 900.0, dtype=np.float32)
    valid = (xx + dx >= 0) & (xx + dx < width) & (yy + dy >= 0) & (yy + dy < height)
    target[valid] = ((ref[(yy[valid] + dy).astype(int), (xx[valid] + dx).astype(int)] - offset) / scale).astype(
        np.float32
    )
    mask = np.zeros((height, width), dtype=np.uint8)

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected.fit"
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    fits.writeto(ref_path, ref.astype(np.float32), overwrite=True)
    fits.writeto(target_path, target, overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_UNREGISTERED_BINARY),
            "--threads",
            "2",
            "--ref-mask",
            str(mask_path),
            "--target-mask",
            str(mask_path),
            "--homography",
            "1",
            "0",
            str(dx),
            "0",
            "1",
            str(dy),
            "0",
            "0",
            "1",
            "--diag-dir",
            str(diag_dir),
            "--report",
            str(diag_dir / "report.json"),
            "--grid-spacing",
            "64",
            "--window-size",
            "96",
            "--min-samples",
            "1000",
            str(ref_path),
            str(target_path),
            str(out_path),
        ],
        check=True,
    )

    corrected = fits.getdata(out_path)
    inner = (xx + dx >= 20) & (xx + dx < width - 20) & (yy + dy >= 20) & (yy + dy < height - 20)
    registered_ref = ref[(yy[inner] + dy).astype(int), (xx[inner] + dx).astype(int)]
    before = np.median(np.abs(target[inner] - registered_ref))
    after = np.median(np.abs(corrected[inner] - registered_ref))

    assert after < before * 0.05
    assert (diag_dir / "scale_map.fits").exists()
    assert (diag_dir / "offset_map.fits").exists()


def test_unregistered_c_core_extrapolates_correction_outside_reference_frame(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR), "bin/lnc_unregistered_pair"], check=True)

    height = 192
    width = 192
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    dx = 28
    dy = 0
    scale = 1.11
    offset = -0.004

    def background(x_values: np.ndarray, y_values: np.ndarray) -> np.ndarray:
        return 0.08 + 0.0002 * x_values + 0.00015 * y_values

    ref = background(xx, yy).astype(np.float32)
    target = ((background(xx + dx, yy + dy) - offset) / scale).astype(np.float32)
    mask = np.zeros((height, width), dtype=np.uint8)

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected.fit"
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    fits.writeto(ref_path, ref, overwrite=True)
    fits.writeto(target_path, target, overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_UNREGISTERED_BINARY),
            "--ref-mask",
            str(mask_path),
            "--target-mask",
            str(mask_path),
            "--homography",
            "1",
            "0",
            str(dx),
            "0",
            "1",
            str(dy),
            "0",
            "0",
            "1",
            "--diag-dir",
            str(diag_dir),
            "--grid-spacing",
            "48",
            "--window-size",
            "80",
            "--min-samples",
            "800",
            str(ref_path),
            str(target_path),
            str(out_path),
        ],
        check=True,
    )

    corrected = fits.getdata(out_path)
    expected = background(xx + dx, yy + dy)
    outside_reference = xx + dx >= width
    before = np.median(np.abs(target[outside_reference] - expected[outside_reference]))
    after = np.median(np.abs(corrected[outside_reference] - expected[outside_reference]))

    assert before > 0.003
    assert after < before * 0.05


def test_lnc_star_scale_findstar_defaults_to_siril_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = load_star_scale_module()
    captured: dict[str, str] = {}
    star_list = tmp_path / "stars.lst"

    def fake_run_siril(siril_path, work_dir, script, context, timeout):
        captured["script"] = script
        captured["context"] = context
        star_list.write_text("# x y fwhm roundness\n10 20 3 0.9\n", encoding="utf-8")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(module, "run_siril", fake_run_siril)

    report = module.run_lnc_findstar(tmp_path / "image.fit", star_list, "siril-cli", 5.0, mode="siril-default")

    assert "setfindstar" not in captured["script"]
    assert "findstar -out=stars.lst" in captured["script"]
    assert "siril-default" in captured["context"]
    assert report["findstar_mode"] == "siril-default"
    assert report["setfindstar_command"] is None


def test_lnc_star_scale_robust_fit_uses_target_over_reference_convention():
    module = load_star_scale_module()

    fit = module.robust_ratio_fit(
        [100.0, 200.0, 300.0, 400.0, 500.0, 600.0],
        [120.0, 240.0, 360.0, 480.0, 5000.0, 720.0],
        clip_sigma=2.0,
        max_iterations=5,
        min_points=4,
    )

    assert fit.ok
    assert fit.n_used == 5
    assert fit.scale_b_over_a == pytest.approx(1.2)
    assert 5 not in [i + 1 for i, kept in enumerate(fit.kept_mask) if kept]


def test_ssa_lnc_c_core_uses_constant_global_scale_and_local_offset_gradient(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR), "bin/lnc_unregistered_pair"], check=True)

    height = 192
    width = 192
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ref = 1000.0 + 0.18 * xx + 0.07 * yy + 6.0 * np.sin(xx / 37.0)
    global_scale = 1.25
    injected_offset = 35.0 + 0.12 * xx - 0.09 * yy
    target = ((ref - injected_offset) / global_scale).astype(np.float32)
    mask = np.zeros((height, width), dtype=np.uint8)

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected.fit"
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    report_path = diag_dir / "report.json"
    fits.writeto(ref_path, ref.astype(np.float32), overwrite=True)
    fits.writeto(target_path, target, overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_UNREGISTERED_BINARY),
            "--ref-mask",
            str(mask_path),
            "--target-mask",
            str(mask_path),
            "--homography",
            "1",
            "0",
            "0",
            "0",
            "1",
            "0",
            "0",
            "0",
            "1",
            "--diag-dir",
            str(diag_dir),
            "--report",
            str(report_path),
            "--background-estimator",
            "trimmed-median",
            "--photometric-model",
            "star-scale-additive",
            "--global-scale",
            str(global_scale),
            "--grid-spacing",
            "48",
            "--window-size",
            "80",
            "--min-samples",
            "1000",
            str(ref_path),
            str(target_path),
            str(out_path),
        ],
        check=True,
    )

    corrected = fits.getdata(out_path)
    scale_map = fits.getdata(diag_dir / "scale_map.fits")
    offset_map = fits.getdata(diag_dir / "offset_map.fits")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    inner = (xx > 20) & (xx < width - 20) & (yy > 20) & (yy < height - 20)

    before = np.median(np.abs(target[inner] - ref[inner]))
    after = np.median(np.abs(corrected[inner] - ref[inner]))
    offset_error = np.median(np.abs(offset_map[inner] - injected_offset[inner]))

    assert after < before * 0.05
    assert np.nanmax(scale_map) == pytest.approx(global_scale, abs=1e-6)
    assert np.nanmin(scale_map) == pytest.approx(global_scale, abs=1e-6)
    assert offset_error < 3.0
    assert report["photometric_model"] == "star-scale-additive"
    assert report["global_scale"] == pytest.approx(global_scale)


def test_group_manifest_includes_per_target_star_scale_diagnostics(tmp_path: Path):
    module = load_group_module()
    sequence = module.SequenceInfo(
        path=tmp_path / "pp_light_.seq",
        name="pp_light_",
        start_index=1,
        fixed_len=5,
        reference_index=1,
        frames=[
            module.SequenceFrame(1, True, tmp_path / "ref.fit", 64, 64, [1, 0, 0, 0, 1, 0, 0, 0, 1]),
            module.SequenceFrame(2, True, tmp_path / "target.fit", 64, 64, [1, 0, 0, 0, 1, 0, 0, 0, 1]),
        ],
    )
    params = {
        "background_estimator": "trimmed-median",
        "photometric_model": "star-scale-additive",
        "scale_min": 0.5,
        "scale_max": 2.0,
        "grid_spacing": 32,
        "window_size": 64,
        "min_samples": 100,
        "trim_fraction": 0.1,
        "smooth_passes": 1,
        "min_valid_fraction": 0.3,
    }

    manifest, skipped = module.build_manifest(
        sequence,
        reference_index=1,
        reference_source="test",
        output_dir=tmp_path,
        params=params,
        target_star_scales={
            2: {
                "ok": True,
                "target_to_reference_scale": 1.234,
                "matched_stars": 42,
                "used_stars": 30,
                "robust_fit": {"r_squared": 0.99, "ratio_mad": 0.02},
            }
        },
    )

    assert skipped == []
    assert manifest["params"]["photometric_model"] == "star-scale-additive"
    assert manifest["targets"][0]["global_scale"] == pytest.approx(1.234)
    assert manifest["targets"][0]["global_scale_source"] == "matched-star-flux"
    assert manifest["targets"][0]["star_scale_diagnostics"]["used_stars"] == 30


def test_group_manifest_skips_failed_star_scale_targets(tmp_path: Path):
    module = load_group_module()
    sequence = module.SequenceInfo(
        path=tmp_path / "pp_light_.seq",
        name="pp_light_",
        start_index=1,
        fixed_len=5,
        reference_index=1,
        frames=[
            module.SequenceFrame(1, True, tmp_path / "ref.fit", 64, 64, [1, 0, 0, 0, 1, 0, 0, 0, 1]),
            module.SequenceFrame(2, True, tmp_path / "target.fit", 64, 64, [1, 0, 0, 0, 1, 0, 0, 0, 1]),
        ],
    )

    manifest, skipped = module.build_manifest(
        sequence,
        reference_index=1,
        reference_source="test",
        output_dir=tmp_path,
        params={"background_estimator": "trimmed-median", "photometric_model": "star-scale-additive"},
        star_scale_failures={2: {"sequence_index": 2, "status": "star_scale_failed", "message": "too_few_points"}},
    )

    assert skipped == [2]
    assert manifest["targets"] == []
    assert manifest["star_scale_failures"][0]["message"] == "too_few_points"


def test_group_c_core_uses_per_target_global_scales(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR), "bin/lnc_group_subs"], check=True)

    height = 128
    width = 128
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ref = 1000.0 + 0.2 * xx + 0.08 * yy + 4.0 * np.sin(xx / 23.0)
    scales = [1.18, 0.82]
    offsets = [
        28.0 + 0.05 * xx - 0.03 * yy,
        -18.0 + 0.02 * xx + 0.04 * yy,
    ]
    targets = [((ref - offset) / scale).astype(np.float32) for scale, offset in zip(scales, offsets)]

    ref_path = tmp_path / "ref.fit"
    out_ref = tmp_path / "lnc_ref.fit"
    fits.writeto(ref_path, ref.astype(np.float32), overwrite=True)
    target_entries = []
    for idx, (target, scale) in enumerate(zip(targets, scales), start=2):
        target_path = tmp_path / f"target_{idx}.fit"
        out_path = tmp_path / f"lnc_target_{idx}.fit"
        fits.writeto(target_path, target, overwrite=True)
        target_entries.append(
            {
                "sequence_index": idx,
                "work_sequence_file": str(target_path),
                "corrected_sequence_file": str(out_path),
                "target_to_reference_homography": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                "global_scale": scale,
            }
        )
    manifest = {
        "sequence_name": "synthetic",
        "params": {
            "background_estimator": "trimmed-median",
            "photometric_model": "star-scale-additive",
            "grid_spacing": 32,
            "window_size": 64,
            "min_samples": 500,
            "trim_fraction": 0.1,
            "scale_min": 0.5,
            "scale_max": 2.0,
            "smooth_passes": 1,
            "min_valid_fraction": 0.5,
        },
        "reference": {
            "sequence_index": 1,
            "work_sequence_file": str(ref_path),
            "corrected_sequence_file": str(out_ref),
        },
        "targets": target_entries,
        "output_summary": str(tmp_path / "group_summary.json"),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    env = {**dict(os.environ), "LNC_WRITE_DIAGNOSTICS": "1"}

    subprocess.run([str(LNC_GROUP_BINARY), "--lnc-threads", "2", "--lnc-workers", "1", str(manifest_path)], check=True, env=env)

    inner = (xx > 16) & (xx < width - 16) & (yy > 16) & (yy < height - 16)
    summary = json.loads((tmp_path / "group_summary.json").read_text(encoding="utf-8"))
    assert summary["photometric_model"] == "star-scale-additive"
    for idx, scale in enumerate(scales, start=2):
        out_path = tmp_path / f"lnc_target_{idx}.fit"
        corrected = fits.getdata(out_path)
        scale_map = fits.getdata(tmp_path / f"lnc_target_{idx}_lnc_diag" / "scale_map.fits")
        before = np.median(np.abs(targets[idx - 2][inner] - ref[inner]))
        after = np.median(np.abs(corrected[inner] - ref[inner]))
        assert after < before * 0.05
        assert np.nanmax(scale_map) == pytest.approx(scale, abs=1e-6)
        assert np.nanmin(scale_map) == pytest.approx(scale, abs=1e-6)
