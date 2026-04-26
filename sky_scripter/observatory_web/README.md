# Observatory web monitor

Small HTTP server plus static UI for PDU power, roof status (Discord), INDI controls, host metrics, and mount/camera status.

## Run from the repo root

```bash
cd /path/to/sky_scripter
```

Bind to the IPv4 address you want the server to listen on (typically your WireGuard address on the astropc). The process exits if that address is not usable on the machine.

```bash
export OBSERVATORY_BIND_HOST=10.x.x.x
export PDU_PASSWORD='your-pdu-password'
python3 -m sky_scripter.observatory_web.server
```

For a quick local check on the same machine, you can use `OBSERVATORY_BIND_HOST=127.0.0.1` if that interface is available.

From another host on the VPN, open `http://<your-WG-IPv4>:8080/` (or the host and port you configured).

## Environment variables

| Variable | Purpose |
|----------|---------|
| `OBSERVATORY_BIND_HOST` | IPv4 to bind (required). |
| `OBSERVATORY_HTTP_PORT` | HTTP port (default `8080`). |
| `PDU_HOST` | PDU hostname or IP (default `192.168.0.100`). |
| `PDU_USER` | Digest user (default `admin`). |
| `PDU_PASSWORD` | Digest password (required for real outlet status/control). |
| `PDU_OUTLETS` | Comma-separated outlet indices (default `0,1,2`). |
| `PDU_LABELS` | Comma-separated labels aligned with outlets. |
| `CAPTURE_DIR` | Path used for disk free space (default `~/Pictures`). |
| `INDI_DRIVERS` | Space-separated driver names passed to `indiserver` (defaults match `startup.sh`). |
INDI mount/camera/focuser names are read from repo-root `sky_scripter.json`. Discord credentials are read from repo-root `.discord_token` and `.discord_channel_id`.

## API (same origin as the UI)

- `GET /api/status` — JSON snapshot.
- `POST /api/power` — JSON `{ "outlet": 0, "on": true }` or `{ "all": true, "on": false }`.
- `POST /api/indi` — JSON `{ "action": "start" }`, `{ "action": "stop" }`, `{ "action": "connect", "device": "ZWO AM5" }`, `{ "action": "park" }`, `{ "action": "unpark" }`, `{ "action": "set_temp", "temperature": -10 }`, or `{ "action": "cooler_off" }`.

## Logging

Logs are written to `.logs/observatory_web-YYYY-MM-DD.log`.

