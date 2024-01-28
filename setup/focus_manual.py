#!/usr/bin/env python3

import argparse
import subprocess
import sys
import re
import shutil

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
    if subprocess.run(['gphoto2', '--set-config', 
                       '/main/imgsettings/imageformat=RAW'], 
                       stdout=subprocess.DEVNULL, stderr=stderr) != 0:
        print("Error setting camera to capture RAW.")
        exit(1)
    # Set the camera to manual mode
    if subprocess.run(['gphoto2', '--set-config',
                       '/main/capturesettings/autoexposuremodedial=Manual'], stdout=subprocess.DEVNULL, stderr=stderr) != 0:
        print("Error setting camera to manual mode.")
        exit(1)

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
load {file}
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
        # Extract the number of stars detected, and the FWHM. Sample output:
        # Found 343 Gaussian profile stars in image, channel #0 (FWHM 5.428217)
        regex = r"Found ([0-9]+) Gaussian profile stars in image, channel #0 \(FWHM ([0-9]+\.[0-9]+)\)"
        match = re.search(regex, result.stdout)
        if not match:
            print("No match found")
            return None, None
        num_stars, fwhm = match.groups()
        return num_stars, fwhm
    except subprocess.CalledProcessError as e:
        return None, None

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
    # Set up keyboard input to not display entered characters, and to not wait for ENTER.
    import termios
    import sys
    import tty
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    # Set up SIGINT handler to restore terminal settings.
    import signal
    def signal_handler(sig, frame):
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)


    while True:
        user_input = input()
        if user_input == 'q':
            break
        if args.verbose:
          print('Capturing image...')
        capture_image('tmp.cr3', args.iso, args.exposure)
        if args.verbose:
          print('Analyzing image...')
        num_stars, fwhm = run_star_detect_siril('.', 'tmp.cr3')
        # Create bar graph with FWHM, ranging from 1 bar for 1.0 to 30 bars for 6.0, clipping to [1.0, 6.0].
        bar_graph = int(max(min(40, (float(fwhm) - 1.0) * 5.0), 1.0))
        print(f'Found %4d stars, FWHM = %5.2f %s' % (int(num_stars), float(fwhm), f'{"❚" * bar_graph}'))

if __name__ == "__main__":
    main()