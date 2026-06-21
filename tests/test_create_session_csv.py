from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from astropy.io import fits


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "analysis" / "create_session_csv.py"


def load_session_csv_module():
    spec = importlib.util.spec_from_file_location("create_session_csv", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fits(path: Path, header_values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = fits.Header()
    for key, value in header_values.items():
        header[key] = value
    fits.PrimaryHDU(header=header).writeto(path, overwrite=True)


def _write_xisf(path: Path, properties: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("xisf")
    image = ET.SubElement(root, "Image")
    for property_id, value in properties.items():
        ET.SubElement(
            image,
            "Property",
            {"id": property_id, "value": str(value)},
        )

    header_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path.write_bytes(
        b"XISF0100"
        + len(header_xml).to_bytes(4, byteorder="little", signed=False)
        + b"\0\0\0\0"
        + header_xml
    )


def test_xisf_alias_metadata_is_discovered_and_aggregated(tmp_path: Path) -> None:
    module = load_session_csv_module()
    for index in range(2):
        _write_xisf(
            tmp_path / f"light_{index}.xisf",
            {
                "Observation:Time:Start": "2026-01-02T03:04:05.123Z",
                "Instrument:Filter:Name": "'Ha'",
                "Observation:ExposureTime": "180",
                "CCD:Gain": "100",
                "CCD:Temperature": "10",
                "Focus:Temperature": "12.5",
            },
        )

    sessions = module.get_session_data(tmp_path)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.date.isoformat() == "2026-01-02"
    assert session.filter == module.default_filter_lookup["H"]
    assert session.number == 2
    assert session.duration == 180.0
    assert session.gain == "100"
    assert session.sensorCooling == 10
    assert session.temperature == 12.5


def test_fits_keyword_behavior_is_unchanged(tmp_path: Path) -> None:
    module = load_session_csv_module()
    _write_fits(
        tmp_path / "light.fits",
        {
            "DATE-OBS": "2026-02-03T04:05:06",
            "FILTER": "R",
            "EXPTIME": 120.0,
            "GAIN": 56,
            "CCD-TEMP": 8.0,
            "FOCUSTEM": 13.25,
        },
    )

    sessions = module.get_session_data(tmp_path)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.date.isoformat() == "2026-02-03"
    assert session.filter == module.default_filter_lookup["R"]
    assert session.number == 1
    assert session.duration == 120.0
    assert session.gain == 56
    assert session.sensorCooling == 8
    assert session.temperature == 13.25


def test_discover_image_files_finds_mixed_suffixes_and_skips_hidden(tmp_path: Path) -> None:
    module = load_session_csv_module()
    visible_paths = [
        tmp_path / "a.fit",
        tmp_path / "nested" / "b.fits",
        tmp_path / "nested" / "c.xisf",
    ]
    for path in visible_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")

    hidden_paths = [
        tmp_path / ".hidden.fit",
        tmp_path / ".cache" / "d.fits",
        tmp_path / "nested" / ".private" / "e.xisf",
    ]
    for path in hidden_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")

    discovered = module.discover_image_files(tmp_path)

    assert discovered == sorted(visible_paths)


def test_unknown_filter_still_falls_back_to_h(tmp_path: Path, capsys) -> None:
    module = load_session_csv_module()
    _write_fits(
        tmp_path / "unknown_filter.fit",
        {
            "DATE-OBS": "2026-03-04T05:06:07",
            "FILTER": "Clear",
            "EXPTIME": 60.0,
            "GAIN": 100,
        },
    )

    sessions = module.get_session_data(tmp_path)

    assert len(sessions) == 1
    assert sessions[0].filter == module.default_filter_lookup["H"]
    assert "Warning: Unknown filter 'Clear'" in capsys.readouterr().out
