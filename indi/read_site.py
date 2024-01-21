#!/usr/bin/python

import sys
import os
import subprocess
import astropy.time

def ReadIndi(device, propname):
  # Call indi_getprop to get the property value
  command = "indi_getprop \"%s.%s\"" % (device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Parse the output to get the property value.
  output = output.split("=")[1].strip()
  return output

def main():

  # Optional parameter: device name
  if len(sys.argv) > 1:
    device = sys.argv[1]
  else:
    device = "SkyAdventurer GTi"
  
  latitude = float(ReadIndi(device, "GEOGRAPHIC_COORD.LAT"))
  longitude = float(ReadIndi(device, "GEOGRAPHIC_COORD.LONG"))
  elevation = float(ReadIndi(device, "GEOGRAPHIC_COORD.ELEV"))
  site_juliandate = float(ReadIndi(device, "JULIAN.JULIANDATE"))
  current_juliandate = float(astropy.time.Time.now().jd)
  juliandate_error_seconds = (current_juliandate - site_juliandate) * 86400

  print("Site details for %s:" % device)
  print("Latitude:              %9.3f" % latitude)
  print("Longitude:             %9.3f" % longitude)
  print("Elevation:             %8.2f" % elevation)
  print("Site Julian Date:      %.7f" % site_juliandate)
  print("Site UTC Time:         %s" % ReadIndi(device, "TIME_UTC.UTC"))
  print("Current Julian Date:   %.7f" % current_juliandate)
  print("Julian Date Error (s): %.3f" % juliandate_error_seconds)

if __name__ == "__main__":
  main()