#!/usr/bin/env python3

import argparse
import subprocess
import sys
import re
import shutil
import os
import tempfile
import time
script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import get_focus, adjust_focus

SIMULATE = False
VERBOSE = False

def setup_camera(args):
    global SIMULATE, VERBOSE
    if SIMULATE:
        return
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    setttings = [
        '/main/imgsettings/imageformat=RAW', 
        '/main/capturesettings/autoexposuremodedial=Manual',
        'iso=%s' % args.iso,
        'shutterspeed=%s' % args.exposure]
    # Set the camera to RAW, manual mode, with selected exposure time and ISO.
    subprocess.run(['gphoto2', 
                    '--set-config'] + setttings,
                    stdout=subprocess.DEVNULL, stderr=stderr)
    

def capture_image(filename):
    global SIMULATE, VERBOSE
    if SIMULATE:
        # Copy sample_data/NGC2244.cr3 to filename.
        shutil.copyfile('sample_data/NGC2244.cr3', filename)
        return
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    result = subprocess.run(['gphoto2',
                              '--capture-image-and-download',
                              '--filename', filename,
                              '--force-overwrite'], 
                              stdout=subprocess.DEVNULL, stderr=stderr)
    if result.returncode != 0:
        print("Error capturing image.")
        # exit(1)


def run_star_detect_siril(this_dir, file):
    # If MacOS, use the Siril.app version
    if sys.platform == 'darwin':
      SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/siril-cli'
    else:
      SIRIL_PATH = '/home/joydeepb/Siril-1.2.1-x86_64.AppImage'
      
    siril_commands = f"""requires 1.2.0
convert light
calibrate_single light_00001 -bias="=2048" -debayer -cfa -equalize_cfa 
load pp_light_00001
findstar
close
"""
    # Define the command to run
    siril_cli_command = [SIRIL_PATH, "-d", this_dir, "-s", "-"]

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
        # print(result.stdout)
        # Extract the number of stars detected, and the FWHM. Sample output:
        # Found 343 Gaussian profile stars in image, channel #0 (FWHM 5.428217)
        regex = r"Found ([0-9]+) Gaussian profile stars in image, channel #1 \(FWHM ([0-9]+\.[0-9]+)\)"
        match = re.search(regex, result.stdout)
        if not match:
            print("No match found")
            return None, None
        num_stars, fwhm = match.groups()
        return int(num_stars), float(fwhm)
    except subprocess.CalledProcessError as e:
        return None, None

def main():
    global SIMULATE, VERBOSE
    parser = argparse.ArgumentParser(description='Manually focus a telescope using a camera and star FWHM detection')

    # Optional arguments: ISO, exposure time
    parser.add_argument('-d', '--device', type=str,
                        help='INDI focuser device name', default='ASI EAF')
    parser.add_argument('-i', '--iso', type=int, 
                        help='ISO setting for camera', default=1600)
    parser.add_argument('-e', '--exposure', type=int, 
                        help='Exposure time for camera', default=2)
    parser.add_argument('-s', '--simulate', action='store_true',
                        help='Simulate camera capture')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()
    
    SIMULATE = args.simulate
    VERBOSE = args.verbose
    # Set up the camera
    setup_camera(args)
    print('Press ENTER to capture an image and analyze it, CTRL-C to quit.')
    # Make a temporary directory to store the image.
    
    with tempfile.TemporaryDirectory() as tmpdirname:
      filename = os.path.join(tmpdirname, 'tmp.cr3')
      while True:
          user_input = input()
          if user_input == 'q':
              sys.exit(0)
          elif user_input == '[':
              adjust_focus(args.device, -100)
              print('Focus value:', get_focus(args.device))
          elif user_input == ']':
              adjust_focus(args.device, 100)
              print('Focus value:', get_focus(args.device))
          elif user_input == ',':
              adjust_focus(args.device, -10)
              print('Focus value:', get_focus(args.device))
          elif user_input == '.':
              adjust_focus(args.device, 10)
              print('Focus value:', get_focus(args.device))
          else:
              if args.verbose:
                  print('Capturing image...')
              # Remove all files in tmpdirname.
              for file in os.listdir(tmpdirname):
                  os.remove(os.path.join(tmpdirname, file))
              capture_image(filename)
              if args.verbose:
                  print('Analyzing image...')
              num_stars, fwhm = run_star_detect_siril(tmpdirname, 'tmp.cr3')
              # Create bar graph with FWHM, ranging from 1 bar for 1.0 to 30
              # bars for 6.0, clipping to [1.0, 6.0].
              if fwhm is None:
                  bar_graph = 0
                  fwhm = 0
              else:
                  bar_graph = int(max(min(60, (float(fwhm) - 1.0) * 5.0), 1.0))
              if num_stars is None:
                  print('No stars found')
                  num_stars = 0
              print(f'Found %4d stars, FWHM = %5.2f %s' % (num_stars, fwhm, f'{"❚" * bar_graph}'))
              # time.sleep(1)

if __name__ == "__main__":
    main()