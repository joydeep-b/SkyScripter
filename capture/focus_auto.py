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
from sky_scripter.util import init_logging, run_star_detect_siril
from sky_scripter.algorithms import auto_focus_fine

VERBOSE = False

def plot_focus_results(results, filename):
  focus_values = [x[0] for x in results]
  num_stars = [x[1] for x in results]
  fwhm_values = [x[2] for x in results]
  fig, ax1 = plt.subplots()
  ax1.set_xlabel('Focus value')
  ax1.set_ylabel('Num stars', color='tab:blue')
  ax1.plot(focus_values, num_stars, color='tab:blue')
  ax1.scatter(focus_values, num_stars, color='tab:blue', marker='+')
  ax1.tick_params(axis='y', labelcolor='tab:blue')
  ax2 = ax1.twinx()
  ax2.set_ylabel('FWHM', color='tab:red')
  ax2.plot(focus_values, fwhm_values, color='tab:red')
  ax2.scatter(focus_values, fwhm_values, color='tab:red', marker='x')
  ax2.tick_params(axis='y', labelcolor='tab:red')
  fig.tight_layout()
  plt.savefig(filename)

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

  # print('Initial focus scan from %d to %d in steps of %d' % (focus_min, focus_max, focus_step_initial))
  # initial_focus_results = []
  # with tempfile.TemporaryDirectory() as tmpdirname:
  #   for f in range(focus_min, focus_max, focus_step_initial):
  #       focuser.set_focus(f)
  #       image_file = capture_image()
  #       for file in os.listdir(tmpdirname):
  #           os.remove(os.path.join(tmpdirname, file))
  #       # Copy the image to the temporary directory
  #       shutil.copy(image_file, tmpdirname)
  #       num_stars, fwhm = run_star_detect_siril(tmpdirname)
  #       if num_stars is not None and fwhm is not None:
  #           print(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm}")
  #           initial_focus_results.append((f, num_stars, fwhm))
  #       else:
  #           print(f"Focus value: {f} No stars detected")
  
  # # Plot the initial results
  # plot_focus_results(initial_focus_results, 'initial_focus_results.png')

  # Find the focus value with the maximum number of stars
  # best_initial_focus = focus_min
  # max_num_stars = 0
  # for i in range(1, len(initial_focus_results)):
  #     if initial_focus_results[i][1] > max_num_stars:
  #         max_num_stars = initial_focus_results[i][1]
  #         best_initial_focus = initial_focus_results[i][0]
  # print(f"Best initial focus value: {best_initial_focus}")

  # Refine the focus
  # focus_min = best_initial_focus - 2 * focus_step_initial
  # focus_max = best_initial_focus + 2 * focus_step_initial
  # Plot the fine results
  best_focus, min_fwhm, fine_focus_results = auto_focus_fine(
      focuser, camera, focus_min, focus_max, focus_step_fine)
  print(f"Best focus value: {best_focus} Min FWHM: {min_fwhm}")
  # Plot the fine results
  plot_focus_results(fine_focus_results, 'fine_focus_results.png')

  # Test the focus.
  image_file = os.path.join(os.getcwd(), 
                            '.focus', 
                            time.strftime("%Y-%m-%dT%H:%M:%S.RAW"))
  camera.capture_image(image_file)
  num_stars, fwhm = run_star_detect_siril(image_file)
  if num_stars is not None and fwhm is not None:
    print(f"Final focus: NumStars: {num_stars} FWHM: {fwhm}")
  else:
    print(f"Focus failed")

    

if __name__ == "__main__":
  main()