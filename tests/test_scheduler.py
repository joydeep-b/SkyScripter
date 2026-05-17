from unittest.mock import patch

from astropy.coordinates import EarthLocation
from astropy.time import Time, TimeDelta
import astropy.units as u

from sky_scripter.sequence import ImagingSession, NightPlan
from sky_scripter.scheduler import NightScheduler


def _make_plan():
    """Two sessions: one high-altitude target, one with moon constraint."""
    plan = NightPlan(latitude=30.0, longitude=-97.0, elevation=200)
    plan.add(ImagingSession("High", L=(300, 10), min_altitude=30))
    plan.add(ImagingSession("Low", Ha=(300, 10), min_altitude=0))
    return plan


def _make_scheduler(plan):
    dark_start = Time('2026-04-20 02:00:00', scale='utc')
    dark_end = Time('2026-04-20 10:00:00', scale='utc')
    location = EarthLocation.from_geodetic(-97.0 * u.deg, 30.0 * u.deg, 200 * u.m)
    mock_coords = [(6.0, 45.0), (12.0, -10.0)]
    with patch.object(NightScheduler, '_resolve_coordinates') as mock_resolve:
        scheduler = NightScheduler(plan, dark_start, dark_end, location)
        scheduler._coords = mock_coords
    return scheduler


def test_precompute_produces_timeline():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    timeline = scheduler.get_timeline()
    assert len(timeline) > 0
    for slot in timeline:
        assert 'index' in slot
        assert 'target' in slot
        assert 'filters' in slot
        assert 'start_local' in slot
        assert 'end_local' in slot
        assert slot['index'] in (0, 1)


def test_pick_next_returns_eligible():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    # Mock is_eligible_now to make both eligible
    with patch.object(scheduler, 'is_eligible_now', return_value=True):
        with patch.object(scheduler, '_find_end_time') as mock_end:
            mock_end.side_effect = lambda idx: (
                Time('2026-04-20 05:00:00') if idx == 0
                else Time('2026-04-20 08:00:00'))
            session, idx = scheduler.pick_next(set())
            assert session is not None
            assert idx == 0  # ends sooner


def test_pick_next_skips_completed():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    with patch.object(scheduler, 'is_eligible_now', return_value=True):
        with patch.object(scheduler, '_find_end_time',
                          return_value=Time('2026-04-20 08:00:00')):
            session, idx = scheduler.pick_next({0})
            assert idx == 1


def test_pick_next_returns_none_when_all_completed():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    session, idx = scheduler.pick_next({0, 1})
    assert session is None
    assert idx == -1


def test_pick_next_skips_zero_remaining_work():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    with patch.object(scheduler, 'is_eligible_now', return_value=True):
        with patch.object(scheduler, '_find_end_time',
                          return_value=Time('2026-04-20 08:00:00')):
            session, idx = scheduler.pick_next(set(), remaining_work={0: 0, 1: 2})
            assert idx == 1


def test_pick_next_uses_priority_when_windows_match():
    plan = _make_plan()
    plan.sessions[0].project_priority = 1
    plan.sessions[1].project_priority = 5
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    with patch.object(scheduler, 'is_eligible_now', return_value=True):
        with patch.object(scheduler, '_find_end_time',
                          return_value=Time('2026-04-20 08:00:00')):
            session, idx = scheduler.pick_next(set(), remaining_work={0: 5, 1: 5})
            assert idx == 1


def test_pick_next_returns_none_when_none_eligible():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    with patch.object(scheduler, 'is_eligible_now', return_value=False):
        session, idx = scheduler.pick_next(set())
        assert session is None
        assert idx == -1


def test_get_coordinates():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    assert scheduler.get_coordinates(0) == (6.0, 45.0)
    assert scheduler.get_coordinates(1) == (12.0, -10.0)


def test_timeline_has_correct_targets():
    plan = _make_plan()
    scheduler = _make_scheduler(plan)
    scheduler.precompute()
    timeline = scheduler.get_timeline()
    targets_seen = {slot['target'] for slot in timeline}
    # At least one of our targets should appear
    assert len(targets_seen) > 0
    for t in targets_seen:
        assert t in ('High', 'Low')


def test_moon_constraints_single_altitude():
    session = ImagingSession("M31", L=(300, 1), max_moon_altitude=0)

    assert NightScheduler._moon_constraints_ok(session, moon_altitude=-5,
                                               moon_phase=90)
    assert not NightScheduler._moon_constraints_ok(session, moon_altitude=10,
                                                   moon_phase=0)


def test_moon_constraints_single_phase():
    session = ImagingSession("M31", L=(300, 1), max_moon_phase=25)

    assert NightScheduler._moon_constraints_ok(session, moon_altitude=80,
                                               moon_phase=10)
    assert not NightScheduler._moon_constraints_ok(session, moon_altitude=-10,
                                                   moon_phase=80)


def test_moon_constraints_altitude_or_phase_when_both_specified():
    session = ImagingSession("M31", L=(300, 1), max_moon_altitude=0,
                             max_moon_phase=25)

    assert NightScheduler._moon_constraints_ok(session, moon_altitude=-5,
                                               moon_phase=90)
    assert NightScheduler._moon_constraints_ok(session, moon_altitude=60,
                                               moon_phase=10)
    assert not NightScheduler._moon_constraints_ok(session, moon_altitude=60,
                                                   moon_phase=90)
