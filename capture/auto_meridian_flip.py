#!/usr/bin/python

import datetime
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

def ReadIndi(device, propname, timeout=2):
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
      # Example:"Star Adventurer GTi.RASTATUS.RAInitialized=Ok"
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
  # target_wait(device)

def stop_tracking(device):
  command = "indi_setprop \"%s.TELESCOPE_TRACK_STATE.TRACK_OFF=On\"" % device
  exec(command)

def get_pier_side(device):
  if ReadIndi(device, "TELESCOPE_PIER_SIDE.PIER_WEST") == "On":
    return "West"
  elif ReadIndi(device, "TELESCOPE_PIER_SIDE.PIER_EAST") == "On":
    return "East"
  else:
    print("Error: could not determine pier side")
    sys.exit(1)

def get_alt_az(device, ra, dec):
  # First get site details from the mount.
  alt = float(ReadIndi(device, "HORIZONTAL_COORD.ALT"))
  az = float(ReadIndi(device, "HORIZONTAL_COORD.AZ"))
  return alt, az

def main():
  parser = argparse.ArgumentParser(
      description='Read site details from an INDI device')
  parser.add_argument('-d', '--device', type=str, 
                      help='INDI device name', default='Star Adventurer GTi')
  parser.add_argument('-m', '--meridian-flip-angle', type=float, 
                      help='HA limit to trigger meridian flip', default=1/60)
  parser.add_argument('--min-altitude', type=float, 
                      help='Minimum altitude for tracking', default=0)
  parser.add_argument('-l', '--log-file', type=str,
                      help='Log file', default="mount_log.txt")
  args = parser.parse_args()
  print("Using device %s" % args.device)

  # Install SIGINT handler for clean exit.
  def signal_handler(sig, frame):
    sys.exit(0)
  signal.signal(signal.SIGINT, signal_handler)

  # Open the log file.
  log_file = open(args.log_file, "a")
  log_interval = -1
  while True:
    (manual_slew, goto_slew, tracking) = get_mount_state(args.device)
    if manual_slew:
      mount_state = "Slewing "
    elif goto_slew:
      mount_state = "Goto    "
    elif tracking:
      mount_state = "Tracking"
    else:
      mount_state = "Idle    "

    ra = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.RA"))
    dec = float(ReadIndi(args.device, "EQUATORIAL_EOD_COORD.DEC"))
    lst = float(ReadIndi(args.device, "TIME_LST.LST"))
    # Convert RA,DEC to HA,DEC.
    ha = lst - ra
    if ha > 12:
      ha -= 24
    pier_side = get_pier_side(args.device)

    # Convert RA, DEC to Alt, Az.
    alt, az = get_alt_az(args.device, ra, dec)

    if pier_side == "East":
      time_to_flip = (12 + args.meridian_flip_angle - ha) * 3600
    else:
      time_to_flip = max(0.0, (args.meridian_flip_angle - ha) * 3600)

    time_to_flip_hours = time_to_flip / 3600
    time_to_flip_minutes = (time_to_flip % 3600) / 60
    time_to_flip_seconds = time_to_flip % 60

    # Get current local date and time.
    current_date_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_string = "%s | %s |" % (current_date_time, mount_state)
    log_string += " RA: %9.6f HA: %9.6f DEC: %9.6f |" % (ra, ha, dec)
    log_string += " Pier side: %s |" % pier_side
    log_string += " Alt: %7.3f Az: %7.3f |" % (alt, az)
    log_string += " Time to flip: %02d:%02d:%02d" % \
        (time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds)
    print(log_string)
    if log_interval > 0:
      log_interval -= 1
    else:
      log_interval = 10
      log_file.write(log_string + "\n")

    if tracking and time_to_flip <= 0:
      print("%s | Performing meridian flip" % current_date_time)
      log_file.write("%s | Performing meridian flip\n" % current_date_time)
      perform_meridian_flip(args.device)

    if tracking and alt < args.min_altitude:
      print("%s | Altitude below minimum, stopping tracking" % current_date_time)
      log_file.write("%s | Altitude below minimum, stopping tracking\n" % current_date_time)
      stop_tracking(args.device)

    time.sleep(1)

if __name__ == "__main__":
  main()