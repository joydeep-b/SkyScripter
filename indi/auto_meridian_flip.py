#!/usr/bin/python

import sys
import time
import argparse
import subprocess
import astropy.time
from astropy.coordinates import SkyCoord


def exec(command):
  # print(command)
  # Execute the command, and check the return code.
  returncode = subprocess.call(command, shell=True)
  if returncode != 0:
    print("Error: command '%s' returned %d" % (command, returncode))
    sys.exit(1)

def ReadIndi(device, propname):
  # Call indi_getprop to get the property value
  command = "indi_getprop \"%s.%s\"" % (device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Parse the output to get the property value.
  output = output.split("=")[1].strip()
  return output

def indi_goto(device, ra, dec):
  command = "indi_setprop \"%s.EQUATORIAL_EOD_COORD.RA=%f;DEC=%f\"" % (device, ra, dec)
  exec("indi_setprop \"%s.ON_COORD_SET.TRACK=On\"" % device)
  exec(command)

def target_wait(device):
  while True:
    target_ra = float(ReadIndi(device, "TARGET_EOD_COORD.RA"))
    target_dec = float(ReadIndi(device, "TARGET_EOD_COORD.DEC"))
    ra = float(ReadIndi(device, "EQUATORIAL_EOD_COORD.RA"))
    dec = float(ReadIndi(device, "EQUATORIAL_EOD_COORD.DEC"))
    if abs(target_ra - ra) < 0.002 and abs(target_dec - dec) < 0.001:
      return
    print("Target: %9.6f %9.6f Current: %9.6f %9.6f Difference: %9.6f %9.6f" % (target_ra, target_dec, ra, dec, target_ra - ra, target_dec - dec))
    time.sleep(1)

def perform_meridian_flip(device):
  target_ra = float(ReadIndi(device, "TARGET_EOD_COORD.RA"))
  target_dec = float(ReadIndi(device, "TARGET_EOD_COORD.DEC"))
  indi_goto(device, target_ra, target_dec)
  time.sleep(1)
  target_wait(device)

def main():
  parser = argparse.ArgumentParser(description='Read site details from an INDI device')
  parser.add_argument('-d', '--device', type=str, help='INDI device name', default='SkyAdventurer GTi')
  parser.add_argument('-a', '--flip-angle', type=float, help='Hour angle flip angle', default=1/60)
  
  args = parser.parse_args()
  print("Using device %s" % args.device)

  while True:
    target_wait(args.device)
    ra = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.RA"))
    dec = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.DEC"))
    lst = float(ReadIndi(args.device, "TIME_LST.LST"))
    # Convert RA,DEC to HA,DEC.
    ha = lst - ra
    time_to_flip = (args.flip_angle - ha) * 3600
    time_to_flip_hours = time_to_flip / 3600
    time_to_flip_minutes = (time_to_flip % 3600) / 60
    time_to_flip_seconds = time_to_flip % 60
    print("RA: %9.6f HA: %9.6f Time to flip: %02d:%02d:%02d" % (ra, ha, time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds))
    if ha > args.flip_angle:
      print("Performing meridian flip")
      perform_meridian_flip(args.device)
      print("Meridian flip complete")
      sys.exit(0)

  
    time.sleep(1)

if __name__ == "__main__":
  main()