#!/usr/bin/env python3
"""Interactive on-sky subsystem test. Requires a clear night."""

import argparse
import os
import time
import threading

from sky_scripter.lib_indi import IndiMount, IndiCamera, IndiFocuser
from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.mount_manager import MountManager
from sky_scripter.focus_manager import FocusManager
from sky_scripter.capture_manager import CaptureManager
from sky_scripter.guide_watchdog import GuideWatchdog, GuideCommander
from sky_scripter.util import lookup_object_coordinates


def test_plate_solve(mount_mgr, target, ra, dec):
  print(f"  Slewing to {target} (RA={ra:.4f} Dec={dec:.4f})...")
  ok = mount_mgr.slew_and_center(ra, dec)
  if ok:
    return True, "aligned successfully"
  return False, "alignment failed"


def test_autofocus(focus_mgr):
  best_focus, best_fwhm = focus_mgr.run_autofocus('L')
  if best_focus is not None:
    return True, f"focus {best_focus}, FWHM {best_fwhm:.1f}\""
  return False, "autofocus failed"


def test_guided_capture(guide_cmd, cap_mgr, capture_dir):
  print("  Starting guiding...")
  if not guide_cmd.start_guiding():
    return False, "guiding failed to start"
  os.makedirs(capture_dir, exist_ok=True)
  files = []
  for i in range(3):
    fname = os.path.join(capture_dir, f"test_L_{i+1}.fits")
    print(f"  Capturing {i+1}/3 (30s)...")
    cap_mgr.capture(fname, 'L', 30, gain=56, offset=20, mode=5)
    files.append(fname)
  guide_cmd.stop_guiding()
  existing = [f for f in files if os.path.exists(f)]
  if len(existing) == 3:
    return True, f"all 3 captures saved"
  return False, f"only {len(existing)}/3 files exist"


def test_dither(guide_cmd):
  print("  Starting guiding...")
  if not guide_cmd.start_guiding():
    return False, "guiding failed to start"
  print("  Dithering...")
  ok = guide_cmd.dither()
  guide_cmd.stop_guiding()
  if ok:
    return True, "dither completed"
  return False, "dither failed"


def test_guide_watchdog_alerts(alert_bus, guide_wd, guide_cmd):
  print("  Starting guiding...")
  if not guide_cmd.start_guiding():
    return False, "guiding failed to start"
  print("  Monitoring RMS for 30s...")
  for _ in range(6):
    time.sleep(5)
    s = guide_wd.status
    print(f"    RMS: RA={s['rms_ra']:.2f} Dec={s['rms_dec']:.2f} "
          f"Total={s['rms_total']:.2f}")
  alert_bus.get_pending()  # drain
  input("  Cover guide scope or defocus, then press ENTER: ")
  print("  Waiting up to 60s for GUIDE_STAR_LOST alert...")
  deadline = time.time() + 60
  found = False
  while time.time() < deadline:
    for a in alert_bus.get_pending():
      if a.code == "GUIDE_STAR_LOST":
        found = True
        break
    if found:
      break
    time.sleep(1)
  input("  Uncover/refocus guide scope, then press ENTER: ")
  guide_cmd.stop_guiding()
  if found:
    return True, "GUIDE_STAR_LOST alert received"
  return False, "no GUIDE_STAR_LOST alert within 60s"


def test_meridian_flip(mount_mgr):
  confirm = input("  Is a target near the meridian available? (y/n): ")
  if confirm.lower() != 'y':
    return None, "skipped by user"
  lst = mount_mgr.mount.get_lst()
  # HA ~ +0:10 means RA = LST - 0.167
  flip_ra = lst - 0.167
  if flip_ra < 0:
    flip_ra += 24
  flip_dec = 30.0
  print(f"  Slewing to HA~+0:10 (RA={flip_ra:.4f} Dec={flip_dec})...")
  mount_mgr.mount.goto(flip_ra, flip_dec)
  print("  Waiting for flip trigger (checking every 10s)...")
  for _ in range(60):
    if mount_mgr.needs_flip():
      break
    time.sleep(10)
  else:
    return False, "flip never triggered"
  print("  Performing flip...")
  mount_mgr.perform_flip()
  ra, dec = mount_mgr.mount.get_ra_dec()
  ok = mount_mgr.slew_and_center(ra, dec)
  if ok:
    return True, "flip + re-alignment succeeded"
  return False, "re-alignment after flip failed"


def test_emergency_park(alert_bus):
  alert_bus.raise_alert(Alert(
    level=AlertLevel.EMERGENCY, source="test",
    code="ROOF_CLOSING", message="Simulated roof closing"))
  was_set = alert_bus.emergency_event.is_set()
  alert_bus.clear_emergency()
  cleared = not alert_bus.emergency_event.is_set()
  if was_set and cleared:
    return True, "emergency event set and cleared"
  return False, f"set={was_set} cleared={cleared}"


TESTS = [
  ("PLATE SOLVE ALIGNMENT",
   "Slew to {target}, plate solve, sync, verify alignment",
   lambda ctx: test_plate_solve(ctx['mount_mgr'], ctx['target'],
                                ctx['ra'], ctx['dec'])),
  ("AUTOFOCUS",
   "Run autofocus with L filter",
   lambda ctx: test_autofocus(ctx['focus_mgr'])),
  ("GUIDED CAPTURE",
   "Start guiding, take 3x30s L captures, stop guiding",
   lambda ctx: test_guided_capture(ctx['guide_cmd'], ctx['cap_mgr'],
                                   ctx['capture_dir'])),
  ("DITHERING",
   "Start guiding, dither, wait for settle",
   lambda ctx: test_dither(ctx['guide_cmd'])),
  ("GUIDE WATCHDOG ALERTS",
   "Monitor guiding, detect GUIDE_STAR_LOST alert",
   lambda ctx: test_guide_watchdog_alerts(ctx['alert_bus'],
                                          ctx['guide_wd'],
                                          ctx['guide_cmd'])),
  ("MERIDIAN FLIP",
   "Slew near meridian, wait for flip, re-align",
   lambda ctx: test_meridian_flip(ctx['mount_mgr'])),
  ("EMERGENCY PARK SIMULATION",
   "Raise simulated ROOF_CLOSING alert, verify emergency event",
   lambda ctx: test_emergency_park(ctx['alert_bus'])),
]


def main():
  parser = argparse.ArgumentParser(description="On-sky subsystem tests")
  parser.add_argument('--mount', default='ZWO AM5')
  parser.add_argument('--camera', default='QHY CCD QHY268M-b93fd94')
  parser.add_argument('--focuser', default='ZWO EAF')
  parser.add_argument('--target', default='Vega')
  parser.add_argument('--capture-dir', default='~/Pictures/test_on_sky')
  args = parser.parse_args()

  capture_dir = os.path.expanduser(args.capture_dir)
  print(f"Resolving {args.target} coordinates...")
  ra, dec = lookup_object_coordinates(args.target)
  print(f"  {args.target}: RA={ra:.4f} Dec={dec:.4f}")

  alert_bus = AlertBus()
  slog = StructuredLogger("test_on_sky")
  mount = IndiMount(args.mount)
  camera = IndiCamera(args.camera)
  focuser = IndiFocuser(args.focuser)
  mount_mgr = MountManager(mount, camera, alert_bus, slog)
  focus_mgr = FocusManager(focuser, camera, alert_bus, slog)
  cap_mgr = CaptureManager(camera, alert_bus, slog)
  guide_cmd = GuideCommander()
  guide_wd = GuideWatchdog(alert_bus, slog)
  guide_wd.start()

  ctx = {
    'mount_mgr': mount_mgr, 'focus_mgr': focus_mgr, 'cap_mgr': cap_mgr,
    'guide_cmd': guide_cmd, 'guide_wd': guide_wd, 'alert_bus': alert_bus,
    'target': args.target, 'ra': ra, 'dec': dec, 'capture_dir': capture_dir,
  }

  total = len(TESTS)
  passed = failed = skipped = 0
  results = []

  print(f"\n=== ON-SKY SUBSYSTEM TEST ===\n")
  for i, (name, desc, fn) in enumerate(TESTS):
    print(f"[Test {i+1}/{total}] {name}")
    print(f"  Will: {desc.format(**ctx)}")
    choice = input("  Press ENTER to run, 's' to skip, 'q' to quit: ").strip()
    if choice == 'q':
      results.append((name, "QUIT"))
      skipped += total - i
      break
    if choice == 's':
      print(f"  Result: SKIPPED\n")
      results.append((name, "SKIPPED"))
      skipped += 1
      continue
    print("  Running...")
    try:
      ok, msg = fn(ctx)
      if ok is None:
        print(f"  Result: SKIPPED - {msg}\n")
        results.append((name, "SKIPPED"))
        skipped += 1
      elif ok:
        print(f"  Result: PASS - {msg}\n")
        results.append((name, "PASS"))
        passed += 1
      else:
        print(f"  Result: FAIL - {msg}\n")
        results.append((name, "FAIL"))
        failed += 1
    except Exception as e:
      print(f"  Result: FAIL - exception: {e}\n")
      results.append((name, "FAIL"))
      failed += 1

  print(f"=== RESULTS: {passed}/{total} PASSED, {failed} FAILED, "
        f"{skipped} SKIPPED ===")
  for name, status in results:
    print(f"  {status:7s}  {name}")


if __name__ == '__main__':
  main()
