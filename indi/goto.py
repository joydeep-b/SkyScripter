#!/usr/bin/python

from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord
import astropy.units as units
import sys
import os
import subprocess
import astropy.time
import argparse

def exec(command):
  # print(command)
  # Execute the command, and check the return code.
  returncode = subprocess.call(command, shell=True)
  if returncode != 0:
    print("Error: command '%s' returned %d" % (command, returncode))
    sys.exit(1)

def get_wcs_coordinates(object_name):
    # Query the object
    result_table = Simbad.query_object(object_name)

    if result_table is None:
        print(f"ERROR: Unable to find object '{object_name}'")
        sys.exit(1)

    # Extract RA and DEC
    ra = result_table['RA'][0]
    dec = result_table['DEC'][0]
    print(f"RA: {ra}, DEC: {dec}")

    # Create a SkyCoord object
    coord = SkyCoord(ra, dec, unit=(units.hourangle, units.deg))

    # return coord.ra.to_string(unit=units.hour, sep=':') + ' ' + \
    #        coord.dec.to_string(unit=units.degree, sep=':')
    # Return RA in hours and DEC in degrees
    return coord.ra.hour, coord.dec.deg

def indi_goto(device, ra, dec):
  command = "indi_setprop \"%s.EQUATORIAL_EOD_COORD.RA=%f;DEC=%f\"" % (device, ra, dec)
  exec("indi_setprop \"%s.ON_COORD_SET.TRACK=On\"" % device)
  exec(command)

def main():
  parser = argparse.ArgumentParser(description='Go to an astronomical object')
  parser.add_argument('-o', '--object', type=str, help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
  parser.add_argument('-w', '--wcs', type=str, help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
  parser.add_argument('-d', '--device', type=str, help='INDI device name', default='SkyAdventurer GTi')

  args = parser.parse_args()
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
      c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg))
      coordinates = c.ra.deg, c.dec.deg
      print(f"Using WCS coordinates: {coordinates}")
      sys.exit(1)

  # Convert coordinates to RA and DEC in decimal degrees.
  ra, dec = coordinates
  # sys.exit(1)
  indi_goto(args.device, ra, dec)

if __name__ == "__main__":
  main()