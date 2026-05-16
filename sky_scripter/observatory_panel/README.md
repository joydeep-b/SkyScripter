# Observatory Panel

Small HTTP server plus static UI for DLI power switch outlet control, roof status (Discord), INDI controls, host metrics, and mount/camera status.

## Run from the repo root

```bash
cd /path/to/sky_scripter
```

Bind to the IPv4 address you want the server to listen on (typically your WireGuard address on the astropc). The process exits if that address is not usable on the machine.

```bash
export OBSERVATORY_BIND_HOST=10.x.x.x
export DLI_PASSWORD='your-dli-password'
python3 -m sky_scripter.observatory_panel.server
```

For a quick local check on the same machine, you can use `OBSERVATORY_BIND_HOST=127.0.0.1` if that interface is available.

From another host on the VPN, open `http://<your-WG-IPv4>:8080/` (or the host and port you configured).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OBSERVATORY_BIND_HOST` | IPv4 to bind (required). |
| `OBSERVATORY_HTTP_PORT` | HTTP port (default from `observatory_panel.http_port`, fallback `8080`). |
| `SKY_SCRIPTER_CONFIG` | Absolute path to `sky_scripter.json` (default inferred from package location). |
| `DLI_HOST` | DLI power switch hostname or IP (default from config, fallback `192.168.0.100`). |
| `DLI_USER` | DLI digest user (default from config, fallback `admin`). |
| `DLI_PASSWORD` | DLI digest password (required for real outlet status/control). |
| `DLI_OUTLETS` | Comma-separated outlet indices (default from config, fallback `0,1,2`). |
| `DLI_LABELS` | Comma-separated labels aligned with outlets. |
| `CAPTURE_DIR` | Path used for disk free space (default from `capture.capture_dir`). |
| `INDI_DRIVERS` | Space-separated driver names passed to `indiserver` (default from `observatory_panel.indi_drivers`). |
| `FITS_PREVIEW_ENABLED` | Enable the FITS preview worker (`true` by default; set `false` to disable). |
| `FITS_PREVIEW_CAPTURE_DIR` | Directory watched for new `.fit` / `.fits` files (default: `CAPTURE_DIR`). |
| `FITS_PREVIEW_MASTER_DARK_DIR` | Master dark directory (default: `~/masters/dark`). |
| `FITS_PREVIEW_MASTER_FLAT_DIR` | Master flat directory (default: `~/masters/flat`). |
| `FITS_PREVIEW_CACHE_DIR` | JPEG preview cache directory (default: `.cache/observatory_panel/fits_previews` under the repo root). |

INDI mount/camera/focuser **roles** are configured under `devices` in repo-root `sky_scripter.json`. Each role may be a single string (one device name) or a **list of aliases** in priority order. The panel resolves the live name from the current `indi_getprop` snapshot: it picks the first alias that exists, or falls back to capability detection (e.g. `TELESCOPE_PARK` for mount, `CCD_TEMPERATURE` for camera, `FOCUS_TEMPERATURE` for focuser). When KStars/Ekos uses different names than your scripts, add both names to the list.

**Collecting names:** start INDI each way (script vs Ekos), then run `indi_getprop -t 2` and note the device prefixes in the output (or save the full snapshot to a file). Add those strings as aliases under `devices.mount`, `devices.camera`, and `devices.focuser`.

Discord credentials are read from repo-root `.discord_token` and `.discord_channel_id`.

## FITS preview pane

The panel runs a background FITS preview worker inside the same `observatory-panel.service` process. It performs one startup scan to find the newest existing capture, then uses Linux file events from `inotifywait` to queue new files without repeatedly walking large capture trees. A slow reconciliation scan catches missed events and new nested target/filter directories.

Install the Linux watcher dependency:

```bash
sudo apt install inotify-tools
```

Preview generation uses Siril only. If `siril-cli` or `siril` is not on `PATH`, the FITS Preview card reports an error instead of using a fallback renderer.

The preview card reads capture metadata from the FITS header: `DATE-OBS` for capture time, `FILTER` for filter, and `EXPTIME` for exposure seconds.

Calibration is best-effort:

- Darks are matched from `FITS_PREVIEW_MASTER_DARK_DIR` by rounded `EXPTIME`, nearest available master temperature among `-10` and `0` using `CCD-TEMP`, and matching `READMODE`, `GAIN`, and `OFFSET` when those headers are present. The expected filename convention is `master_dark_MODE<readmode>_GAIN<gain>_OFFSET<offset>_EXPTIME<exptime>_TEMP<temp>.fit`.
- Flats are matched from `FITS_PREVIEW_MASTER_FLAT_DIR` by `FILTER` using `master_flat_<FILTER>.fit` or `.fits`.
- If no matching masters are found, the worker skips calibration and still asks Siril to generate a stretched JPEG from the raw light.

## API (same origin as the UI)

- `GET /api/status` — JSON snapshot.
- `POST /api/power` — JSON `{ "outlet": 0, "on": true }` or `{ "all": true, "on": false }`.
- `POST /api/indi` — JSON `{ "action": "start" }`, `{ "action": "stop" }`, `{ "action": "connect", "device": "ZWO AM5" }`, `{ "action": "park" }`, `{ "action": "unpark" }`, `{ "action": "set_temp", "temperature": -10 }`, or `{ "action": "cooler_off" }`.

## Run as a systemd service

An example unit lives at [`setup/observatory-panel.service.example`](../../setup/observatory-panel.service.example). All configuration is set inline with `Environment=` lines so the unit file is the single source of truth for the running panel.

systemd does not run your shell profile, so it will not activate conda for you. Point `ExecStart` at the **same** `python3` you get after `conda activate base` (for example `/home/joydeepb/miniconda3/bin/python3`), and set `Environment=PATH=...` with that env’s `bin` directory first so dependencies and subprocesses match an interactive session.

Install it (edit before enabling):

```bash
sudo cp setup/observatory-panel.service.example \
    /etc/systemd/system/observatory-panel.service
sudoedit /etc/systemd/system/observatory-panel.service
sudo systemctl daemon-reload
sudo systemctl enable --now observatory-panel.service
```

At minimum, edit:

- `User=` / `Group=` — account that owns the repo and runs the panel.
- `WorkingDirectory=` — absolute path to the `sky_scripter` repo root.
- `ExecStart=` — absolute path to conda base’s `python3` (run `conda activate base` then `which python3` and paste the path).
- `Environment=PATH=...` — example file prepends `/home/joydeepb/miniconda3/bin`; change both this and `ExecStart` if your install is Anaconda, a different user, or a non-base environment.
- `Environment=SKY_SCRIPTER_CONFIG=...` — absolute path to repo-root `sky_scripter.json`; this keeps service startup from depending on Python's import location.
- `Environment=OBSERVATORY_BIND_HOST=...` — IPv4 to bind, often the WireGuard address on the astropc.
- `Environment=DLI_PASSWORD=...` — DLI digest password, replacing the `CHANGE_ME` placeholder.

Optional environment variables (commented out in the example) override values from `sky_scripter.json`:

- `OBSERVATORY_HTTP_PORT`
- `SKY_SCRIPTER_CONFIG`
- `DLI_HOST`, `DLI_USER`, `DLI_OUTLETS`, `DLI_LABELS`
- `CAPTURE_DIR`
- `INDI_DRIVERS`
- `FITS_PREVIEW_ENABLED`
- `FITS_PREVIEW_CAPTURE_DIR`
- `FITS_PREVIEW_MASTER_DARK_DIR`
- `FITS_PREVIEW_MASTER_FLAT_DIR`
- `FITS_PREVIEW_CACHE_DIR`

If the bind address only appears once a VPN is up, add an ordering dependency on the relevant unit, for example:

```ini
After=network-online.target wg-quick@wg0.service
Wants=network-online.target wg-quick@wg0.service
```

Operate the service with the usual systemd commands:

```bash
sudo systemctl status observatory-panel.service
sudo systemctl restart observatory-panel.service
sudo systemctl disable --now observatory-panel.service
journalctl -u observatory-panel.service -f
```

## Logging

Logs are written to `.logs/observatory_panel-YYYY-MM-DD.log`. When run under systemd, stdout and stderr are also captured by the journal (see `journalctl -u observatory-panel.service`).
