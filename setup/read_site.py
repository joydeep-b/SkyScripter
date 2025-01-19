#!/usr/bin/env python3

import time
import argparse
import subprocess
import astropy.time
import sys
from astropy.coordinates import SkyCoord

def ReadIndi(device, propname):
  # Call indi_getprop to get the property value
  command = "indi_getprop \"%s.%s\"" % (device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Parse the output to get the property value.
  try:
    output = output.split("=")[1].strip()
  except:
    print("Unable to read property '%s' from device '%s'" % (propname, device))
    sys.exit(1)
  return output

def main():
  parser = argparse.ArgumentParser(description='Read site details from an INDI device')
  parser.add_argument('-d', '--device', type=str, help='INDI device name', default='ZWO AM5')
  parser.add_argument('-t', '--track-status', action='store_true', help='Print tracking status')
  
  args = parser.parse_args()
  latitude = float(ReadIndi(args.device, "GEOGRAPHIC_COORD.LAT"))
  longitude = float(ReadIndi(args.device, "GEOGRAPHIC_COORD.LONG"))
  elevation = float(ReadIndi(args.device, "GEOGRAPHIC_COORD.ELEV"))
  # site_juliandate = float(ReadIndi(args.device, "JULIAN.JULIANDATE"))
  # current_juliandate = float(astropy.time.Time.now().jd)
  # juliandate_error_seconds = (current_juliandate - site_juliandate) * 86400
  # Get the current time in UTC in format 2024-01-21T18:38:23
  current_utc = astropy.time.Time.now().utc.datetime.strftime('%Y-%m-%dT%H:%M:%S')
  site_utc = ReadIndi(args.device, "TIME_UTC.UTC")
  # Offset in seconds between site and current UTC time.
  offset_seconds = (astropy.time.Time.now().unix - astropy.time.Time(site_utc).unix)

  print("Site details for %s:" % args.device)
  print("Latitude:              %9.3f" % latitude)
  print("Longitude:             %9.3f" % longitude)
  print("Elevation:             %8.2f" % elevation)
  # print("Site Julian Date:      %.7f" % site_juliandate)
  print("Site UTC Time:         %s" % site_utc)
  print("Current UTC Time:      %s" % current_utc)
  print("Time offset (s):       %d" % offset_seconds)
  # print("Current Julian Date:   %.7f" % current_juliandate)
  # print("Julian Date Error (s): %.3f" % juliandate_error_seconds)

  if args.track_status:
    print("Tracking status")
    while True:
      ra = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.RA"))
      dec = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.DEC"))
      lst = float(ReadIndi(args.device, "TIME_LST.LST"))
      # Convert RA,DEC to HA,DEC.
      ha = lst - ra
      print("RA: %9.6f DEC: %9.3f HA: %9.6f" % (ra, dec, ha))
      time.sleep(1)

if __name__ == "__main__":
  main()