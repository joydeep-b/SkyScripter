#!/usr/bin/python

import sys
import time
import argparse
import subprocess
import astropy.time
import astropy.units as units
from astropy.coordinates import SkyCoord
import signal

def exec(command):
  # print(command)
  # Execute the command, and check the return code.
  returncode = subprocess.call(command, shell=True)
  if returncode != 0:
    print("Error: command '%s' returned %d" % (command, returncode))
    sys.exit(1)

def write_indi(device, propname, value):
  command = "indi_setprop \"%s.%s=%s\"" % (device, propname, value)
  exec(command)

def read_indi(device, propname, timeout=2):
  # Call indi_getprop to get the property value
  command = "indi_getprop -t %d \"%s.%s\"" % (timeout, device, propname)
  # Execute the command and get the output.
  output = subprocess.run(command, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
  # Check for multiple lines of output.
  lines = output.splitlines()
  if len(lines) == 1:
    # Parse the output to get the property value.
    output = output.split("=")[1].strip()
    return output  
  else:
    # Parse the output from each line to get all property values.
    output = []
    for line in lines:
      # Get key-value pair. 
      # Example:"SkyAdventurer GTi.RASTATUS.RAInitialized=Ok"
      # Key="RAInitialized" Value="Ok"
      key = line.split("=")[0].split(".")[-1]
      value = line.split("=")[1].strip()
      output.append((key, value))
    return output

def get_mount_state(device):
  ra_status = ReadIndi(device, "RASTATUS.*", 1)
  de_status = ReadIndi(device, "DESTATUS.*", 1)

  # If ra_status has ("RAGoto", "Ok"), or de_status has ("DEGoto", "Ok"), then the mount is running a goto slew.
  goto_slew = False
  for key, value in ra_status:
    if key == "RAGoto" and value == "Ok":
      goto_slew = True
      break
  for key, value in de_status:
    if key == "DEGoto" and value == "Ok":
      goto_slew = True
      break
  
  # If not goto_slew, and ra_status has ('RARunning', 'Ok'), ('RAGoto', 'Busy'), and ('RAHighspeed', 'Busy'), then the mount is tracking.
  tracking = False
  if (not goto_slew) and \
      ("RARunning", "Ok") in ra_status and \
      ("RAGoto", "Busy") in ra_status and \
      ("RAHighspeed", "Busy") in ra_status:
    tracking = True

  # If not goto_slew, not tracking, and ra_status has ('RARunning', 'Ok') or 
  # de_status has ('DERunning', 'Ok'), then the mount is running a manual slew.
  manual_slew = False
  if (not goto_slew) and (not tracking) and \
      (("RARunning", "Ok") in ra_status or \
      ("DERunning", "Ok") in de_status):
    manual_slew = True

  return manual_slew, goto_slew, tracking

def stop_tracking(device):
  command = "indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_OFF=On\"" % device
  exec(command)

def start_tracking(device):
  command = "indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_ON=On\"" % device
  exec(command)

def main():
  parser = argparse.ArgumentParser(
      description='Set tracking details for an INDI device')
  parser.add_argument('-d', '--device', type=str, 
                      help='INDI device name', default='SkyAdventurer GTi')
  args = parser.parse_args()
  print("Using device %s" % args.device)

  sidereal_tracking = read_indi(args.device, 
                                "TELESCOPE_TRACK_MODE.TRACK_SIDEREAL")
  lunar_tracking = read_indi(args.device,
                              "TELESCOPE_TRACK_MODE.TRACK_LUNAR")
  solar_tracking = read_indi(args.device,
                              "TELESCOPE_TRACK_MODE.TRACK_SOLAR")
  custom_tracking = read_indi(args.device,
                              "TELESCOPE_TRACK_MODE.TRACK_CUSTOM")
  tracking_mode = "Unknown"
  if sidereal_tracking == "On":
    tracking_mode = "Sidereal"
  elif lunar_tracking == "On":
    tracking_mode = "Lunar"
  elif solar_tracking == "On":
    tracking_mode = "Solar"
  elif custom_tracking == "On":
    tracking_mode = "Custom"
  print("Current tracking mode: %s" % tracking_mode)

  # Get desired tracking mode from user.
  print("Select desired tracking mode: [S]idereal, [L]unar, S[o]lar (default = Unchanged)")
  desired_tracking_mode = input("Tracking mode: ")
  if desired_tracking_mode == "s":
    write_indi(args.device, "TELESCOPE_TRACK_MODE.TRACK_SIDEREAL", "On")
  elif desired_tracking_mode == "l":
    write_indi(args.device, "TELESCOPE_TRACK_MODE.TRACK_LUNAR", "On")
  elif desired_tracking_mode == "o":
    write_indi(args.device, "TELESCOPE_TRACK_MODE.TRACK_SOLAR", "On")

  # Get tracking state from mount.
  track_state_on = read_indi(args.device, "TELESCOPE_TRACK_STATE.TRACK_ON")
  track_state_off = read_indi(args.device, "TELESCOPE_TRACK_STATE.TRACK_OFF")
  track_state = "Unknown"
  if track_state_on == "On":
    track_state = "On"
  elif track_state_off == "On":
    track_state = "Off"
  print("Current tracking state: %s" % track_state)
  desired_track_state = input("Select desired tracking state: O[n], O[f]f (default = Unchanged): ")
  if desired_track_state == "n":
    start_tracking(args.device)
  elif desired_track_state == "f":
    stop_tracking(args.device)

if __name__ == "__main__":
  main()