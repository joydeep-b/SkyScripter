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

from sky_scripter.lib_indi import read_indi, write_indi

SIMULATE = False
VERBOSE = False

def setup_camera():
    global SIMULATE, VERBOSE
    if SIMULATE:
        return
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    # Set the camera to JPEG mode
    subprocess.run(['gphoto2', '--set-config', '/main/imgsettings/imageformat=RAW'], stdout=subprocess.DEVNULL)
    # Set the camera to manual mode
    subprocess.run(['gphoto2', '--set-config',
                       '/main/capturesettings/autoexposuremodedial=Manual'], stdout=subprocess.DEVNULL)
    

def capture_image(filename, iso, shutter_speed):
    global SIMULATE, VERBOSE
    if SIMULATE:
        # Copy sample_data/NGC2244.cr3 to filename.
        shutil.copyfile('sample_data/NGC2244.cr3', filename)
        return
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    result = subprocess.run(['gphoto2',
                              '--set-config', f'iso={iso}',
                              '--set-config', f'shutterspeed={shutter_speed}',
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
        return num_stars, fwhm
    except subprocess.CalledProcessError as e:
        return None, None

def adjust_focus(steps):
    focus_value = int(read_indi('ASI EAF', 'ABS_FOCUS_POSITION.FOCUS_ABSOLUTE_POSITION'))
    if focus_value + steps < 0:
        print('Focus value cannot be negative.')
        return
    err = write_indi('ASI EAF', 'ABS_FOCUS_POSITION', ['FOCUS_ABSOLUTE_POSITION'], [focus_value + steps])
    if err:
        print('Error adjusting focus.')
        return
    print(f'New focus value: {focus_value + steps}')


def main():
    global SIMULATE, VERBOSE
    parser = argparse.ArgumentParser(description='Manually focus a telescope using a camera and star FWHM detection')

    # Optional arguments: ISO, exposure time
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
    setup_camera()
    print('Press ENTER to capture an image and analyze it, CTRL-C to quit.')
    # # Set up keyboard input to not display entered characters, and to not wait for ENTER.
    # import termios
    # import sys
    # import tty
    # fd = sys.stdin.fileno()
    # old_settings = termios.tcgetattr(fd)
    # tty.setcbreak(fd)
    # # Set up SIGINT handler to restore terminal settings.
    # import signal
    # def signal_handler(sig, frame):
    #     termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    #     sys.exit(0)
    # signal.signal(signal.SIGINT, signal_handler)

    # Make a temporary directory to store the image.
    
    with tempfile.TemporaryDirectory() as tmpdirname:
      filename = os.path.join(tmpdirname, 'tmp.cr3')
      while True:
          user_input = input()
          if user_input == 'q':
              sys.exit(0)
          elif user_input == '[':
              adjust_focus(-100)
          elif user_input == ']':
              adjust_focus(100)
          elif user_input == ',':
              adjust_focus(-10)
          elif user_input == '.':
              adjust_focus(10)
          else:
              if args.verbose:
                  print('Capturing image...')
              # Remove all files in tmpdirname.
              for file in os.listdir(tmpdirname):
                  os.remove(os.path.join(tmpdirname, file))
              capture_image(filename, args.iso, args.exposure)
              if args.verbose:
                  print('Analyzing image...')
              num_stars, fwhm = run_star_detect_siril(tmpdirname, 'tmp.cr3')
              # Create bar graph with FWHM, ranging from 1 bar for 1.0 to 30 bars for 6.0, clipping to [1.0, 6.0].
              bar_graph = int(max(min(60, (float(fwhm) - 1.0) * 5.0), 1.0))
              print(f'Found %4d stars, FWHM = %5.2f %s' % (int(num_stars), float(fwhm), f'{"❚" * bar_graph}'))
              # time.sleep(1)

if __name__ == "__main__":
    main()