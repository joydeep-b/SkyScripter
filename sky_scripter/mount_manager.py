from sky_scripter.lib_indi import IndiMount, IndiCamera
from sky_scripter.algorithms import align_to_object
from sky_scripter.util import print_and_log
from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger


class MountManager:
  def __init__(self, mount: IndiMount, camera: IndiCamera, alert_bus: AlertBus,
               logger: StructuredLogger, meridian_flip_angle: float = 0.3,
               align_threshold: float = 20.0, focal_length_mm: float = None,
               pixel_size_um: float = None):
    self.mount = mount
    self.camera = camera
    self.alert_bus = alert_bus
    self.logger = logger
    self.meridian_flip_angle = meridian_flip_angle
    self.align_threshold = align_threshold
    self.focal_length_mm = focal_length_mm
    self.pixel_size_um = pixel_size_um

  def slew_and_center(self, ra: float, dec: float) -> bool:
    self.logger.log("mount", "slew_start", ra=ra, dec=dec)
    saved = (self.camera.mode, self.camera.gain, self.camera.offset, self.camera.exposure)
    self.camera.set_capture_settings(mode=5, gain=70, offset=20, exposure=2)
    self.camera.change_filter('L')
    try:
      for attempt in range(2):
        if align_to_object(self.mount, self.camera, ra, dec, self.align_threshold,
                         focal_length_mm=self.focal_length_mm,
                         pixel_size_um=self.pixel_size_um):
          self.logger.log("mount", "align_complete", ra=ra, dec=dec, attempts=attempt + 1)
          return True
        print_and_log(f"Alignment attempt {attempt + 1} failed, retrying...")
    finally:
      self.camera.set_capture_settings(*saved)
    self.logger.log("mount", "align_failed", ra=ra, dec=dec)
    self.alert_bus.raise_alert(Alert(
      level=AlertLevel.CRITICAL, source="mount", code="align_failed",
      message=f"Plate-solve alignment failed after 2 attempts for RA={ra:.4f} Dec={dec:.4f}"))
    return False

  def get_time_to_flip(self) -> float:
    ra, _ = self.mount.get_ra_dec()
    ha = self.mount.get_lst() - ra
    if ha > 12:
      ha -= 24
    pier_side = self.mount.get_pier_side()
    if pier_side == "East":
      return (12 + self.meridian_flip_angle - ha) * 3600
    return max(0.0, (self.meridian_flip_angle - ha) * 3600)

  def needs_flip(self) -> bool:
    return self.get_time_to_flip() <= 0

  def perform_flip(self) -> bool:
    ra, dec = self.mount.get_ra_dec()
    self.logger.log("mount", "meridian_flip_start", ra=ra, dec=dec)
    self.mount.goto(ra + 3, dec)
    self.mount.goto(ra, dec)
    self.logger.log("mount", "meridian_flip_complete", ra=ra, dec=dec)
    return True

  def get_status(self) -> dict:
    ra, dec = self.mount.get_ra_dec()
    alt, az = self.mount.get_alt_az()
    lst = self.mount.get_lst()
    ha = lst - ra
    if ha > 12:
      ha -= 24
    pier_side = self.mount.get_pier_side()
    if pier_side == "East":
      time_to_flip = (12 + self.meridian_flip_angle - ha) * 3600
    else:
      time_to_flip = max(0.0, (self.meridian_flip_angle - ha) * 3600)
    return {
      "ra": ra, "dec": dec, "alt": alt, "az": az,
      "ha": ha, "pier_side": pier_side,
      "lst": lst, "time_to_flip_seconds": time_to_flip,
    }

  def park(self):
    print_and_log("Parking mount")
    self.logger.log("mount", "park")
    self.mount.park()

  def unpark(self):
    print_and_log("Unparking mount")
    self.logger.log("mount", "unpark")
    self.mount.unpark()
