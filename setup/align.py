#!/usr/bin/env python3

import subprocess
import re
import argparse
import sys
from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord
from astropy.coordinates import FK5
from astropy.coordinates import ICRS
import astropy.units as units
from astropy.coordinates import GCRS
import astropy.time
import math

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
          '--set-config', f'shutterspeed={shutter_speed}',
          '--capture-image-and-download',
          '--filename', 'tmp.jpg',
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
        # print(result.stdout)
        # print(result.stderr)
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


def get_wcs_coordinates(object_name):
    # Query the object
    result_table = Simbad.query_object(object_name)

    if result_table is None:
        print(f"ERROR: Unable to find object '{object_name}'")
        sys.exit(1)
    # Extract RA and DEC
    ra = result_table['RA'][0]
    dec = result_table['DEC'][0]
    # print(f"RA: {ra}, DEC: {dec}")

    # Convert J2000 coordinates to JNow.
    c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg), frame=ICRS())
    # jnow_coord = c.transform_to(GCRS(obstime=astropy.time.Time.now()))
    jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))
    
    ra = jnow_coord.ra.to(units.hourangle)
    dec = c.dec
    
    coord = SkyCoord(ra, dec, unit=(units.hourangle, units.deg))
    return coord.ra.hour, coord.dec.deg

def get_coordinates(args):
    if args.object is None and args.wcs is None:
        print('ERROR: No object or WCS coordinates specified')
        sys.exit(1)
    if args.object is not None and args.wcs is not None:
      print('ERROR: Both object and WCS coordinates specified')
      sys.exit(1)
    if args.object is not None:
        coordinates = get_wcs_coordinates(args.object)
        # Print WCS coordinates in 6 decimal places
        print(f"Using WCS coordinates of '{args.object}': {coordinates}")
    else:
        coordinates = args.wcs.split()
        # Convert coordinates to RA and DEC in decimal degrees.
        ra, dec = coordinates
        c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg))
        coordinates = c.ra.deg, c.dec.deg
    return coordinates

def compute_error(ra_target, dec_target, ra, dec):
    # Compute error in arcseconds. RA is in hours, DEC is in degrees.
    ra_error = abs(ra_target - ra) / 24 * 360 * 3600
    dec_error = abs(dec_target - dec) * 3600
    return math.sqrt(ra_error**2 + dec_error**2)

def main():
    parser = argparse.ArgumentParser(description='Go to an astronomical object and align the mount to it')
    parser.add_argument('-o', '--object', type=str, 
                        help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
    parser.add_argument('-w', '--wcs', type=str, 
                        help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
    parser.add_argument('-d', '--device', type=str, 
                        help='INDI device name', default='SkyAdventurer GTi')
    parser.add_argument('-t', '--threshold', type=float, 
                        help='Max align error in arcseconds', default=30)
    
    args = parser.parse_args()

    setup_camera()

    ra_target, dec_target = get_coordinates(args)
    # Repeat capture, sync, goto until within threshold, or max iterations reached.
    complete = False
    max_iterations = 10
    iteration = 0
    while not complete and iteration < max_iterations:
        iteration += 1
        print(f"Iteration {iteration}", end=' | ')
        print('GoTo', end=' | ')
        sync(args.device, ra_target, dec_target)
        print("Capture", end=' | ')
        capture_image()
        print('Plate solve', end=' | ')
        ra, dec = run_plate_solve_astap('tmp.jpg', None, None)
        print('Sync', end=' | ')
        sync(args.device, ra, dec)
        verify_sync(args.device, ra, dec)
        error = compute_error(ra_target, dec_target, ra, dec)
        print("RA: %9.6f, DEC: %9.6f, Error: %4.1f" % (ra, dec, error))
        if error < args.threshold:
          complete = True
        elif iteration == max_iterations:
          print("ERROR: Max iterations reached")
          sys.exit(1)


if __name__ == '__main__':
    main()