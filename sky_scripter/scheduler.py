import math
from datetime import datetime

from astropy.coordinates import SkyCoord, get_body, AltAz, EarthLocation
from astropy.time import Time, TimeDelta
import astropy.units as u

from sky_scripter.util import lookup_object_coordinates


LOCAL_TZ = datetime.now().astimezone().tzinfo


def _format_local_time(t: Time) -> str:
  return t.to_datetime(timezone=LOCAL_TZ).strftime('%H:%M')


class NightScheduler:
  """Precomputes per-session eligibility windows across a night and provides
  dynamic pick_next() scheduling based on current conditions."""

  def __init__(self, plan, dark_start, dark_end, location):
    self.plan = plan
    self.dark_start = dark_start
    self.dark_end = dark_end
    self.location = location
    self._coords = []
    self._timeline = []
    self._moon_phase = None
    self._resolve_coordinates()

  def _resolve_coordinates(self):
    for session in self.plan.sessions:
      if session.target is not None:
        ra, dec = lookup_object_coordinates(session.target)
      else:
        parts = session.wcs.split()
        c = SkyCoord(parts[0], parts[1], unit=(u.hour, u.deg))
        ra, dec = c.ra.hour, c.dec.deg
      self._coords.append((ra, dec))

  def precompute(self, step_minutes=5):
    night_start = self.dark_start + TimeDelta(-60 * 60, format='sec')
    night_end = self.dark_end + TimeDelta(60 * 60, format='sec')
    n_steps = int((night_end - night_start).sec / (step_minutes * 60)) + 1
    times = night_start + TimeDelta([i * step_minutes * 60 for i in range(n_steps)], format='sec')

    moon_positions = get_body('moon', times)
    sun = get_body('sun', times[len(times) // 2])
    elongation = moon_positions[len(times) // 2].separation(sun).deg
    self._moon_phase = (1 - math.cos(math.radians(elongation))) / 2 * 100

    altaz_frame = AltAz(obstime=times, location=self.location)
    moon_alts = moon_positions.transform_to(altaz_frame).alt.deg

    self._timeline = []
    for idx, session in enumerate(self.plan.sessions):
      ra, dec = self._coords[idx]
      target = SkyCoord(ra * u.hour, dec * u.deg)
      target_alts = target.transform_to(altaz_frame).alt.deg

      window_start = self.dark_start + TimeDelta(session.start_offset * 60, format='sec')
      window_end = self.dark_end + TimeDelta(session.end_offset * 60, format='sec')

      eligible_start = None
      eligible_end = None
      for i, t in enumerate(times):
        ok = True
        if t < window_start or t > window_end:
          ok = False
        if ok and session.min_altitude and target_alts[i] < session.min_altitude:
          ok = False
        if ok and not self._moon_constraints_ok(session, moon_alts[i],
                                                self._moon_phase):
          ok = False

        if ok and eligible_start is None:
          eligible_start = t
        if ok:
          eligible_end = t
        if not ok and eligible_start is not None:
          self._timeline.append({
            'index': idx,
            'target': session.target or session.wcs,
            'filters': ''.join(n for n, _, _ in session.filters),
            'start_iso': eligible_start.iso,
            'end_iso': eligible_end.iso,
            'start_local': _format_local_time(eligible_start),
            'end_local': _format_local_time(eligible_end),
          })
          eligible_start = None
          eligible_end = None

      if eligible_start is not None:
        self._timeline.append({
          'index': idx,
          'target': session.target or session.wcs,
          'filters': ''.join(n for n, _, _ in session.filters),
          'start_iso': eligible_start.iso,
          'end_iso': eligible_end.iso,
          'start_local': _format_local_time(eligible_start),
          'end_local': _format_local_time(eligible_end),
        })

  def get_timeline(self):
    return list(self._timeline)

  def is_eligible_now(self, idx):
    session = self.plan.sessions[idx]
    now = Time.now()

    if self.dark_start is not None:
      window_start = self.dark_start + TimeDelta(session.start_offset * 60, format='sec')
      window_end = self.dark_end + TimeDelta(session.end_offset * 60, format='sec')
      if now < window_start or now > window_end:
        return False

    ra, dec = self._coords[idx]
    target = SkyCoord(ra * u.hour, dec * u.deg)
    altaz = AltAz(obstime=now, location=self.location)
    target_alt = target.transform_to(altaz).alt.deg
    if session.min_altitude and target_alt < session.min_altitude:
      return False

    if session.max_moon_altitude is not None or session.max_moon_phase is not None:
      moon = get_body('moon', now)
      moon_alt = moon.transform_to(altaz).alt.deg
      if self._moon_phase is None and session.max_moon_phase is not None:
        sun = get_body('sun', now)
        elongation = moon.separation(sun).deg
        self._moon_phase = (1 - math.cos(math.radians(elongation))) / 2 * 100
      if not self._moon_constraints_ok(session, moon_alt, self._moon_phase):
        return False

    return True

  @staticmethod
  def _moon_constraints_ok(session, moon_altitude: float | None,
                           moon_phase: float | None) -> bool:
    """Evaluate optional moon constraints.

    With one moon constraint, that constraint must pass. With both max altitude
    and max phase specified, either passing is sufficient.
    """
    has_alt = session.max_moon_altitude is not None
    has_phase = session.max_moon_phase is not None
    if not has_alt and not has_phase:
      return True

    alt_ok = has_alt and moon_altitude is not None and \
        moon_altitude <= session.max_moon_altitude
    phase_ok = has_phase and moon_phase is not None and \
        moon_phase <= session.max_moon_phase

    if has_alt and has_phase:
      return alt_ok or phase_ok
    return alt_ok if has_alt else phase_ok

  def _find_end_time(self, idx):
    """Find the end of the current eligibility window for session idx."""
    for slot in self._timeline:
      if slot['index'] == idx:
        end = Time(slot['end_iso'])
        if end > Time.now():
          return end
    return None

  def pick_next(self, completed, remaining_work: dict[int, int] | None = None):
    """Pick an eligible session.

    Targets with explicit remaining work are skipped when their remaining count is
    zero. Among eligible targets, the soonest-ending window wins, then higher
    project priority, then larger remaining frame count.
    """
    candidates = []
    for idx in range(len(self.plan.sessions)):
      if idx in completed:
        continue
      remaining = remaining_work.get(idx) if remaining_work is not None else \
          getattr(self.plan.sessions[idx], 'remaining_frames', None)
      if remaining is not None and remaining <= 0:
        continue
      if not self.is_eligible_now(idx):
        continue
      end_time = self._find_end_time(idx)
      priority = getattr(self.plan.sessions[idx], 'project_priority', 0)
      remaining_sort = remaining if remaining is not None else \
          self.plan.sessions[idx].total_frames()
      candidates.append((end_time, -priority, -remaining_sort, idx))

    if not candidates:
      return None, -1

    candidates.sort(key=lambda c: (
      c[0] if c[0] is not None else Time('2099-01-01'), c[1], c[2]))
    _, _, _, best_idx = candidates[0]
    return self.plan.sessions[best_idx], best_idx

  def get_coordinates(self, idx):
    return self._coords[idx]
