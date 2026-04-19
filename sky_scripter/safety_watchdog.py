import os
import threading
import time

from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.lib_indi import IndiCamera


class SafetyWatchdog(threading.Thread):
  def __init__(self, alert_bus: AlertBus, logger: StructuredLogger,
               capture_dir: str, camera: IndiCamera = None,
               poll_interval: float = 30.0,
               disk_warning_gb: float = 20.0, disk_critical_gb: float = 5.0,
               cooler_target_temp: float = None):
    super().__init__(daemon=True)
    self._alert_bus = alert_bus
    self._logger = logger
    self._capture_dir = capture_dir
    self._camera = camera
    self._poll_interval = poll_interval
    self._disk_warning_gb = disk_warning_gb
    self._disk_critical_gb = disk_critical_gb
    self._cooler_target_temp = cooler_target_temp
    self._disk_alerted_level = None  # None / "warning" / "critical"
    self._cooler_alerted = False
    self._lock = threading.Lock()
    self._status = {"disk_free_gb": None, "sensor_temp": None}

  def run(self):
    while True:
      disk_free_gb = self._check_disk()
      sensor_temp = self._check_cooler()
      with self._lock:
        self._status = {"disk_free_gb": disk_free_gb, "sensor_temp": sensor_temp}
      self._logger.log("safety", "safety_check",
                       disk_free_gb=round(disk_free_gb, 2),
                       sensor_temp=sensor_temp)
      time.sleep(self._poll_interval)

  def _check_disk(self) -> float:
    st = os.statvfs(self._capture_dir)
    free_gb = (st.f_frsize * st.f_bavail) / (1024 ** 3)
    if free_gb < self._disk_critical_gb:
      if self._disk_alerted_level != "critical":
        self._disk_alerted_level = "critical"
        self._alert_bus.raise_alert(Alert(
          level=AlertLevel.CRITICAL, source="safety_watchdog",
          code="DISK_SPACE_CRITICAL",
          message=f"Disk space critically low: {free_gb:.1f} GB free",
          data={"free_gb": round(free_gb, 2)}))
    elif free_gb < self._disk_warning_gb:
      if self._disk_alerted_level not in ("warning", "critical"):
        self._disk_alerted_level = "warning"
        self._alert_bus.raise_alert(Alert(
          level=AlertLevel.WARNING, source="safety_watchdog",
          code="DISK_SPACE_LOW",
          message=f"Disk space low: {free_gb:.1f} GB free",
          data={"free_gb": round(free_gb, 2)}))
    return free_gb

  def _check_cooler(self) -> float | None:
    if self._camera is None or self._cooler_target_temp is None:
      return None
    try:
      temp = self._camera.get_temperature()
    except Exception:
      return None
    if temp > self._cooler_target_temp + 3 and not self._cooler_alerted:
      self._cooler_alerted = True
      self._alert_bus.raise_alert(Alert(
        level=AlertLevel.WARNING, source="safety_watchdog",
        code="COOLER_FAILING",
        message=f"Sensor temp {temp:.1f}°C exceeds target "
                f"{self._cooler_target_temp:.1f}°C by >3°C",
        data={"sensor_temp": temp, "target_temp": self._cooler_target_temp}))
    return temp

  @property
  def status(self) -> dict:
    with self._lock:
      return dict(self._status)
