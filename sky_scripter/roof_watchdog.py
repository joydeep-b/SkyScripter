import threading
import time

from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger


class RoofWatchdog(threading.Thread):
  def __init__(self, alert_bus: AlertBus, logger: StructuredLogger,
               status_file: str = None, poll_interval: float = 5.0,
               unknown_timeout: float = 30.0):
    super().__init__(daemon=True)
    self._alert_bus = alert_bus
    self._logger = logger
    self._status_file = status_file
    self._poll_interval = poll_interval
    self._unknown_timeout = unknown_timeout
    self._lock = threading.Lock()
    self._roof_is_open: bool | None = None
    self._last_check_time: float | None = None
    self._was_previously_open = False
    self._last_readable_time: float | None = None
    self._warned_unknown = False

  def run(self):
    if self._status_file is None:
      return
    self._last_readable_time = time.time()
    while True:
      try:
        with open(self._status_file, 'r') as f:
          content = f.readline().strip().upper()
        is_open = content == "OPEN"
        self._last_readable_time = time.time()
        self._warned_unknown = False
      except (OSError, IOError):
        if not self._warned_unknown and time.time() - self._last_readable_time > self._unknown_timeout:
          self._alert_bus.raise_alert(Alert(
            level=AlertLevel.WARNING, source="roof_watchdog",
            code="ROOF_STATUS_UNKNOWN",
            message=f"Roof status file unreadable for >{self._unknown_timeout}s"))
          self._warned_unknown = True
        time.sleep(self._poll_interval)
        continue

      prev_open = self._roof_is_open
      with self._lock:
        self._roof_is_open = is_open
        self._last_check_time = time.time()

      if self._was_previously_open and not is_open:
        self._alert_bus.raise_alert(Alert(
          level=AlertLevel.EMERGENCY, source="roof_watchdog",
          code="ROOF_CLOSING", message="Roof closing detected"))

      if prev_open != is_open:
        self._logger.log("roof_watchdog", "roof_status",
                         roof_is_open=is_open)

      self._was_previously_open = is_open
      time.sleep(self._poll_interval)

  @property
  def status(self) -> dict:
    with self._lock:
      return {"roof_is_open": self._roof_is_open,
              "last_check_time": self._last_check_time}
