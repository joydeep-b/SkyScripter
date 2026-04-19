import json
import os

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), '..', 'sky_scripter.json')

DEFAULTS = {
  "site": {
    "latitude": None,
    "longitude": None,
    "elevation": 0,
  },
  "devices": {
    "mount": "ZWO AM5",
    "camera": "QHY CCD QHY268M-b93fd94",
    "focuser": "ZWO EAF",
  },
  "phd2": {
    "host": "localhost",
    "port": 4400,
  },
  "capture": {
    "gain": 56,
    "offset": 20,
    "mode": 5,
    "capture_dir": "~/Pictures",
  },
  "focus": {
    "gain": 70,
    "offset": 20,
    "mode": 5,
    "step": 6,
    "num_steps": 7,
    "exposure_broadband": 2,
    "exposure_narrowband": 4,
    "interval_minutes": 60,
    "temp_threshold": 2.0,
    "calibration_path": "focus_calibration.json",
  },
  "cooler": {
    "target_temp": -10.0,
    "warmup_rate": 2.0,
    "warmup_interval": 30,
  },
  "guiding": {
    "rms_threshold": 2.0,
    "drift_timeout": 60.0,
    "dither_pixels": 4,
    "dither_settle_pixels": 0.5,
    "dither_settle_timeout": 60,
  },
  "safety": {
    "disk_warning_gb": 20.0,
    "disk_critical_gb": 5.0,
    "min_altitude": 0,
  },
  "schedule": {
    "start_offset": 0,
    "end_offset": 0,
  },
  "roof": {
    "status_file": None,
    "poll_interval": 5.0,
  },
  "web": {
    "ws_port": 8765,
    "http_port": 8080,
  },
}


def _deep_merge(base, override):
  result = dict(base)
  for k, v in override.items():
    if k in result and isinstance(result[k], dict) and isinstance(v, dict):
      result[k] = _deep_merge(result[k], v)
    else:
      result[k] = v
  return result


class Config:
  """Global configuration loaded from sky_scripter.json with built-in defaults."""

  def __init__(self, path=None):
    self._data = dict(DEFAULTS)
    if path is None:
      path = DEFAULT_CONFIG_PATH
    self._path = path
    if os.path.exists(path):
      with open(path) as f:
        user = json.load(f)
      self._data = _deep_merge(self._data, user)

  def __getitem__(self, key):
    return self._data[key]

  def get(self, *keys, default=None):
    """Nested key lookup: config.get('site', 'latitude')"""
    d = self._data
    for k in keys:
      if isinstance(d, dict) and k in d:
        d = d[k]
      else:
        return default
    return d

  def save(self, path=None):
    path = path or self._path
    with open(path, 'w') as f:
      json.dump(self._data, f, indent=2)

  @property
  def data(self):
    return self._data

  @staticmethod
  def generate_default(path=None):
    """Write a default sky_scripter.json for the user to edit."""
    path = path or DEFAULT_CONFIG_PATH
    with open(path, 'w') as f:
      json.dump(DEFAULTS, f, indent=2)
    return path
