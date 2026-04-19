#!/usr/bin/env python3
"""Start the web monitor with simulated data for dashboard testing."""

import asyncio
import json
import math
import os
import random
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(__file__))

from sky_scripter.alert_bus import AlertBus, Alert, AlertLevel
from sky_scripter.structured_log import StructuredLogger
from sky_scripter.web_monitor.server import MonitorServer


DEMO_SESSIONS = [
    {"target": "NGC 6888", "filters": "HaOIII", "hours": 2.0},
    {"target": "M31",      "filters": "LRGB",   "hours": 3.5},
    {"target": "IC 1396",  "filters": "HaSIIOIII", "hours": 2.5},
    {"target": "M42",      "filters": "Ha",     "hours": 1.5},
]


def _build_demo_timeline():
    """Build a fake timeline matching the NightScheduler slot format."""
    from datetime import datetime, timedelta
    now = datetime.now()
    dark_start = now.replace(hour=21, minute=0, second=0, microsecond=0)
    cursor = dark_start
    timeline = []
    for idx, sess in enumerate(DEMO_SESSIONS):
        start = cursor
        end = start + timedelta(hours=sess["hours"])
        timeline.append({
            "index": idx,
            "target": sess["target"],
            "filters": sess["filters"],
            "start_iso": start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_iso": end.strftime("%Y-%m-%d %H:%M:%S"),
            "start_local": start.strftime("%H:%M"),
            "end_local": end.strftime("%H:%M"),
        })
        cursor = end
    return timeline


class DemoSchedule:
    """Mimics NightScheduler.get_timeline()."""

    def __init__(self):
        self._timeline = _build_demo_timeline()

    def get_timeline(self):
        return list(self._timeline)


class DemoOrchestrator:
    """Fake orchestrator that cycles through states with realistic data."""

    def __init__(self):
        self._state = "capturing"
        self._target = "M31"
        self._filter = "Ha"
        self._frame = "12 / 30"
        self._exposure_elapsed = 0.0
        self._exposure_total = 300.0
        self._ra = 0.7123
        self._dec = 41.269
        self._alt = 62.3
        self._pier_side = "WEST"
        self._flip_sec = 7200
        self._terminate = False
        self._schedule = DemoSchedule()
        self._active_session_idx = 1
        self._completed = {0}

    class _State:
        def __init__(self, v):
            self.value = v

    @property
    def state(self):
        return self._State(self._state)

    @property
    def session_id(self):
        return self._target

    @property
    def focus_position(self):
        return 24350

    @property
    def focus_fwhm(self):
        return 2.1 + random.gauss(0, 0.15)


class DemoGuideWatchdog:
    @property
    def status(self):
        return {
            "status": "guiding",
            "rms_ra": 0.4 + random.gauss(0, 0.08),
            "rms_dec": 0.35 + random.gauss(0, 0.06),
            "rms_total": 0.55 + random.gauss(0, 0.1),
            "snr": 42.0 + random.gauss(0, 3),
        }


class DemoRoofWatchdog:
    @property
    def status(self):
        return {
            "state": "OPEN",
            "last_check": time.strftime("%H:%M:%S"),
        }


class DemoSafetyWatchdog:
    @property
    def status(self):
        return {
            "status": "OK",
            "disk_free_gb": 482.3,
            "sensor_temp": -10.1 + random.gauss(0, 0.2),
            "cooler_power": 62 + random.randint(-3, 3),
        }


def build_rich_status(orch, guide_wd, roof_wd, safety_wd):
    """Build the full status dict the dashboard expects."""
    return {
        "type": "status",
        "main": {
            "state": orch.state.value,
            "target": orch._target,
            "filter": orch._filter,
            "frame": orch._frame,
            "exposure_elapsed": orch._exposure_elapsed,
            "exposure_total": orch._exposure_total,
        },
        "mount": {
            "ra": f"{orch._ra:.4f}h",
            "dec": f"{orch._dec:.4f}°",
            "alt": orch._alt,
            "pier_side": orch._pier_side,
            "time_to_flip": orch._flip_sec,
        },
        "focus": {
            "position": orch.focus_position,
            "fwhm": orch.focus_fwhm,
            "temp": -10.1 + random.gauss(0, 0.2),
        },
        "guide": guide_wd.status,
        "roof": roof_wd.status,
        "safety": safety_wd.status,
        "schedule": {
            "timeline": orch._schedule.get_timeline(),
            "active_index": orch._active_session_idx,
            "completed": list(orch._completed),
        } if orch._schedule else None,
        "recent_logs": [],
        "recent_alerts": [],
    }


STATES = ["capturing", "dithering", "capturing", "focusing", "capturing", "slewing"]
TARGETS = ["M31", "NGC 6888", "M42", "IC 1396"]
FILTERS = ["Ha", "OIII", "SII", "L", "R", "G", "B"]
SUBSYSTEMS = ["capture", "guide", "mount", "orchestrator"]
EVENTS = [
    ("capture", "frame_saved", "Saved frame 12/30"),
    ("guide", "rms_update", "RMS total=0.55\""),
    ("mount", "tracking", "Sidereal tracking active"),
    ("orchestrator", "state_change", "Capturing on M31"),
    ("capture", "dither_complete", "Dither settled in 4.2s"),
    ("guide", "star_selected", "Guide star SNR=42"),
]


def simulation_loop(orch, srv, struct_log, alert_bus, guide_wd, roof_wd, safety_wd):
    """Periodically broadcast status updates and fake log entries."""
    state_idx = 0
    frame_num = 1
    total_frames = 30

    while True:
        orch._exposure_elapsed += 2
        if orch._exposure_elapsed >= orch._exposure_total:
            orch._exposure_elapsed = 0
            frame_num += 1
            if frame_num > total_frames:
                frame_num = 1
                state_idx = (state_idx + 1) % len(STATES)
                orch._state = STATES[state_idx]
                timeline = orch._schedule.get_timeline()
                if orch._active_session_idx < len(timeline) - 1:
                    orch._completed.add(orch._active_session_idx)
                    orch._active_session_idx += 1
                sess = timeline[orch._active_session_idx]
                orch._target = sess["target"]
                orch._filter = random.choice(list(sess["filters"]))
                total_frames = random.randint(10, 40)
            orch._frame = f"{frame_num} / {total_frames}"

        orch._alt = max(20, orch._alt + random.gauss(0, 0.1))
        orch._flip_sec = max(0, orch._flip_sec - 2)

        status = build_rich_status(orch, guide_wd, roof_wd, safety_wd)
        srv._broadcast(status)

        if random.random() < 0.3:
            sub, evt, detail = random.choice(EVENTS)
            struct_log.log(sub, evt, details=detail)

        time.sleep(2)


def main():
    ws_port = 8765
    http_port = 8080
    if len(sys.argv) > 1:
        ws_port = int(sys.argv[1])
    if len(sys.argv) > 2:
        http_port = int(sys.argv[2])

    orch = DemoOrchestrator()
    guide_wd = DemoGuideWatchdog()
    roof_wd = DemoRoofWatchdog()
    safety_wd = DemoSafetyWatchdog()
    alert_bus = AlertBus()
    struct_log = StructuredLogger("demo")

    srv = MonitorServer(
        orch, guide_wd, roof_wd, safety_wd,
        alert_bus, struct_log,
        port=ws_port, http_port=http_port,
    )
    srv.start()

    print(f"\n  Dashboard:  http://localhost:{http_port}")
    print(f"  WebSocket:  ws://localhost:{ws_port}")
    print(f"\n  Press Ctrl+C to stop.\n")

    sim = threading.Thread(
        target=simulation_loop,
        args=(orch, srv, struct_log, alert_bus, guide_wd, roof_wd, safety_wd),
        daemon=True,
    )
    sim.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
