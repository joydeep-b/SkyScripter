#!/usr/bin/env python3

import argparse
import subprocess
import sys
import re
import shutil
import os
import tempfile
import time
import matplotlib.pyplot as plt
import math

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiFocuser
from sky_scripter.lib_gphoto import GphotoClient
from sky_scripter.util import init_logging, run_star_detect_siril, print_and_log
from sky_scripter.algorithms import auto_focus

VERBOSE = False

def main():
  global VERBOSE
  init_logging('focus_auto')
  parser = argparse.ArgumentParser(description='Manually focus a telescope using a camera and star FWHM detection')

  # Optional arguments: ISO, exposure time
  parser.add_argument('-d', '--device', type=str,
                      help='INDI focuser device name', default='ASI EAF')
  parser.add_argument('-i', '--iso', type=int, 
                      help='ISO setting for camera', default=1600)
  parser.add_argument('-e', '--exposure', type=int, 
                      help='Exposure time for camera', default=2)
  parser.add_argument('-v', '--verbose', action='store_true',
                      help='Verbose output')

  args = parser.parse_args()
  
  VERBOSE = args.verbose
  camera  = GphotoClient()
  camera.initialize(image_format='RAW', 
                    mode='Manual', 
                    iso=args.iso, 
                    shutter_speed=args.exposure)
  focuser = IndiFocuser(args.device)

  # Initial scan to detect number of stars
  focus_min = 5050
  focus_max = 5150
  focus_step_initial = 50
  refine_multiplier = 5
  focus_step_fine = focus_step_initial // refine_multiplier

  best_focus, min_fwhm, focus_results = auto_focus(
      focuser, camera, focus_min, focus_max, focus_step_fine)
  print_and_log(f"Best focus value: {best_focus} Min FWHM: {min_fwhm}")
  # Plot the fine results

  # Test the focus.
  with tempfile.TemporaryDirectory() as tempdir:
    image_file = os.path.join(tempdir, 'test.RAW')
    camera.capture_image(image_file)
    num_stars, fwhm = run_star_detect_siril(image_file)
    if num_stars is not None and fwhm is not None:
      print_and_log(f"Final focus: NumStars: {num_stars} FWHM: {fwhm}")
    else:
      print_and_log(f"Focus verification failed")

if __name__ == "__main__":
  main()