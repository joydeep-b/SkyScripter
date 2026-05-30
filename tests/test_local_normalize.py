from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = REPO_ROOT / "processing" / "local_normalize.py"
LNC_DIR = REPO_ROOT / "processing" / "lnc"
LNC_BINARY = LNC_DIR / "local_normalize"
LNC_TRIMMED_MEDIAN_BINARY = LNC_DIR / "local_normalize_trimmed_median"
LNC_V2_BINARY = LNC_DIR / "local_normalize_v2"


def load_wrapper_module():
    spec = importlib.util.spec_from_file_location("local_normalize_wrapper", WRAPPER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_select_lnc_binary_routes_background_estimators():
    module = load_wrapper_module()

    assert module.select_lnc_binary(
        argparse.Namespace(binary=None, background_estimator="trimmed-mean")
    ) == module.LNC_BINARY
    assert module.select_lnc_binary(
        argparse.Namespace(binary=None, background_estimator="trimmed-median")
    ) == module.LNC_TRIMMED_MEDIAN_BINARY
    assert module.select_lnc_binary(
        argparse.Namespace(binary=None, background_estimator="sample-median")
    ) == module.LNC_V2_BINARY


def test_parse_star_list_with_header(tmp_path: Path):
    module = load_wrapper_module()
    star_list = tmp_path / "stars.lst"
    star_list.write_text(
        "# x y fwhm roundness\n"
        "10.5 20.25 3.2 0.9\n"
        "30.0 40.0 4.5 0.8\n",
        encoding="utf-8",
    )

    stars = module.parse_star_list(star_list)

    assert len(stars) == 2
    assert stars[0].x == 10.5
    assert stars[0].y == 20.25
    assert stars[0].fwhm == 3.2


def test_parse_siril_command_timings():
    module = load_wrapper_module()
    output = """
log: Running command: load
log: Running command: findstar
log: Findstar: processing for channel 0...
log: Execution time: 271.79 ms
log: Found 3099 Gaussian profile stars in image, channel #0 (FWHM 2.959456)
log: Running command: close
log: Running command: exit
log: Total execution time: 4.31 s
"""

    report = module.parse_siril_findstar_report(output)

    assert report["stars"] == 3099
    assert report["fwhm"] == 2.959456
    assert report["findstar_seconds"] == pytest.approx(0.27179)
    assert report["script_total_seconds"] == 4.31
    assert report["command_timings_seconds"]["findstar"] == pytest.approx(0.27179)


def test_build_mask_flips_siril_y_coordinates(tmp_path: Path):
    module = load_wrapper_module()
    reference = tmp_path / "reference.fit"
    target = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    data = np.zeros((5, 7), dtype=np.float32)
    fits.writeto(reference, data, overwrite=True)
    fits.writeto(target, data, overwrite=True)

    module.build_mask(
        ref_path=reference,
        target_path=target,
        ref_stars=[module.Star(x=2.0, y=1.0, fwhm=0.1)],
        target_stars=[],
        output_path=mask_path,
        radius_min=0.1,
        radius_factor=1.0,
        radius_max=0.1,
        saturation_threshold=None,
        saturation_dilation=0,
    )

    mask = fits.getdata(mask_path).astype(bool)
    assert mask[3, 2]
    assert not mask[1, 2]


def test_c_core_corrects_affine_synthetic_image(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR)], check=True)

    height = 256
    width = 256
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ref = 1000.0 + 0.25 * xx + 0.15 * yy + 20.0 * np.sin(xx / 30.0)
    scale = 1.18
    offset = -73.0
    target = (ref - offset) / scale

    # Add star-like outliers with different amplitudes. The supplied mask should
    # keep these out of the affine fit.
    mask = np.zeros((height, width), dtype=np.uint8)
    for cx, cy in [(70, 80), (160, 140), (210, 50)]:
        rr = (xx - cx) ** 2 + (yy - cy) ** 2
        star = rr <= 8**2
        ref[star] += 9000.0
        target[star] += 3000.0
        mask[rr <= 14**2] = 1

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected.fit"
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    report_path = diag_dir / "report.json"

    fits.writeto(ref_path, ref.astype(np.float32), overwrite=True)
    fits.writeto(target_path, target.astype(np.float32), overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_BINARY),
            "--mask",
            str(mask_path),
            "--diag-dir",
            str(diag_dir),
            "--report",
            str(report_path),
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
    unmasked = mask == 0
    before = np.median(np.abs(target[unmasked] - ref[unmasked]))
    after = np.median(np.abs(corrected[unmasked] - ref[unmasked]))

    assert after < before * 0.02
    assert (diag_dir / "scale_map.fits").exists()
    assert (diag_dir / "offset_map.fits").exists()
    assert report_path.exists()
    assert not (diag_dir / "ref_background.fits").exists()
    assert not (diag_dir / "target_background.fits").exists()

    subprocess.run(
        [
            str(LNC_BINARY),
            "--mask",
            str(mask_path),
            "--diag-dir",
            str(diag_dir),
            "--save-backgrounds",
            "--grid-spacing",
            "64",
            "--window-size",
            "96",
            "--min-samples",
            "1000",
            str(ref_path),
            str(target_path),
            str(tmp_path / "corrected_with_backgrounds.fit"),
        ],
        check=True,
    )
    assert (diag_dir / "ref_background.fits").exists()
    assert (diag_dir / "target_background.fits").exists()


def test_c_trimmed_median_core_uses_median_fallback(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR)], check=True)

    height = 128
    width = 128
    yy, xx = np.mgrid[0:height, 0:width]
    ref = np.full((height, width), 1000.0, dtype=np.float32)
    target = np.full((height, width), 900.0, dtype=np.float32)

    # A constant target forces the local affine fit into its fallback path. The
    # median estimator should ignore this skew in the reference background.
    outlier_region = (xx < 24) & (yy < 96)
    ref[outlier_region] = 5000.0
    mask = np.zeros((height, width), dtype=np.uint8)

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected_trimmed_median.fit"
    diag_dir = tmp_path / "diag_trimmed_median"
    diag_dir.mkdir()
    report_path = diag_dir / "report.json"

    fits.writeto(ref_path, ref, overwrite=True)
    fits.writeto(target_path, target, overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_TRIMMED_MEDIAN_BINARY),
            "--mask",
            str(mask_path),
            "--diag-dir",
            str(diag_dir),
            "--save-backgrounds",
            "--report",
            str(report_path),
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
    normal_region = ~outlier_region
    before = np.median(np.abs(target[normal_region] - ref[normal_region]))
    after = np.median(np.abs(corrected[normal_region] - ref[normal_region]))

    assert after < before * 0.01
    assert np.median(fits.getdata(diag_dir / "ref_background.fits")) == pytest.approx(1000.0)
    assert np.median(fits.getdata(diag_dir / "target_background.fits")) == pytest.approx(900.0)
    assert json.loads(report_path.read_text(encoding="utf-8"))["background_estimator"] == "trimmed-median"


def test_c_v2_core_corrects_affine_with_sample_medians(tmp_path: Path):
    subprocess.run(["make", "-C", str(LNC_DIR)], check=True)

    height = 256
    width = 256
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ref = 900.0 + 0.18 * xx + 0.11 * yy
    scale = 1.12
    offset = -41.0
    target = (ref - offset) / scale

    mask = np.zeros((height, width), dtype=np.uint8)
    rr = (xx - 128) ** 2 + (yy - 128) ** 2
    ref[rr < 42**2] += 450.0
    target[rr < 42**2] += 450.0 / scale

    ref_path = tmp_path / "ref.fit"
    target_path = tmp_path / "target.fit"
    mask_path = tmp_path / "mask.fit"
    out_path = tmp_path / "corrected_v2.fit"
    diag_dir = tmp_path / "diag_v2"
    diag_dir.mkdir()

    fits.writeto(ref_path, ref.astype(np.float32), overwrite=True)
    fits.writeto(target_path, target.astype(np.float32), overwrite=True)
    fits.writeto(mask_path, mask, overwrite=True)

    subprocess.run(
        [
            str(LNC_V2_BINARY),
            "--mask",
            str(mask_path),
            "--diag-dir",
            str(diag_dir),
            "--grid-spacing",
            "64",
            "--window-size",
            "128",
            "--sample-patch-size",
            "9",
            "--sample-stride",
            "16",
            "--min-patches",
            "4",
            str(ref_path),
            str(target_path),
            str(out_path),
        ],
        check=True,
    )

    corrected = fits.getdata(out_path)
    background_region = rr > 55**2
    before = np.median(np.abs(target[background_region] - ref[background_region]))
    after = np.median(np.abs(corrected[background_region] - ref[background_region]))

    assert after < before * 0.05
    assert (diag_dir / "accepted_patch_fraction.fits").exists()


def test_write_masked_intermediate_fits(tmp_path: Path):
    module = load_wrapper_module()
    reference = tmp_path / "reference.fit"
    target = tmp_path / "target.fit"
    mask = tmp_path / "mask.fit"
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()

    header = fits.Header()
    header["OBJECT"] = "Masked Test"
    fits.writeto(reference, np.arange(9, dtype=np.float32).reshape(3, 3), header=header, overwrite=True)
    fits.writeto(target, (np.arange(9, dtype=np.float32) + 10).reshape(3, 3), header=header, overwrite=True)
    fits.writeto(mask, np.array([[0, 1, 0], [0, 0, 0], [1, 0, 0]], dtype=np.uint8), overwrite=True)

    outputs = module.write_intermediate_masked_fits(
        reference_fits=reference,
        target_fits=target,
        mask_path=mask,
        diag_dir=diag_dir,
    )

    ref_masked = fits.getdata(outputs["reference_masked"])
    target_masked = fits.getdata(outputs["target_masked"])
    assert np.isnan(ref_masked[0, 1])
    assert np.isnan(target_masked[2, 0])
    assert ref_masked[1, 1] == pytest.approx(4 / 65535.0)
    assert target_masked[1, 1] == pytest.approx(14 / 65535.0)
    with fits.open(outputs["reference_masked"], memmap=False) as hdul:
        assert hdul[0].header["OBJECT"] == "Masked Test"
        assert hdul[0].header["LNCMASKD"]
        assert hdul[0].header["LNCNORM"]


def test_apply_output_format_uint16_and_float32(tmp_path: Path):
    module = load_wrapper_module()
    source = tmp_path / "source.fit"
    data = np.array([[-10.0, 0.0, 32768.4], [65535.0, 70000.0, np.nan]], dtype=np.float32)

    fits.writeto(source, data, overwrite=True)
    module.apply_output_format(source, "uint16")
    with fits.open(source, memmap=False) as hdul:
        assert hdul[0].data.dtype == np.uint16
        assert hdul[0].data.min() == 0
        assert hdul[0].data.max() == 65535
        assert hdul[0].data[0, 2] == 32768
        assert hdul[0].header["LNCFMT"] == "uint16"

    fits.writeto(source, data, overwrite=True)
    module.apply_output_format(source, "float32")
    with fits.open(source, memmap=False) as hdul:
        assert hdul[0].data.dtype.kind == "f"
        assert hdul[0].data.min() == 0.0
        assert hdul[0].data.max() == 1.0
        assert hdul[0].data[0, 2] == pytest.approx(32768.4 / 65535.0)
        assert hdul[0].header["LNCFMT"] == "float32"


def test_apply_output_format_preserves_normalized_float_scale(tmp_path: Path):
    module = load_wrapper_module()
    source = tmp_path / "source_normalized.fit"
    data = np.array([[-0.1, 0.0, 0.25], [0.75, 1.0, 1.2]], dtype=np.float32)

    fits.writeto(source, data, overwrite=True)
    module.apply_output_format(source, "float32", module.VALUE_SCALE_NORMALIZED)

    with fits.open(source, memmap=False) as hdul:
        assert hdul[0].data.dtype.kind == "f"
        assert hdul[0].data[0, 0] == pytest.approx(0.0)
        assert hdul[0].data[0, 2] == pytest.approx(0.25)
        assert hdul[0].data[1, 0] == pytest.approx(0.75)
        assert hdul[0].data[1, 2] == pytest.approx(1.0)
        assert hdul[0].header["LNCVSCL"] == module.VALUE_SCALE_NORMALIZED


def test_normalize_core_adu_diagnostics(tmp_path: Path):
    module = load_wrapper_module()
    diag_dir = tmp_path / "diag"
    diag_dir.mkdir()
    for name in ("offset_map.fits", "ref_background.fits", "target_background.fits"):
        fits.writeto(diag_dir / name, np.array([[-1.0, 32767.5, 70000.0]], dtype=np.float32), overwrite=True)

    outputs = module.normalize_core_adu_diagnostics(diag_dir, save_backgrounds=True)

    assert set(outputs) == {"offset_map", "ref_background", "target_background"}
    for name, path in outputs.items():
        with fits.open(path, memmap=False) as hdul:
            assert hdul[0].data.dtype.kind == "f"
            assert float(np.nanmin(hdul[0].data)) == 0.0
            assert float(np.nanmax(hdul[0].data)) == 1.0
            assert hdul[0].header["LNCNORM"]
            if name == "offset_map":
                assert hdul[0].data[0, 1] == pytest.approx((32767.5 + 1.0) / 70001.0)
                assert hdul[0].header["LNCVMIN"] == pytest.approx(-1.0)
                assert hdul[0].header["LNCVMAX"] == pytest.approx(70000.0)
            else:
                assert hdul[0].data[0, 1] == pytest.approx(32767.5 / 65535.0)


def test_normalize_core_diagnostics_normalizes_scale_map(tmp_path: Path):
    module = load_wrapper_module()
    diag_dir = tmp_path / "diag_scale"
    diag_dir.mkdir()
    fits.writeto(diag_dir / "scale_map.fits", np.array([[0.8, 1.0, 1.2]], dtype=np.float32), overwrite=True)
    fits.writeto(diag_dir / "offset_map.fits", np.array([[-0.005, -0.0025, 0.0]], dtype=np.float32), overwrite=True)

    outputs = module.normalize_core_diagnostics(
        diag_dir,
        save_backgrounds=False,
        value_scale=module.VALUE_SCALE_NORMALIZED,
    )

    assert set(outputs) == {"scale_map", "offset_map"}
    with fits.open(outputs["scale_map"], memmap=False) as hdul:
        assert float(np.nanmin(hdul[0].data)) == pytest.approx(0.0)
        assert float(np.nanmax(hdul[0].data)) == pytest.approx(1.0)
        assert hdul[0].header["LNCNORM"]
        assert hdul[0].header["LNCVMIN"] == pytest.approx(0.8)
        assert hdul[0].header["LNCVMAX"] == pytest.approx(1.2)
    with fits.open(outputs["offset_map"], memmap=False) as hdul:
        assert float(np.nanmin(hdul[0].data)) == pytest.approx(0.0)
        assert float(np.nanmax(hdul[0].data)) == pytest.approx(1.0)
        assert hdul[0].data[0, 1] == pytest.approx(0.5)
        assert hdul[0].header["LNCNORM"]
        assert hdul[0].header["LNCVMIN"] == pytest.approx(-0.005)
        assert hdul[0].header["LNCVMAX"] == pytest.approx(0.0)


def test_preserve_header_and_add_lnc_metadata(tmp_path: Path):
    module = load_wrapper_module()
    target = tmp_path / "target.fit"
    reference = tmp_path / "reference.fit"
    output = tmp_path / "corrected.fit"
    core_report = tmp_path / "report.json"

    header = fits.Header()
    header["OBJECT"] = "NGC 6995"
    header["FILTER"] = "O"
    header["EXPTIME"] = 600.0
    data = np.arange(100, dtype=np.float32).reshape(10, 10)
    target_hdu = fits.PrimaryHDU(data.astype(np.int16), header=header)
    target_hdu.scale("int16", bzero=32768)
    target_hdu.writeto(target, overwrite=True)
    fits.writeto(reference, data + 1, overwrite=True)
    fits.writeto(output, data + 2, overwrite=True)
    core_report.write_text("{}", encoding="utf-8")

    module.preserve_header_and_add_lnc_metadata(
        output_path=output,
        target_fits=target,
        reference_source=reference,
        target_source=target,
        reference_fits=reference,
        parameters={"grid_spacing": 64, "window_size": 128},
        mask_stats={"total_masked_pixels": 12, "masked_fraction": 0.12},
        core_report=core_report,
    )

    with fits.open(output, memmap=False) as hdul:
        assert np.array_equal(hdul[0].data, data + 2)
        assert hdul[0].header["OBJECT"] == "NGC 6995"
        assert hdul[0].header["FILTER"] == "O"
        assert hdul[0].header["LNCVRS"] == "1"
        assert hdul[0].header["LNCREF"] == reference.name
        assert hdul[0].header["LNCTARG"] == target.name
        assert hdul[0].header["LNCMASK"] == 12
        assert "BZERO" not in hdul[0].header
        assert "BSCALE" not in hdul[0].header
