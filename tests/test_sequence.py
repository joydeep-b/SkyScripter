import tempfile
import os

import pytest

from sky_scripter.sequence import ImagingSession, NightPlan


def test_basic_session():
    s = ImagingSession("M31", L=(300, 20), R=(300, 10))
    assert s.target == "M31"
    assert len(s.filters) == 2
    assert s.total_frames() == 30
    assert s.total_exposure_time() == 300 * 20 + 300 * 10


def test_wcs_session():
    s = ImagingSession(wcs="5:35:17 -5:23:24", L=(60, 5))
    assert s.wcs == "5:35:17 -5:23:24"
    assert s.target is None


def test_mutual_exclusion():
    with pytest.raises(ValueError, match="both"):
        ImagingSession("M31", wcs="5:35:17 -5:23:24")
    with pytest.raises(ValueError, match="either"):
        ImagingSession()


def test_unknown_filter():
    with pytest.raises(ValueError, match="Unknown filter.*X"):
        ImagingSession("M31", X=(300, 10))


def test_sequence_steps():
    s = ImagingSession("M42", L=(60, 2), R=(120, 1))
    steps = list(s.sequence_steps())
    assert len(steps) == 3
    # L frames come first (order follows kwargs insertion order)
    assert steps[0] == ("L", 60, 56, 20, 5)
    assert steps[1] == ("L", 60, 56, 20, 5)
    assert steps[2] == ("R", 120, 56, 20, 5)


def test_to_dict_from_dict():
    original = ImagingSession("M31", gain=100, offset=30, mode=3,
                              min_altitude=25, dither_every=2,
                              L=(300, 20), Ha=(600, 10))
    d = original.to_dict()
    restored = ImagingSession.from_dict(d)
    assert restored.target == original.target
    assert restored.wcs == original.wcs
    assert restored.gain == original.gain
    assert restored.offset == original.offset
    assert restored.mode == original.mode
    assert restored.min_altitude == original.min_altitude
    assert restored.dither_every == original.dither_every
    assert restored.total_frames() == original.total_frames()
    assert restored.total_exposure_time() == original.total_exposure_time()


def test_night_plan_save_load():
    plan = NightPlan()
    plan.add(ImagingSession("M31", L=(300, 20)))
    plan.add(ImagingSession("M42", R=(120, 10), Ha=(600, 5)))

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        plan.save(path)
        loaded = NightPlan.load(path)
        assert len(loaded.sessions) == 2
        assert loaded.sessions[0].target == "M31"
        assert loaded.sessions[1].target == "M42"
        assert loaded.sessions[0].total_frames() == 20
        assert loaded.sessions[1].total_frames() == 15
    finally:
        os.unlink(path)


def test_defaults():
    s = ImagingSession("M31", L=(60, 1))
    assert s.gain == 56
    assert s.offset == 20
    assert s.mode == 5
    assert s.dither_every == 1
    assert s.min_altitude == 0
    assert s.max_moon_altitude is None
    assert s.max_moon_phase is None
    assert s.start_offset == 0
    assert s.end_offset == 0


def test_moon_constraints():
    s = ImagingSession("M31", L=(300, 20), max_moon_altitude=30, max_moon_phase=50)
    assert s.max_moon_altitude == 30
    assert s.max_moon_phase == 50
    d = s.to_dict()
    assert d['max_moon_altitude'] == 30
    assert d['max_moon_phase'] == 50
    restored = ImagingSession.from_dict(d)
    assert restored.max_moon_altitude == 30
    assert restored.max_moon_phase == 50


def test_moon_constraints_omitted():
    s = ImagingSession("M31", L=(300, 20))
    d = s.to_dict()
    assert 'max_moon_altitude' not in d
    assert 'max_moon_phase' not in d
    restored = ImagingSession.from_dict(d)
    assert restored.max_moon_altitude is None
    assert restored.max_moon_phase is None


def test_time_offsets():
    s = ImagingSession("M31", L=(300, 20), start_offset=-30, end_offset=15)
    assert s.start_offset == -30
    assert s.end_offset == 15
    d = s.to_dict()
    assert d['start_offset'] == -30
    assert d['end_offset'] == 15
    restored = ImagingSession.from_dict(d)
    assert restored.start_offset == -30
    assert restored.end_offset == 15


def test_time_offsets_omitted():
    s = ImagingSession("M31", L=(300, 20))
    d = s.to_dict()
    assert 'start_offset' not in d
    assert 'end_offset' not in d
    restored = ImagingSession.from_dict(d)
    assert restored.start_offset == 0
    assert restored.end_offset == 0


def test_night_plan_location():
    plan = NightPlan(latitude=30.5, longitude=-97.8, elevation=200)
    plan.add(ImagingSession("M31", L=(300, 20)))
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        plan.save(path)
        loaded = NightPlan.load(path)
        assert loaded.latitude == 30.5
        assert loaded.longitude == -97.8
        assert loaded.elevation == 200
        assert len(loaded.sessions) == 1
    finally:
        os.unlink(path)
