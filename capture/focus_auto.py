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

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import set_focus

VERBOSE = False

def setup_camera(args):
    global VERBOSE
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
    

def capture_image():
    global VERBOSE
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    # Filename = .focus/YYYY-MM-DDTHH:MM:SS.RAW
    filename = os.path.join('.focus', time.strftime("%Y-%m-%dT%H:%M:%S.RAW"))
    result = subprocess.run(['gphoto2',
                              '--capture-image-and-download',
                              '--filename', filename,
                              '--force-overwrite'], 
                              stdout=subprocess.DEVNULL, stderr=stderr)
    if result.returncode != 0:
        print("Error capturing image.")
        exit(1)
    return filename


def run_star_detect_siril(dir):
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
    siril_cli_command = [SIRIL_PATH, "-d", dir, "-s", "-"]

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

def plot_initial_focus_results(results):
    focus_values = [x[0] for x in results]
    num_stars = [x[1] for x in results]
    fwhm_values = [x[2] for x in results]
    fig, ax1 = plt.subplots()
    ax1.set_xlabel('Focus value')
    ax1.set_ylabel('Num stars', color='tab:blue')
    ax1.plot(focus_values, num_stars, color='tab:blue')
    ax1.tick_params(axis='y', labelcolor='tab:blue')
    ax2 = ax1.twinx()
    ax2.set_ylabel('FWHM', color='tab:red')
    ax2.plot(focus_values, fwhm_values, color='tab:red')
    ax2.tick_params(axis='y', labelcolor='tab:red')
    fig.tight_layout()
    plt.savefig('initial_focus_results.png')
   
def main():
    global VERBOSE
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
    
    # Initial scan to detect number of stars
    focus_min = 3000
    focus_max = 6000
    focus_step_initial = 200
    focus_step_fine = 50
    focus_num_fine_steps = 5

    initial_focus_results = []
    with tempfile.TemporaryDirectory() as tmpdirname:
      for f in range(focus_min, focus_max, focus_step_initial):
          set_focus(args.device, f)
          image_file = capture_image()
          for file in os.listdir(tmpdirname):
              os.remove(os.path.join(tmpdirname, file))
          # Copy the image to the temporary directory
          shutil.copy(image_file, tmpdirname)
          num_stars, fwhm = run_star_detect_siril(tmpdirname)
          if num_stars is not None and fwhm is not None:
              print(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm}")
              initial_focus_results.append((f, num_stars, fwhm))
    
    # Plot the initial results
    plot_initial_focus_results(initial_focus_results)

    # Find the focus value with the maximum number of stars
    max_num_stars = max([x[1] for x in initial_focus_results])
    best_initial_focus = [x[0] for x in initial_focus_results if x[1] == max_num_stars][0]

    # Refine the focus
    focus_min = best_initial_focus - focus_step_fine * focus_num_fine_steps / 2
    focus_max = best_initial_focus + focus_step_fine * focus_num_fine_steps / 2
    fine_focus_results = []
    with tempfile.TemporaryDirectory() as tmpdirname:
      for f in range(focus_min, focus_max, focus_step_fine):
          set_focus(args.device, f)
          image_file = capture_image()
          for file in os.listdir(tmpdirname):
              os.remove(os.path.join(tmpdirname, file))
          # Copy the image to the temporary directory
          shutil.copy(image_file, tmpdirname)
          num_stars, fwhm = run_star_detect_siril(tmpdirname)
          if num_stars is not None and fwhm is not None:
              print(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm}")
              fine_focus_results.append((f, num_stars, fwhm))
    

if __name__ == "__main__":
    main()