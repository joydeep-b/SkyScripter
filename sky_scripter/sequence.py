import json

KNOWN_FILTERS = {'L', 'R', 'G', 'B', 'Ha', 'OIII', 'SII', 'S', 'H', 'O'}

class ImagingSession:
  def __init__(self, target=None, *, wcs=None, gain=56, offset=20, mode=5,
               min_altitude=0, dither_every=1,
               max_moon_altitude=None, max_moon_phase=None,
               start_offset=0, end_offset=0, **kwargs):
    if target is None and wcs is None:
      raise ValueError("Must specify either 'target' or 'wcs'")
    if target is not None and wcs is not None:
      raise ValueError("Cannot specify both 'target' and 'wcs'")
    self.target = target
    self.wcs = wcs
    self.gain = gain
    self.offset = offset
    self.mode = mode
    self.min_altitude = min_altitude
    self.dither_every = dither_every
    self.max_moon_altitude = max_moon_altitude
    self.max_moon_phase = max_moon_phase
    self.start_offset = start_offset
    self.end_offset = end_offset
    self.filters = []
    for name, value in kwargs.items():
      if name not in KNOWN_FILTERS:
        raise ValueError(f"Unknown filter: '{name}'")
      self.filters.append((name, value[0], value[1]))

  def sequence_steps(self):
    for name, exposure, count in self.filters:
      for _ in range(count):
        yield (name, exposure, self.gain, self.offset, self.mode)

  def total_exposure_time(self):
    return sum(exposure * count for _, exposure, count in self.filters)

  def total_frames(self):
    return sum(count for _, _, count in self.filters)

  def to_dict(self):
    d = {}
    if self.target is not None:
      d['target'] = self.target
    if self.wcs is not None:
      d['wcs'] = self.wcs
    d['sequences'] = {name: [exposure, count] for name, exposure, count in self.filters}
    d['gain'] = self.gain
    d['offset'] = self.offset
    d['mode'] = self.mode
    d['min_altitude'] = self.min_altitude
    d['dither_every'] = self.dither_every
    if self.max_moon_altitude is not None:
      d['max_moon_altitude'] = self.max_moon_altitude
    if self.max_moon_phase is not None:
      d['max_moon_phase'] = self.max_moon_phase
    if self.start_offset != 0:
      d['start_offset'] = self.start_offset
    if self.end_offset != 0:
      d['end_offset'] = self.end_offset
    return d

  @classmethod
  def from_dict(cls, d):
    target = d.get('target')
    wcs = d.get('wcs')
    filters = {name: tuple(val) for name, val in d.get('sequences', {}).items()}
    return cls(
      target=target, wcs=wcs,
      gain=d.get('gain', 56), offset=d.get('offset', 20), mode=d.get('mode', 5),
      min_altitude=d.get('min_altitude', 0), dither_every=d.get('dither_every', 1),
      max_moon_altitude=d.get('max_moon_altitude'),
      max_moon_phase=d.get('max_moon_phase'),
      start_offset=d.get('start_offset', 0),
      end_offset=d.get('end_offset', 0),
      **filters,
    )


class NightPlan:
  def __init__(self, latitude=None, longitude=None, elevation=0):
    self.sessions = []
    self.latitude = latitude
    self.longitude = longitude
    self.elevation = elevation

  def add(self, session):
    self.sessions.append(session)

  def __iter__(self):
    return iter(self.sessions)

  def astro_dark_times(self):
    """Returns (dark_start, dark_end) as astropy Time objects for tonight."""
    from astropy.coordinates import EarthLocation
    from astropy.time import Time
    from astroplan import Observer
    import astropy.units as u
    location = EarthLocation.from_geodetic(
      self.longitude * u.deg, self.latitude * u.deg, self.elevation * u.m)
    observer = Observer(location=location)
    now = Time.now()
    dark_start = observer.twilight_evening_astronomical(now, which='next')
    dark_end = observer.twilight_morning_astronomical(now, which='next')
    return dark_start, dark_end

  def save(self, path):
    d = {'sessions': [s.to_dict() for s in self.sessions]}
    if self.latitude is not None:
      d['latitude'] = self.latitude
    if self.longitude is not None:
      d['longitude'] = self.longitude
    if self.elevation != 0:
      d['elevation'] = self.elevation
    with open(path, 'w') as f:
      json.dump(d, f, indent=2)

  @classmethod
  def load(cls, path):
    with open(path) as f:
      data = json.load(f)
    plan = cls(latitude=data.get('latitude'),
               longitude=data.get('longitude'),
               elevation=data.get('elevation', 0))
    for s in data['sessions']:
      plan.add(ImagingSession.from_dict(s))
    return plan
