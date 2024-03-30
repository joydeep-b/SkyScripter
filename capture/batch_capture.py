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

def start_guiding(phd2client: Phd2Client):
  phd2client.start_guiding()

def stop_guiding(phd2client: Phd2Client):
  phd2client.stop_guiding()

def get_image_filename(capture_dir: str):
  # Check for the first available image filename.
  for i in range(100000):
    filename = os.path.join(capture_dir, f'capture-{i:05d}.CR3')
    if not os.path.exists(filename):
      return filename
  raise ValueError('Too many images in the capture directory')

def get_time_to_flip(mount: IndiMount, args: argparse.Namespace):
  ra, dec, _, _, lst = mount.get_coordinates()
  ha = lst - ra
  if ha > 12:
    ha -= 24
  pier_side = mount.get_pier_side()
  if pier_side == "East":
    time_to_flip = (12 + args.meridian_flip_angle - ha) * 3600
  else:
    time_to_flip = max(0.0, (args.meridian_flip_angle - ha) * 3600)
  time_to_flip_hours = int(time_to_flip // 3600)
  time_to_flip_minutes = int((time_to_flip % 3600) // 60)
  time_to_flip_seconds = int(time_to_flip % 60)
  return time_to_flip, time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds

def need_meridian_flip(mount: IndiMount, args: argparse.Namespace):
  time_to_flip, _, _, _ = get_time_to_flip(mount, args)
  return time_to_flip <= 0

def perform_meridian_flip(mount: IndiMount):
  # Perform a meridian flip.
  ra, dec, _, _, lst = mount.get_coordinates()
  # Intermediary step: Slew to point 3 hours west of the meridian.
  intermediate_ra = ra + 3
  mount.goto(intermediate_ra, dec)
  # Then flip the mount.
  mount.goto(ra, dec)

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
    print_and_log("Continuing with the capture despite sprinkler event")

def set_up_capture_directory(args, coordinates):
  if args.object is not None:
    capture_name = args.object
  elif args.wcs is not None:
    # Use WCS coordinates as the capture name.
    c = SkyCoord(coordinates[0], 
                 coordinates[1], 
                 unit=(units.hourangle, units.deg))
    capture_name = c.to_string('hmsdms').replace(' ', '')

  # Get current datetime
  current_datetime = datetime.now()
  # Subtract 8 hours
  eight_hours_ago = current_datetime - timedelta(hours=8)
  # Get the date in YYYY-MM-DD format
  date_8_hours_ago = eight_hours_ago.strftime('%Y-%m-%d')

  capture_dir = os.path.join(os.getcwd(),
                             date_8_hours_ago, 
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

def reset_alignment_camera(alignment_camera, args):
  alignment_camera.initialize('RAW', 'Manual', 1600, '2')

def reset_capture_camera(capture_camera, args):
  capture_camera.initialize('RAW', 'Bulb', args.iso, args.shutter_speed)

def run_auto_focus(focus_camera, capture_camera, focuser, args):
  if args.simulate:
    return
  reset_alignment_camera(focus_camera, args)
  current_focus = focuser.get_focus()
  focus_step = args.focus_step_size
  focus_min = current_focus - focus_step * args.focus_steps
  focus_max = current_focus + focus_step * args.focus_steps
  auto_focus(focuser, focus_camera, focus_min, focus_max, focus_step)
  reset_capture_camera(capture_camera, args)

def run_alignment(mount, alignment_camera, capture_camera, coordinates, 
                  phd2client, args):
  phd2client.stop_guiding()
  reset_alignment_camera(alignment_camera, args)
  align_to_object(mount, alignment_camera, coordinates[0], coordinates[1], 
                  args.align_threshold)
  reset_capture_camera(capture_camera, args)
  phd2client.start_guiding()

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
      help='INDI focuser device name', default='ASI EAF')
  # Camera settings.
  parser.add_argument('-i', '--iso', type=int,
      help='ISO value', default=400)
  parser.add_argument('-s', '--shutter_speed', type=str,
      help='Shutter speed', default='90')
  # Alignment and mount limit settings.
  parser.add_argument('--align-threshold', type=float,
      help='Alignment threshold in arcseconds', default=20)
  parser.add_argument('--min-altitude', type=float,
      help='Minimum altitude for tracking', default=0)
  parser.add_argument('--meridian-flip-angle', type=float,
      help='HA limit to trigger meridian flip', default=0.2)
  # Dithering settings.
  parser.add_argument('--dither-period', type=int,
      help='Dithering period in number of images', default=10)
  # Auto focus settings.
  parser.add_argument('--focus-step-size', type=int,
      help='Focus step size', default=6)
  parser.add_argument('--focus-steps', type=int,
      help='Number of focus steps on either side of start', default=7)
  parser.add_argument('--focus-interval', type=int,
      help='Focus interval in minutes', default=40)
  parser.add_argument('--skip-initial-focus', action='store_true',
      help='Skip initial focus')
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
      time.sleep(0.1)
      terminate_string = ' [Terminating]' if terminate else ''
      print(f'\r[Remaining: {duration - (time.time() - t_start):.1f}s] {terminate_string}  ', end='', flush=True)
    
    # Check if a process by the name "gphoto2" is running.
    is_gphoto_running = os.system('pgrep gphoto2 > /dev/null') == 0
    while is_gphoto_running:
      time.sleep(0.1)
      sys_result = os.system('pgrep gphoto2 > /dev/null')
      is_gphoto_running = sys_result == 0
      terminate_string = ' [Terminating]' if terminate else ''
      print(f'\r[Remaining: {duration - (time.time() - t_start):.1f}s] {terminate_string}  ', end='', flush=True)

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


def print_and_log_mount_state(mount, args):
  current_date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  manual_slew, goto_slew, tracking = mount.get_mount_state()
  if manual_slew:
    mount_state = "Slewing "
  elif goto_slew:
    mount_state = "Goto    "
  elif tracking:
    mount_state = "Tracking"
  else:
    mount_state = "Idle    "
  
  ra, dec, alt, az, lst = mount.get_coordinates()
  ha = lst - ra
  pier_side = mount.get_pier_side()
  _, time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds = \
      get_time_to_flip(mount, args)
  log_string = "%s | %s |" % (current_date_time, mount_state)
  log_string += " RA: %9.6f HA: %9.6f DEC: %9.6f |" % (ra, ha, dec)
  log_string += " Pier side: %s |" % pier_side
  log_string += " Alt: %7.3f Az: %7.3f |" % (alt, az)
  log_string += " Time to flip: %02d:%02d:%02d" % \
      (time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds)
  print_and_log(log_string)

def main():
  args, parser = get_args()
  init_logging('batch_capture', also_to_console=args.verbose)
  logging.info(f"Starting batch capture with arguments: {args}")
  signal.signal(signal.SIGINT, signal_handler)
  signal.signal(signal.SIGTERM, signal_handler)

  coordinates = parse_coordinates(args, parser)
  check_sprinklers()
  capture_dir = set_up_capture_directory(args, coordinates)
  print("Connecting to devices")
  mount = IndiMount(args.mount, simulate=args.simulate)
  focuser = IndiFocuser(args.focuser, simulate=args.simulate)
  capture_camera = GphotoClient(simulate=args.simulate)
  capture_camera.initialize('RAW', 'Bulb', args.iso, args.shutter_speed)
  alignment_camera = GphotoClient(simulate=args.simulate)
  alignment_camera.initialize('RAW', 'Manual', 1600, '2')
  print("Connecting to PHD2")
  phd2client = Phd2Client()
  setup_camera(capture_camera, args)
  phd2client.connect()
  phd2client.stop_guiding()
  

  # Initial actions: Align, Start guiding, Auto focus.
  print_and_log('Running initial alignment')
  run_alignment(mount, alignment_camera, capture_camera, coordinates, 
                phd2client, args)
  print_and_log_mount_state(mount, args)
  print_and_log('Starting guiding')
  start_guiding(phd2client)
  if not args.skip_initial_focus:
    print_and_log('Running initial auto focus')
    run_auto_focus(alignment_camera, focuser, args)
  t_last_focus = time.time()

  _, _, alt, _, _ = mount.get_coordinates()
  num_images = 1  
  reset_capture_camera(capture_camera, args)
  while (alt > args.min_altitude or args.simulate) and not terminate:
    print_and_log_mount_state(mount, args)
    if need_meridian_flip(mount, args):
      print_and_log("Meridian flip needed")
      stop_guiding(phd2client)
      perform_meridian_flip(mount)
      print_and_log("Meridian flip complete, running alignment")
      run_alignment(mount, alignment_camera, capture_camera, coordinates, 
                    phd2client, args)
      print_and_log("Alignment complete, starting guiding")
      start_guiding(phd2client)
      print_and_log("Running auto focus")
      run_auto_focus(alignment_camera, focuser, args)
      t_last_focus = time.time()
      print_and_log("Resuming capture")
    if time.time() - t_last_focus > args.focus_interval * 60:
      print_and_log("Running auto focus")
      run_auto_focus(alignment_camera, capture_camera, focuser, args)
      t_last_focus = time.time()
      reset_capture_camera(capture_camera, args)
    if num_images % args.dither_period == 0:
      print_and_log("Dithering...")
      if phd2client.dither(pixels=4, settle_pixels=0.5, settle_timeout=60):
        print_and_log("Dithering complete")
      else:
        print_and_log("Dithering failed")
    image_file = get_image_filename(capture_dir)
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