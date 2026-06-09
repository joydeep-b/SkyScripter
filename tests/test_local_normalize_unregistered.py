from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "processing" / "lnc" / "scripts" / "lnc_unregistered_pair.py"
LNC_DIR = REPO_ROOT / "processing" / "lnc"
LNC_UNREGISTERED_BINARY = LNC_DIR / "bin" / "lnc_unregistered_pair"


def load_wrapper_module():
    sys.path.insert(0, str(REPO_ROOT / "processing" / "lnc" / "scripts"))
    spec = importlib.util.spec_from_file_location("local_normalize_unregistered_wrapper", WRAPPER_PATH)
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
