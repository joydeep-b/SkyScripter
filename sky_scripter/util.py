import subprocess
import logging
import sys
import os
import time
import re
from astroquery.simbad import Simbad 
from astropy.coordinates import SkyCoord, FK5, ICRS
import astropy.units as units
import astropy.time

def init_logging(name, alsologtostdout=False):
  script_dir = os.path.dirname(__file__)
  logfile = os.path.join(
      script_dir,
      '..', '.logs', name + '-' + time.strftime("%Y-%m-%d") + '.log')
  logging.basicConfig(
      filename=logfile, 
      level=logging.INFO, 
      format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
      filemode='a')
  if not alsologtostdout:
    return
  # define a Handler which writes INFO messages or higher to the sys.stderr
  console = logging.StreamHandler()
  console.setLevel(logging.INFO)
  # set a format which is simpler for console use
  formatter = logging.Formatter('%(name)-12s: %(levelname)-8s: %(message)s')
  # tell the handler to use this format
  console.setFormatter(formatter)
  # add the handler to the root logger
  logging.getLogger('').addHandler(console)

def exec_or_fail(command):
  result = subprocess.run(command, capture_output=True, text=True, shell=True)
  if result.returncode != 0:
    logging.error("Error: command '%s' returned %d" % (command, result.returncode))
    logging.error(result.stderr)
    sys.exit(1)
  return result.stdout

def exec_or_pass(command):
  result = subprocess.run(command, capture_output=True, text=True)
  if result.returncode != 0:
    logging.warning("Warning: command '%s' returned %d.\nStderr:" % (command, result.returncode))
    logging.warning(result.stderr)
  return result.stdout

def get_wcs_coordinates(object_name):
    # Query the object
    result_table = Simbad.query_object(object_name)

    if result_table is None:
        print(f"ERROR: Unable to find object '{object_name}'")
        sys.exit(1)
    # Extract RA and DEC
    ra = result_table['RA'][0]
    dec = result_table['DEC'][0]

    # Convert J2000 coordinates to JNow.
    c = SkyCoord(ra, dec, unit=(units.hourangle, units.deg), frame=ICRS())
    jnow_coord = c.transform_to(FK5(equinox=astropy.time.Time.now()))
    
    return jnow_coord.ra.hour, jnow_coord.dec.deg

def run_plate_solve_astap(file, astap_path='astap'):
  astap_cli_command = [astap_path, "-f", file, "-r", "180"]
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