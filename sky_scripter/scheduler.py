import math

from astropy.coordinates import SkyCoord, get_body, AltAz, EarthLocation
from astropy.time import Time, TimeDelta
import astropy.units as u

from sky_scripter.util import lookup_object_coordinates


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
        if ok and session.max_moon_altitude is not None and moon_alts[i] > session.max_moon_altitude:
          ok = False
        if ok and session.max_moon_phase is not None and self._moon_phase > session.max_moon_phase:
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
            'start_local': eligible_start.to_datetime().strftime('%H:%M'),
            'end_local': eligible_end.to_datetime().strftime('%H:%M'),
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
          'start_local': eligible_start.to_datetime().strftime('%H:%M'),
          'end_local': eligible_end.to_datetime().strftime('%H:%M'),
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
      if session.max_moon_altitude is not None:
        moon_alt = moon.transform_to(altaz).alt.deg
        if moon_alt > session.max_moon_altitude:
          return False
      if session.max_moon_phase is not None and self._moon_phase is not None:
        if self._moon_phase > session.max_moon_phase:
          return False

    return True

  def _find_end_time(self, idx):
    """Find the end of the current eligibility window for session idx."""
    for slot in self._timeline:
      if slot['index'] == idx:
        end = Time(slot['end_iso'])
        if end > Time.now():
          return end
    return None

  def pick_next(self, completed):
    """Pick the eligible, not-yet-completed session whose window ends soonest."""
    candidates = []
    for idx in range(len(self.plan.sessions)):
      if idx in completed:
        continue
      if not self.is_eligible_now(idx):
        continue
      end_time = self._find_end_time(idx)
      candidates.append((end_time, idx))

    if not candidates:
      return None, -1

    candidates.sort(key=lambda c: c[0] if c[0] is not None else Time('2099-01-01'))
    _, best_idx = candidates[0]
    return self.plan.sessions[best_idx], best_idx

  def get_coordinates(self, idx):
    return self._coords[idx]
