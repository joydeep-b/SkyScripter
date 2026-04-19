import json
import logging
import os
import time

from sky_scripter.autofocus import auto_focus
from sky_scripter.lib_indi import IndiFocuser, IndiCamera
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.alert_bus import AlertBus

NARROWBAND_FILTERS = {'SHO', 'Ha', 'OIII', 'SII'}


class FocusManager:
  def __init__(self, focuser: IndiFocuser, camera: IndiCamera,
               alert_bus: AlertBus, logger: StructuredLogger,
               calibration_path: str = 'focus_calibration.json',
               focus_step: int = 6, num_steps: int = 7):
    self.focuser = focuser
    self.camera = camera
    self.alert_bus = alert_bus
    self.logger = logger
    self.focus_step = focus_step
    self.num_steps = num_steps
    self.last_focus_time = None
    self.last_focus_temp = None
    self.last_focus_filter = None
    try:
      with open(calibration_path) as f:
        self.calibration = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
      self.calibration = None

  def run_autofocus(self, filter_name: str) -> tuple:
    exposure = 4 if filter_name in NARROWBAND_FILTERS else 2
    self.camera.set_capture_settings(mode=5, gain=70, offset=20,
                                     exposure=exposure)
    self.camera.change_filter(filter_name)
    try:
      best_focus, best_fwhm, results, plot_file = auto_focus(
          self.focuser, self.camera, self.focus_step, self.num_steps,
          filter_name=filter_name)
    except Exception:
      logging.exception("Autofocus failed")
      return None, None
    if best_focus is None:
      return None, None
    self.last_focus_time = time.time()
    try:
      self.last_focus_temp = self.camera.get_temperature()
    except Exception:
      self.last_focus_temp = None
    self.last_focus_filter = filter_name
    _, _, r_squared = None, None, None
    if results and len(results) >= 3:
      import numpy as np
      X = [r[0] for r in results]
      Y = [r[2] for r in results]
      p = np.polyfit(X, Y, 2)
      ss_res = np.sum((np.array(Y) - np.polyval(p, X)) ** 2)
      ss_tot = np.sum((np.array(Y) - np.mean(Y)) ** 2)
      r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    self.logger.log("focus", "focus_complete",
                    filter=filter_name, position=best_focus,
                    fwhm=best_fwhm, temp=self.last_focus_temp,
                    r_squared=r_squared)
    return best_focus, best_fwhm

  def should_refocus(self, interval_minutes: float = 60,
                     temp_threshold: float = 2.0) -> bool:
    if self.last_focus_time is None:
      return True
    if time.time() - self.last_focus_time > interval_minutes * 60:
      return True
    if self.last_focus_temp is not None:
      try:
        current_temp = self.camera.get_temperature()
        if abs(current_temp - self.last_focus_temp) >= temp_threshold:
          return True
      except Exception:
        pass
    return False

  def apply_filter_offset(self, from_filter: str, to_filter: str):
    if self.calibration is None:
      return
    filters = self.calibration.get('filters', {})
    if from_filter not in filters or to_filter not in filters:
      return
    delta = int(filters[to_filter]['offset'] - filters[from_filter]['offset'])
    if delta != 0:
      self.focuser.adjust_focus(delta)

  def predict_focus(self, filter_name: str, temperature: float) -> int | None:
    if self.calibration is None:
      return None
    filters = self.calibration.get('filters', {})
    if filter_name not in filters:
      return None
    cal = filters[filter_name]
    return int(cal['slope'] * temperature + cal['intercept'])
