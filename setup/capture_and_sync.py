#!/usr/bin/env python3
import subprocess
import re
import argparse
import sys
import time

iso = 3200
shutter_speed = 2

def exec(command):
    # print(command)
    # Execute the command, and check the return code.
    returncode = subprocess.call(command, stdout=subprocess.DEVNULL)
    if returncode != 0:
        print("Error: command '%s' returned %d" % (command, returncode))
        sys.exit(1)

def exec_shell(command):
    # print(command)
    # Execute the command, and check the return code.
    returncode = subprocess.call(command, shell=True)
    if returncode != 0:
        print("Error: command '%s' returned %d" % (command, returncode))
        sys.exit(1)
       

def capture_image():
    global iso, shutter_speed
    # print(f'Capturing image with iso={iso}, shutter_speed={shutter_speed}')
    # Capture in desired iso, aperture, and shutter speed, pipe output to /dev/null.
    exec(['gphoto2',
          '--set-config', f'iso={iso}',
          '--set-config', '/main/imgsettings/imageformat=RAW',
          '--set-config', f'shutterspeed={shutter_speed}',
          '--capture-image-and-download',
          '--filename', 'tmp.cr3',
          '--force-overwrite'])

def setup_camera():
    # Set the camera to JPEG mode
    exec(['gphoto2', '--set-config', '/main/imgsettings/imageformat=0'])
    # Set the camera to manual mode
    exec(['gphoto2', '--set-config', '/main/capturesettings/autoexposuremodedial=Manual'])

def extract_and_convert_coordinates_astap(output):
    # Define the regex pattern, to match output like this:
    # Solution found: 05: 36 03.8	-05° 27 14
    regex = r"Solution found: ([0-9]+): ([0-9]+) ([0-9]+\.[0-9]+)\t([+-])([0-9]+)° ([0-9]+) ([0-9]+)"

    # Search for the pattern in the output
    match = re.search(regex, output)
    if not match:
        print("No match found")
        return None, None

    # Extract matched groups
    alpha_h, alpha_m, alpha_s, delta_sign, delta_d, delta_m, delta_s = match.groups()
    # print(f"RA: {alpha_h}h{alpha_m}m{alpha_s}s, DEC: {delta_sign}{delta_d}°{delta_m}'{delta_s}")

    # Convert alpha (RA) to decimal degrees
    alpha = float(alpha_h) + float(alpha_m)/60 + float(alpha_s)/3600

    # Convert delta (DEC) to decimal degrees
    delta_multiplier = 1 if delta_sign == '+' else -1
    delta = delta_multiplier * (float(delta_d) + float(delta_m)/60 + float(delta_s)/3600)

    return alpha, delta

def run_plate_solve_astap(file, wcs_coords, focal_option):
    ASTAP_PATH = 'astap'
    astap_cli_command = [ASTAP_PATH, "-f", file]
    try:
        result = subprocess.run(astap_cli_command, 
                                text=True, 
                                capture_output=True,
                                check=True)
        print(result.stdout)
        print(result.stderr)
        ra, dec = extract_and_convert_coordinates_astap(result.stdout)
        return ra, dec
    except subprocess.CalledProcessError as e:
        return None, None

def set_tracking(device):
    exec_shell("indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_ON=On\"" % device)

def ReadIndi(device, propname):
  # Call indi_getprop to get the property value
  command = "indi_getprop \"%s.%s\"" % (device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Parse the output to get the property value.
  output = output.split("=")[1].strip()
  return output

def verify_sync(device, ra_expected, dec_expected):
    ra = float(ReadIndi(device, "EQUATORIAL_EOD_COORD.RA"))
    dec = float(ReadIndi(device, "EQUATORIAL_EOD_COORD.DEC"))
    if abs(ra - ra_expected) > 0.001 or abs(dec - dec_expected) > 0.001:
        print("ERROR: Sync failed")
        sys.exit(1)
    
def sync(device, ra, dec):
    exec_shell("indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_ON=On\"" % device)
    exec_shell("indi_setprop \"%s.ON_COORD_SET.SYNC=On\"" % device)
    exec_shell("indi_setprop \"%s.EQUATORIAL_EOD_COORD.RA=%f;DEC=%f\"" % (device, ra, dec))

def main():
    parser = argparse.ArgumentParser(description='Capture and plate solve an image, then sync the mount')
    parser.add_argument('-d', '--device', type=str, help='INDI device name', default='SkyAdventurer GTi')
    args = parser.parse_args()

    print("Set tracking...")
    set_tracking(args.device)
    print("Capturing image...")
    setup_camera()
    capture_image()
    print('Running plate solve...')
    ra, dec = run_plate_solve_astap('tmp.cr3', None, None)
    # ra = 2
    # dec = 89
    if ra is None or dec is None:
        print('Plate solve failed')
        sys.exit(1)
    print(f'RA: {ra}, DEC: {dec}')
    print('Syncing mount...')
    time.sleep(1)
    sync(args.device, ra, dec)
    print('Verifying sync...')
    verify_sync(args.device, ra, dec)
    print('Done.')

if __name__ == '__main__':
    main()