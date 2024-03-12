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
from sky_scripter.util import init_logging, parse_coordinates
from sky_scripter.lib_gphoto import GphotoClient
from sky_scripter.algorithms import align_to_object

SIMULATE = True

def main():
  init_logging('align')

  parser = argparse.ArgumentParser(description='Go to an astronomical object and align the mount to it')
  parser.add_argument('-o', '--object', type=str, 
            help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
  parser.add_argument('-w', '--wcs', type=str, 
            help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
  parser.add_argument('-d', '--device', type=str, 
            help='INDI device name', default='Star Adventurer GTi')
  parser.add_argument('-t', '--threshold', type=float, 
            help='Max align error in arcseconds', default=30)
  parser.add_argument('-i', '--iso', type=int, 
            help='ISO value', default=3200)
  parser.add_argument('-s', '--shutter_speed', type=int, 
            help='Shutter speed in seconds', default=2)
  
  args = parser.parse_args()
  print(f"Using device {args.device}")
  mount = IndiMount(args.device)  
  camera = GphotoClient()
  camera.initialize(image_format='RAW',
                    mode='Manual', 
                    iso=args.iso, 
                    shutter_speed=args.shutter_speed)

  # Create .logs/images directory if it doesn't exist.
  image_dir = os.path.join(os.getcwd(), '.logs', 'images')
  os.makedirs(image_dir, exist_ok=True)
  ra_target, dec_target = parse_coordinates(args, parser)
  align_to_object(mount, 
                  camera, 
                  ra_target, dec_target, 
                  args.threshold, 
                  image_dir)
  

if __name__ == '__main__':
  main()