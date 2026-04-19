# Sky Scripter -- Autonomous Imaging Sequencer

Sky Scripter is an autonomous astrophotography imaging system. It slews to
targets, plate-solves for alignment, autofocuses, guides with PHD2, captures
image sequences, handles meridian flips, and monitors safety conditions -- all
unattended through the night.

## Architecture

The system is organized around a **main thread** running the imaging pipeline
and **background watchdog threads** monitoring safety conditions. Watchdogs
communicate with the main thread through a thread-safe `AlertBus`.

```
Main Thread                          Background Threads
-----------                          ------------------
SessionOrchestrator                  GuideWatchdog  (PHD2 events)
  NightScheduler                     RoofWatchdog   (status file)
  MountManager                       SafetyWatchdog (disk, cooler)
  CaptureManager
  FocusManager           <--- AlertBus --->
  CoolerManager
  GuideCommander
                                     MonitorServer  (WebSocket + HTTP)
```

The orchestrator picks which session to run based on the `NightScheduler`,
which precomputes eligibility windows for every target across the night
considering altitude, moon phase, moon altitude, and time-of-night constraints.
At each decision point the scheduler picks the target whose window ends
soonest, maximizing total imaging time.

## Directory Layout

```
sky_scripter/
  config.py            Global configuration loader
  sequence.py          ImagingSession / NightPlan DSL
  scheduler.py         NightScheduler (precompute + pick_next)
  orchestrator.py      SessionOrchestrator (main state machine)
  alert_bus.py         Thread-safe alert queue + emergency event
  structured_log.py    JSON structured logger with ring buffer
  mount_manager.py     Plate-solve alignment, meridian flips
  capture_manager.py   Emergency-aware capture, FITS header enrichment
  focus_manager.py     Autofocus with temperature tracking + calibration
  cooler_manager.py    CCD cooling and gradual warm-up
  guide_watchdog.py    PHD2 event listener + GuideCommander
  roof_watchdog.py     Roof status file polling
  safety_watchdog.py   Disk space + cooler health checks
  lib_indi.py          INDI device wrappers (mount, camera, focuser)
  lib_phd2.py          PHD2 JSON-RPC client
  algorithms.py        Plate-solve alignment loop
  autofocus.py         V-curve autofocus routine
  util.py              Logging, Simbad lookup, ASTAP/Siril helpers
  web_monitor/
    server.py          WebSocket + HTTP server
    static/index.html  Night-vision-friendly dashboard

analysis/
  focus_calibrator.py  Offline focus-vs-temperature model fitting

tests/
  test_alert_bus.py    AlertBus unit tests
  test_sequence.py     Sequence DSL unit tests
  test_scheduler.py    NightScheduler unit tests
  test_config.py       Config loader unit tests

test_hardware.py       Daytime hardware connectivity checklist
test_on_sky.py         Interactive on-sky subsystem tests
sky_scripter.json      Global site/device/parameter configuration
```

## Quick Start

### 1. Configure your site

Copy the example config and edit it with your observatory location and hardware:

```bash
cp sky_scripter.json.example sky_scripter.json
```

Then edit `sky_scripter.json`:

```json
{
  "site": {
    "latitude": 30.27,
    "longitude": -97.74,
    "elevation": 200
  },
  "devices": {
    "mount": "ZWO AM5",
    "camera": "QHY CCD QHY268M-b93fd94",
    "focuser": "ZWO EAF"
  },
  "cooler": {
    "target_temp": -10.0
  }
}
```

Only override what differs from the defaults. Unspecified keys keep their
built-in values (see the full `sky_scripter.json` for all available settings).

### 2. Define a night plan

A night plan is a Python script or JSON file describing what to image.

**Python DSL (recommended):**

```python
from sky_scripter.sequence import ImagingSession, NightPlan

plan = NightPlan(latitude=30.27, longitude=-97.74, elevation=200)

# Broadband target -- only when moon is below horizon
plan.add(ImagingSession(
    "M31",
    L=(300, 20), R=(300, 10), G=(300, 10), B=(300, 10),
    max_moon_altitude=0,
    max_moon_phase=25,
    min_altitude=25,
    dither_every=3,
))

# Narrowband target -- moon doesn't matter
plan.add(ImagingSession(
    "NGC 6888",
    Ha=(300, 30), OIII=(300, 20),
    min_altitude=30,
))

# Start narrowband early, before full dark
plan.add(ImagingSession(
    "M42",
    Ha=(300, 15),
    start_offset=-30,   # 30 min before astro dark
    end_offset=-15,     # stop 15 min before dawn
    min_altitude=20,
))

plan.save("tonight.json")
```

Each `ImagingSession` takes a target name (resolved via Simbad) or WCS
coordinates, plus filter sequences as `(exposure_seconds, count)` tuples.

**JSON equivalent:**

```json
{
  "latitude": 30.27,
  "longitude": -97.74,
  "elevation": 200,
  "sessions": [
    {
      "target": "M31",
      "sequences": {"L": [300, 20], "R": [300, 10], "G": [300, 10], "B": [300, 10]},
      "gain": 56, "offset": 20, "mode": 5,
      "min_altitude": 25, "dither_every": 3,
      "max_moon_altitude": 0, "max_moon_phase": 25
    }
  ]
}
```

### 3. Run hardware tests (daytime)

Before your first session, verify all connections:

```bash
python test_hardware.py
```

This checks INDI server, mount, focuser, filter wheel, camera, cooler, PHD2,
and optionally roof status and the web monitor. No sky needed.

### 4. Run on-sky tests (first clear night)

Walk through each subsystem interactively with human confirmation:

```bash
python test_on_sky.py --target Vega
```

Tests plate-solve alignment, autofocus, guided capture, dithering, guide
star loss recovery, and meridian flip handling.

### 5. Run a full session

```python
# run_tonight.py
from sky_scripter.sequence import NightPlan

plan = NightPlan.load("tonight.json")
# ... create orchestrator with all managers and watchdogs ...
# orchestrator.run()
```

The orchestrator will:
1. Compute astronomical dark times for your location
2. Precompute a schedule showing when each target is eligible
3. Start cooling the CCD
4. Start watchdog threads (guide, roof, safety)
5. Loop: pick the best eligible target, slew, align, focus, guide, capture
6. Handle meridian flips, refocusing, dithering, and filter changes
7. Respond to alerts (guide star loss, drift, roof closure)
8. Warm up the CCD and park the mount at dawn

## Session Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target` | -- | Object name (Simbad lookup) |
| `wcs` | -- | WCS coordinates, e.g. `"5:35:17 -5:23:24"` |
| `L`, `R`, `G`, `B`, `Ha`, `OIII`, `SII` | -- | `(exposure_sec, count)` per filter |
| `gain` | 56 | Camera gain |
| `offset` | 20 | Camera offset |
| `mode` | 5 | Camera readout mode |
| `min_altitude` | 0 | Minimum target altitude (degrees) |
| `dither_every` | 1 | Dither every N frames |
| `max_moon_altitude` | None | Skip if moon is above this altitude |
| `max_moon_phase` | None | Skip if moon illumination exceeds this % |
| `start_offset` | 0 | Minutes relative to astro dark start |
| `end_offset` | 0 | Minutes relative to astro dark end |

## Scheduling

The `NightScheduler` evaluates all constraints at 5-minute intervals across the
night to build an eligibility timeline per target. When the orchestrator needs a
new target, `pick_next()` selects the eligible session whose window ends
soonest (the one that will set first). This ensures targets with short windows
are prioritized.

Constraints checked:
- Target altitude >= `min_altitude`
- Moon altitude <= `max_moon_altitude` (if set)
- Moon illumination <= `max_moon_phase` (if set)
- Time within `[dark_start + start_offset, dark_end + end_offset]`

The precomputed schedule is logged at startup and displayed in the web
dashboard.

## Safety and Alerts

Background watchdog threads run continuously and raise alerts through the
`AlertBus`:

| Watchdog | Monitors | Alert Codes |
|----------|----------|-------------|
| GuideWatchdog | PHD2 events via TCP | `GUIDE_STAR_LOST`, `GUIDE_DRIFT_EXCEEDED`, `GUIDE_DISCONNECTED` |
| RoofWatchdog | Status file | `ROOF_CLOSING` (emergency), `ROOF_STATUS_UNKNOWN` |
| SafetyWatchdog | Disk, cooler | `DISK_SPACE_LOW`, `DISK_SPACE_CRITICAL`, `COOLER_FAILING` |

Alert levels:
- **EMERGENCY** (e.g., roof closing): immediately kills exposure, parks mount
- **CRITICAL** (e.g., guide star lost): triggers recovery sequence
- **WARNING** (e.g., low disk): logged, continues imaging

The `CaptureManager` waits on the emergency event during exposures, so even a
5-minute exposure can be aborted within 0.5 seconds of a roof closure.

## Web Dashboard

The web monitor runs on two ports (configurable in `sky_scripter.json`):
- **HTTP** (default 8080): serves the dashboard at `http://host:8080`
- **WebSocket** (default 8765): real-time status updates

The dashboard shows:
- **Night plan bar**: each session as a colored block (green = active,
  yellow = upcoming, gray = completed)
- **Main process tile**: state, target, filter, frame count, mount
  coordinates, focus position, FWHM
- **Guide watchdog tile**: RMS RA/Dec/Total, star SNR, RMS history chart
- **Roof watchdog tile**: OPEN/CLOSED status
- **Safety watchdog tile**: disk free, sensor temperature
- **Log stream**: color-coded scrolling log

The dashboard uses a dark red-on-black color scheme for night vision.

## Focus Calibration

The `focus_calibrator` offline tool analyzes autofocus logs to build
per-filter temperature models:

```bash
python -m analysis.focus_calibrator \
    --focus-log .focus/focus_log.csv \
    --output focus_calibration.json \
    --plot-dir .focus/calibration
```

This fits a linear `focus_position = slope * temperature + intercept` model per
filter using robust sigma-clipped regression, and computes inter-filter offsets.
The `FocusManager` loads `focus_calibration.json` at startup to predict initial
focus positions and apply filter offsets without a full autofocus run.

## Global Configuration

`sky_scripter.json` stores site-specific and hardware defaults. The `Config` class
deep-merges user overrides onto built-in defaults, so you only need to specify
values that differ from the defaults.

| Section | Key Settings |
|---------|--------------|
| `site` | `latitude`, `longitude`, `elevation` |
| `devices` | `mount`, `camera`, `focuser` |
| `phd2` | `host`, `port` |
| `capture` | `gain`, `offset`, `mode`, `capture_dir` |
| `focus` | `step`, `num_steps`, `interval_minutes`, `temp_threshold` |
| `cooler` | `target_temp`, `warmup_rate` |
| `guiding` | `rms_threshold`, `drift_timeout`, `dither_pixels` |
| `safety` | `disk_warning_gb`, `disk_critical_gb`, `min_altitude` |
| `roof` | `status_file`, `poll_interval` |
| `web` | `ws_port`, `http_port` |

## Dependencies

- Python 3.10+
- INDI server with `indi_getprop` / `indi_setprop`
- PHD2 with server mode enabled (port 4400)
- ASTAP (plate solving)
- Siril (star detection for autofocus)
- Python packages: `astropy`, `astroplan`, `numpy`, `matplotlib`, `websockets`

## Tests

```bash
python -m pytest tests/ -v
```

Runs unit tests for AlertBus, Sequence DSL, NightScheduler, and Config.
No hardware or sky needed.
