#!/usr/bin/env python3

import sys
import time
import argparse
import subprocess
import os
import signal
import logging

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount
from sky_scripter.util import init_logging

def main():
  logfile = os.path.join(script_dir, '..', '.logs', 'tracking-' + time.strftime("%Y-%m-%d-%H-%M-%S") + '.log')
  init_logging(logfile)

  parser = argparse.ArgumentParser(
      description='Set tracking details for an INDI device')
  parser.add_argument('-d', '--device', type=str, 
                      help='INDI device name', default='Star Adventurer GTi')
  args = parser.parse_args()
  print("Using device %s" % args.device)

  mount = IndiMount(args.device)

  tracking_mode = mount.get_tracking_mode()
  print("Current tracking mode: %s" % tracking_mode)
  desired_tracking_mode = input("Select desired tracking mode: [S]idereal, [L]unar, S[o]lar (default = Unchanged) : ")
  if desired_tracking_mode == "s":
    logging.info("Setting tracking mode to sidereal")
    mount.set_tracking_mode("TRACK_SIDEREAL")
  elif desired_tracking_mode == "l":
    logging.info("Setting tracking mode to lunar")
    mount.set_tracking_mode("TRACK_LUNAR")
  elif desired_tracking_mode == "o":
    logging.info("Setting tracking mode to solar")
    mount.set_tracking_mode("TRACK_SOLAR")
  else:
    logging.info("Keeping tracking mode unchanged")

  # Change tracking state as per user input.
  tracking_state = mount.get_tracking_state()
  print("Current tracking state: %s" % tracking_state)
  desired_tracking_state = input("Select desired tracking state: O[n], O[f]f (default = Unchanged): ")
  if desired_tracking_state == "n":
    logging.info("Starting tracking")
    mount.start_tracking()
  elif desired_tracking_state == "f":
    logging.info("Stopping tracking")
    mount.stop_tracking()
  else:
    logging.info("Keeping tracking state unchanged")

if __name__ == "__main__":
  main()