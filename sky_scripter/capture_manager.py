import logging
import os
import sys
import time

from sky_scripter.lib_indi import IndiCamera
from sky_scripter.alert_bus import AlertBus
from sky_scripter.structured_log import StructuredLogger


class CaptureManager:
  def __init__(self, camera: IndiCamera, alert_bus: AlertBus,
               logger: StructuredLogger):
    self.camera = camera
    self.alert_bus = alert_bus
    self.logger = logger

  def capture(self, filename: str, filter_name: str, exposure: float,
              gain: int, offset: int, mode: int,
              extra_fits_headers: dict = None) -> bool:
    self.camera.change_filter(filter_name)
    self.camera.set_capture_settings(mode, gain, offset, exposure)
    self.logger.log("capture", "capture_start",
                    filter=filter_name, exposure=exposure, filename=filename)

    pid = os.fork()
    if pid == 0:
      self.camera.capture_image(filename, exposure=exposure)
      sys.exit(0)

    # Wait for exposure, but wake every 0.5s to check for emergencies.
    t_start = time.time()
    aborted = False
    while os.system('pgrep indi_cam_client > /dev/null') == 0:
      if self.alert_bus.emergency_event.wait(timeout=0.5):
        os.system('pkill indi_cam_client')
        logging.error("Emergency event during capture, aborting")
        self.logger.log("capture", "capture_aborted",
                        filter=filter_name, exposure=exposure,
                        filename=filename, reason="emergency_event")
        aborted = True
        break

    os.waitpid(pid, 0)
    if aborted:
      return False

    if not os.path.exists(filename) or os.path.getsize(filename) == 0:
      logging.error(f"Capture failed: {filename} missing or empty")
      self.logger.log("capture", "capture_failed",
                      filter=filter_name, exposure=exposure, filename=filename)
      return False

    file_size_mb = os.path.getsize(filename) / (1024 * 1024)

    if extra_fits_headers:
      from astropy.io import fits
      with fits.open(filename, mode='update') as hdul:
        for key, value in extra_fits_headers.items():
          hdul[0].header[key] = value

    self.logger.log("capture", "capture_complete",
                    filter=filter_name, exposure=exposure,
                    filename=filename, file_size_mb=round(file_size_mb, 2))
    return True
