#!/usr/bin/env python3

from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord
from astropy.coordinates import FK5
from astropy.coordinates import ICRS
import astropy.units as units
from astropy.coordinates import GCRS
import sys
import os
import subprocess
import astropy.time
import argparse
import logging

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount
from sky_scripter.util import init_logging, get_wcs_coordinates
 
# def get_wcs_coordinates(object_name):
#     # Query the object
#     result_table = Simbad.query_object(object_name)

#     if result_table is None:
#         print(f"ERROR: Unable to find object '{object_name}'")
#         sys.exit(1)
#     # Extract RA and DEC
#     ra = result_table['RA'][0]
#     dec = result_table['DEC'][0]

#     # Convert J2000 coordinates to JNow.
#     c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg), frame=ICRS())
#     jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))
    
#     return jnow_coord.ra.hour, jnow_coord.dec.deg

def main():
  init_logging('goto')
  parser = argparse.ArgumentParser(description='Go to an astronomical object')
  parser.add_argument('-o', '--object', type=str, help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
  parser.add_argument('-w', '--wcs', type=str, help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
  parser.add_argument('-d', '--device', type=str, help='INDI device name', default='Star Adventurer GTi')

  args = parser.parse_args()
  mount = IndiMount(args.device)

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
      print(f"Using FK5(equinox=now) coordinates of '{args.object}': {coordinates}")
      logging.info(f"Using FK5(equinox=now) coordinates of '{args.object}': {coordinates}")
  else:
      coordinates = args.wcs.split()
      # Convert coordinates to RA and DEC in decimal degrees.
      ra, dec = coordinates
      c = SkyCoord(ra, dec, unit=(units.hour, units.deg))
      coordinates = c.ra.hour, c.dec.deg
      print(f"Using given coordinates: {coordinates}")
      logging.info(f"Using given coordinates: {coordinates}")

  # Print the RA, DEC in HH:MM:SS, DD:MM:SS format.
  c = SkyCoord(coordinates[0], coordinates[1],
               unit=(units.hourangle, units.deg))
  log_message = "GoTo RA %s, DEC %s" % \
      (c.ra.to_string(unit=units.hour, sep=':'),
       c.dec.to_string(unit=units.degree, sep=':'))
  print(log_message)
  logging.info(log_message)
  
  ra, dec = coordinates
  mount.goto(ra, dec)

if __name__ == "__main__":
  main()