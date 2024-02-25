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

from sky_scripter.lib_indi import set_focus

VERBOSE = False

def setup_camera(args):
    global VERBOSE
    stderr = subprocess.DEVNULL
    if VERBOSE:
      stderr = None
    settings = [
        '--set-config', '/main/imgsettings/imageformat=RAW', 
        '--set-config', '/main/capturesettings/autoexposuremodedial=Manual',
        '--set-config', ('iso=%s' % args.iso),
        '--set-config', ('shutterspeed=%s' % args.exposure)]
    # print(settings)
    subprocess.run(['gphoto2'] + settings,
                    stdout=subprocess.DEVNULL, stderr=stderr)
    # Set the camera to RAW, manual mode, with selected exposure time and ISO.
    subprocess.run(['gphoto2', 
                    '--set-config'] + settings,
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
crop 2048 1366 4096 2732
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
            return None, None
        num_stars, fwhm = match.groups()
        return int(num_stars), float(fwhm)
    except subprocess.CalledProcessError as e:
        return None, None

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
    
    setup_camera(args)
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
    #       set_focus(args.device, f)
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
    fine_focus_results = []
    print('Fine focus scan from %d to %d in steps of %d' % (focus_min, focus_max, focus_step_fine))
    set_focus(args.device, focus_min - focus_step_initial)
    min_fwhm = 100
    best_focus = focus_min
    with tempfile.TemporaryDirectory() as tmpdirname:
      for f in range(focus_min, focus_max + 1, focus_step_fine):
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
              if fwhm < min_fwhm:
                  min_fwhm = fwhm
                  best_focus = f
          else:
              print(f"Focus value: {f} No stars detected")
    # Plot the fine results
    plot_focus_results(fine_focus_results, 'fine_focus_results.png')
    print(f"Best focus value: {best_focus}")
    set_focus(args.device, focus_min)
    set_focus(args.device, best_focus)

    # Test the focus.
    with tempfile.TemporaryDirectory() as tmpdirname:
        image_file = capture_image()
        shutil.copy(image_file, tmpdirname)
        num_stars, fwhm = run_star_detect_siril(tmpdirname)
        if num_stars is not None and fwhm is not None:
            print(f"Final focus: NumStars: {num_stars} FWHM: {fwhm}")
        else:
            print(f"Focus failed")

    

if __name__ == "__main__":
    main()