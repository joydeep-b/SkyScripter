import argparse
import glob
import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from sky_scripter.sequence import ImagingSession, NightPlan


def sanitize_name(value: str) -> str:
  sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
  sanitized = re.sub(r"_+", "_", sanitized).strip("_")
  return sanitized or "unnamed"


@dataclass
class ProjectFilter:
  exposure: float
  target_frames: int
  gain: int | None = None
  offset: int | None = None
  mode: int | None = None

  @classmethod
  def from_dict(cls, data: dict):
    return cls(
      exposure=float(data["exposure"]),
      target_frames=int(data["target_frames"]),
      gain=data.get("gain"),
      offset=data.get("offset"),
      mode=data.get("mode"),
    )

  def to_dict(self) -> dict:
    data = {"exposure": self.exposure, "target_frames": self.target_frames}
    if self.gain is not None:
      data["gain"] = self.gain
    if self.offset is not None:
      data["offset"] = self.offset
    if self.mode is not None:
      data["mode"] = self.mode
    return data


@dataclass
class ProjectTarget:
  name: str
  target: str | None = None
  wcs: str | None = None
  filters: dict[str, ProjectFilter] = field(default_factory=dict)
  gain: int = 56
  offset: int = 20
  mode: int = 5
  min_altitude: float = 0
  dither_every: int = 1
  max_moon_altitude: float | None = None
  max_moon_phase: float | None = None
  start_offset: float | None = None
  end_offset: float | None = None
  priority: int = 0

  @property
  def identity(self) -> str:
    return self.target or self.wcs or self.name

  @property
  def project_name(self) -> str:
    return self.name or self.identity

  @classmethod
  def from_dict(cls, data: dict):
    target = data.get("target")
    wcs = data.get("wcs")
    if not target and not wcs:
      raise ValueError("Project target must specify 'target' or 'wcs'")
    if target and wcs:
      raise ValueError("Project target cannot specify both 'target' and 'wcs'")
    filters = {
      name: ProjectFilter.from_dict(filter_data)
      for name, filter_data in data.get("filters", {}).items()
    }
    if not filters:
      raise ValueError(f"Project target {target or wcs} has no filters")
    return cls(
      name=data.get("name") or target or wcs,
      target=target,
      wcs=wcs,
      filters=filters,
      gain=int(data.get("gain", 56)),
      offset=int(data.get("offset", 20)),
      mode=int(data.get("mode", 5)),
      min_altitude=float(data.get("min_altitude", 0)),
      dither_every=int(data.get("dither_every", 1)),
      max_moon_altitude=data.get("max_moon_altitude"),
      max_moon_phase=data.get("max_moon_phase"),
      start_offset=float(data["start_offset"]) if "start_offset" in data else None,
      end_offset=float(data["end_offset"]) if "end_offset" in data else None,
      priority=int(data.get("priority", 0)),
    )

  def to_dict(self) -> dict:
    data = {
      "name": self.name,
      "filters": {name: f.to_dict() for name, f in self.filters.items()},
      "gain": self.gain,
      "offset": self.offset,
      "mode": self.mode,
      "min_altitude": self.min_altitude,
      "dither_every": self.dither_every,
      "priority": self.priority,
    }
    if self.target:
      data["target"] = self.target
    if self.wcs:
      data["wcs"] = self.wcs
    if self.max_moon_altitude is not None:
      data["max_moon_altitude"] = self.max_moon_altitude
    if self.max_moon_phase is not None:
      data["max_moon_phase"] = self.max_moon_phase
    if self.start_offset is not None:
      data["start_offset"] = self.start_offset
    if self.end_offset is not None:
      data["end_offset"] = self.end_offset
    return data

  def remaining_filters(self, store) -> dict[str, tuple[float, int]]:
    remaining = {}
    for name, spec in self.filters.items():
      done = store.accepted_count(self.project_name, name, spec.exposure)
      count = max(0, spec.target_frames - done)
      if count > 0:
        remaining[name] = (spec.exposure, count)
    return remaining


@dataclass
class ProjectPlan:
  targets: list[ProjectTarget]
  latitude: float | None = None
  longitude: float | None = None
  elevation: float = 0

  @classmethod
  def load(cls, path: str):
    with open(path) as f:
      data = json.load(f)
    if "targets" not in data:
      raise ValueError("Project plan must contain a 'targets' list")
    return cls(
      targets=[ProjectTarget.from_dict(t) for t in data["targets"]],
      latitude=data.get("latitude"),
      longitude=data.get("longitude"),
      elevation=float(data.get("elevation", 0)),
    )

  def save(self, path: str):
    data = {
      "targets": [target.to_dict() for target in self.targets],
    }
    if self.latitude is not None:
      data["latitude"] = self.latitude
    if self.longitude is not None:
      data["longitude"] = self.longitude
    if self.elevation:
      data["elevation"] = self.elevation
    with open(path, "w") as f:
      json.dump(data, f, indent=2)

  def validate(self) -> list[str]:
    errors = []
    seen = set()
    seen_sanitized = set()
    for target in self.targets:
      if target.identity in seen:
        errors.append(f"Duplicate target identity: {target.identity}")
      seen.add(target.identity)
      safe_project = sanitize_name(target.project_name)
      if safe_project in seen_sanitized:
        errors.append(f"Duplicate sanitized project name: {safe_project}")
      seen_sanitized.add(safe_project)
      if target.dither_every < 1:
        errors.append(f"{target.identity}: dither_every must be >= 1")
      seen_filters = set()
      for filter_name, spec in target.filters.items():
        safe_filter = sanitize_name(filter_name)
        if safe_filter in seen_filters:
          errors.append(f"{target.identity}: duplicate sanitized filter name {safe_filter}")
        seen_filters.add(safe_filter)
        if spec.exposure <= 0:
          errors.append(f"{target.identity}/{filter_name}: exposure must be > 0")
        if spec.target_frames <= 0:
          errors.append(f"{target.identity}/{filter_name}: target_frames must be > 0")
    return errors

  def to_night_plan(self, store, default_latitude=None, default_longitude=None,
                    default_elevation=0, default_start_offset=0,
                    default_end_offset=0):
    plan = NightPlan(
      latitude=self.latitude if self.latitude is not None else default_latitude,
      longitude=self.longitude if self.longitude is not None else default_longitude,
      elevation=self.elevation if self.elevation else default_elevation,
    )
    session_target_map = {}
    for target in sorted(self.targets, key=lambda t: -t.priority):
      remaining = target.remaining_filters(store)
      if not remaining:
        continue
      kwargs = {name: value for name, value in remaining.items()}
      session = ImagingSession(
        target=target.target,
        wcs=target.wcs,
        gain=target.gain,
        offset=target.offset,
        mode=target.mode,
        min_altitude=target.min_altitude,
        dither_every=target.dither_every,
        max_moon_altitude=target.max_moon_altitude,
        max_moon_phase=target.max_moon_phase,
        start_offset=target.start_offset
        if target.start_offset is not None else default_start_offset,
        end_offset=target.end_offset
        if target.end_offset is not None else default_end_offset,
        **kwargs,
      )
      session.project_identity = target.project_name
      session.project_priority = target.priority
      session.remaining_frames = sum(count for _, count in remaining.values())
      plan.add(session)
      session_target_map[len(plan.sessions) - 1] = target.project_name
    return plan, session_target_map

  def progress_summary(self, store) -> list[dict]:
    rows = []
    for target in self.targets:
      filters = []
      complete = True
      for filter_name, spec in target.filters.items():
        accepted = store.accepted_count(target.project_name, filter_name, spec.exposure)
        remaining = max(0, spec.target_frames - accepted)
        complete = complete and remaining == 0
        filters.append({
          "filter": filter_name,
          "exposure": spec.exposure,
          "target_frames": spec.target_frames,
          "accepted": accepted,
          "remaining": remaining,
        })
      rows.append({
        "target": target.project_name,
        "identity": target.identity,
        "name": target.name,
        "priority": target.priority,
        "complete": complete,
        "filters": filters,
      })
    return rows


class ProgressStore:
  def __init__(self, capture_dir: str):
    self.capture_dir = os.path.expanduser(capture_dir)

  def project_dir(self, project_name: str) -> str:
    return os.path.join(self.capture_dir, sanitize_name(project_name))

  def filter_dir(self, project_name: str, filter_name: str) -> str:
    return os.path.join(self.project_dir(project_name), sanitize_name(filter_name))

  def filename_for_index(self, project_name: str, filter_name: str, index: int) -> str:
    safe_project = sanitize_name(project_name)
    safe_filter = sanitize_name(filter_name)
    return os.path.join(
      self.filter_dir(project_name, filter_name),
      f"{safe_project}-{safe_filter}-{index:05d}.fits",
    )

  def next_filename(self, project_name: str, filter_name: str) -> str:
    os.makedirs(self.filter_dir(project_name, filter_name), exist_ok=True)
    return self.filename_for_index(
      project_name, filter_name,
      self._max_index(project_name, filter_name) + 1,
    )

  def accepted_count(self, target: str, filter_name: str, exposure: float) -> int:
    return len(self._matching_files(target, filter_name))

  def _matching_files(self, project_name: str, filter_name: str) -> list[str]:
    safe_project = sanitize_name(project_name)
    safe_filter = sanitize_name(filter_name)
    pattern = os.path.join(
      self.filter_dir(project_name, filter_name),
      f"{safe_project}-{safe_filter}-[0-9][0-9][0-9][0-9][0-9].fits",
    )
    return sorted(path for path in glob.glob(pattern)
                  if os.path.isfile(path) and os.path.getsize(path) > 0)

  def _max_index(self, project_name: str, filter_name: str) -> int:
    safe_project = sanitize_name(project_name)
    safe_filter = sanitize_name(filter_name)
    prefix = f"{safe_project}-{safe_filter}-"
    max_index = 0
    for path in self._matching_files(project_name, filter_name):
      stem = os.path.basename(path)
      if not stem.startswith(prefix) or not stem.endswith(".fits"):
        continue
      try:
        max_index = max(max_index, int(stem[len(prefix):-5]))
      except ValueError:
        continue
    return max_index

  def recent_frames(self, count: int = 20) -> list[dict]:
    pattern = os.path.join(self.capture_dir, "*", "*", "*.fits")
    paths = [path for path in glob.glob(pattern)
             if os.path.isfile(path) and os.path.getsize(path) > 0]
    paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return [{
      "filename": path,
      "target": os.path.basename(os.path.dirname(os.path.dirname(path))),
      "filter_name": os.path.basename(os.path.dirname(path)),
      "status": "accepted",
      "created_at": os.path.getmtime(path),
    } for path in paths[:count]]


def build_example_project(target: str, filters: Iterable[str],
                          exposure: float, frames: int,
                          latitude=None, longitude=None, elevation=0,
                          max_moon_altitude=None,
                          max_moon_phase=None) -> ProjectPlan:
  specs = {
    name: ProjectFilter(exposure=exposure, target_frames=frames)
    for name in filters
  }
  return ProjectPlan(
    targets=[ProjectTarget(name=target, target=target, filters=specs,
                           max_moon_altitude=max_moon_altitude,
                           max_moon_phase=max_moon_phase)],
    latitude=latitude,
    longitude=longitude,
    elevation=elevation,
  )


def comma_list(value: str) -> list[str]:
  items = [item.strip() for item in value.split(",") if item.strip()]
  if not items:
    raise argparse.ArgumentTypeError("must contain at least one item")
  return items
