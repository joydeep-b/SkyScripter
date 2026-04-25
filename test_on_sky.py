#!/usr/bin/env python3
"""Interactive on-sky subsystem test. Requires a clear night."""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime

from sky_scripter.lib_indi import IndiMount, IndiCamera, IndiFocuser
from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.mount_manager import MountManager
from sky_scripter.focus_manager import FocusManager
from sky_scripter.capture_manager import CaptureManager
from sky_scripter.guide_watchdog import GuideWatchdog, GuideCommander
from sky_scripter.config import Config
from sky_scripter.util import lookup_object_coordinates, get_siril_path


LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.logs')


class Runtime:
  args = slog = mount_mgr = guide_cmd = active_test = None
  guiding_started = False
  capture_in_progress = False


def log_event(rt, subsystem, event, **data):
  if rt and rt.slog:
    try:
      rt.slog.log(subsystem, event, **data)
    except Exception:
      traceback.print_exc()


def _safe_name(value):
  return ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in value)


def write_exception_report(exc, rt, label, cleanup_results=None):
  os.makedirs(LOG_DIR, exist_ok=True)
  timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
  active = _safe_name(rt.active_test or label)
  path = os.path.join(LOG_DIR, f'test_on_sky-{active}-{timestamp}.traceback.log')
  lines = [
    f"timestamp: {timestamp}",
    f"label: {label}",
    f"active_test: {rt.active_test}",
    f"exception_type: {type(exc).__name__}",
    f"exception: {exc!r}",
  ]
  if rt.args:
    lines.append(f"args: {vars(rt.args)}")
  lines += [
    f"guiding_started: {rt.guiding_started}",
    f"capture_in_progress: {rt.capture_in_progress}",
  ]
  if cleanup_results:
    lines.append("cleanup_results:")
    lines += [f"  - {result}" for result in cleanup_results]
  lines += ["", "traceback:",
            ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))]
  with open(path, 'w') as f:
    f.write('\n'.join(lines))
  return path


def stop_guiding_if_needed(rt, reason):
  if not rt.guiding_started:
    return True
  print(f"  Stopping guiding ({reason})...")
  log_event(rt, "guide", "stop_guiding_start", reason=reason)
  try:
    ok = rt.guide_cmd.stop_guiding()
    log_event(rt, "guide", "stop_guiding_complete",
              reason=reason, ok=ok)
    if ok:
      rt.guiding_started = False
    return ok
  except Exception as e:
    log_event(rt, "guide", "stop_guiding_exception",
              reason=reason, error=repr(e))
    return False


def start_guiding(rt):
  log_event(rt, "guide", "start_guiding_start")
  ok = rt.guide_cmd.start_guiding()
  log_event(rt, "guide", "start_guiding_complete", ok=ok)
  if ok:
    rt.guiding_started = True
  return ok


def cleanup_after_crash(rt, reason):
  results = []

  if rt.capture_in_progress:
    try:
      subprocess.run(['pkill', 'indi_cam_client'], timeout=5, check=False)
      results.append("capture_abort: attempted pkill indi_cam_client")
      log_event(rt, "cleanup", "capture_abort_attempted", reason=reason)
    except Exception as e:
      results.append(f"capture_abort: failed {e!r}")
      log_event(rt, "cleanup", "capture_abort_failed",
                reason=reason, error=repr(e))

  if rt.guiding_started and rt.guide_cmd:
    try:
      ok = rt.guide_cmd.stop_guiding()
      results.append(f"stop_guiding: ok={ok}")
      log_event(rt, "cleanup", "stop_guiding", reason=reason, ok=ok)
      if ok:
        rt.guiding_started = False
    except Exception as e:
      results.append(f"stop_guiding: failed {e!r}")
      log_event(rt, "cleanup", "stop_guiding_failed",
                reason=reason, error=repr(e))
  else:
    results.append("stop_guiding: skipped")

  if rt.mount_mgr and rt.args and rt.args.park_on_crash:
    try:
      rt.mount_mgr.park()
      results.append("park_mount: attempted")
      log_event(rt, "cleanup", "park_mount", reason=reason, ok=True)
    except Exception as e:
      results.append(f"park_mount: failed {e!r}")
      log_event(rt, "cleanup", "park_mount_failed",
                reason=reason, error=repr(e))
  else:
    results.append("park_mount: skipped (use --park-on-crash to enable)")
    log_event(rt, "cleanup", "park_mount_skipped", reason=reason)

  return results


def _target_altitude(mount, cfg, ra, dec):
  from astropy.coordinates import AltAz, EarthLocation, SkyCoord
  from astropy.time import Time
  import astropy.units as u

  lat = cfg.get('site', 'latitude')
  lon = cfg.get('site', 'longitude')
  elev = cfg.get('site', 'elevation', default=0)
  if lat is None or lon is None:
    lat = float(mount.read("GEOGRAPHIC_COORD.LAT"))
    lon = float(mount.read("GEOGRAPHIC_COORD.LONG"))
    elev = float(mount.read("GEOGRAPHIC_COORD.ELEV"))
  location = EarthLocation(lat=float(lat) * u.deg, lon=float(lon) * u.deg,
                           height=float(elev) * u.m)
  coord = SkyCoord(ra=ra * u.hourangle, dec=dec * u.deg, frame='icrs')
  return coord.transform_to(AltAz(obstime=Time.now(), location=location)).alt.degree


def run_preflight(target, cfg, mount, camera, ra, dec, capture_dir, slog):
  results = []

  def add(name, status, message):
    results.append((name, status, message))
    slog.log("preflight", "check", name=name, status=status.lower(),
             message=message)

  try:
    completed = subprocess.run(['indi_getprop', '-t', '2'], timeout=5,
                               capture_output=True, text=True, check=False)
    if completed.returncode == 0 and completed.stdout.strip():
      add("INDI server", "PASS", "indi_getprop returned properties")
    else:
      add("INDI server", "FAIL", f"indi_getprop returned {completed.returncode}")
  except Exception as e:
    add("INDI server", "FAIL", repr(e))

  astap = shutil.which('astap')
  add("ASTAP", "PASS" if astap else "FAIL",
      astap or "astap executable not found")

  siril = get_siril_path()
  add("Siril", "PASS" if siril else "FAIL",
      siril or "siril-cli executable not found on PATH")

  try:
    os.makedirs(capture_dir, exist_ok=True)
    test_path = os.path.join(capture_dir, '.test_on_sky_write_check')
    with open(test_path, 'w') as f:
      f.write('ok\n')
    os.remove(test_path)
    free_gb = shutil.disk_usage(capture_dir).free / (1024 ** 3)
    disk_warning = cfg.get('safety', 'disk_warning_gb', default=20.0)
    if free_gb < disk_warning:
      add("Capture directory", "WARN",
          f"writable, but only {free_gb:.1f} GB free")
    else:
      add("Capture directory", "PASS", f"writable, {free_gb:.1f} GB free")
  except Exception as e:
    add("Capture directory", "FAIL", repr(e))

  try:
    filters = camera.get_filter_names()
    if 'L' in filters:
      add("Filter L", "PASS", f"filters={filters}")
    else:
      add("Filter L", "FAIL", f"L not found; filters={filters}")
  except Exception as e:
    add("Filter L", "FAIL", repr(e))

  phd2_host = cfg.get('phd2', 'host', default='localhost')
  phd2_port = cfg.get('phd2', 'port', default=4400)
  try:
    with socket.create_connection((phd2_host, phd2_port), timeout=3):
      pass
    add("PHD2 socket", "PASS", f"{phd2_host}:{phd2_port}")
  except Exception as e:
    add("PHD2 socket", "FAIL", repr(e))

  try:
    alt = _target_altitude(mount, cfg, ra, dec)
    min_alt = cfg.get('safety', 'min_altitude', default=0)
    if alt < min_alt:
      add("Target altitude", "WARN",
          f"{target} altitude {alt:.1f} deg below min {min_alt}")
    else:
      add("Target altitude", "PASS", f"{target} altitude {alt:.1f} deg")
  except Exception as e:
    add("Target altitude", "WARN", repr(e))

  print("\n=== PREFLIGHT CHECKS ===")
  for name, status, message in results:
    print(f"  {status:4s}  {name}: {message}")
  failures = [r for r in results if r[1] == "FAIL"]
  if failures:
    choice = input("  Preflight failures found. Continue anyway? [y/N] ").strip()
    if choice.lower() != 'y':
      return False
  return True


def test_plate_solve(rt):
  print(f"  Slewing to {rt.target} (RA={rt.ra:.4f} Dec={rt.dec:.4f})...")
  ok = rt.mount_mgr.slew_and_center(rt.ra, rt.dec)
  if ok:
    return True, "aligned successfully"
  return False, "alignment failed"


def test_autofocus(rt):
  best_focus, best_fwhm = rt.focus_mgr.run_autofocus('L')
  if best_focus is not None and best_fwhm is not None:
    return True, f"focus {best_focus}, FWHM {best_fwhm:.1f}\""
  return False, "autofocus failed"


def test_guided_capture(rt):
  print("  Starting guiding...")
  if not start_guiding(rt):
    return False, "guiding failed to start"
  stopped = False
  try:
    os.makedirs(rt.capture_dir, exist_ok=True)
    files = []
    settings = rt.capture_settings
    for i in range(3):
      fname = os.path.join(rt.capture_dir, f"test_L_{i+1}.fits")
      print(f"  Capturing {i+1}/3 ({settings['exposure']}s)...")
      rt.capture_in_progress = True
      try:
        ok = rt.cap_mgr.capture(fname, 'L', settings['exposure'],
                                gain=settings['gain'],
                                offset=settings['offset'],
                                mode=settings['mode'])
      finally:
        rt.capture_in_progress = False
      if not ok:
        return False, f"capture failed: {fname}"
      files.append(fname)
  finally:
    stopped = stop_guiding_if_needed(rt, "guided capture")
  existing = [f for f in files if os.path.exists(f)]
  if not stopped:
    return False, "captures completed but guiding stop failed"
  if len(existing) == 3:
    return True, f"all 3 captures saved"
  return False, f"only {len(existing)}/3 files exist"


def test_dither(rt):
  print("  Starting guiding...")
  if not start_guiding(rt):
    return False, "guiding failed to start"
  stopped = False
  try:
    print("  Dithering...")
    settings = rt.guiding_settings
    ok = rt.guide_cmd.dither(pixels=settings['dither_pixels'],
                             settle_pixels=settings['dither_settle_pixels'],
                             settle_timeout=settings['dither_settle_timeout'])
  finally:
    stopped = stop_guiding_if_needed(rt, "dither")
  if not stopped:
    return False, "dither completed but guiding stop failed"
  if ok:
    return True, "dither completed"
  return False, "dither failed"


def test_guide_watchdog_alerts(rt):
  print("  Starting guiding...")
  if not start_guiding(rt):
    return False, "guiding failed to start"
  found = False
  stopped = False
  try:
    print("  Monitoring RMS for 30s...")
    for _ in range(6):
      time.sleep(5)
      s = rt.guide_wd.status
      print(f"    RMS: RA={s['rms_ra']:.2f} Dec={s['rms_dec']:.2f} "
            f"Total={s['rms_total']:.2f}")
    rt.alert_bus.get_pending()  # drain
    input("  Cover guide scope or defocus, then press ENTER: ")
    print("  Waiting up to 60s for GUIDE_STAR_LOST alert...")
    deadline = time.time() + 60
    while time.time() < deadline:
      for a in rt.alert_bus.get_pending():
        if a.code == "GUIDE_STAR_LOST":
          found = True
          break
      if found:
        break
      time.sleep(1)
    input("  Uncover/refocus guide scope, then press ENTER: ")
  finally:
    stopped = stop_guiding_if_needed(rt, "guide watchdog")
  if not stopped:
    return False, "watchdog test completed but guiding stop failed"
  if found:
    return True, "GUIDE_STAR_LOST alert received"
  return False, "no GUIDE_STAR_LOST alert within 60s"


def test_meridian_flip(rt):
  print("  WARNING: this test slews to a synthetic near-meridian target,")
  print("  waits up to 10 minutes for the flip trigger, then re-aligns.")
  confirm = input("  Type MERIDIAN to run this risky opt-in test: ").strip()
  if confirm != 'MERIDIAN':
    return None, "skipped by user"
  lst = rt.mount_mgr.mount.get_lst()
  # HA ~ +0:10 means RA = LST - 0.167
  flip_ra = lst - 0.167
  if flip_ra < 0:
    flip_ra += 24
  flip_dec = 30.0
  print(f"  Slewing to HA~+0:10 (RA={flip_ra:.4f} Dec={flip_dec})...")
  rt.mount_mgr.mount.goto(flip_ra, flip_dec)
  print("  Waiting for flip trigger (checking every 10s)...")
  for _ in range(60):
    if rt.mount_mgr.needs_flip():
      break
    time.sleep(10)
  else:
    return False, "flip never triggered"
  print("  Performing flip...")
  rt.mount_mgr.perform_flip()
  ra, dec = rt.mount_mgr.mount.get_ra_dec()
  ok = rt.mount_mgr.slew_and_center(ra, dec)
  if ok:
    return True, "flip + re-alignment succeeded"
  return False, "re-alignment after flip failed"


def test_emergency_alert(rt):
  rt.alert_bus.raise_alert(Alert(
    level=AlertLevel.EMERGENCY, source="test",
    code="ROOF_CLOSING", message="Simulated roof closing"))
  was_set = rt.alert_bus.emergency_event.is_set()
  rt.alert_bus.clear_emergency()
  cleared = not rt.alert_bus.emergency_event.is_set()
  if was_set and cleared:
    return True, "emergency event set and cleared; mount parking not tested"
  return False, f"set={was_set} cleared={cleared}"


TESTS = [
  ("autofocus", "AUTOFOCUS", "Run autofocus with L filter", test_autofocus),
  ("plate-solve", "PLATE SOLVE ALIGNMENT",
   "Slew to {target}, plate solve, sync, verify alignment", test_plate_solve),
  ("guided-capture", "GUIDED CAPTURE",
   "Start guiding, take 3x30s L captures, stop guiding", test_guided_capture),
  ("dither", "DITHERING", "Start guiding, dither, wait for settle", test_dither),
  ("guide-watchdog", "GUIDE WATCHDOG ALERTS",
   "Monitor guiding, detect GUIDE_STAR_LOST alert", test_guide_watchdog_alerts),
  ("meridian-flip", "MERIDIAN FLIP",
   "Risky opt-in: slew near meridian, wait for flip, re-align", test_meridian_flip),
  ("emergency-alert", "EMERGENCY ALERT SIMULATION",
   "Raise simulated ROOF_CLOSING alert; does not verify mount parking",
   test_emergency_alert),
]


def _comma_set(value):
  if not value:
    return set()
  return {item.strip() for item in value.split(',') if item.strip()}


def _selected_tests(args, parser):
  known = {slug for slug, _, _, _ in TESTS}
  only = _comma_set(args.only)
  skip = _comma_set(args.skip)
  invalid = (only | skip) - known
  if invalid:
    parser.error(f"Unknown test slug(s): {', '.join(sorted(invalid))}. "
                 f"Known: {', '.join(sorted(known))}")
  return [t for t in TESTS if (not only or t[0] in only) and t[0] not in skip]


def parse_args():
  known_tests = ', '.join(slug for slug, _, _, _ in TESTS)

  parser = argparse.ArgumentParser(description="On-sky subsystem tests")
  parser.add_argument('--config', default='sky_scripter.json',
                      help='Path to sky_scripter.json')
  parser.add_argument('--target', default='M 81')
  parser.add_argument('--capture-exposure', type=float, default=30)
  parser.add_argument('--only', help=f'Comma-separated test slugs: {known_tests}')
  parser.add_argument('--skip', help=f'Comma-separated test slugs: {known_tests}')
  parser.add_argument('--skip-preflight', action='store_true')
  parser.add_argument('--park-on-crash', action='store_true',
                      help='Attempt to park the mount after an unexpected crash')
  args = parser.parse_args()
  cfg = Config(args.config)
  return args, cfg, parser


def run(rt):
  args, cfg, parser = parse_args()
  rt.args = args
  rt.target = args.target
  rt.capture_dir = os.path.expanduser(
      cfg.get('capture', 'capture_dir', default='~/Pictures/test_on_sky'))
  mount_name = cfg.get('devices', 'mount')
  camera_name = cfg.get('devices', 'camera')
  focuser_name = cfg.get('devices', 'focuser')
  phd2_host = cfg.get('phd2', 'host', default='localhost')
  phd2_port = cfg.get('phd2', 'port', default=4400)
  os.makedirs(LOG_DIR, exist_ok=True)
  rt.alert_bus = AlertBus()
  rt.slog = StructuredLogger("test_on_sky")
  log_event(rt, "runner", "start", args=vars(args),
            config_path=args.config, mount=mount_name, camera=camera_name,
            focuser=focuser_name, capture_dir=rt.capture_dir,
            phd2_host=phd2_host, phd2_port=phd2_port)

  print(f"Resolving {rt.target} coordinates...")
  rt.ra, rt.dec = lookup_object_coordinates(rt.target)
  print(f"  {rt.target}: RA={rt.ra:.4f} Dec={rt.dec:.4f}")

  mount = IndiMount(mount_name)
  camera = IndiCamera(camera_name)
  focuser = IndiFocuser(focuser_name)
  rt.mount_mgr = MountManager(mount, camera, rt.alert_bus, rt.slog)
  rt.focus_mgr = FocusManager(
      focuser, camera, rt.alert_bus, rt.slog,
      calibration_path=cfg.get('focus', 'calibration_path',
                               default='focus_calibration.json'),
      focus_step=cfg.get('focus', 'step', default=6),
      num_steps=cfg.get('focus', 'num_steps', default=7),
      min_position=cfg.get('focus', 'min_position'),
      max_position=cfg.get('focus', 'max_position'))
  rt.cap_mgr = CaptureManager(camera, rt.alert_bus, rt.slog)
  rt.guide_cmd = GuideCommander(phd2_host, phd2_port)
  rt.guide_wd = GuideWatchdog(
      rt.alert_bus, rt.slog, phd2_host=phd2_host, phd2_port=phd2_port,
      rms_threshold=cfg.get('guiding', 'rms_threshold', default=2.0),
      drift_timeout=cfg.get('guiding', 'drift_timeout', default=60.0))
  rt.guide_wd.start()

  if not args.skip_preflight:
    if not run_preflight(rt.target, cfg, mount, camera, rt.ra, rt.dec,
                         rt.capture_dir, rt.slog):
      log_event(rt, "runner", "preflight_aborted")
      return 1

  rt.capture_settings = {
    'gain': cfg.get('capture', 'gain', default=56),
    'offset': cfg.get('capture', 'offset', default=20),
    'mode': cfg.get('capture', 'mode', default=5),
    'exposure': args.capture_exposure,
  }
  rt.guiding_settings = {
    'dither_pixels': cfg.get('guiding', 'dither_pixels', default=4),
    'dither_settle_pixels': cfg.get('guiding', 'dither_settle_pixels',
                                    default=0.5),
    'dither_settle_timeout': cfg.get('guiding', 'dither_settle_timeout',
                                     default=60),
  }

  tests = _selected_tests(args, parser)
  total = len(tests)
  passed = failed = skipped = 0
  results = []

  print(f"\n=== ON-SKY SUBSYSTEM TEST ===\n")
  for i, (slug, name, desc, fn) in enumerate(tests):
    rt.active_test = slug
    print(f"[Test {i+1}/{total}] {name}")
    print(f"  Will: {desc.format(target=rt.target)}")
    log_event(rt, "test", "prompt", slug=slug, name=name)
    choice = input("  Press ENTER to run, 's' to skip, 'q' to quit: ").strip()
    if choice == 'q':
      results.append((name, "QUIT"))
      skipped += total - i
      log_event(rt, "test", "quit", slug=slug, skipped_remaining=total - i)
      break
    if choice == 's':
      print(f"  Result: SKIPPED\n")
      results.append((name, "SKIPPED"))
      skipped += 1
      log_event(rt, "test", "skipped", slug=slug)
      continue
    print("  Running...")
    log_event(rt, "test", "start", slug=slug, name=name)
    event = "end"
    try:
      ok, msg = fn(rt)
      status = "SKIPPED" if ok is None else "PASS" if ok else "FAIL"
      print(f"  Result: {status} - {msg}\n")
    except Exception as e:
      report = write_exception_report(e, rt, f"test-{slug}")
      status, msg = "FAIL", f"exception: {e}; traceback saved: {report}"
      event = "exception"
      print(f"  Result: FAIL - exception: {e}")
      print(f"  Traceback saved: {report}\n")

    results.append((name, status))
    passed += status == "PASS"
    failed += status == "FAIL"
    skipped += status == "SKIPPED"
    log_event(rt, "test", event, slug=slug, status=status.lower(),
              message=msg)

  rt.active_test = None
  log_event(rt, "runner", "complete", passed=passed, failed=failed,
            skipped=skipped, total=total)
  print(f"=== RESULTS: {passed}/{total} PASSED, {failed} FAILED, "
        f"{skipped} SKIPPED ===")
  for name, status in results:
    print(f"  {status:7s}  {name}")
  return 1 if failed else 0


def main():
  rt = Runtime()
  try:
    return run(rt)
  except BaseException as exc:
    if isinstance(exc, SystemExit) and exc.code == 0:
      return 0
    reason = "keyboard_interrupt" if isinstance(exc, KeyboardInterrupt) else "crash"
    cleanup_results = cleanup_after_crash(rt, reason)
    report = write_exception_report(exc, rt, reason, cleanup_results)
    log_event(rt, "runner", reason, error=repr(exc), traceback=report,
              cleanup_results=cleanup_results)
    print(f"\nUnexpected shutdown: {type(exc).__name__}: {exc}")
    print(f"Traceback saved: {report}")
    print("Cleanup attempted:")
    for result in cleanup_results:
      print(f"  - {result}")
    if isinstance(exc, KeyboardInterrupt):
      return 130
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
      return exc.code
    return 1


if __name__ == '__main__':
  sys.exit(main())
