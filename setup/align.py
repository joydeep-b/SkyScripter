#!/usr/bin/env python3

import subprocess
import re
import argparse
import sys
import shutil
import logging
from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord
from astropy.coordinates import FK5
from astropy.coordinates import ICRS
import astropy.units as units
from astropy.coordinates import GCRS
import astropy.time
import math
import time
import os

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount
from sky_scripter.util import exec_or_fail, init_logging, get_wcs_coordinates, run_plate_solve_astap

SIMULATE = True
iso = 3200
shutter_speed = 2

def capture_image():
  global iso, shutter_speed
  if SIMULATE:
    # Copy sample_data/NGC2244.jpg to tmp.jpg
    shutil.copy('sample_data/NGC2244.jpg', 'tmp.jpg')
    return
  # print(f'Capturing image with iso={iso}, shutter_speed={shutter_speed}')
  # Capture in desired iso, aperture, and shutter speed, pipe output to /dev/null.
  exec_or_fail(['gphoto2',
          '--set-config', f'iso={iso}',
          '--set-config', '/main/imgsettings/imageformat=RAW',
          '--set-config', f'shutterspeed={shutter_speed}',
          '--capture-image-and-download',
          '--filename', 'tmp.cr3',
          '--force-overwrite'])

def setup_camera():
  if SIMULATE:
    return
  # Set the camera to JPEG mode
  exec_or_fail(['gphoto2', '--set-config', '/main/imgsettings/imageformat=0'])
  # Set the camera to manual mode
  exec_or_fail(['gphoto2', '--set-config', '/main/capturesettings/autoexposuremodedial=Manual'])

def set_tracking(device):
  exec_or_fail("indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_ON=On\"" % device)

def get_coordinates(args, parser):
  if args.object is None and args.wcs is None:
    print('ERROR: No object or WCS coordinates specified')
    parser.print_help()
    sys.exit(1)
  if args.object is not None and args.wcs is not None:
    print('ERROR: Both object and WCS coordinates specified')
    parser.print_help()
    sys.exit(1)
  if args.object is not None:
    coordinates = get_wcs_coordinates(args.object)
    # Print WCS coordinates in 6 decimal places
    print(f"Using WCS coordinates of '{args.object}': {coordinates}")
  else:
    coordinates = args.wcs.split()
    # Convert coordinates to RA and DEC in decimal degrees.
    ra, dec = coordinates
    c = SkyCoord(ra, dec, unit=(units.deg, units.deg))
    # print("Provided: %s %s Interpreted: %f %f" % (ra, dec, c.ra.deg, c.dec.deg))
    # sys.exit(1)
    coordinates = c.ra.deg, c.dec.deg
  return coordinates

def compute_error(ra_target, dec_target, ra, dec):
  # Compute error in arcseconds. RA is in hours, DEC is in degrees.
  ra_error = abs(ra_target - ra) / 24 * 360 * 3600
  dec_error = abs(dec_target - dec) * 3600
  return math.sqrt(ra_error**2 + dec_error**2)


def main():
  init_logging('align', alsologtostdout=True)

  parser = argparse.ArgumentParser(description='Go to an astronomical object and align the mount to it')
  parser.add_argument('-o', '--object', type=str, 
            help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
  parser.add_argument('-w', '--wcs', type=str, 
            help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
  parser.add_argument('-d', '--device', type=str, 
            help='INDI device name', default='Star Adventurer GTi')
  parser.add_argument('-t', '--threshold', type=float, 
            help='Max align error in arcseconds', default=30)
  
  args = parser.parse_args()
  print(f"Using device {args.device}")
  mount = IndiMount(args.device)  

  setup_camera()

  ra_target, dec_target = get_coordinates(args, parser)
  # Repeat capture, sync, goto until within threshold, or max iterations reached.
  complete = False
  max_iterations = 10
  iteration = 0
  while not complete and iteration < max_iterations:
    iteration += 1
    print(f"Iteration {iteration}", end=' | ', flush=True)
    
    t_start = astropy.time.Time.now()

    print('GoTo', end=' | ', flush=True)
    mount.goto(ra_target, dec_target)
    
    print("Capture", end=' | ', flush=True)
    capture_image()
    
    print('Plate solve', end=' | ', flush=True)
    ra, dec = run_plate_solve_astap('tmp.cr3', None, None)
    
    print('Sync', end=' | ', flush=True)
    mount.sync(ra, dec)
    
    t_end = astropy.time.Time.now()

    error = compute_error(ra_target, dec_target, ra, dec)
    # Print RA in HH:MM:SS and DEC in DD:MM:SS, and error in arcseconds.
    ra_hms = astropy.coordinates.Angle(ra, unit=units.hour).to_string(unit=units.hour, sep=':')
    dec_dms = astropy.coordinates.Angle(dec, unit=units.deg).to_string(unit=units.deg, sep=':')
    print(f"RA: {ra_hms}, DEC: {dec_dms}, Error: {error:4.1f} | Iteration time: {(t_end - t_start).sec:4.1f}")
    # print("RA: %9.6f, DEC: %9.6f, Error: %4.1f | Iteration time: %4.1f" % (ra, dec, error, (t_end - t_start).sec))

    if error < args.threshold:
      complete = True
    elif iteration == max_iterations:
      print("ERROR: Max iterations reached")
      sys.exit(1)


if __name__ == '__main__':
  main()