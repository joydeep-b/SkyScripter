import csv
import logging
import os
import tempfile
import time

import matplotlib.pyplot as plt
import numpy as np

from sky_scripter.lib_indi import IndiFocuser, IndiCamera
from sky_scripter.util import print_and_log, run_star_detect_siril

FOCUS_LOG_FILE = os.path.join('.focus', 'focus_log.csv')
FOCUS_LOG_COLUMNS = [
    'datetime', 'filter', 'temperature', 'initial_focus', 'best_focus',
    'num_stars', 'fwhm', 'r_squared', 'num_samples', 'focus_min', 'focus_max'
]


def measure_stars(camera: IndiCamera):
  with tempfile.TemporaryDirectory() as tmpdir:
    image_file = os.path.join(tmpdir, time.strftime("%Y-%m-%dT%H:%M:%S.fits"))
    camera.capture_image(image_file)
    return run_star_detect_siril(image_file)


def _sweep(focuser, camera, positions, backlash):
  """Sweep focus positions in order, returning list of (pos, num_stars, fwhm)."""
  positions = sorted(positions)
  if positions:
    focuser.set_focus(positions[0] - backlash)
  results = []
  for pos in positions:
    focuser.set_focus(pos)
    num_stars, fwhm = measure_stars(camera)
    if num_stars is not None and fwhm is not None:
      print_and_log(f"  Focus:{pos:6d}  Stars:{num_stars:4d}  FWHM:{fwhm:.3f}")
      results.append((pos, num_stars, fwhm))
    else:
      print_and_log(f"  Focus:{pos:6d}  No stars detected")
  return results


def _fit_parabola(results):
  """Fit a parabola to (pos, fwhm) data. Returns (coeffs, vertex, r_squared)."""
  if len(results) < 3:
    return None, None, 0.0
  X = np.array([r[0] for r in results], dtype=float)
  Y = np.array([r[2] for r in results], dtype=float)
  p = np.polyfit(X, Y, 2)
  vertex = -p[1] / (2 * p[0]) if p[0] != 0 else X[np.argmin(Y)]
  ss_res = np.sum((Y - np.polyval(p, X)) ** 2)
  ss_tot = np.sum((Y - np.mean(Y)) ** 2)
  r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
  return p, vertex, r_squared


def _save_plot(results, p, vertex, plot_file):
  X = [r[0] for r in results]
  Y = [r[2] for r in results]
  plt.clf()
  plt.plot(X, Y, 'o', label='data')
  if p is not None:
    X_fit = np.linspace(min(X), max(X), 200)
    plt.plot(X_fit, np.polyval(p, X_fit), label='fit')
    plt.axvline(vertex, color='red', linestyle='dashed', label=f'best={int(vertex)}')
  plt.xlabel('Focus')
  plt.ylabel('FWHM')
  plt.legend()
  plt.savefig(plot_file)


def _append_focus_log(filter_name, temperature, initial_focus, best_focus,
                      num_stars, fwhm, r_squared, num_samples,
                      focus_min, focus_max):
  os.makedirs(os.path.dirname(FOCUS_LOG_FILE), exist_ok=True)
  write_header = not os.path.exists(FOCUS_LOG_FILE)
  with open(FOCUS_LOG_FILE, 'a', newline='') as f:
    writer = csv.writer(f)
    if write_header:
      writer.writerow(FOCUS_LOG_COLUMNS)
    writer.writerow([
        time.strftime("%Y-%m-%d %H:%M:%S"), filter_name or '',
        f'{temperature:.1f}' if temperature is not None else '',
        initial_focus, best_focus, num_stars,
        f'{fwhm:.3f}' if fwhm is not None else '',
        f'{r_squared:.3f}' if r_squared is not None else '',
        num_samples, focus_min, focus_max
    ])


def _finish(focuser, camera, all_results, p, vertex, r_squared, backlash,
            filter_name, temperature, initial_focus, focus_min, focus_max):
  """Move to best focus, verify, save plot and log. Returns the 4-tuple."""
  if p is not None and p[0] > 0:
    best_focus = int(vertex)
    best_fwhm = float(np.polyval(p, vertex))
  else:
    best = min(all_results, key=lambda r: r[2])
    best_focus, best_fwhm = best[0], best[2]
    print_and_log("WARNING: Using best discrete sample (parabola fit failed)")

  focuser.set_focus(max(0, best_focus - backlash))
  focuser.set_focus(best_focus)

  num_stars, verify_fwhm = measure_stars(camera)
  print_and_log(f"Best focus: {best_focus}  FWHM: {best_fwhm:.3f}  "
                f"Verified FWHM: {verify_fwhm}  R²: {r_squared:.3f}")

  plot_file = os.path.join('.focus',
                           time.strftime("%Y-%m-%d-%H-%M-%S-focus_plot.png"))
  _save_plot(all_results, p, vertex, plot_file)
  _append_focus_log(filter_name, temperature, initial_focus, best_focus,
                    num_stars, verify_fwhm or best_fwhm, r_squared,
                    len(all_results), focus_min, focus_max)
  return best_focus, best_fwhm, all_results, plot_file


def auto_focus(focuser: IndiFocuser,
               camera: IndiCamera,
               focus_step: int = 6,
               num_steps: int = 7,
               backlash: int = 50,
               filter_name: str = None,
               max_extensions: int = 3,
               timeout: int = 300):
  """Autofocus with adaptive range extension and coarse-sweep fallback.
  Returns (best_focus, best_fwhm, results, plot_file)."""
  os.makedirs(os.path.join('.focus', 'images'), exist_ok=True)
  initial_focus = focuser.get_focus()
  focuser_max = focuser.get_max_focus()
  try:
    temperature = camera.get_temperature()
  except Exception:
    temperature = None

  focus_min = max(0, initial_focus - focus_step * num_steps)
  focus_max = min(focuser_max, initial_focus + focus_step * num_steps)
  all_results = []
  sampled_positions = set()
  tried_coarse = False
  extensions = 0
  t_start = time.time()

  while True:
    if time.time() - t_start > timeout:
      print_and_log("WARNING: Autofocus timed out, restoring initial focus")
      focuser.set_focus(max(0, initial_focus - backlash))
      focuser.set_focus(initial_focus)
      return None, None, all_results, None

    # Sweep positions not yet sampled.
    new_positions = [p for p in range(focus_min, focus_max + 1, focus_step)
                     if p not in sampled_positions]
    if new_positions:
      print_and_log(f"Sweep [{focus_min}, {focus_max}] step={focus_step}")
      results = _sweep(focuser, camera, new_positions, backlash)
      all_results.extend(results)
      sampled_positions.update(new_positions)

    # Fit parabola to all collected data.
    p, vertex, r_squared = _fit_parabola(all_results)

    # Good fit: vertex is concave-up and inside the sampled range.
    if p is not None and p[0] > 0 and focus_min <= vertex <= focus_max:
      print_and_log(f"Good fit: vertex={vertex:.1f} R²={r_squared:.3f}")
      return _finish(focuser, camera, all_results, p, vertex, r_squared,
                     backlash, filter_name, temperature, initial_focus,
                     focus_min, focus_max)

    # Decent data but vertex outside range: extend toward vertex.
    num_detected = sum(1 for r in all_results if r[0] in sampled_positions)
    decent_data = (p is not None and p[0] > 0
                   and num_detected >= len(sampled_positions) // 2)
    if decent_data and extensions < max_extensions:
      span = focus_max - focus_min
      if vertex < focus_min:
        focus_min = max(0, focus_min - span)
        print_and_log(f"Extending left to focus_min={focus_min}")
      else:
        focus_max = min(focuser_max, focus_max + span)
        print_and_log(f"Extending right to focus_max={focus_max}")
      extensions += 1
      continue

    # Poor fit or extensions exhausted: try coarse sweep once.
    if not tried_coarse:
      print_and_log("Falling back to coarse sweep")
      coarse_step = max(focuser_max // 20, 100)
      coarse_positions = list(range(0, focuser_max + 1, coarse_step))
      coarse_results = _sweep(focuser, camera, coarse_positions, backlash)
      tried_coarse = True
      if coarse_results:
        best_coarse = max(coarse_results, key=lambda r: r[1])
        center = best_coarse[0]
        focus_min = max(0, center - focus_step * num_steps)
        focus_max = min(focuser_max, center + focus_step * num_steps)
        all_results = []
        sampled_positions = set()
        extensions = 0
        print_and_log(f"Coarse best at {center} ({best_coarse[1]} stars), "
                      f"narrowing to [{focus_min}, {focus_max}]")
        continue
      else:
        print_and_log("ERROR: No stars detected during coarse sweep")
        return None, None, [], None

    # All fallbacks exhausted: use best discrete sample.
    if all_results:
      print_and_log("All fallbacks exhausted, using best discrete sample")
      return _finish(focuser, camera, all_results, p, vertex, r_squared,
                     backlash, filter_name, temperature, initial_focus,
                     focus_min, focus_max)

    print_and_log("ERROR: Autofocus failed, no usable data")
    focuser.set_focus(max(0, initial_focus - backlash))
    focuser.set_focus(initial_focus)
    return None, None, [], None
