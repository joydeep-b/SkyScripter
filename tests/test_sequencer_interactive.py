import json

from sky_scripter import sequencer


def _feed(monkeypatch, values):
    iterator = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(iterator))


def test_interactive_create_named_project(monkeypatch, tmp_path):
    output = tmp_path / "named_project.json"
    _feed(monkeypatch, [
        "1",       # named target
        "M31",
        "L,R",
        "120",
        "5",
        "56",
        "20",
        "5",
        "30",
        "15",
        "40",
        "3",
        "1",
        str(output),
    ])

    assert sequencer.interactive_create_project() == 0

    data = json.loads(output.read_text())
    target = data["targets"][0]
    assert target["target"] == "M31"
    assert "wcs" not in target
    assert target["filters"]["L"]["exposure"] == 120.0
    assert target["filters"]["R"]["target_frames"] == 5
    assert target["max_moon_altitude"] == 15.0
    assert target["max_moon_phase"] == 40.0


def test_interactive_create_wcs_project(monkeypatch, tmp_path):
    output = tmp_path / "wcs_project.json"
    _feed(monkeypatch, [
        "2",                    # WCS coordinates
        "5:35:17 -5:23:24",
        "Orion_Nebula_WCS",
        "Ha,OIII",
        "300",
        "10",
        "56",
        "20",
        "5",
        "25",
        "",
        "",
        "2",
        "0",
        str(output),
    ])

    assert sequencer.interactive_create_project() == 0

    data = json.loads(output.read_text())
    target = data["targets"][0]
    assert target["wcs"] == "5:35:17 -5:23:24"
    assert "target" not in target
    assert target["name"] == "Orion_Nebula_WCS"
    assert sorted(target["filters"]) == ["Ha", "OIII"]
    assert "max_moon_altitude" not in target
    assert "max_moon_phase" not in target


def test_parse_no_args_uses_interactive_dispatch():
    args = sequencer.parse_args([])

    assert callable(args.func)
