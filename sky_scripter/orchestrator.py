import glob
import os
import signal
import time
from enum import Enum

from astropy.coordinates import EarthLocation
from astropy.time import Time
import astropy.units as units

from sky_scripter.alert_bus import AlertBus, AlertLevel
from sky_scripter.capture_manager import CaptureManager
from sky_scripter.cooler_manager import CoolerManager
from sky_scripter.focus_manager import FocusManager
from sky_scripter.guide_watchdog import GuideWatchdog, GuideCommander
from sky_scripter.mount_manager import MountManager
from sky_scripter.roof_watchdog import RoofWatchdog
from sky_scripter.safety_watchdog import SafetyWatchdog
from sky_scripter.scheduler import NightScheduler
from sky_scripter.sequence import NightPlan
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.util import print_and_log


class SessionState(Enum):
  STARTUP = "startup"
  SLEWING = "slewing"
  ALIGNING = "aligning"
  FOCUSING = "focusing"
  GUIDE_START = "guide_start"
  CAPTURING = "capturing"
  DITHERING = "dithering"
  REFOCUSING = "refocusing"
  FLIPPING = "flipping"
  ALERT_RESPONSE = "alert_response"
  EMERGENCY_PARK = "emergency_park"
  WAITING_SAFE = "waiting_safe"
  WAITING_TARGET = "waiting_target"
  SHUTDOWN = "shutdown"


class SessionOrchestrator:
  def __init__(self, plan: NightPlan, mount_mgr: MountManager,
               capture_mgr: CaptureManager, focus_mgr: FocusManager,
               cooler_mgr: CoolerManager, guide_cmd: GuideCommander,
               guide_wd: GuideWatchdog, roof_wd: RoofWatchdog,
               safety_wd: SafetyWatchdog, alert_bus: AlertBus,
               logger: StructuredLogger, capture_dir: str,
               cooler_temp: float = -10.0, min_altitude: float = 0.0):
    self.plan = plan
    self.mount_mgr = mount_mgr
    self.capture_mgr = capture_mgr
    self.focus_mgr = focus_mgr
    self.cooler_mgr = cooler_mgr
    self.guide_cmd = guide_cmd
    self.guide_wd = guide_wd
    self.roof_wd = roof_wd
    self.safety_wd = safety_wd
    self.alert_bus = alert_bus
    self.logger = logger
    self.capture_dir = capture_dir
    self.cooler_temp = cooler_temp
    self.min_altitude = min_altitude
    self.state = SessionState.STARTUP
    self._terminate = False
    self.session_id = time.strftime("%Y%m%d_%H%M%S")
    self.focus_position = None
    self.focus_fwhm = None
    self._current_ra = None
    self._current_dec = None
    self._dark_start = None
    self._dark_end = None
    self._location = None
    self._schedule = None
    self._active_session_idx = None
    self._completed = set()

  def run(self):
    signal.signal(signal.SIGINT, lambda *_: setattr(self, '_terminate', True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(self, '_terminate', True))
    self.guide_wd.start()
    self.roof_wd.start()
    self.safety_wd.start()
    self.cooler_mgr.start_cooling(self.cooler_temp)
    os.makedirs(self.capture_dir, exist_ok=True)

    if self.plan.latitude is not None and self.plan.longitude is not None:
      self._dark_start, self._dark_end = self.plan.astro_dark_times()
      self._location = EarthLocation.from_geodetic(
        self.plan.longitude * units.deg, self.plan.latitude * units.deg,
        self.plan.elevation * units.m)
      print_and_log(f"Astro dark: {self._dark_start.iso} to {self._dark_end.iso}")

    self.logger.log("orchestrator", "session_start",
                    session_id=self.session_id, cooler_temp=self.cooler_temp)
    try:
      self._run_session()
    finally:
      self._shutdown()

  def _run_session(self):
    if self._location is None:
      print_and_log("No observer location set, running sessions sequentially")
      self._run_sequential()
      return

    scheduler = NightScheduler(self.plan, self._dark_start, self._dark_end,
                               self._location)
    scheduler.precompute()
    self._schedule = scheduler
    self.logger.log("orchestrator", "schedule_computed",
                    timeline=scheduler.get_timeline())
    print_and_log("Night plan:")
    for slot in scheduler.get_timeline():
      print_and_log(f"  {slot['start_local']}-{slot['end_local']} "
                    f"{slot['target']} ({slot['filters']})")

    while not self._terminate:
      if not self._handle_alerts():
        break
      session, idx = scheduler.pick_next(self._completed)
      if session is None:
        self._set_state(SessionState.WAITING_TARGET)
        now = Time.now()
        if now > self._dark_end:
          print_and_log("Past astronomical dawn, ending night")
          break
        print_and_log("No eligible session right now, waiting 60s...")
        self.alert_bus.emergency_event.wait(timeout=60)
        if self.alert_bus.emergency_event.is_set():
          break
        continue

      self._active_session_idx = idx
      ra, dec = scheduler.get_coordinates(idx)
      self._current_ra, self._current_dec = ra, dec
      target_name = session.target or session.wcs
      print_and_log(f"Scheduled: {target_name} (RA={ra:.4f} Dec={dec:.4f})")

      self._run_single_session(session, idx, ra, dec)
      self._completed.add(idx)
      self._active_session_idx = None

  def _run_sequential(self):
    """Fallback: run sessions in order when no location is configured."""
    for idx, session in enumerate(self.plan.sessions):
      if self._terminate:
        break
      ra, dec = None, None
      if session.target is not None:
        from sky_scripter.util import lookup_object_coordinates
        ra, dec = lookup_object_coordinates(session.target)
      else:
        from astropy.coordinates import SkyCoord
        parts = session.wcs.split()
        c = SkyCoord(parts[0], parts[1], unit=(units.hour, units.deg))
        ra, dec = c.ra.hour, c.dec.deg
      self._current_ra, self._current_dec = ra, dec
      self._active_session_idx = idx
      self._run_single_session(session, idx, ra, dec)
      self._completed.add(idx)
      self._active_session_idx = None

  def _run_single_session(self, session, idx, ra, dec):
    target_name = session.target or session.wcs
    print_and_log(f"Target: {target_name} -> RA={ra:.4f} Dec={dec:.4f}")

    self._set_state(SessionState.SLEWING)
    if not self.mount_mgr.slew_and_center(ra, dec):
      print_and_log("Alignment failed, skipping target")
      return

    self._set_state(SessionState.FOCUSING)
    first_filter = session.filters[0][0]
    self.focus_position, self.focus_fwhm = self.focus_mgr.run_autofocus(first_filter)

    self._set_state(SessionState.GUIDE_START)
    self.guide_cmd.start_guiding()

    frame_count = 0
    last_filter = first_filter
    filter_frame_counts = {}

    for filter_name, exposure, gain, offset, mode in session.sequence_steps():
      if self._terminate:
        break
      if not self._handle_alerts():
        break

      if self._schedule is not None and not self._schedule.is_eligible_now(idx):
        print_and_log(f"Session no longer eligible (constraints), moving on")
        break

      if self.mount_mgr.needs_flip():
        self._set_state(SessionState.FLIPPING)
        self.guide_cmd.stop_guiding()
        self.mount_mgr.perform_flip()
        self.mount_mgr.slew_and_center(ra, dec)
        self.guide_cmd.start_guiding()

      if self.focus_mgr.should_refocus():
        self._set_state(SessionState.REFOCUSING)
        self.guide_cmd.stop_guiding()
        self.focus_position, self.focus_fwhm = self.focus_mgr.run_autofocus(filter_name)
        last_filter = filter_name
        self.guide_cmd.start_guiding()

      if filter_name != last_filter:
        self.focus_mgr.apply_filter_offset(last_filter, filter_name)
        last_filter = filter_name

      if frame_count > 0 and frame_count % session.dither_every == 0:
        self._set_state(SessionState.DITHERING)
        self.guide_cmd.dither()

      self._set_state(SessionState.CAPTURING)
      filter_frame_counts[filter_name] = filter_frame_counts.get(filter_name, 0) + 1
      filename = self._get_image_filename()
      headers = self._build_fits_headers(session, filter_name,
                                         filter_frame_counts[filter_name])
      print_and_log(f"Capturing {headers['SEQIDX']} exp={exposure}s -> "
                    f"{os.path.basename(filename)}")
      self.capture_mgr.capture(filename, filter_name, exposure, gain,
                               offset, mode, headers)
      frame_count += 1

      min_alt = session.min_altitude or self.min_altitude
      if min_alt > 0:
        status = self.mount_mgr.get_status()
        if status['alt'] < min_alt:
          print_and_log(f"Altitude {status['alt']:.1f}° below minimum "
                        f"{min_alt}°, moving on")
          break

    self.guide_cmd.stop_guiding()

  def _handle_alerts(self) -> bool:
    alerts = self.alert_bus.get_pending()
    for alert in alerts:
      if alert.level == AlertLevel.EMERGENCY:
        self._set_state(SessionState.EMERGENCY_PARK)
        print_and_log(f"EMERGENCY: {alert.message}")
        self.guide_cmd.stop_guiding()
        self.mount_mgr.park()
        return False

      if alert.level == AlertLevel.CRITICAL:
        self._set_state(SessionState.ALERT_RESPONSE)
        if alert.code == "GUIDE_STAR_LOST":
          print_and_log("Guide star lost, waiting 30s for auto-recovery...")
          time.sleep(30)
          if not self.guide_wd.status['is_guiding']:
            print_and_log("Auto-recovery failed, restarting guiding...")
            self.guide_cmd.stop_guiding()
            if not self.guide_cmd.start_guiding():
              print_and_log("Guiding restart failed, re-aligning...")
              self.mount_mgr.slew_and_center(self._current_ra, self._current_dec)
              if not self.guide_cmd.start_guiding():
                print_and_log("Recovery failed, aborting session")
                return False
        elif alert.code == "GUIDE_DISCONNECTED":
          print_and_log("PHD2 disconnected, attempting reconnect...")
          time.sleep(5)
          self.guide_cmd.start_guiding()
        else:
          print_and_log(f"CRITICAL: {alert.code} - {alert.message}")

      if alert.level == AlertLevel.WARNING:
        self.logger.log("orchestrator", "alert_warning",
                        code=alert.code, message=alert.message)
    return True

  def _get_image_filename(self) -> str:
    existing = glob.glob(os.path.join(self.capture_dir, 'capture-*.fits'))
    if not existing:
      return os.path.join(self.capture_dir, 'capture-00001.fits')
    nums = []
    for f in existing:
      try:
        nums.append(int(os.path.basename(f).split('-')[1].split('.')[0]))
      except (ValueError, IndexError):
        continue
    idx = max(nums) + 1 if nums else 1
    return os.path.join(self.capture_dir, f'capture-{idx:05d}.fits')

  def _build_fits_headers(self, session, filter_name, frame_idx) -> dict:
    guide = self.guide_wd.status
    mount = self.mount_mgr.get_status()
    filter_total = sum(c for n, _, c in session.filters if n == filter_name)
    return {
      'OBJECT': session.target or 'unknown',
      'SESSID': self.session_id,
      'FOCUSPOS': self.focus_position,
      'FOCFWHM': self.focus_fwhm,
      'GUIDRMS': round(guide['rms_total'], 3),
      'GUIDRMRA': round(guide['rms_ra'], 3),
      'GUIDRMDE': round(guide['rms_dec'], 3),
      'SEQIDX': f"{filter_name} {frame_idx}/{filter_total}",
      'PIERSIDE': mount['pier_side'],
    }

  def _set_state(self, state: SessionState):
    self.state = state
    self.logger.log("orchestrator", "state_change", state=state.value)

  def _shutdown(self):
    self._set_state(SessionState.SHUTDOWN)
    print_and_log("Shutting down...")
    try:
      self.guide_cmd.stop_guiding()
    except Exception:
      pass
    self.cooler_mgr.warm_up()
    self.mount_mgr.park()
    self.logger.log("orchestrator", "session_end", session_id=self.session_id)
    print_and_log("Shutdown complete")
