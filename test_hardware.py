#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from sky_scripter.lib_indi import IndiMount, IndiCamera, IndiFocuser
from sky_scripter.lib_phd2 import Phd2Client

results = []

def run_test(name, fn):
  try:
    extra = fn()
    msg = f"[PASS] {name}"
    if extra:
      msg += f": {extra}"
    print(msg)
    results.append(("PASS", name))
  except Exception as e:
    print(f"[FAIL] {name}: {e}")
    results.append(("FAIL", name))

def skip_test(name, reason):
  print(f"[SKIP] {name} ({reason})")
  results.append(("SKIP", name))

def test_indi_server():
  out = subprocess.check_output(
    ["indi_getprop", "-t", "2"], timeout=5, stderr=subprocess.STDOUT)
  assert len(out) > 0, "indi_getprop returned nothing"

def test_mount_readout(mount_name):
  mount = IndiMount(mount_name)
  ra, dec = mount.get_ra_dec()
  print(f"       RA={ra:.4f}h  DEC={dec:.4f}°")

def test_mount_park_unpark(mount_name):
  resp = input("Mount will move. Continue? [y/N] ").strip().lower()
  if resp != 'y':
    raise Exception("Skipped by user")
  mount = IndiMount(mount_name)
  mount.unpark()
  time.sleep(2)
  state = mount.get_tracking_state()
  print(f"       Tracking state after unpark: {state}")
  mount.park()

def test_focuser(focuser_name):
  foc = IndiFocuser(focuser_name)
  pos0 = foc.get_focus()
  print(f"       Initial position: {pos0}")
  foc.set_focus(pos0 + 10)
  pos1 = foc.get_focus()
  foc.set_focus(pos0)
  pos2 = foc.get_focus()
  assert abs(pos2 - pos0) <= 5, f"Position drift: started {pos0}, ended {pos2}"

def test_filter_wheel(camera_name):
  cam = IndiCamera(camera_name)
  names = cam.get_filter_names()
  print(f"       Filters: {names}")
  for name in names[:3]:
    cam.change_filter(name)

def test_camera_capture(camera_name):
  cam = IndiCamera(camera_name)
  path = "/tmp/test_capture.fits"
  if os.path.exists(path):
    os.remove(path)
  cam.capture_image(path, gain=56, exposure=1)
  assert os.path.exists(path), "FITS file not created"
  assert os.path.getsize(path) > 0, "FITS file is empty"

def test_fits_headers():
  from astropy.io import fits
  path = "/tmp/test_capture.fits"
  with fits.open(path) as hdul:
    header = hdul[0].header
    check_keys = ["EXPOSURE", "DATE-OBS", "CCD-TEMP", "GAIN",
                   "OFFSET", "XBINNING", "YBINNING", "INSTRUME"]
    found = []
    for k in check_keys:
      if k in header:
        found.append(f"{k}={header[k]}")
    if found:
      print(f"       AUTO headers: {', '.join(found)}")
    missing = [k for k in check_keys if k not in header]
    if missing:
      print(f"       Missing headers: {', '.join(missing)}")

def test_cooler(camera_name):
  cam = IndiCamera(camera_name)
  temp = cam.get_temperature()
  return f"{temp:.1f}°C"

def test_phd2():
  client = Phd2Client()
  client.connect()
  return f"version {client.version}"

def test_roof_status(status_file):
  with open(status_file, 'r') as f:
    content = f.readline().strip()
  return f"status={content}"

def test_websocket_server():
  from sky_scripter.alert_bus import AlertBus
  from sky_scripter.web_monitor.server import MonitorServer

  class _Stub:
    state = type('', (), {'value': 'test'})()
    session_id = "test"
    focus_position = 0
    focus_fwhm = 0.0
    _terminate = False
    @property
    def status(self):
      return {}
  class _StubLogger:
    def on_entry(self, cb): pass
    def get_recent(self, n): return []

  alert_bus = AlertBus()
  stub = _Stub()
  srv = MonitorServer(stub, stub, stub, stub, alert_bus, _StubLogger(),
                      port=18765, http_port=18080)
  srv.start()
  time.sleep(0.5)

  async def _check():
    async with __import__('websockets').connect("ws://localhost:18765") as ws:
      msg = await asyncio.wait_for(ws.recv(), timeout=3)
      data = json.loads(msg)
      assert data.get("type") == "status", f"Unexpected: {data}"

  asyncio.run(_check())

def main():
  parser = argparse.ArgumentParser(description="Daytime hardware test")
  parser.add_argument("--mount", default="ZWO AM5")
  parser.add_argument("--camera", default="QHY CCD QHY268M-b93fd94")
  parser.add_argument("--focuser", default="ZWO EAF")
  parser.add_argument("--roof-status-file", default=None)
  parser.add_argument("--skip-mount-move", action="store_true")
  args = parser.parse_args()

  print("\n=== HARDWARE TEST ===")

  run_test("INDI server connectivity", test_indi_server)
  run_test("Mount RA/Dec readout", lambda: test_mount_readout(args.mount))

  if args.skip_mount_move:
    skip_test("Mount park/unpark", "--skip-mount-move")
  else:
    run_test("Mount park/unpark", lambda: test_mount_park_unpark(args.mount))

  run_test("Focuser position read/move", lambda: test_focuser(args.focuser))
  run_test("Filter wheel cycling", lambda: test_filter_wheel(args.camera))
  run_test("Camera 1s dark capture", lambda: test_camera_capture(args.camera))
  run_test("FITS header verification", test_fits_headers)
  run_test("Cooler temperature readout", lambda: test_cooler(args.camera))
  run_test("PHD2 connection", test_phd2)

  if args.roof_status_file:
    run_test("Roof status", lambda: test_roof_status(args.roof_status_file))
  else:
    skip_test("Roof status", "no --roof-status-file")

  run_test("WebSocket server", test_websocket_server)

  passed = sum(1 for s, _ in results if s == "PASS")
  failed = sum(1 for s, _ in results if s == "FAIL")
  skipped = sum(1 for s, _ in results if s == "SKIP")
  total = passed + failed + skipped
  print(f"\n=== RESULTS: {passed}/{total} PASSED, {failed} FAILED, {skipped} SKIPPED ===\n")

if __name__ == "__main__":
  main()
