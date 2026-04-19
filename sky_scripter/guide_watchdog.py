import collections
import json
import logging
import math
import socket
import threading
import time

from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.lib_phd2 import Phd2Client
from sky_scripter.structured_log import StructuredLogger

logger = logging.getLogger(__name__)


class GuideWatchdog(threading.Thread):
  def __init__(self, alert_bus: AlertBus, logger: StructuredLogger,
               phd2_host: str = 'localhost', phd2_port: int = 4400,
               rms_threshold: float = 2.0, drift_timeout: float = 60.0):
    super().__init__(daemon=True)
    self.alert_bus = alert_bus
    self.slog = logger
    self.phd2_host = phd2_host
    self.phd2_port = phd2_port
    self.rms_threshold = rms_threshold
    self.drift_timeout = drift_timeout
    self._lock = threading.Lock()
    self._rms_ra = 0.0
    self._rms_dec = 0.0
    self._rms_total = 0.0
    self._is_guiding = False
    self._is_settling = False
    self._star_snr = 0.0
    # Sliding window: (timestamp, ra_dist, dec_dist)
    self._window = collections.deque()
    self._window_seconds = 60
    self._drift_start_time = None
    self._last_log_time = 0.0

  def run(self):
    while True:
      try:
        self._run_loop()
      except Exception as e:
        logger.error(f"Guide watchdog connection error: {e}")
        self.alert_bus.raise_alert(Alert(
          level=AlertLevel.CRITICAL, source="guide_watchdog",
          code="GUIDE_DISCONNECTED",
          message=f"PHD2 connection lost: {e}"))
        with self._lock:
          self._is_guiding = False
        time.sleep(5)

  def _run_loop(self):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((self.phd2_host, self.phd2_port))
    sock.settimeout(5.0)
    buf = b''
    while True:
      try:
        chunk = sock.recv(4096)
        if not chunk:
          raise ConnectionError("PHD2 socket closed")
        buf += chunk
      except socket.timeout:
        continue
      while b'\n' in buf:
        line, buf = buf.split(b'\n', 1)
        line = line.strip()
        if not line:
          continue
        try:
          msg = json.loads(line)
        except json.JSONDecodeError:
          continue
        if 'Event' not in msg:
          continue
        self._handle_event(msg)

  def _handle_event(self, msg):
    event = msg['Event']
    now = time.time()

    if event == 'GuideStep':
      ra_dist = msg.get('RADistanceRaw', 0.0)
      dec_dist = msg.get('DECDistanceRaw', 0.0)
      snr = msg.get('SNR', 0.0)
      self._window.append((now, ra_dist, dec_dist))
      cutoff = now - self._window_seconds
      while self._window and self._window[0][0] < cutoff:
        self._window.popleft()
      n = len(self._window)
      ra_sq_sum = sum(e[1] ** 2 for e in self._window)
      dec_sq_sum = sum(e[2] ** 2 for e in self._window)
      rms_ra = math.sqrt(ra_sq_sum / n)
      rms_dec = math.sqrt(dec_sq_sum / n)
      rms_total = math.sqrt(ra_sq_sum / n + dec_sq_sum / n)
      with self._lock:
        self._rms_ra = rms_ra
        self._rms_dec = rms_dec
        self._rms_total = rms_total
        self._star_snr = snr
        self._is_guiding = True
      # Drift detection
      if rms_total > self.rms_threshold:
        if self._drift_start_time is None:
          self._drift_start_time = now
        elif now - self._drift_start_time > self.drift_timeout:
          self.alert_bus.raise_alert(Alert(
            level=AlertLevel.CRITICAL, source="guide_watchdog",
            code="GUIDE_DRIFT_EXCEEDED",
            message=f"Guide RMS {rms_total:.2f} exceeded {self.rms_threshold} "
                    f"for {self.drift_timeout}s",
            data={"rms_total": rms_total, "rms_ra": rms_ra, "rms_dec": rms_dec}))
          self._drift_start_time = now  # Reset to avoid spamming
      else:
        self._drift_start_time = None
      # Periodic structured log
      if now - self._last_log_time >= 10.0:
        self._last_log_time = now
        self.slog.log("guide", "guide_rms",
                      rms_ra=round(rms_ra, 3), rms_dec=round(rms_dec, 3),
                      rms_total=round(rms_total, 3), snr=round(snr, 1))

    elif event == 'StarLost':
      self.alert_bus.raise_alert(Alert(
        level=AlertLevel.CRITICAL, source="guide_watchdog",
        code="GUIDE_STAR_LOST", message="PHD2 lost guide star"))

    elif event == 'Settling':
      with self._lock:
        self._is_settling = True

    elif event == 'SettleDone':
      with self._lock:
        self._is_settling = False

    elif event == 'LoopingExposures':
      with self._lock:
        self._is_guiding = False

    elif event == 'AppState':
      state = msg.get('State', '')
      with self._lock:
        self._is_guiding = (state == 'Guiding')

  @property
  def status(self) -> dict:
    with self._lock:
      return {
        "rms_ra": self._rms_ra,
        "rms_dec": self._rms_dec,
        "rms_total": self._rms_total,
        "is_guiding": self._is_guiding,
        "star_snr": self._star_snr,
        "is_settling": self._is_settling,
      }


class GuideCommander:
  def __init__(self, phd2_host: str = 'localhost', phd2_port: int = 4400):
    self.phd2_host = phd2_host
    self.phd2_port = phd2_port

  def start_guiding(self, timeout: float = 360) -> bool:
    client = Phd2Client(self.phd2_host, self.phd2_port)
    client.connect()
    return client.start_guiding(timeout=timeout)

  def stop_guiding(self) -> bool:
    client = Phd2Client(self.phd2_host, self.phd2_port)
    client.connect()
    return client.stop_guiding()

  def dither(self, pixels: float = 4, settle_pixels: float = 0.5,
             settle_timeout: float = 60) -> bool:
    client = Phd2Client(self.phd2_host, self.phd2_port)
    client.connect()
    return client.dither(pixels=pixels, settle_pixels=settle_pixels,
                         settle_timeout=settle_timeout)
