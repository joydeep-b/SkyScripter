import math
import astropy.time
import time
import astropy.units as units
import logging
import os
import sys
import numpy as np
import matplotlib.pyplot as plt
import tempfile

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount, IndiFocuser, IndiCamera
from sky_scripter.util import exec_or_fail, init_logging, parse_coordinates, run_plate_solve_astap, print_and_log, run_star_detect_siril
from sky_scripter.lib_gphoto import GphotoClient

def align_to_object(mount: IndiMount,
                    camera: IndiCamera,
                    ra_target, dec_target,
                    threshold,
                    max_iterations=10):
  image_dir = os.path.join(os.getcwd(), '.align')
  os.makedirs(image_dir, exist_ok=True)
  def compute_error(ra_target, dec_target, ra, dec):
    # Compute error in arcseconds. RA is in hours, DEC is in degrees.
    ra_error = abs(ra_target - ra) / 24 * 360 * 3600
    dec_error = abs(dec_target - dec) * 3600
    return math.sqrt(ra_error**2 + dec_error**2)

  def image_filename():
    return os.path.join(
        image_dir,
        'align-' + time.strftime("%Y-%m-%d-%H-%M-%S") + '.fits')
  # Repeat capture, sync, goto until within threshold, or max iterations reached.
  iteration = 0
  while iteration < max_iterations:
    iteration += 1
    print(f"Iteration {iteration}", end=' | ', flush=True)
    t_start = astropy.time.Time.now()
    print('GoTo', end=' | ', flush=True)
    mount.goto(ra_target, dec_target)
    print("Capture", end=' | ', flush=True)
    filename = image_filename()
    camera.capture_image(filename)
    print('Plate solve', end=' | ', flush=True)
    ra, dec = run_plate_solve_astap(filename)
    print('Sync', end=' | ', flush=True)
    mount.sync(ra, dec)
    t_end = astropy.time.Time.now()
    error = compute_error(ra_target, dec_target, ra, dec)
    # Print RA in HH:MM:SS and DEC in DD:MM:SS, and error in arcseconds.
    ra_hms = astropy.coordinates.Angle(ra, unit=units.hour). \
        to_string(unit=units.hour, sep=':')
    dec_dms = astropy.coordinates.Angle(dec, unit=units.deg). \
        to_string(unit=units.deg, sep=':')
    print(f"RA: {ra_hms}, DEC: {dec_dms}, Error: {error:4.1f}" +
          f" | Iteration time: {(t_end - t_start).sec:4.1f}")
    logging.info(f"Iteration {iteration} " +
                 f"RA: {ra_hms:17s}, DEC: {dec_dms:17s}, " +
                 f"Error: {error:4.1f}, " +
                 f"Iteration time: {(t_end - t_start).sec:4.1f}, " +
                 f"Filename: {filename}")
    if error < threshold:
      complete = True
      print_and_log(f"Alignment complete in {iteration} iterations")
      return True

  if iteration == max_iterations:
    print("ERROR: Max iterations reached")
    logging.error("Max iterations reached")
    return False

def measure_stars(camera: IndiCamera):
  # Create a temporary directory to store images.
  with tempfile.TemporaryDirectory() as tmpdirname:
    image_file = os.path.join(tmpdirname,
                              time.strftime("%Y-%m-%dT%H:%M:%S.fits"))
    camera.capture_image(image_file)
    num_stars, fwhm = run_star_detect_siril(image_file)
  return num_stars, fwhm

def auto_focus(focuser: IndiFocuser, 
               camera: IndiFocuser, 
               focus_min: float, 
               focus_max: float, 
               focus_step: int, 
               backlash=50):
  os.makedirs(os.path.join(os.getcwd(), '.focus', 'images'), exist_ok=True)
  focus_results = []
  print_and_log('Focus scan from %d to %d in steps of %d' % \
                (focus_min, focus_max, focus_step))

  initial_focus = focuser.get_focus()
  _, initial_fwhm = measure_stars(camera)
  if initial_fwhm is None:
    logging.error("Unable to measure initial FWHM")
    return None, None, None
  print_and_log(f"Initial focus value: {initial_focus} Initial FWHM: {initial_fwhm}")

  focuser.set_focus(focus_min - backlash)
  _, min_focuser_fwhm = measure_stars(camera)
  if min_focuser_fwhm is not None and min_focuser_fwhm < initial_fwhm:
    logging.error("FWHM at focus_min - focus_step is less than initial FWHM")
    return None, None, None
  print_and_log(f"Focus value: {focus_min - focus_step} FWHM: {min_focuser_fwhm}")

  min_fwhm = 100
  focus_at_min_fwhm = focus_min
  # Make a list of focus test points.
  focus_test_points = list(range(focus_min, focus_max + 1, focus_step))
  while len(focus_test_points) > 0:
    focus_test_points = sorted(focus_test_points)
    f = focus_test_points.pop(0)
    focuser.set_focus(f)
    num_stars, fwhm = measure_stars(camera)
    if num_stars is not None and fwhm is not None:
      print_and_log(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm:.3f}")
      focus_results.append((f, num_stars, fwhm))
      if fwhm < min_fwhm:
        min_fwhm = fwhm
        focus_at_min_fwhm = f
    else:
      print_and_log(f"Focus value: {f} No stars detected")

  # Incremental algorithm:
  # 1. Iterate until the focus test points are empty.
  # 2. When the focus test points are empty:
  #    a. Fit a parabola to the data.
  #    b. Find the minimum of the parabola.
  #    c. If the parabola is concave down:
  #       i. Fit a line to the data.
  #       ii. Check if the line is increasing, or decreasing.
  #       iii. If the line is increasing, reduce focus_min and generate new focus test points from
  #            the new focus_min to the old focus_max.
  #       iv. If the line is decreasing, increase focus_max and generate new focus test points from
  #           the old focus_max to the new focus_max.
  #    c. Ensure that there are at least 3 points on either side of the minimum.

  # Fit a parabola to the data.
  X = [x[0] for x in focus_results]
  Y = [x[2] for x in focus_results]
  p = np.polyfit(X, Y, 2)
  print_and_log(f"Parabola coefficients: {p}")
  Y_fit = np.polyval(p, X)
  minima = -p[1] / (2 * p[0])
  print_and_log(f"Parabola minimum: {minima}")
  best_focus = int(minima)
  if p[0] <= 0:
    logging.error("Parabola is concave down, focus may be outside of range.")
    best_focus = focus_at_min_fwhm
  else:
    min_fwhm = np.polyval(p, minima)
    focus_at_min_fwhm = int(minima)
  focuser.set_focus(focus_min - backlash)
  focuser.set_focus(best_focus)
  plt.clf()
  plt.plot(X, Y, 'o', label='data')
  plt.plot(X, Y_fit, label='fit')
  plt.plot(minima, np.polyval(p, minima), 'x', label='minima', color='red',
           markersize=10)
  plt.axvline(minima, color='red', linestyle='dashed')
  plt.axhline(np.polyval(p, minima), color='red', linestyle='dashed')
  plt.xlabel('Focus')
  plt.ylabel('FWHM')
  plt.legend()
  plot_file = os.path.join(os.getcwd(),
                           '.focus',
                           time.strftime("%Y-%m-%d-%H-%M-%S-focus_plot.png"))
  plt.savefig(plot_file)
  return focus_at_min_fwhm, min_fwhm, focus_results, plot_file