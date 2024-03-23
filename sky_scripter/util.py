import subprocess
import logging
import sys
import os
import time
import re
import shutil
import tempfile
from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord, FK5, ICRS
import astropy.units as units
import astropy.time


def init_logging(name, also_to_console=False):
  script_dir = os.path.dirname(__file__)
  logfile = os.path.join(
      script_dir,
      '..', '.logs', name + '-' + time.strftime("%Y-%m-%d") + '.log')
  logging.basicConfig(
      filename=logfile, 
      level=logging.INFO, 
      format='%(asctime)s %(filename)-20s %(levelname)-8s %(message)s',
      filemode='a')
  if also_to_console:
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s %(filename)-12s: %(levelname)-8s %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

def print_and_log(message, level=logging.INFO):
  print(message)
  logging.log(level, message)

def exec_or_fail(command, allowed_return_codes=[0]):
  # Concatenate the command into a single string
  if type(command) == list:
    command = ' '.join(command)
  # print(f"Executing command: {command}")
  result = subprocess.run(command, capture_output=True, text=True, shell=True)
  if result.stderr:
    # print(f"Error running command '{command}': {result.stderr}")
    logging.error(f"stderr from running command '{command}': {result.stderr}")
  # print(f"stdout: {result.stdout}")
  # print(f"stderr: {result.stderr}")
  # print(f"returncode: {result.returncode}")

  if result.returncode not in allowed_return_codes:
    logging.error("command '%s' returned %d" % (command, result.returncode))
    logging.error(result.stderr)
    print("command '%s' returned %d" % (command, result.returncode))
    sys.exit(1)
  return result.stdout

if sys.platform == 'darwin':
  astap_path_autodetected = '/Applications/astap.app/Contents/MacOS/astap'
else:
  # Get the path to the astap executable from `which astap`
  astap_path_autodetected = exec_or_fail('which astap').strip()

def exec_or_pass(command, allowed_return_codes=[0]):
  result = subprocess.run(command, capture_output=True, text=True)
  if result.returncode not in allowed_return_codes:
    logging.warning("command '%s' returned %d.\nStderr:" % (command, result.returncode))
    logging.warning(result.stderr)
  return result.stdout

def lookup_object_coordinates(object_name):
    # Query the object from Simbad.
    result_table = Simbad.query_object(object_name)

    if result_table is None:
        logging.error(f"ERROR: Unable to find object '{object_name}'")
        print(f"ERROR: Unable to find object '{object_name}'")
        sys.exit(1)
    # Extract RA and DEC
    ra = result_table['RA'][0]
    dec = result_table['DEC'][0]

    # Convert J2000 coordinates to JNow.
    c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg), frame=ICRS())
    jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))
    
    return jnow_coord.ra.hour, jnow_coord.dec.deg

def parse_coordinates(args, parser):
  if args.object is None and args.wcs is None:
    print('ERROR: No object or WCS coordinates specified')
    parser.print_help()
    sys.exit(1)
  if args.object is not None and args.wcs is not None:
    print('ERROR: Both object and WCS coordinates specified')
    parser.print_help()
    sys.exit(1)
  if args.object is not None:
    logging.info(f"Looking up coordinates for object '{args.object}'")
    coordinates = lookup_object_coordinates(args.object)
    # Print WCS coordinates in 6 decimal places
    c = SkyCoord(coordinates[0], coordinates[1], unit=(units.hourangle, units.deg))
    coordinates_string = c.to_string('hmsdms', precision=0, sep=':')
    print_and_log(f"Using WCS coordinates of '{args.object}': {coordinates_string}")
  else:
    print_and_log(f"Using WCS coordinates: {args.wcs}")
    coordinates = args.wcs.split()
    # Convert coordinates to RA and DEC in decimal degrees.
    ra, dec = coordinates
    c = SkyCoord(ra, dec, unit=(units.hour, units.deg))
    # print("Provided: %s %s Interpreted: %f %f" % (ra, dec, c.ra.hour, c.dec.deg))
    # sys.exit(1)
    coordinates = c.ra.hour, c.dec.deg
  return coordinates

def run_plate_solve_astap(file, astap_path=astap_path_autodetected):
  astap_cli_command = [astap_path + " -f " + file + " -r 180"]
  astap_output = exec_or_fail(astap_cli_command)
  # Define the regex pattern, to match output like this:
  # Solution found: 05: 36 03.8	-05° 27 14
  regex = r"Solution found: ([0-9]+): ([0-9]+) ([0-9]+\.[0-9]+)\t([+-])([0-9]+)° ([0-9]+) ([0-9]+)"

  # Search for the pattern in the output
  match = re.search(regex, astap_output)
  if not match:
    logging.warning("No plate solve solution found in ASTAP output:")
    logging.warning(astap_output)
    return None, None

  # Extract matched groups
  alpha_h, alpha_m, alpha_s, delta_sign, delta_d, delta_m, delta_s = match.groups()

  # Convert alpha (RA) to decimal degrees
  alpha = float(alpha_h) + float(alpha_m)/60 + float(alpha_s)/3600

  # Convert delta (DEC) to decimal degrees
  delta_multiplier = 1 if delta_sign == '+' else -1
  delta = delta_multiplier * (float(delta_d) + float(delta_m)/60 + float(delta_s)/3600)

  # TODO: Convert J2000 coordinates to JNow.
  c = SkyCoord(alpha, delta, unit=(units.hourangle, units.deg), frame=ICRS())
  jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))

  alpha = jnow_coord.ra.hour
  delta = jnow_coord.dec.deg

  return alpha, delta


def run_star_detect_siril(image_file):
  with tempfile.TemporaryDirectory() as tmpdirname:
    shutil.copy(image_file, tmpdirname)
    # If MacOS, use the Siril.app version
    if sys.platform == 'darwin':
      SIRIL_PATH = '/Applications/Siril.app/Contents/MacOS/Siril'
    else:
      SIRIL_PATH = '/home/joydeepb/Siril-1.2.1-x86_64.AppImage'
    siril_commands = f"""requires 1.2.0
convert light
calibrate_single light_00001 -bias="=2048" -debayer -cfa -equalize_cfa 
load pp_light_00001
crop 2048 1366 4096 2732
setfindstar -radius=3 -sigma=0.5 -roundness=0.8 -focal=403.2 -pixelsize=4.39 -moffat -minbeta=1.5 -relax=on
findstar
close
"""
    # Define the command to run
    siril_cli_command = [SIRIL_PATH, "-d", tmpdirname, "-s", "-"]
    # Run the command and capture output
    try:
      result = subprocess.run(siril_cli_command, 
                              input=siril_commands,
                              text=True, 
                              capture_output=True,
                              check=True)
      if result.returncode != 0:
        print("Error running Siril.")
        exit(1)
      # print(result.stdout)
      # Extract the number of stars detected, and the FWHM. Sample output:
      # Found 343 Gaussian profile stars in image, channel #0 (FWHM 5.428217)
      regex = r"Found ([0-9]+) [a-z,A-Z]* profile stars in image, channel #[0-9] \(FWHM ([0-9]+\.[0-9]+)\)"
      match = re.search(regex, result.stdout)
      if not match:
        return None, None
      num_stars, fwhm = match.groups()
      return int(num_stars), float(fwhm)
    except subprocess.CalledProcessError as e:
      return None, None