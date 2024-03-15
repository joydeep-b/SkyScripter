import math
import astropy.time
import time
import astropy.units as units
import logging
import os
import sys

script_dir = os.path.dirname(__file__)
parent_dir = os.path.dirname(script_dir)
sys.path.append(parent_dir)

from sky_scripter.lib_indi import IndiMount
from sky_scripter.util import exec_or_fail, init_logging, parse_coordinates, run_plate_solve_astap, print_and_log, run_star_detect_siril
from sky_scripter.lib_gphoto import GphotoClient

def align_to_object(mount, 
                    camera, 
                    ra_target, dec_target, 
                    threshold, 
                    image_dir, 
                    max_iterations=10):
  def compute_error(ra_target, dec_target, ra, dec):
    # Compute error in arcseconds. RA is in hours, DEC is in degrees.
    ra_error = abs(ra_target - ra) / 24 * 360 * 3600
    dec_error = abs(dec_target - dec) * 3600
    return math.sqrt(ra_error**2 + dec_error**2)
  
  def image_filename():
    return os.path.join(
        image_dir, 
        'align-' + time.strftime("%Y-%m-%d-%H-%M-%S") + '.CR3')
  # Repeat capture, sync, goto until within threshold, or max iterations reached.
  complete = False
  iteration = 0
  while not complete and iteration < max_iterations:
    iteration += 1
    print(f"Iteration {iteration}", end=' | ', flush=True)
    t_start = astropy.time.Time.now()
    print('GoTo', end=' | ', flush=True)
    mount.goto(ra_target, dec_target)
    print("Capture", end=' | ', flush=True)
    filename = image_filename()
    camera.capture_image(filename)
    print('Plate solve', end=' | ', flush=True)
    ra, dec = run_plate_solve_astap(filename, None, None)
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
    logging.info(f"Iteration {iteration} RA: {ra_hms}, DEC: {dec_dms}, " + 
                 f"Error: {error:4.1f}, " +
                 f"Iteration time: {(t_end - t_start).sec:4.1f}, " +
                 f"Filename: {filename}")

  if error < threshold:
    complete = True
    print_and_log(f"Alignment complete in {iteration} iterations")
    return True
  elif iteration == max_iterations:
    print("ERROR: Max iterations reached")
    logging.error("Max iterations reached")
    return False
  

def auto_focus_fine(focuser, 
                    camera, 
                    focus_min, 
                    focus_max, 
                    focus_step, 
                    use_num_stars=False):
  focus_results = []
  print('Fine focus scan from %d to %d in steps of %d' % (focus_min, focus_max, focus_step))
  focuser.set_focus(focus_min - focus_step)
  min_fwhm = 100
  max_num_stars = 0
  best_focus = focus_min
  for f in range(focus_min, focus_max + 1, focus_step):
    focuser.set_focus(f)
    image_file = os.path.join(os.getcwd(), 
                              '.focus', 
                              time.strftime("%Y-%m-%dT%H:%M:%S.RAW"))
    camera.capture_image(image_file)
    num_stars, fwhm = run_star_detect_siril(image_file)
    if num_stars is not None and fwhm is not None:
        print(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm}")
        focus_results.append((f, num_stars, fwhm))
        if fwhm < min_fwhm:
            min_fwhm = fwhm
            if not use_num_stars:
                best_focus = f
        if num_stars > max_num_stars:
            max_num_stars = num_stars
            if use_num_stars:
                best_focus = f
    else:
        print(f"Focus value: {f} No stars detected")
  focuser.set_focus(focus_min)
  focuser.set_focus(best_focus)
  return best_focus, min_fwhm, focus_results

def auto_focus_coarse(focuser, camera, focus_min, focus_max, focus_step):
  focus_results = []
  print('Coarse focus scan from %d to %d in steps of %d' % (focus_min, focus_max, focus_step))
  focuser.set_focus(focus_min - focus_step)
  max_num_stars = 0
  best_focus = focus_min
  for f in range(focus_min, focus_max + 1, focus_step):
    focuser.set_focus(f)
    image_file = os.path.join(os.getcwd(), 
                              '.focus', 
                              time.strftime("%Y-%m-%dT%H:%M:%S.RAW"))
    camera.capture_image(image_file)
    num_stars, fwhm = run_star_detect_siril(image_file)
    if num_stars is not None and fwhm is not None:
        print(f"Focus value: {f} NumStars: {num_stars} FWHM: {fwhm}")
        focus_results.append((f, num_stars, fwhm))
        if num_stars > max_num_stars:
            max_num_stars = num_stars
            best_focus = f
    else:
        print(f"Focus value: {f} No stars detected")
  focuser.set_focus(focus_min)
  focuser.set_focus(best_focus)
  return best_focus, max_num_stars, focus_results