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

# Settings for the script.
settings = {
  'mount': 'ZWO AM5',
  'focuser': 'ZWO EAF',
  'camera': 'QHY CCD QHY268M-b93fd94',
  'target': 'M31',
  'capture_dir': '~/Pictures',
  'mode': 5,
  'gain': 56,
  'offset': 20,
  'sequences': [
    {'filter': 'L', 'exposure': 300, 'repeat': 1},
    {'filter': 'R', 'exposure': 300, 'repeat': 1},
    {'filter': 'G', 'exposure': 300, 'repeat': 1},
    {'filter': 'B', 'exposure': 300, 'repeat': 1},
  ]
}

sys.path.append(os.getcwd())

from sky_scripter.lib_gphoto import GphotoClient
from sky_scripter.lib_indi import IndiCamera, IndiFocuser, IndiMount
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
    filename = os.path.join(capture_dir, f'capture-{i:05d}.fits')
    if not os.path.exists(filename):
      return filename
  raise ValueError('Too many images in the capture directory')

def get_time_to_flip(mount: IndiMount, args: argparse.Namespace):
  ra, _, = mount.get_ra_dec()
  lst = mount.get_lst()
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
  ra, dec = mount.get_ra_dec()
  # Intermediary step: Slew to point 3 hours west of the meridian.
  intermediate_ra = ra + 3
  mount.goto(intermediate_ra, dec)
  # Then flip the mount.
  mount.goto(ra, dec)

def confirm_abort():
  print("Abort? (y/n)")
  response = input()
  if response.lower() == 'y':
    print("Aborting the capture")
    logging.warning("Aborting the capture")
    sys.exit(1)
  else:
    logging.warning("User override: Continuing with the capture")

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
      confirm_abort()
  if not events_found:
    print_and_log("No upcoming sprinkler events, continuing with the capture")
  else:
    print_and_log("Continuing with the capture despite sprinkler event")

def set_up_capture_directory(args: argparse.Namespace, coordinates: tuple):
  if args.object is not None:
    capture_name = args.object
  elif args.wcs is not None:
    # Use WCS coordinates as the capture name.
    c = SkyCoord(coordinates[0],
                 coordinates[1],
                 unit=(units.hourangle, units.deg))
    capture_name = c.to_string('hmsdms').replace(' ', '')
  else:
    print("Unable to determine the capture name")
    sys.exit(1)

  # Get the user's home directory.
  home_dir = os.path.expanduser("~")
  capture_dir = os.path.join(home_dir,
                             'Pictures',
                             capture_name.replace(' ', '_'))
  os.makedirs(capture_dir, exist_ok=True)
  print_and_log(f"Capture directory: {capture_dir}")
  return capture_dir

def run_auto_focus(camera: IndiCamera,
                   focuser: IndiFocuser,
                   filter: str,
                   args: argparse.Namespace):
  if filter not in ['L', 'R', 'G', 'B', 'S', 'H', 'O']:
    print_and_log(f"Invalid filter for focusing: '{filter}'", level=logging.ERROR)
    return
  # For filter=L, R, G, B, use exposure time of 2 seconds. For other filters, use 4 seconds.
  if filter in ['L', 'R', 'G', 'B']:
    exposure = 2
  else:
    exposure = 4
  camera.change_filter(filter)
  camera.set_capture_settings(mode=5, gain=70, offset=20, exposure=exposure)
  if args.simulate:
    return
  current_focus = focuser.get_focus()
  focus_step = args.focus_step_size
  focus_min = current_focus - focus_step * args.focus_steps
  focus_max = current_focus + focus_step * args.focus_steps
  focus_at_min_fwhm, min_fwhm, focus_results, plot_file = \
      auto_focus(focuser, camera, focus_min, focus_max, focus_step)
  return focus_at_min_fwhm, min_fwhm, focus_results, plot_file

def run_alignment(mount: IndiMount,
                  camera: IndiCamera,
                  coordinates: tuple,
                  phd2client: Phd2Client | None,
                  args: argparse.Namespace):
  if phd2client is not None:
    phd2client.stop_guiding()
  camera.change_filter('L')
  camera.set_capture_settings(mode=5, gain=70, offset=20, exposure=2)
  align_to_object(mount, camera, coordinates[0], coordinates[1], args.align_threshold)
  if phd2client is not None:
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
      help='INDI mount device name', default='ZWO AM5')
  parser.add_argument('-f', '--focuser', type=str,
      help='INDI focuser device name', default='ZWO EAF')
  parser.add_argument('-c', '--camera', type=str,
      help='INDI camera device name', default='QHY CCD QHY268M-b93fd94')

  # Alignment and mount limit settings.
  parser.add_argument('--align-threshold', type=float,
      help='Alignment threshold in arcseconds', default=20)
  parser.add_argument('--min-altitude', type=float,
      help='Minimum altitude for tracking', default=0)
  parser.add_argument('--meridian-flip-angle', type=float,
      help='HA limit to trigger meridian flip', default=0.3)
  # Dithering settings.
  parser.add_argument('--dither-period', type=int,
      help='Dithering period in number of images', default=10)
  # Auto focus settings.
  parser.add_argument('--focus-step-size', type=int,
      help='Focus step size', default=6)
  parser.add_argument('--focus-steps', type=int,
      help='Number of focus steps on either side of start', default=7)
  parser.add_argument('--focus-interval', type=int,
      help='Focus interval in minutes', default=60)
  parser.add_argument('--skip-initial-focus', action='store_true',
      help='Skip initial focus')
  # Application settings.
  parser.add_argument('--simulate', action='store_true',
      help='Simulate the capture without actually taking images')
  parser.add_argument('-v', '--verbose', action='store_true',
      help='Print verbose messages')
  parser.add_argument('--test', action='store_true',
      help='Run in test mode')

  return parser.parse_args(), parser

def capture_image(camera, filename, exposure):
  global terminate
  pid = os.fork()
  if pid == 0:
    # Child process
    camera.capture_image(filename, exposure=exposure)
    logging.info(f"Image saved to {filename}")
    sys.exit(0)
  else:
    # Parent process: Show a progress bar for the duration, with 20 ticks.
    t_start = time.time()
    duration = exposure
    while time.time() - t_start < duration:
      time.sleep(0.1)
      terminate_string = ' [Terminating]' if terminate else ''
      print(f'\r[Remaining: {duration - (time.time() - t_start):.1f}s] {terminate_string}  ', end='', flush=True)

    # Check if a process by the name "indi_cam_client" is running.
    is_capture_running = os.system('pgrep indi_cam_client > /dev/null') == 0
    while is_capture_running:
      time.sleep(0.1)
      sys_result = os.system('pgrep indi_cam_client > /dev/null')
      is_capture_running = sys_result == 0
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
  ra, dec = mount.get_ra_dec()
  lst = mount.get_lst()
  ha = lst - ra
  pier_side = mount.get_pier_side()
  _, time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds = \
      get_time_to_flip(mount, args)
  log_string = "%s " % (current_date_time)
  log_string += " RA: %9.6f HA: %9.6f DEC: %9.6f |" % (ra, ha, dec)
  log_string += " Pier side: %s |" % pier_side
  # log_string += " Alt: %7.3f Az: %7.3f |" % (alt, az)
  log_string += " Time to flip: %02d:%02d:%02d" % \
      (time_to_flip_hours, time_to_flip_minutes, time_to_flip_seconds)
  print_and_log(log_string)

def check_disk_space():
  # Minimum desired space: 20 GB
  min_desired_space = 20 * 1024 * 1024 * 1024
  statvfs = os.statvfs(os.getcwd())
  free_space = statvfs.f_frsize * statvfs.f_bavail
  if free_space < min_desired_space:
    print_and_log(f"Low disk space: {free_space / (1024 * 1024 * 1024):.2f} GB")
    confirm_abort()

def load_capture_settings():
  # TODO: Load settings from a JSON file.
  return settings

def handle_meridian_flip(mount, phd2client, camera, focuser, coordinates, args):
  print_and_log("Meridian flip needed")
  if phd2client is not None:
    stop_guiding(phd2client)
  perform_meridian_flip(mount)
  print_and_log("Meridian flip complete, running alignment")
  run_alignment(mount, camera, coordinates, phd2client, args)
  print_and_log("Alignment complete")
  if phd2client is not None:
    print_and_log("Resuming guiding")
    start_guiding(phd2client)
  if camera is not None:
    if focuser is not None:
      print_and_log("Running auto focus")
      run_auto_focus(camera, focuser, args)
    print_and_log("Resuming capture")

def test_alignment(args, parser):
  coordinates = parse_coordinates(args, parser)
  mount = IndiMount(args.mount, simulate=args.simulate)
  camera = IndiCamera(args.camera)
  print_and_log('Testing alignment')
  run_alignment(mount, camera, coordinates, None, args)

def test_auto_focus():
  camera = IndiCamera('QHY CCD QHY268M-b93fd94')
  focuser = IndiFocuser('ZWO EAF')
  # Ask which filter to use for testing.
  print("Select a filter to test auto focus (L, R, G, B, S, H, O):")
  filter = input()
  if filter not in ['L', 'R', 'G', 'B', 'S', 'H', 'O']:
    print_and_log("Invalid filter", level=logging.ERROR)
    return
  print_and_log(f'Testing auto focus with filter {filter}')
  focus_at_min_fwhm, min_fwhm, _, plot_file = run_auto_focus(camera, focuser, filter, None)
  print(f"Focus at min FWHM: {focus_at_min_fwhm} Min FWHM: {min_fwhm}")
  print("Opening the plot file")
  # Open the plot file in the default viewer.
  os.system(f'open {plot_file}')

def test_meridian_flip(args):
  mount = IndiMount('ZWO AM5', simulate=False)
  print_and_log("Testing meridian flip")
  # Ask the user what declination to use for the test.
  print("Enter the declination to use for the test:")
  dec = float(input())
  # Ask the user how many minutes ahead of the meridian to start the test from.
  print("Enter the number of minutes ahead of the meridian to start the test from:")
  minutes = float(input())
  # Calculate the RA for the test.
  lst = mount.get_lst()
  ra = lst - minutes / 60
  print(f"Testing meridian flip at RA {ra} DEC {dec}")
  mount.goto(ra, dec)
  print("Waiting for the mount to reach the target")
  while True:
    print_and_log_mount_state(mount, args)
    if need_meridian_flip(mount, args):
      handle_meridian_flip(mount, None, None, None, (ra, dec), args)
      print_and_log("Meridian flip complete")
      break

def test_capture(args):
  camera = IndiCamera(args.camera)
  print("Enter the exposure time in seconds to use for the test:")
  exposure = float(input())
  print("Enter the filter to use for the test (L, R, G, B, S, H, O):")
  filter = input()
  if filter not in ['L', 'R', 'G', 'B', 'S', 'H', 'O']:
    print_and_log("Invalid filter", level=logging.ERROR)
    return
  print("Enter the mode to use for the test:")
  mode = int(input())
  print("Enter the gain to use for the test:")
  gain = int(input())
  print("Enter the offset to use for the test:")
  offset = int(input())
  camera.set_capture_settings(mode, gain, offset, exposure)
  print("Enter the output directory for the test: [Default: ~/Pictures/test_capture]")
  output_dir = input()
  if output_dir == '':
    output_dir = '~/Pictures/test_capture'
  os.makedirs(output_dir, exist_ok=True)
  print("Enter the number of images to capture:")
  num_images = int(input())
  for i in range(num_images):
    image_file = os.path.join(output_dir, f'test_capture_{i:05d}.fits')
    print(f"Capturing image {i} to {image_file}")
    capture_image(camera, image_file, exposure)

def test_guiding():
  global terminate
  phd2client = Phd2Client()
  phd2client.connect()
  print("Starting guiding")
  start_guiding(phd2client)
  print("Guiding started, press Ctrl-C to stop, enter to dither")
  while not terminate:
    time.sleep(1)
    input()
    print("Dithering...")
    if phd2client.dither(pixels=4, settle_pixels=0.5, settle_timeout=60):
      print("Dithering complete")
    else:
      print("Dithering failed")
  print("Stopping guiding")
  stop_guiding(phd2client)

def test_mode(args, parser):
  global terminate, terminate_count
  init_logging('batch_capture_test_mode', also_to_console=args.verbose)
  signal.signal(signal.SIGINT, signal_handler)
  print("Running in test mode")
  while not terminate:
    # Print the menu.
    print("Test mode options:")
    print("1. Test target alignment")
    print("2. Test auto focus")
    print("3. Test meridian flip")
    print("4. Test guiding and dithering")
    print("5. Test capture")
    print("8. Monitor mount state")
    print("p. Park the mount")
    print("u. Unpark the mount")
    print("x. Exit")
    # Get the user's choice.
    choice = input("Enter your choice: ")
    if choice == '1':
      test_alignment(args, parser)
    elif choice == '2':
      test_auto_focus()
    elif choice == '3':
      test_meridian_flip(args)
    elif choice == '4':
      test_guiding()
    elif choice == '5':
      test_capture(args)
    elif choice == '8':
      mount = IndiMount(args.mount, simulate=args.simulate)
      while not terminate:
        print_and_log_mount_state(mount, args)
        time.sleep(1)
    elif choice == 'p':
      mount = IndiMount(args.mount, simulate=args.simulate)
      mount.park()
    elif choice == 'u':
      mount = IndiMount(args.mount, simulate=args.simulate)
      mount.unpark()
    elif choice == 'x':
      break
    else:
      print("Invalid choice")

def main():
  args, parser = get_args()
  if args.test:
    test_mode(args, parser)
    return

  init_logging('batch_capture', also_to_console=args.verbose)
  logging.info(f"Starting batch capture with arguments: {args}")
  signal.signal(signal.SIGINT, signal_handler)
  signal.signal(signal.SIGTERM, signal_handler)

  check_disk_space()
  coordinates = parse_coordinates(args, parser)
  # check_sprinklers()
  capture_dir = set_up_capture_directory(args, coordinates)
  capture_settings = load_capture_settings()
  print("Connecting to devices")
  mount = IndiMount(args.mount, simulate=args.simulate)
  focuser = IndiFocuser(args.focuser, simulate=args.simulate)
  camera = IndiCamera(args.camera)
  mount.unpark()
  print("Connecting to PHD2")
  phd2client = Phd2Client()
  phd2client.connect()
  phd2client.stop_guiding()


  # Initial actions: Align, Start guiding, Auto focus.
  print_and_log('Running initial alignment')
  run_alignment(mount, camera, coordinates, phd2client, args)
  print_and_log_mount_state(mount, args)
  print_and_log('Starting guiding')
  # start_guiding(phd2client)
  if not args.skip_initial_focus:
    print_and_log('Running initial auto focus')
    run_auto_focus(camera, focuser, "L",  args)
  t_last_focus = time.time()

  alt, _ = mount.get_alt_az()
  sequences = capture_settings['sequences']
  while (alt > args.min_altitude or args.simulate) and not terminate:
    for seq in sequences:
      filter = seq['filter']
      exposure = seq['exposure']
      repeat = seq['repeat']
      camera.change_filter(filter)
      # run_auto_focus(camera, focuser, filter, args)
      for i in range(repeat):
        print_and_log_mount_state(mount, args)
        if need_meridian_flip(mount, args):
          handle_meridian_flip(mount, phd2client, camera, focuser, coordinates, args)
          t_last_focus = time.time()
        if time.time() - t_last_focus > args.focus_interval * 60:
          print_and_log("Running auto focus")
          # run_auto_focus(camera, focuser, args)
          t_last_focus = time.time()
        if (i + 1) % args.dither_period == 0:
          print_and_log("Dithering...")
          if phd2client.dither(pixels=4, settle_pixels=0.5, settle_timeout=60):
            print_and_log("Dithering complete")
          else:
            print_and_log("Dithering failed")
        image_file = get_image_filename(capture_dir)
        image_file_short = os.path.basename(image_file)
        print_and_log(f"Capturing image {i} to {image_file_short}")
        camera.set_capture_settings(capture_settings['mode'],
                                    capture_settings['gain'],
                                    capture_settings['offset'],
                                    exposure)
        capture_image(camera, image_file, exposure)
        alt, _ = mount.get_alt_az()

  # End of capture: Stop guiding, park the mount, print summary.
  # phd2client.stop_guiding()
  mount.park()
  if terminate:
    print_and_log("Capture terminated by user")
  elif alt <= args.min_altitude:
    print_and_log("Altitude limit reached, capture stopped")



if __name__ == '__main__':
  main()