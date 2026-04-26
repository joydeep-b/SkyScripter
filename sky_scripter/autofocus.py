import csv
import logging
import os
import tempfile
import time

import matplotlib
matplotlib.use('Agg')  # Non-interactive backend; plot is only saved to PNG.
import matplotlib.pyplot as plt
import numpy as np

from sky_scripter.lib_indi import IndiFocuser, IndiCamera
from sky_scripter.util import print_and_log, run_star_detect_siril, StarDetectionError

FOCUS_LOG_FILE = os.path.join('.focus', 'focus_log.csv')
FOCUS_LOG_COLUMNS = [
    'datetime', 'filter', 'temperature', 'initial_focus', 'best_focus',
    'num_stars', 'fwhm', 'r_squared', 'num_samples', 'focus_min', 'focus_max'
]


def measure_stars(camera: IndiCamera, logger: logging.Logger | None = None):
  if logger is None:
    logger = logging.getLogger(__name__)
  with tempfile.TemporaryDirectory() as tmpdir:
    image_file = os.path.join(tmpdir, time.strftime("%Y-%m-%dT%H:%M:%S.fits"))
    camera.capture_image(image_file)
    return run_star_detect_siril(image_file, logger=logger)


def _clamp_focus(value, focus_min, focus_max):
  return max(focus_min, min(focus_max, int(value)))


def _positions(start, stop, step):
  values = list(range(start, stop + 1, max(1, step)))
  if values and values[-1] != stop:
    values.append(stop)
  return values


def _sweep(focuser, camera, positions, backlash, hard_min, hard_max,
           logger: logging.Logger | None = None):
  """Sweep focus positions in order, returning list of (pos, num_stars, fwhm)."""
  if logger is None:
    logger = logging.getLogger(__name__)
  positions = sorted(set(_clamp_focus(p, hard_min, hard_max) for p in positions))
  if positions:
    focuser.set_focus(_clamp_focus(positions[0] - backlash, hard_min, hard_max))
  results = []
  for pos in positions:
    focuser.set_focus(pos)
    try:
      num_stars, fwhm = measure_stars(camera, logger=logger)
    except StarDetectionError as e:
      print_and_log(f"  Focus:{pos:6d}  Star detection error: {e}",
                    level=logging.ERROR)
      raise
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


def _is_monotonic(results):
  """True if FWHM is monotonic in focus position (no minimum bracketed)."""
  if len(results) < 3:
    return False
  ordered = sorted(results, key=lambda r: r[0])
  fwhms = [r[2] for r in ordered]
  decreasing = all(fwhms[i] >= fwhms[i + 1] for i in range(len(fwhms) - 1))
  increasing = all(fwhms[i] <= fwhms[i + 1] for i in range(len(fwhms) - 1))
  return decreasing or increasing


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
            filter_name, temperature, initial_focus, focus_min, focus_max,
            hard_min, hard_max, logger: logging.Logger | None = None):
  """Move to best focus, verify, save plot and log. Returns the 4-tuple."""
  if logger is None:
    logger = logging.getLogger(__name__)
  if p is not None and p[0] > 0:
    best_focus = _clamp_focus(vertex, hard_min, hard_max)
    best_fwhm = float(np.polyval(p, vertex))
  else:
    best = min(all_results, key=lambda r: r[2])
    best_focus, best_fwhm = best[0], best[2]
    print_and_log("WARNING: Using best discrete sample (parabola fit failed)")

  focuser.set_focus(_clamp_focus(best_focus - backlash, hard_min, hard_max))
  focuser.set_focus(best_focus)

  num_stars, verify_fwhm = measure_stars(camera, logger=logger)
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
               timeout: int = 300,
               min_position: int | None = None,
               max_position: int | None = None,
               logger: logging.Logger | None = None):
  """Autofocus with adaptive range extension and coarse-sweep fallback.
  Returns (best_focus, best_fwhm, results, plot_file)."""
  if logger is None:
    logger = logging.getLogger(__name__)
  os.makedirs(os.path.join('.focus', 'images'), exist_ok=True)
  initial_focus = focuser.get_focus()
  focuser_max = focuser.get_max_focus()
  hard_min = 0 if min_position is None else max(0, int(min_position))
  hard_max = focuser_max if max_position is None else min(focuser_max,
                                                          int(max_position))
  if hard_min > hard_max:
    print_and_log(f"ERROR: Invalid focus bounds [{hard_min}, {hard_max}]")
    return None, None, [], None
  if not hard_min <= initial_focus <= hard_max:
    print_and_log(f"ERROR: Current focus {initial_focus} is outside configured "
                  f"bounds [{hard_min}, {hard_max}]; refusing autofocus")
    return None, None, [], None
  try:
    temperature = camera.get_temperature()
  except Exception:
    temperature = None

  focus_min = max(hard_min, initial_focus - focus_step * num_steps)
  focus_max = min(hard_max, initial_focus + focus_step * num_steps)
  all_results = []
  sampled_positions = set()
  tried_coarse = False
  extensions = 0
  t_start = time.time()

  while True:
    if time.time() - t_start > timeout:
      print_and_log("WARNING: Autofocus timed out, restoring initial focus")
      focuser.set_focus(_clamp_focus(initial_focus - backlash, hard_min,
                                     hard_max))
      focuser.set_focus(initial_focus)
      return None, None, all_results, None

    # Sweep positions not yet sampled.
    new_positions = [p for p in _positions(focus_min, focus_max, focus_step)
                     if p not in sampled_positions]
    if new_positions:
      print_and_log(f"Sweep [{focus_min}, {focus_max}] step={focus_step}")
      results = _sweep(focuser, camera, new_positions, backlash,
                       hard_min, hard_max, logger=logger)
      all_results.extend(results)
      sampled_positions.update(new_positions)

    # Fit parabola to all collected data.
    p, vertex, r_squared = _fit_parabola(all_results)

    # Good fit: vertex is concave-up and inside the sampled range.
    if p is not None and p[0] > 0 and focus_min <= vertex <= focus_max:
      print_and_log(f"Good fit: vertex={vertex:.1f} R²={r_squared:.3f}")
      return _finish(focuser, camera, all_results, p, vertex, r_squared,
                     backlash, filter_name, temperature, initial_focus,
                     focus_min, focus_max, hard_min, hard_max, logger=logger)

    # Monotonic data means we never bracketed a minimum. Jump straight to the coarse sweep.
    monotonic = _is_monotonic(all_results)

    # Decent data but vertex outside range: extend toward vertex.
    num_detected = sum(1 for r in all_results if r[0] in sampled_positions)
    decent_data = (p is not None and p[0] > 0
                   and num_detected >= len(sampled_positions) // 2
                   and not monotonic)
    if decent_data and extensions < max_extensions:
      span = focus_max - focus_min
      if vertex < focus_min:
        focus_min = max(hard_min, focus_min - span)
        print_and_log(f"Extending left to focus_min={focus_min}")
      else:
        focus_max = min(hard_max, focus_max + span)
        print_and_log(f"Extending right to focus_max={focus_max}")
      extensions += 1
      continue

    # Poor fit, monotonic data, or extensions exhausted: try coarse sweep.
    if not tried_coarse:
      if monotonic:
        print_and_log("Monotonic FWHM (no minimum bracketed); coarse sweep")
      else:
        print_and_log("Falling back to coarse sweep")
      coarse_step = max((hard_max - hard_min) // 20, 100)
      coarse_positions = _positions(hard_min, hard_max, coarse_step)
      print_and_log(f"Coarse sweep [{hard_min}, {hard_max}] step={coarse_step}")
      coarse_results = _sweep(focuser, camera, coarse_positions, backlash,
                              hard_min, hard_max, logger=logger)
      tried_coarse = True
      if coarse_results:
        best_coarse = min(coarse_results, key=lambda r: r[2])
        center = best_coarse[0]
        focus_min = max(hard_min, center - focus_step * num_steps)
        focus_max = min(hard_max, center + focus_step * num_steps)
        all_results = []
        sampled_positions = set()
        extensions = 0
        print_and_log(f"Coarse best at {center} (FWHM {best_coarse[2]:.2f}, "
                      f"{best_coarse[1]} stars), narrowing to "
                      f"[{focus_min}, {focus_max}]")
        continue
      else:
        print_and_log("ERROR: No stars detected during coarse sweep")
        return None, None, [], None

    # All fallbacks exhausted: use best discrete sample.
    if all_results:
      print_and_log("All fallbacks exhausted, using best discrete sample")
      return _finish(focuser, camera, all_results, p, vertex, r_squared,
                     backlash, filter_name, temperature, initial_focus,
                     focus_min, focus_max, hard_min, hard_max, logger=logger)

    print_and_log("ERROR: Autofocus failed, no usable data")
    focuser.set_focus(_clamp_focus(initial_focus - backlash, hard_min,
                                   hard_max))
    focuser.set_focus(initial_focus)
    return None, None, [], None
