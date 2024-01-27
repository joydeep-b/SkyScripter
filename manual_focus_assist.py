import argparse
import subprocess
import sys
import re

def setup_camera():
    # Set the camera to JPEG mode
    if subprocess.run(['gphoto2', '--set-config', '/main/imgsettings/imageformat=RAW']) != 0:
        print("Error setting camera to capture RAW.")
        exit(1)
    # Set the camera to manual mode
    if subprocess.run(['gphoto2', '--set-config', '/main/capturesettings/autoexposuremodedial=Manual']) != 0:
        print("Error setting camera to manual mode.")
        exit(1)

def capture_image(filename, iso, shutter_speed):
    result = subprocess.run(['gphoto2',
                              '--set-config', f'iso={iso}',
                              '--set-config', f'shutterspeed={shutter_speed}',
                              '--capture-image-and-download',
                              '--filename', filename,
                              '--force-overwrite'], stdout=subprocess.DEVNULL)
    if result.returncode != 0:
        print("Error capturing image.")
        # exit(1)


def run_star_detect_siril(this_dir, file):
    # If MacOS, use the Siril.app version
    if sys.platform == 'darwin':
      SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/siril-cli'
    else:
      SIRIL_PATH = 'siril-cli'
      
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
    parser = argparse.ArgumentParser(description='Manually focus a telescope using a camera and star FWHM detection')

    # Optional arguments: ISO, exposure time
    parser.add_argument('-i', '--iso', type=int, help='ISO setting for camera', default=1600)
    parser.add_argument('-e', '--exposure', type=int, help='Exposure time for camera', default=2)

    args = parser.parse_args()
    # Set up the camera
    setup_camera()
    print('Press ENTER to capture an image and analyze it, or type "q" to quit')
    while True:
        user_input = input()
        if user_input == 'q':
            break
        print('Capturing image...')
        capture_image('tmp.cr3', args.iso, args.exposure)
        print('Analyzing image...')
        num_stars, fwhm = run_star_detect_siril('.', 'tmp.cr3')
        print(f'Found %4d stars, FWHM = %5.2f' % (int(num_stars), float(fwhm)))

if __name__ == "__main__":
    main()