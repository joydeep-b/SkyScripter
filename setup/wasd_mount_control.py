#!/usr/bin/env python3

import argparse
import os
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount


@contextmanager
def cbreak_stdin():
  """Temporarily put stdin into cbreak mode for single-key reads."""
  fd = sys.stdin.fileno()
  old_attrs = termios.tcgetattr(fd)
  try:
    tty.setcbreak(fd)
    yield
  finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


class MountController:
  def __init__(self, mount: IndiMount):
    self.mount = mount

  def stop_all(self):
    self.mount.write(
      "TELESCOPE_MOTION_NS",
      ["MOTION_NORTH", "MOTION_SOUTH"],
      ["Off", "Off"],
    )
    self.mount.write(
      "TELESCOPE_MOTION_WE",
      ["MOTION_WEST", "MOTION_EAST"],
      ["Off", "Off"],
    )

  def move_north(self):
    self.mount.write(
      "TELESCOPE_MOTION_NS",
      ["MOTION_NORTH", "MOTION_SOUTH"],
      ["On", "Off"],
    )

  def move_south(self):
    self.mount.write(
      "TELESCOPE_MOTION_NS",
      ["MOTION_NORTH", "MOTION_SOUTH"],
      ["Off", "On"],
    )

  def move_west(self):
    self.mount.write(
      "TELESCOPE_MOTION_WE",
      ["MOTION_WEST", "MOTION_EAST"],
      ["On", "Off"],
    )

  def move_east(self):
    self.mount.write(
      "TELESCOPE_MOTION_WE",
      ["MOTION_WEST", "MOTION_EAST"],
      ["Off", "On"],
    )

  def nudge_north(self, nudge_ms: int):
    self.mount.write(
      "TELESCOPE_TIMED_GUIDE_NS",
      ["TIMED_GUIDE_N", "TIMED_GUIDE_S"],
      [nudge_ms, 0],
    )

  def nudge_south(self, nudge_ms: int):
    self.mount.write(
      "TELESCOPE_TIMED_GUIDE_NS",
      ["TIMED_GUIDE_N", "TIMED_GUIDE_S"],
      [0, nudge_ms],
    )

  def nudge_west(self, nudge_ms: int):
    self.mount.write(
      "TELESCOPE_TIMED_GUIDE_WE",
      ["TIMED_GUIDE_W", "TIMED_GUIDE_E"],
      [nudge_ms, 0],
    )

  def nudge_east(self, nudge_ms: int):
    self.mount.write(
      "TELESCOPE_TIMED_GUIDE_WE",
      ["TIMED_GUIDE_W", "TIMED_GUIDE_E"],
      [0, nudge_ms],
    )


def parse_args():
  parser = argparse.ArgumentParser(
    description="Control INDI mount RA/DEC with WASD keys."
  )
  parser.add_argument(
    "-d",
    "--device",
    type=str,
    default="SkyAdventurer GTi",
    help="INDI mount device name (default: SkyAdventurer GTi).",
  )
  parser.add_argument(
    "--nudge",
    type=int,
    default=0,
    help="Nudge mode pulse duration in ms (if > 0).",
  )
  parser.add_argument(
    "--hold-timeout",
    type=float,
    default=0.7,
    help=(
      "Continuous mode only: stop motion if no repeated keypress within this "
      "many seconds (default: 0.7)."
    ),
  )
  parser.add_argument(
    "--rate",
    type=float,
    default=None,
    help=(
      "Optional guide rate for both axes. If unsupported by driver, prints a "
      "warning and continues."
    ),
  )
  return parser.parse_args()


def print_help(device: str, nudge_ms: int):
  mode_name = "nudge" if nudge_ms > 0 else "continuous (joystick-like)"
  print(f"Connected to: {device}")
  print(f"Control mode: {mode_name}")
  if nudge_ms > 0:
    print(f"Nudge duration: {nudge_ms} ms")
  print("")
  print("Controls:")
  print("  w/s : DEC + / DEC -")
  print("  a/d : RA  - / RA  +")
  print("  x   : stop all motion")
  print("  q   : quit")
  print("")
  if nudge_ms == 0:
    print("Tip: hold a key for repeated movement; release to stop.")
  print("")


def ensure_connected(mount: IndiMount):
  try:
    state = mount.read("CONNECTION.CONNECT", timeout=2)
  except Exception as exc:
    raise RuntimeError(f"Unable to query INDI mount connection: {exc}") from exc

  if state != "On":
    try:
      mount.write("CONNECTION", "CONNECT", "On")
      time.sleep(0.5)
    except Exception as exc:
      raise RuntimeError(
        f"Mount appears disconnected and reconnect failed: {exc}"
      ) from exc


def maybe_set_guide_rate(mount: IndiMount, rate: float | None):
  if rate is None:
    return
  try:
    mount.write(
      "GUIDE_RATE",
      ["GUIDE_RATE_WE", "GUIDE_RATE_NS"],
      [rate, rate],
    )
    print(f"Guide rate set to {rate}")
  except Exception as exc:
    print(f"Warning: unable to set guide rate on this mount: {exc}")


def handle_nudge_mode(controller: MountController, key: str, nudge_ms: int):
  if key == "w":
    controller.nudge_north(nudge_ms)
  elif key == "s":
    controller.nudge_south(nudge_ms)
  elif key == "a":
    controller.nudge_west(nudge_ms)
  elif key == "d":
    controller.nudge_east(nudge_ms)


def handle_continuous_mode(controller: MountController, key: str):
  if key == "w":
    controller.move_north()
  elif key == "s":
    controller.move_south()
  elif key == "a":
    controller.move_west()
  elif key == "d":
    controller.move_east()


def run_controller(mount: IndiMount, nudge_ms: int, hold_timeout: float):
  controller = MountController(mount)
  last_motion_key_ts = 0.0
  has_active_motion = False

  print_help(mount.device, nudge_ms)

  # Keep terminal responsive while supporting key-repeat based "hold" behavior.
  with cbreak_stdin():
    while True:
      ready, _, _ = select.select([sys.stdin], [], [], 0.05)

      if ready:
        key = sys.stdin.read(1).lower()
        if key == "q":
          break
        if key == "x":
          controller.stop_all()
          has_active_motion = False
          continue

        if key in ("w", "a", "s", "d"):
          if nudge_ms > 0:
            handle_nudge_mode(controller, key, nudge_ms)
          else:
            handle_continuous_mode(controller, key)
            has_active_motion = True
            last_motion_key_ts = time.time()

      if nudge_ms == 0 and has_active_motion:
        if time.time() - last_motion_key_ts > hold_timeout:
          controller.stop_all()
          has_active_motion = False

  controller.stop_all()


def main():
  if not sys.stdin.isatty():
    print("Error: this script requires a real terminal (TTY).")
    return 1

  args = parse_args()
  if args.nudge < 0:
    print("Error: --nudge must be >= 0")
    return 1
  if args.hold_timeout <= 0:
    print("Error: --hold-timeout must be > 0")
    return 1

  mount = IndiMount(args.device)
  try:
    ensure_connected(mount)
    maybe_set_guide_rate(mount, args.rate)
    run_controller(mount, args.nudge, args.hold_timeout)
  except KeyboardInterrupt:
    print("\nInterrupted, stopping mount motion.")
    try:
      MountController(mount).stop_all()
    except Exception:
      pass
  except Exception as exc:
    print(f"Error: {exc}")
    try:
      MountController(mount).stop_all()
    except Exception:
      pass
    return 1

  print("Exited cleanly.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
