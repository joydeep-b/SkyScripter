#!/usr/bin/env python3
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime

from astropy.coordinates import EarthLocation
import astropy.units as u

from sky_scripter.alert_bus import AlertBus
from sky_scripter.capture_manager import CaptureManager
from sky_scripter.config import Config
from sky_scripter.cooler_manager import CoolerManager
from sky_scripter.discord_roof_watchdog import DiscordRoofWatchdog, read_discord_creds
from sky_scripter.focus_manager import FocusManager
from sky_scripter.guide_watchdog import GuideCommander, GuideWatchdog
from sky_scripter.lib_indi import IndiCamera, IndiFocuser, IndiMount
from sky_scripter.mount_manager import MountManager
from sky_scripter.project import (ProgressStore, ProjectFilter, ProjectPlan,
                                  ProjectTarget, build_example_project, comma_list)
from sky_scripter.roof_watchdog import RoofWatchdog
from sky_scripter.safety_watchdog import SafetyWatchdog
from sky_scripter.scheduler import NightScheduler
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.orchestrator import SessionOrchestrator
from sky_scripter.util import init_logging, print_and_log
from sky_scripter.web_monitor.server import MonitorServer

LOCAL_TZ = datetime.now().astimezone().tzinfo


def _repo_root() -> str:
  return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _device_name(value):
  if isinstance(value, list):
    return value[0]
  return value


def _load_project(args, cfg):
  project = ProjectPlan.load(args.project)
  if project.latitude is None:
    project.latitude = cfg.get("site", "latitude")
  if project.longitude is None:
    project.longitude = cfg.get("site", "longitude")
  if not project.elevation:
    project.elevation = cfg.get("site", "elevation", default=0)
  return project


def _build_night_plan(project, store, cfg):
  return project.to_night_plan(
    store,
    default_latitude=cfg.get("site", "latitude"),
    default_longitude=cfg.get("site", "longitude"),
    default_elevation=cfg.get("site", "elevation", default=0),
    default_start_offset=cfg.get("schedule", "start_offset", default=0),
    default_end_offset=cfg.get("schedule", "end_offset", default=0),
  )


def _progress_store(cfg):
  capture_dir = os.path.expanduser(cfg.get("capture", "capture_dir", default="~/Pictures"))
  return ProgressStore(capture_dir)


def _check_command(command, timeout=5):
  try:
    completed = subprocess.run(command, timeout=timeout, capture_output=True,
                               text=True, check=False)
    return completed.returncode == 0, completed.stdout.strip() or completed.stderr.strip()
  except Exception as exc:
    return False, repr(exc)


def run_preflight(args, cfg, project, store) -> int:
  capture_dir = os.path.expanduser(cfg.get("capture", "capture_dir", default="~/Pictures"))
  plan, _ = _build_night_plan(project, store, cfg)
  checks = []
  schedule_windows = []
  dark_window = None

  def add(name, ok, detail):
    checks.append((name, ok, detail))

  errors = project.validate()
  add("Project JSON", not errors, "; ".join(errors) if errors else "valid")
  add("Remaining work", len(plan.sessions) > 0,
      f"{len(plan.sessions)} target(s) with remaining frames")

  ok, detail = _check_command(["indi_getprop", "-t", "2"])
  add("INDI server", ok, "properties returned" if ok else detail)

  phd2_host = cfg.get("phd2", "host", default="localhost")
  phd2_port = cfg.get("phd2", "port", default=4400)
  try:
    with socket.create_connection((phd2_host, phd2_port), timeout=3):
      add("PHD2 socket", True, f"{phd2_host}:{phd2_port}")
  except Exception as exc:
    add("PHD2 socket", False, repr(exc))

  try:
    os.makedirs(capture_dir, exist_ok=True)
    usage = shutil.disk_usage(capture_dir)
    add("Capture directory", True,
        f"{capture_dir}, free={usage.free / (1024 ** 3):.1f} GB")
  except Exception as exc:
    add("Capture directory", False, repr(exc))

  token, channel_id = read_discord_creds(_repo_root())
  add("Discord roof credentials", bool(token and channel_id),
      "configured" if token and channel_id else "missing token/channel id")

  if plan.latitude is not None and plan.longitude is not None and plan.sessions:
    try:
      dark_start, dark_end = plan.astro_dark_times()
      dark_window = (dark_start, dark_end)
      location = EarthLocation.from_geodetic(
        plan.longitude * u.deg, plan.latitude * u.deg, plan.elevation * u.m)
      scheduler = NightScheduler(plan, dark_start, dark_end, location)
      scheduler.precompute()
      schedule_windows = scheduler.get_timeline()
      add("Schedule preview", True,
          f"{len(schedule_windows)} eligible window(s)")
    except Exception as exc:
      add("Schedule preview", False, repr(exc))
  else:
    add("Schedule preview", False, "site location or remaining sessions missing")

  print("\n=== SEQUENCER PREFLIGHT ===")
  for name, ok, detail in checks:
    status = "PASS" if ok else "FAIL"
    print(f"{status:4s}  {name}: {detail}")
  print("\nProgress:")
  _print_progress(project, store)
  if dark_window:
    dark_start, dark_end = dark_window
    print("\nAstronomical dark:")
    print(f"  {dark_start.to_datetime(timezone=LOCAL_TZ).strftime('%H:%M')}-"
          f"{dark_end.to_datetime(timezone=LOCAL_TZ).strftime('%H:%M')}")
  if schedule_windows:
    print("\nSchedule windows:")
    for slot in schedule_windows:
      print(f"  {slot['start_local']}-{slot['end_local']} "
            f"{slot['target']} {slot['filters']}")
  failures = [c for c in checks if not c[1]]
  return 1 if failures else 0


def _make_roof_watchdog(args, cfg, alert_bus, logger):
  if args.roof_source == "discord":
    return DiscordRoofWatchdog(
      alert_bus, logger, _repo_root(),
      poll_interval=cfg.get("roof", "poll_interval", default=10.0))
  if args.roof_source == "file":
    return RoofWatchdog(
      alert_bus, logger,
      status_file=cfg.get("roof", "status_file"),
      poll_interval=cfg.get("roof", "poll_interval", default=5.0))
  return RoofWatchdog(alert_bus, logger, status_file=None)


def run_once(args, cfg, project, store) -> int:
  plan, session_target_map = _build_night_plan(project, store, cfg)
  if not plan.sessions:
    print("Project is complete: no remaining frames.")
    return 0

  capture_dir = os.path.expanduser(cfg.get("capture", "capture_dir", default="~/Pictures"))
  alert_bus = AlertBus()
  logger = StructuredLogger("sequencer")

  mount = IndiMount(_device_name(cfg.get("devices", "mount")), simulate=args.simulate)
  camera = IndiCamera(_device_name(cfg.get("devices", "camera")), simulate=args.simulate)
  focuser = IndiFocuser(_device_name(cfg.get("devices", "focuser")), simulate=args.simulate)

  mount_mgr = MountManager(
    mount, camera, alert_bus, logger,
    focal_length_mm=cfg.get("optics", "focal_length_mm"),
    pixel_size_um=cfg.get("optics", "pixel_size_um"))
  cap_mgr = CaptureManager(camera, alert_bus, logger)
  focus_mgr = FocusManager(
    focuser, camera, alert_bus, logger,
    calibration_path=cfg.get("focus", "calibration_path", default="focus_calibration.json"),
    focus_step=cfg.get("focus", "step", default=6),
    num_steps=cfg.get("focus", "num_steps", default=7),
    min_position=cfg.get("focus", "min_position"),
    max_position=cfg.get("focus", "max_position"))
  cooler_mgr = CoolerManager(camera, logger)

  phd2_host = cfg.get("phd2", "host", default="localhost")
  phd2_port = cfg.get("phd2", "port", default=4400)
  guide_cmd = GuideCommander(phd2_host, phd2_port)
  guide_wd = GuideWatchdog(
    alert_bus, logger, phd2_host=phd2_host, phd2_port=phd2_port,
    rms_threshold=cfg.get("guiding", "rms_threshold", default=2.0),
    drift_timeout=cfg.get("guiding", "drift_timeout", default=60.0))
  roof_wd = _make_roof_watchdog(args, cfg, alert_bus, logger)
  safety_wd = SafetyWatchdog(
    alert_bus, logger, capture_dir, camera=camera,
    disk_warning_gb=cfg.get("safety", "disk_warning_gb", default=20.0),
    disk_critical_gb=cfg.get("safety", "disk_critical_gb", default=5.0),
    cooler_target_temp=cfg.get("cooler", "target_temp", default=-10.0))

  orchestrator = SessionOrchestrator(
    plan, mount_mgr, cap_mgr, focus_mgr, cooler_mgr, guide_cmd, guide_wd,
    roof_wd, safety_wd, alert_bus, logger, capture_dir,
    cooler_temp=cfg.get("cooler", "target_temp", default=-10.0),
    min_altitude=cfg.get("safety", "min_altitude", default=0),
    progress_store=store,
    project_plan=project,
    session_target_map=session_target_map,
    max_capture_failures=args.max_capture_failures)

  if not args.no_monitor:
    monitor = MonitorServer(
      orchestrator, guide_wd, roof_wd, safety_wd, alert_bus, logger,
      port=cfg.get("web", "ws_port", default=8765),
      http_port=cfg.get("web", "http_port", default=8080))
    monitor.start()

  orchestrator.run()
  return 0


def _read_file_roof_state(cfg):
  status_file = cfg.get("roof", "status_file")
  if not status_file:
    return "UNKNOWN"
  try:
    with open(status_file) as f:
      return "OPEN" if f.readline().strip().upper() == "OPEN" else "CLOSED"
  except OSError:
    return "UNKNOWN"


def _current_roof_state(args, cfg) -> str:
  if args.roof_source == "none" or args.ignore_roof_closed:
    return "OPEN"
  if args.roof_source == "file":
    return _read_file_roof_state(cfg)
  alert_bus = AlertBus()
  logger = StructuredLogger("sequencer_preflight")
  roof = DiscordRoofWatchdog(alert_bus, logger, _repo_root(),
                             poll_interval=cfg.get("roof", "poll_interval",
                                                   default=10.0))
  return roof.check_once()


def run_daemon(args, cfg, project, store) -> int:
  while True:
    roof_state = _current_roof_state(args, cfg)
    if roof_state != "OPEN":
      print(f"Roof is {roof_state}; waiting {args.recheck_minutes} minutes")
      if args.once:
        return 0
      time.sleep(args.recheck_minutes * 60)
      continue
    rc = run_once(args, cfg, project, store)
    if args.once or rc != 0:
      return rc
    if all(row["complete"] for row in project.progress_summary(store)):
      print("Project complete.")
      return 0
    print_and_log(f"Waiting {args.recheck_minutes} minutes before regenerating plan")
    time.sleep(args.recheck_minutes * 60)


def _print_progress(project, store):
  for row in project.progress_summary(store):
    state = "complete" if row["complete"] else "active"
    print(f"  {row['target']} ({state})")
    for f in row["filters"]:
      print(f"    {f['filter']}: {f['accepted']}/{f['target_frames']} "
            f"accepted, {f['remaining']} remaining")


def cmd_init_project(args):
  project = build_example_project(args.target, args.filters, args.exposure,
                                  args.frames,
                                  max_moon_altitude=args.max_moon_altitude,
                                  max_moon_phase=args.max_moon_phase)
  project.save(args.output)
  print(f"Wrote {args.output}")
  return 0


def cmd_validate(args):
  project = ProjectPlan.load(args.project)
  errors = project.validate()
  if errors:
    for error in errors:
      print(f"ERROR: {error}")
    return 1
  print("Project plan is valid.")
  return 0


def cmd_show(args):
  cfg = Config(args.config)
  project = _load_project(args, cfg)
  store = _progress_store(cfg)
  print("Project progress:")
  _print_progress(project, store)
  plan, _ = _build_night_plan(project, store, cfg)
  print(f"\nRemaining schedulable sessions: {len(plan.sessions)}")
  if plan.latitude is not None and plan.longitude is not None and plan.sessions:
    dark_start, dark_end = plan.astro_dark_times()
    location = EarthLocation.from_geodetic(
      plan.longitude * u.deg, plan.latitude * u.deg, plan.elevation * u.m)
    scheduler = NightScheduler(plan, dark_start, dark_end, location)
    scheduler.precompute()
    for slot in scheduler.get_timeline():
      print(f"  {slot['start_local']}-{slot['end_local']} "
            f"{slot['target']} {slot['filters']}")
  return 0


def cmd_preflight(args):
  cfg = Config(args.config)
  project = _load_project(args, cfg)
  store = _progress_store(cfg)
  return run_preflight(args, cfg, project, store)


def cmd_run(args):
  init_logging("sequencer", also_to_console=args.verbose)
  cfg = Config(args.config)
  project = _load_project(args, cfg)
  errors = project.validate()
  if errors:
    for error in errors:
      print(f"ERROR: {error}")
    return 1
  store = _progress_store(cfg)
  if args.preflight:
    rc = run_preflight(args, cfg, project, store)
    if rc != 0 and not args.continue_on_preflight_failure:
      return rc
  return run_daemon(args, cfg, project, store)


def _prompt(prompt: str, default: str | None = None) -> str:
  suffix = f" [{default}]" if default is not None else ""
  value = input(f"{prompt}{suffix}: ").strip()
  if value == "" and default is not None:
    return default
  return value


def _prompt_choice(prompt: str, choices: list[tuple[str, str]]) -> str:
  while True:
    print(prompt)
    for key, label in choices:
      print(f"  {key}. {label}")
    choice = input("> ").strip().lower()
    for key, _ in choices:
      if choice == key.lower():
        return key
    print("Please choose one of: " + ", ".join(key for key, _ in choices))


def _prompt_float(prompt: str, default: float | None = None) -> float:
  while True:
    raw = _prompt(prompt, None if default is None else str(default))
    try:
      return float(raw)
    except ValueError:
      print("Please enter a number.")


def _prompt_optional_float(prompt: str) -> float | None:
  while True:
    raw = _prompt(prompt, "")
    if raw == "":
      return None
    try:
      return float(raw)
    except ValueError:
      print("Please enter a number, or leave blank for no constraint.")


def _prompt_int(prompt: str, default: int | None = None) -> int:
  while True:
    raw = _prompt(prompt, None if default is None else str(default))
    try:
      return int(raw)
    except ValueError:
      print("Please enter an integer.")


def _prompt_bool(prompt: str, default: bool = False) -> bool:
  default_text = "Y/n" if default else "y/N"
  while True:
    raw = input(f"{prompt} [{default_text}]: ").strip().lower()
    if raw == "":
      return default
    if raw in ("y", "yes"):
      return True
    if raw in ("n", "no"):
      return False
    print("Please answer y or n.")


def _prompt_project_path(prompt="Project JSON path", default="project.json"):
  while True:
    path = _prompt(prompt, default)
    if path:
      return path
    print("Project path is required.")


def interactive_create_project() -> int:
  target_kind = _prompt_choice("Create project for:", [
    ("1", "Named target, resolved via Simbad (example: M31)"),
    ("2", "WCS coordinates"),
  ])
  target = None
  wcs = None
  if target_kind == "1":
    target = _prompt("Target name", "M31")
    name = target
  else:
    print('WCS format example: "5:35:17 -5:23:24" (RA as h:m:s, Dec as d:m:s)')
    wcs = _prompt("WCS coordinates")
    name = _prompt("Project target label", wcs.replace(" ", "_"))

  filters = comma_list(_prompt("Filters, comma separated", "L,R,G,B"))
  exposure = _prompt_float("Exposure seconds per frame", 300)
  frames = _prompt_int("Target frames per filter", 20)
  gain = _prompt_int("Gain", 56)
  offset = _prompt_int("Offset", 20)
  mode = _prompt_int("Readout mode", 5)
  min_altitude = _prompt_float("Minimum altitude in degrees", 30)
  max_moon_altitude = _prompt_optional_float(
    "Max moon altitude in degrees (blank for no constraint)")
  max_moon_phase = _prompt_optional_float(
    "Max moon phase percent (blank for no constraint)")
  dither_every = _prompt_int("Dither every N frames", 3)
  priority = _prompt_int("Priority (higher runs first when windows tie)", 0)
  output = _prompt_project_path("Output project JSON", "project.json")

  filter_specs = {
    filter_name: ProjectFilter(exposure=exposure, target_frames=frames)
    for filter_name in filters
  }
  project = ProjectPlan(targets=[ProjectTarget(
    name=name,
    target=target,
    wcs=wcs,
    filters=filter_specs,
    gain=gain,
    offset=offset,
    mode=mode,
    min_altitude=min_altitude,
    max_moon_altitude=max_moon_altitude,
    max_moon_phase=max_moon_phase,
    dither_every=dither_every,
    priority=priority,
  )])
  errors = project.validate()
  if errors:
    for error in errors:
      print(f"ERROR: {error}")
    return 1
  project.save(output)
  print(f"Wrote {output}")
  return 0


def interactive_preflight() -> int:
  project = _prompt_project_path()
  config = _prompt("Config path", "sky_scripter.json")
  args = argparse.Namespace(
    project=project,
    config=config,
  )
  return cmd_preflight(args)


def interactive_run() -> int:
  project = _prompt_project_path()
  config = _prompt("Config path", "sky_scripter.json")
  roof_source = _prompt_choice("Roof status source:", [
    ("discord", "Discord roof messages"),
    ("file", "Configured roof status file"),
    ("none", "No roof gate"),
  ])
  args = argparse.Namespace(
    project=project,
    config=config,
    preflight=_prompt_bool("Run preflight first", True),
    continue_on_preflight_failure=_prompt_bool("Continue if preflight fails", False),
    roof_source=roof_source,
    ignore_roof_closed=_prompt_bool("Ignore closed/unknown roof state", False),
    no_monitor=not _prompt_bool("Start web monitor", True),
    once=_prompt_bool("Run one generated sequence then exit", True),
    simulate=_prompt_bool("Simulate hardware actions", False),
    verbose=_prompt_bool("Verbose console logging", True),
    recheck_minutes=_prompt_float("Minutes between roof/plan rechecks", 15),
    max_capture_failures=_prompt_int("Max consecutive capture failures", 3),
  )
  return cmd_run(args)


def interactive_main() -> int:
  print("Sky Scripter Sequencer")
  choice = _prompt_choice("What would you like to do?", [
    ("1", "Create a project"),
    ("2", "Preflight a project"),
    ("3", "Run the sequencer"),
  ])
  if choice == "1":
    return interactive_create_project()
  elif choice == "2":
    return interactive_preflight()
  elif choice == "3":
    return interactive_run()
  else:
    print("Invalid choice.")
    return 1


def parse_args(argv=None):
  if argv is None and len(sys.argv) == 1:
    return argparse.Namespace(func=lambda _args: interactive_main())
  if argv == []:
    return argparse.Namespace(func=lambda _args: interactive_main())
  parser = argparse.ArgumentParser(description="Sky Scripter multi-night sequencer")
  sub = parser.add_subparsers(dest="command", required=True)

  init = sub.add_parser("init-project", help="Create a simple project JSON")
  init.add_argument("--target", required=True)
  init.add_argument("--filters", type=comma_list, default=["L"])
  init.add_argument("--exposure", type=float, default=300)
  init.add_argument("--frames", type=int, default=10)
  init.add_argument("--max-moon-altitude", type=float,
                    help="Allow capture when moon altitude is at or below this value")
  init.add_argument("--max-moon-phase", type=float,
                    help="Allow capture when moon phase percent is at or below this value")
  init.add_argument("--output", default="project.json")
  init.set_defaults(func=cmd_init_project)

  validate = sub.add_parser("validate-plan", help="Validate a project JSON")
  validate.add_argument("project")
  validate.set_defaults(func=cmd_validate)

  show = sub.add_parser("show-plan", help="Show progress and tonight's windows")
  show.add_argument("project")
  show.add_argument("--config", default="sky_scripter.json")
  show.set_defaults(func=cmd_show)

  preflight = sub.add_parser("preflight", help="Run preflight checks")
  preflight.add_argument("project")
  preflight.add_argument("--config", default="sky_scripter.json")
  preflight.set_defaults(func=cmd_preflight)

  run = sub.add_parser("run", help="Run the multi-night sequencer")
  run.add_argument("project")
  run.add_argument("--config", default="sky_scripter.json")
  run.add_argument("--preflight", action="store_true")
  run.add_argument("--continue-on-preflight-failure", action="store_true")
  run.add_argument("--roof-source", choices=["discord", "file", "none"], default="discord")
  run.add_argument("--ignore-roof-closed", action="store_true",
                   help="Run even if the configured roof source is not OPEN")
  run.add_argument("--no-monitor", action="store_true")
  run.add_argument("--once", action="store_true")
  run.add_argument("--simulate", action="store_true")
  run.add_argument("--verbose", action="store_true")
  run.add_argument("--recheck-minutes", type=float, default=15)
  run.add_argument("--max-capture-failures", type=int, default=3)
  run.set_defaults(func=cmd_run)

  return parser.parse_args(argv)


def main(argv=None):
  args = parse_args(argv)
  return args.func(args)


if __name__ == "__main__":
  sys.exit(main())
