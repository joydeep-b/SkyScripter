#!/usr/bin/env python3

import argparse
import sys
import os
import time
import subprocess
from dateutil.parser import parse
import logging
from datetime import datetime, timedelta, timezone
from dateutil import tz
import signal
import multiprocessing

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

if sys.platform == 'darwin':
  SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
else:
  SIRIL_PATH = '/home/joydeepb/Siril-1.2.1-x86_64.AppImage'

from sky_scripter.lib_gphoto import GphotoClient
from sky_scripter.lib_indi import IndiMount, IndiFocuser
from sky_scripter.algorithms import auto_focus, align_to_object
from sky_scripter.util import init_logging, print_and_log, parse_coordinates
from sky_scripter.lib_phd2 import Phd2Client
from sky_scripter.lib_rachio import RachioClient, get_rachio_key
from sky_scripter.algorithms import auto_focus

# Global variable to indicate if the capture should be terminated - set by
# signal handler, and checked by the main loop.
terminate = False
terminate_count = 0

def signal_handler(signum, frame):
  global terminate
  terminate = True

def get_args():
  parser = argparse.ArgumentParser(description='Incremental preprocessing script')
  # Target to image.
  parser.add_argument('-d', '--directory', type=str, required=True,
      help='Directory to process images from')
  parser.add_argument('-v', '--verbose', action='store_true',
      help='Print verbose messages')
  return parser.parse_args()

def signal_handler(signum, frame):
  global terminate, terminate_count
  terminate = True
  if terminate_count > 1:
    print_and_log('Terminating immediately')
    sys.exit(1)
  if terminate_count > 0:
    print_and_log('Hit Ctrl-C again to terminate immediately')
  terminate_count += 1

def convert_lights(directory):
  siril_commands = f"""requires 1.2.0
convert light -out=../.process
close
"""
  # Define the command to run
  siril_cli_command = [SIRIL_PATH, "-d", directory, "-s", "-"]
  # Run the command and capture output
  try:
    result = subprocess.run(siril_cli_command,
                            input=siril_commands,
                            text=True,
                            capture_output=True,
                            check=True)
    if result.returncode != 0:
      print("Error running Siril.")
      exit(1)
  except subprocess.CalledProcessError as e:
    print("Error running Siril.")
    exit(1)

def calibrate_single_light(light_file, directory):
  # Check if pp_light file exists
  pp_file = light_file.replace('light_', 'pp_light_')
  bkg_file = light_file.replace('light_', 'bkg_pp_light_')
  if os.path.exists(os.path.join(directory, bkg_file)):
    return
  print(f'Calibrating {light_file}')
  siril_commands = f"""requires 1.2.0
calibrate_single {light_file} -dark=/Users/joydeepbiswas/Astrophotography/masters/dark/master_dark_MODE$READMODE:%1d$_GAIN$GAIN:%2d$_OFFSET$OFFSET:%2d$_EXPTIME$EXPTIME:%3d$ -flat=/Users/joydeepbiswas/Astrophotography/masters/flat/master_flat_$FILTER:%s$ -cc=dark
load {pp_file}
subsky 2 -tolerance=100
save {bkg_file}
close
"""
  # Define the command to run
  siril_cli_command = [SIRIL_PATH, "-d", directory, "-s", "-"]
  # Run the command and capture output
  try:
    result = subprocess.run(siril_cli_command,
                            input=siril_commands,
                            text=True,
                            capture_output=True,
                            check=True)
    if result.returncode != 0:
      print("Error running Siril.")
      exit(1)
  except subprocess.CalledProcessError as e:
    print("Error running Siril.")
    exit(1)

def calibrate_lights(directory):
  # Find all light_*.fit files in the directory
  light_files = [f for f in os.listdir(directory) if f.startswith('light_') and f.endswith('.fit')]

  with multiprocessing.Pool(15) as pool:
    pool.starmap(calibrate_single_light, [(f, directory) for f in light_files])

def register_and_stack(directory):
  print(f'Registering and stacking')
  siril_commands = f"""requires 1.2.0
register bkg_pp_light
stack r_bkg_pp_light rej 3 3  -norm=addscale -output_norm -weight_from_wfwhm -out=../../master_light_$FILTER:%s$
close
"""
  # Define the command to run
  siril_cli_command = [SIRIL_PATH, "-d", directory, "-s", "-"]
  # Run the command and capture output
  try:
    result = subprocess.run(siril_cli_command,
                            input=siril_commands,
                            text=True,
                            capture_output=True,
                            check=True)
    if result.returncode != 0:
      print("Error running Siril.")
      exit(1)
  except subprocess.CalledProcessError as e:
    print("Error running Siril.")
    exit(1)

def main():
  args = get_args()
  input_directory = os.path.abspath(args.directory)
  print(f'Processing directory {input_directory}')
  print(f'Converting lights')
  convert_lights(input_directory)
  process_dir = os.path.join(input_directory, '../.process')
  print(f'Calibrating lights')
  calibrate_lights(process_dir)
  register_and_stack(process_dir)

if __name__ == '__main__':
  main()