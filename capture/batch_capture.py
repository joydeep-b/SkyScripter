#!/usr/bin/env python3

import argparse
import sys
import os
import time
import astropy.units as units
from astropy.coordinates import SkyCoord
from dateutil.parser import parse
import logging
from datetime import datetime, timedelta, timezone
from dateutil import tz
import signal

sys.path.append(os.getcwd())

from sky_scripter.lib_gphoto import GphotoClient
from sky_scripter.lib_indi import IndiMount, IndiFocuser
from sky_scripter.algorithms import auto_focus, align_to_object
from sky_scripter.util import init_logging, print_and_log, parse_coordinates
from sky_scripter.lib_phd2 import Phd2Client
from sky_scripter.lib_rachio import RachioClient, get_rachio_key
from sky_scripter.algorithms import auto_focus

# Global variable to indicate if the capture should be terminated - set by
# signal handler, and checked by the main loop.
terminate = False
terminate_count = 0

def start_guiding(phd2client):
  phd2client.start_guiding()

def stop_guiding(phd2client):
  phd2client.stop_guiding()

def get_image_filename(capture_dir):
  # Check for the first available image filename.
  for i in range(100000):
    filename = os.path.join(capture_dir, f'capture-{i:05d}.CR3')
    if not os.path.exists(filename):
      return filename
  raise ValueError('Too many images in the capture directory')

def need_meridian_flip(mount, args):
  ra, dec, _, _, lst = mount.get_coordinates()
  ha = lst - ra
  if ha > 12:
    ha -= 24
  pier_side = mount.get_pier_side()

  if pier_side == "East":
    time_to_flip = (12 + args.meridian_flip_angle - ha) * 3600
  else:
    time_to_flip = max(0.0, (args.meridian_flip_angle - ha) * 3600)
  return time_to_flip < 0

def perform_meridian_flip(mount):
  # Perform a meridian flip.
  pass

def check_sprinklers():
  print("Checking for upcoming sprinkler events")
  rachio_client = RachioClient(get_rachio_key())
  schedule = rachio_client.get_upcoming_schedule()
  events_found = False
  for event in schedule['entries']:
    event_name = event['scheduleName']
    # Get the start and end times of the event in the local timezone.
    event_start = parse(event['startTime'], tzinfos={'Z': 'UTC'})
    event_end = parse(event['endTime'], tzinfos={'Z': 'UTC'})
    event_start_local = event_start.astimezone(tz.tzlocal())
    event_end_local = event_end.astimezone(tz.tzlocal())
    # Check if the start time is within 12 hours.
    if event_start < datetime.now(tz.tzlocal()) + timedelta(hours=12):
      events_found = True
      logging.warning(f"Sprinkler event: {event_name}, start time: {event_start_local}, end time: {event_end_local}")
      print(f"Sprinkler event to start within 12 hours: {event_name}, start time: {event_start_local}, end time: {event_end_local}")
      print("Abort? (y/n)")
      response = input()
      if response.lower() == 'y':
        print("Aborting the capture")
        logging.warning("Aborting the capture")
        sys.exit(1)
      else:
        logging.warning("User override: Continuing with the capture despite sprinkler event")
  if not events_found:
    print_and_log("No upcoming sprinkler events, continuing with the capture")
  else:
    logging.warning("User override: Continuing with the capture despite sprinkler event")
    print("Continuing with the capture despite sprinkler event")

def set_up_capture_directory(args, coordinates):
  if args.object is not None:
    capture_name = args.object
  elif args.wcs is not None:
    # Use WCS coordinates as the capture name.
    c = SkyCoord(coordinates[0], 
                 coordinates[1], 
                 unit=(units.hourangle, units.deg))
    capture_name = c.to_string('hmsdms').replace(' ', '')

  capture_dir = os.path.join(os.getcwd(),
                            time.strftime("%Y-%m-%d"), 
                            capture_name.replace(' ', '_'))  
  os.makedirs(capture_dir, exist_ok=True)
  print_and_log(f"Capture directory: {capture_dir}")
  return capture_dir

def setup_camera(camera, args):
  shutter_speed_num = eval(args.shutter_speed)
  camera_mode = 'Bulb' if shutter_speed_num > 30 else 'Manual'
  camera.initialize(image_format='RAW',
                    mode=camera_mode,
                    iso=args.iso,
                    shutter_speed=args.shutter_speed)

def run_auto_focus(camera, focuser, args):
  if args.simulate:
    return
  current_focus = focuser.get_focus()
  focus_step = args.focus_step_size
  focus_min = current_focus - focus_step * args.focus_steps
  focus_max = current_focus + focus_step * args.focus_steps
  auto_focus(focuser, camera, focus_min, focus_max, focus_step)

def signal_handler(signum, frame):
  global terminate
  terminate = True

def get_args():
  parser = argparse.ArgumentParser(description='Automated batch capture of a target')
  # Target to image.
  parser.add_argument('-o', '--object', type=str, 
      help='Astronomical object name, either a catalog name (e.g., "M31") or a common name (e.g., "Andromeda Galaxy")')
  parser.add_argument('-w', '--wcs', type=str, 
      help='WCS coordinates (e.g., "5:35:17 -5:23:24")')
  # Hardware configuration.
  parser.add_argument('-m', '--mount', type=str,
      help='INDI mount device name', default='Star Adventurer GTi')
  parser.add_argument('-f', '--focuser', type=str,
      help='INDI focuser device name', default='ZWO EAF')
  # Camera settings.
  parser.add_argument('-i', '--iso', type=int,
      help='ISO value', default=400)
  parser.add_argument('-s', '--shutter_speed', type=str,
      help='Shutter speed', default='90')
  # Alignment and mount limit settings.
  parser.add_argument('--align-threshold', type=float,
      help='Alignment threshold in arcseconds', default=15)
  parser.add_argument('--min-altitude', type=float,
      help='Minimum altitude for tracking', default=0)
  parser.add_argument('--meridian-flip-angle', type=float,
      help='HA limit to trigger meridian flip', default=0.2)
  # Auto focus settings.
  parser.add_argument('--focus-step-size', type=int,
      help='Focus step size', default=100)
  parser.add_argument('--focus-steps', type=int,
      help='Number of focus steps on either side of start', default=3)
  parser.add_argument('--focus-interval', type=int,
      help='Focus interval in minutes', default=60)
  # Application settings.
  parser.add_argument('--simulate', action='store_true',
      help='Simulate the capture without actually taking images')
  parser.add_argument('-v', '--verbose', action='store_true',
      help='Print verbose messages')
  
  return parser.parse_args(), parser

def capture_image(camera, filename, args):
  global terminate
  pid = os.fork()
  if pid == 0:
    # Child process
    camera.capture_image(filename)
    logging.info(f"Image saved to {filename}")
    sys.exit(0)
  else:
    # Parent process: Show a progress bar for the duration, with 20 ticks.
    t_start = time.time()
    duration = eval(args.shutter_speed)
    while time.time() - t_start < duration:
      terminate_string = ' [Terminating]' if terminate else ''
      print(f'\r[Remaining: {duration - (time.time() - t_start):.1f}s] {terminate_string}  ', end='', flush=True)
      time.sleep(0.1)
    os.waitpid(pid, 0)
    print('\r' + ' ' * 40 + '\r', end='', flush=True)
    return
  
def signal_handler(signum, frame):
  global terminate, terminate_count
  terminate = True
  if terminate_count > 1:
    print_and_log('Terminating immediately')
    sys.exit(1)
  if terminate_count > 0:
    print_and_log('Hit Ctrl-C again to terminate immediately')
  terminate_count += 1

def reset_alignment_camera(alignment_camera, args):
  alignment_camera.initialize('RAW', 'Manual', 1600, '2')

def reset_capture_camera(capture_camera, args):
  capture_camera.initialize('RAW', 'Bulb', args.iso, args.shutter_speed)

def main():
  args, parser = get_args()
  init_logging('batch_capture', also_to_console=args.verbose)
  signal.signal(signal.SIGINT, signal_handler)
  signal.signal(signal.SIGTERM, signal_handler)

  coordinates = parse_coordinates(args, parser)
  check_sprinklers()
  capture_dir = set_up_capture_directory(args, coordinates)
  mount = IndiMount(args.mount, simulate=args.simulate)
  focuser = IndiFocuser(args.focuser, simulate=args.simulate)
  capture_camera = GphotoClient(simulate=args.simulate)
  capture_camera.initialize('RAW', 'Bulb', args.iso, args.shutter_speed)
  alignment_camera = GphotoClient(simulate=args.simulate)
  alignment_camera.initialize('RAW', 'Manual', 1600, '2')
  phd2client = Phd2Client()
  setup_camera(capture_camera, args)
  phd2client.connect()
  
  # Initial actions: Align, Start guiding, Auto focus.
  print_and_log('Running initial alignment')
  reset_alignment_camera(alignment_camera, args)
  align_to_object(mount, alignment_camera, coordinates[0], coordinates[1], 
                  args.align_threshold)
  print_and_log('Starting guiding')
  start_guiding(phd2client)
  print_and_log('Running initial auto focus')
  run_auto_focus(alignment_camera, focuser, args)
  t_last_focus = time.time()

  _, _, alt, _, _ = mount.get_coordinates()
  num_images = 0  
  reset_capture_camera(capture_camera, args)
  while (alt > args.min_altitude or args.simulate) and not terminate:
    if need_meridian_flip(mount, args):
      print_and_log("Meridian flip needed")
      stop_guiding(phd2client)
      perform_meridian_flip(mount)
      print_and_log("Meridian flip complete")
      reset_alignment_camera(alignment_camera, args)
      align_to_object(mount, alignment_camera, coordinates[0], coordinates[1], 
                      args.align_threshold)
      start_guiding(phd2client)
      run_auto_focus(capture_camera, focuser, args)
      t_last_focus = time.time()
      reset_capture_camera(capture_camera, args)
    if time.time() - t_last_focus > args.focus_interval * 60:
      print_and_log("Running auto focus")
      reset_alignment_camera(alignment_camera, args)
      run_auto_focus(capture_camera, focuser, args)
      t_last_focus = time.time()
      reset_capture_camera(capture_camera, args)
    image_file = get_image_filename(capture_dir)
    # Get just the filename without the directory.
    image_file_short = os.path.basename(image_file)
    print_and_log(f"Capturing image {num_images} to {image_file_short}")
    capture_image(capture_camera, image_file, args)
    # TODO: Dithering
    num_images += 1
    _, _, alt, _, _ = mount.get_coordinates()
  
  # End of capture: Stop guiding, print summary.
  phd2client.stop_guiding()
  if terminate:
    print_and_log("Capture terminated by user")
  elif alt <= args.min_altitude:
    print_and_log("Altitude limit reached, capture stopped")
  
    

if __name__ == '__main__':
  main()