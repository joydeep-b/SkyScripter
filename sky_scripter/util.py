import subprocess
import logging
import sys
import os
import time
import re
import shutil
import tempfile
import math
from astroquery.simbad import Simbad
from astropy.coordinates import SkyCoord, FK5, ICRS
import astropy.units as units
import astropy.time


def init_logging(name, also_to_console=False):
  script_dir = os.path.dirname(__file__)
  # Make sure the log directory exists, create it if not.
  log_dir = os.path.join(script_dir, '..', '.logs')
  if not os.path.exists(log_dir):
    os.makedirs(log_dir)
  logfile = os.path.join(log_dir, name + '-' + time.strftime("%Y-%m-%d") + '.log')
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
  astap_path_autodetected = shutil.which('astap')

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


class StarDetectionError(RuntimeError):
  """Siril ran incorrectly or returned output we could not interpret."""


def get_siril_path():
  if sys.platform == 'darwin':
    return '/Applications/Siril.app/Contents/MacOS/Siril'
  return shutil.which('siril-cli') or shutil.which('siril')


def _summarize_process_output(result):
  parts = []
  if result.stdout:
    parts.append("stdout:\n" + result.stdout[-4000:])
  if result.stderr:
    parts.append("stderr:\n" + result.stderr[-4000:])
  return "\n".join(parts) if parts else "<no stdout/stderr>"


def run_plate_solve_astap(file, astap_path=astap_path_autodetected):
  if astap_path is None:
    logging.warning("ASTAP executable not found")
    return None, None
  astap_cli_command = [astap_path + " -f " + file + " -r 180"]
  astap_output = exec_or_fail(astap_cli_command)
  # Define the regex pattern, to match output like this:
  # Solution found: 05: 36 03.8	-05° 27 14
  regex = r"Solution found: ([\ ]*[0-9]+): ([\ ]*[0-9]+) ([\ ]*[0-9]+\.[0-9]+)\t([+-])([\ ]*[0-9]+)° ([\ ]*[0-9]+) ([\ ]*[0-9]+\.[0-9]+)"
  # print(f"ASTAP output:\n {astap_output}\n========\n")
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

  if False:
    print(f"RA: {alpha}, DEC: {delta}")

  # TODO: Convert J2000 coordinates to JNow.
  c = SkyCoord(alpha, delta, unit=(units.hourangle, units.deg), frame=ICRS())
  jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))

  alpha = jnow_coord.ra.hour
  delta = jnow_coord.dec.deg

  return alpha, delta


def run_star_detect_siril(image_file):
  with tempfile.TemporaryDirectory() as tmpdirname:
    shutil.copy(image_file, tmpdirname)
    siril_path = get_siril_path()
    if siril_path is None or (os.path.isabs(siril_path)
                              and not os.path.exists(siril_path)):
      raise StarDetectionError(f"siril executable not found: {siril_path}")
    siril_commands = f"""requires 1.2.0
convert light
# calibrate_single light_00001 -bias="=2048" -debayer -cfa -equalize_cfa
# load pp_light_00001
# crop 2048 1366 4096 2732
load light_00001
setfindstar -radius=3 -sigma=0.5 -roundness=0.8 -focal=403.2 -pixelsize=4.39 -moffat -minbeta=1.5 -relax=on
findstar
close
"""
    siril_cli_command = [siril_path, "-d", tmpdirname, "-s", "-"]
    try:
      result = subprocess.run(siril_cli_command,
                              input=siril_commands,
                              text=True,
                              capture_output=True,
                              check=False)
    except OSError as e:
      raise StarDetectionError(f"Could not execute Siril: {e}") from e
    if result.returncode != 0:
      raise StarDetectionError(
          f"Siril star detection failed with code {result.returncode}\n"
          f"{_summarize_process_output(result)}")

    output = result.stdout + "\n" + result.stderr
    # Sample: log: Found 343 Gaussian profile stars in image, channel #0 (FWHM 5.428217)
    regex = re.compile(
        r"^log: Found\s+(\d+)\s+([A-Za-z]+)\s+profile stars in image, "
        r"channel #(\d+) \(FWHM ([0-9]+(?:\.[0-9]+)?)\)$",
        re.MULTILINE)
    matches = regex.findall(output)
    if not matches:
      # Siril's findstar omits the "Found N profile stars" line entirely
      # when it detects 0 stars. Detect this by the presence of the
      # "Findstar: processing for channel" line without a following Found.
      findstar_ran = re.search(
          r"^log: Findstar:\s+processing for channel",
          output, re.MULTILINE)
      no_stars = re.search(r"\b(no stars?|0 stars?)\b", output, re.I)
      if findstar_ran or no_stars:
        return None, None
      raise StarDetectionError(
          "Could not parse Siril star-detection output\n"
          f"{_summarize_process_output(result)}")

    num_stars, _profile, _channel, fwhm = matches[-1]
    num_stars, fwhm = int(num_stars), float(fwhm)
    if num_stars <= 0:
      return None, None
    if not math.isfinite(fwhm) or fwhm <= 0:
      raise StarDetectionError(
          f"Siril returned invalid FWHM: stars={num_stars}, FWHM={fwhm}\n"
          f"{_summarize_process_output(result)}")
    return num_stars, fwhm