#!/usr/bin/env python3
"""Observatory Panel — status + control HTTP server.

Run (from repo root; bind to your WireGuard IPv4 on the astropc):
  OBSERVATORY_BIND_HOST=10.x.x.x DLI_PASSWORD=... python3 -m sky_scripter.observatory_panel.server

Env: OBSERVATORY_HTTP_PORT, DLI_HOST, DLI_USER, DLI_PASSWORD, DLI_OUTLETS, DLI_LABELS,
CAPTURE_DIR, INDI_DRIVERS, SKY_SCRIPTER_CONFIG. Discord reads .discord_token /
.discord_channel_id in repo root.
INDI device roles (mount/camera/focuser) use ``devices.*`` in sky_scripter.json: each may be a
string or a list of aliases; see observatory_panel/README.md.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from sky_scripter.config import Config
from sky_scripter.dli_power import DliPowerSwitch
from sky_scripter import indi_service
from sky_scripter.system_status import host_status as build_host_status

logger = logging.getLogger(__name__)

_PACKAGE_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CONFIG_PATH = os.path.abspath(
    os.path.expanduser(os.environ.get("SKY_SCRIPTER_CONFIG", os.path.join(_PACKAGE_REPO_ROOT, "sky_scripter.json")))
)
_REPO_ROOT = os.path.dirname(_CONFIG_PATH) if os.path.isfile(_CONFIG_PATH) else _PACKAGE_REPO_ROOT
_cfg = Config(_CONFIG_PATH if os.path.isfile(_CONFIG_PATH) else "/nonexistent/sky_scripter.json")

_BIND_HOST = os.environ.get("OBSERVATORY_BIND_HOST", "").strip()
_HTTP_PORT = int(
    os.environ.get(
        "OBSERVATORY_HTTP_PORT",
        str(_cfg.get("observatory_panel", "http_port", default=8080) or 8080),
    )
)

_dli_env_outlets = os.environ.get("DLI_OUTLETS")
if _dli_env_outlets is not None:
    _DLI_OUTLETS = [
        int(x.strip())
        for x in _dli_env_outlets.split(",")
        if x.strip().isdigit()
    ]
else:
    _raw_outlets = _cfg.get("dli", "outlets", default=[0, 1, 2])
    _DLI_OUTLETS = [int(x) for x in _raw_outlets] if isinstance(_raw_outlets, list) else [0, 1, 2]

_dli_env_labels = os.environ.get("DLI_LABELS")
if _dli_env_labels is not None:
    _label_parts = [x.strip() for x in _dli_env_labels.split(",")]
else:
    _label_parts = list(_cfg.get("dli", "labels", default=["Dew Heater", "Camera", "Mount"]))
_DLI_LABELS = [
    _label_parts[i] if i < len(_label_parts) and _label_parts[i] else f"Outlet {_DLI_OUTLETS[i]}"
    for i in range(len(_DLI_OUTLETS))
]

_CAPTURE_DIR = os.path.expanduser(
    os.environ.get(
        "CAPTURE_DIR",
        str(_cfg.get("capture", "capture_dir", default="~/Pictures") or "~/Pictures"),
    )
).strip()

_INDI_DRIVERS = os.environ.get(
    "INDI_DRIVERS",
    str(
        _cfg.get(
            "observatory_panel",
            "indi_drivers",
            default="indi_lx200am5 indi_asi_focuser indi_qhy_ccd indi_playerone_ccd",
        )
        or ""
    ),
).strip()

_DLI = DliPowerSwitch(
    host=os.environ.get("DLI_HOST", str(_cfg.get("dli", "host", default="192.168.0.100") or "192.168.0.100")),
    user=os.environ.get("DLI_USER", str(_cfg.get("dli", "user", default="admin") or "admin")),
    password=os.environ.get("DLI_PASSWORD", str(_cfg.get("dli", "password", default="") or "")),
)

_MOUNT_ALIASES = indi_service.normalize_device_aliases(_cfg["devices"]["mount"])
_CAMERA_ALIASES = indi_service.normalize_device_aliases(_cfg["devices"]["camera"])
_FOCUSER_ALIASES = indi_service.normalize_device_aliases(_cfg["devices"]["focuser"])

_cmd_lock = threading.Lock()
_last_cmd: dict = {"time": None, "what": "", "ok": None, "detail": ""}


def _read_discord_creds() -> tuple[str | None, str | None]:
    tok = cid = None
    p = os.path.join(_REPO_ROOT, ".discord_token")
    try:
        tok = open(p, encoding="utf-8").read().strip() or None
    except OSError:
        tok = None
    p = os.path.join(_REPO_ROOT, ".discord_channel_id")
    try:
        cid = open(p, encoding="utf-8").read().strip() or None
    except OSError:
        cid = None
    return tok, cid


def discord_message_text(msg: dict) -> str:
    """Flatten Discord API message JSON (content + embeds) to searchable text."""
    parts: list[str] = []
    if msg.get("content"):
        parts.append(str(msg["content"]))
    for e in msg.get("embeds") or []:
        if not isinstance(e, dict):
            continue
        for key in ("title", "description"):
            if e.get(key):
                parts.append(str(e[key]))
        for f in e.get("fields") or []:
            if isinstance(f, dict):
                if f.get("name"):
                    parts.append(str(f["name"]))
                if f.get("value"):
                    parts.append(str(f["value"]))
        foot = e.get("footer") or {}
        if isinstance(foot, dict) and foot.get("text"):
            parts.append(str(foot["text"]))
        auth = e.get("author") or {}
        if isinstance(auth, dict) and auth.get("name"):
            parts.append(str(auth["name"]))
    return "\n".join(parts).strip()


def infer_roof_state(text: str) -> str:
    t = text.lower()
    if "roof" not in t:
        return "UNKNOWN"
    if "opening" in t:
        return "OPEN"
    if "closing" in t:
        return "CLOSED"
    return "UNKNOWN"


def is_roof_status_text(text: str) -> bool:
    t = text.lower()
    if "roof" in t and ("opening" in t or "closing" in t):
        return True
    return False


def pick_roof_from_messages(messages: list[dict]) -> tuple[dict | None, str | None]:
    """messages: newest first from Discord API."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        txt = discord_message_text(msg)
        if not txt or not is_roof_status_text(txt):
            continue
        return msg, txt
    return None, None


def fetch_discord_messages(token: str, channel_id: str, limit: int) -> tuple[list[dict] | None, str]:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "sky-scripter-observatory-panel (urllib)")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            return data, ""
        return None, "unexpected discord json"
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"discord http {e.code}: {err}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return None, str(e)


def hms(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        hours = float(value)
    except ValueError:
        return value
    h = int(hours)
    minutes_float = (hours - h) * 60
    m = int(minutes_float)
    s = (minutes_float - m) * 60
    return f"{h:02d}h {m:02d}m {s:04.1f}s"


def dms(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        deg = float(value)
    except ValueError:
        return value
    sign = "-" if deg < 0 else "+"
    deg = abs(deg)
    d = int(deg)
    minutes_float = (deg - d) * 60
    m = int(minutes_float)
    s = (minutes_float - m) * 60
    return f"{sign}{d:02d}° {m:02d}' {s:04.1f}\""


def mount_camera_status(
    *,
    snapshot_err: str | None = None,
    snapshot_raw: str = "",
    resolved: dict[str, str | None] | None = None,
) -> dict:
    """Equipment status using resolved INDI device names.

    If ``snapshot_err`` is set (``indi_getprop`` failed), return an error payload.
    Otherwise pass ``snapshot_raw`` and optional precomputed ``resolved`` from the same
    snapshot to avoid a second ``indi_getprop`` round-trip.
    """
    out: dict = {"mount": {}, "camera": {}, "error": None}

    if snapshot_err:
        out["error"] = snapshot_err
        return out

    raw = snapshot_raw
    if resolved is None:
        if not raw.strip():
            raw, ierr = indi_service.indi_getprop_snapshot()
            if ierr:
                out["error"] = ierr
                return out
        resolved = (
            indi_service.resolve_all_roles(raw, _MOUNT_ALIASES, _CAMERA_ALIASES, _FOCUSER_ALIASES)
            if raw.strip()
            else {
                "mount": None,
                "camera": None,
                "focuser": None,
            }
        )

    mount_dev = resolved.get("mount")
    camera_dev = resolved.get("camera")
    focuser_dev = resolved.get("focuser")

    missing: list[str] = []
    if not mount_dev:
        missing.append("mount")
    if not camera_dev:
        missing.append("camera")
    if missing:
        out["error"] = (
            "Could not resolve INDI device(s) for role(s): "
            + ", ".join(missing)
            + ". Add matching names under devices.* in sky_scripter.json (string or list of aliases)."
        )
        return out

    props: dict[str, str] = {
        "park": f"{mount_dev}.TELESCOPE_PARK.PARK",
        "ra": f"{mount_dev}.EQUATORIAL_EOD_COORD.RA",
        "dec": f"{mount_dev}.EQUATORIAL_EOD_COORD.DEC",
        "temp": f"{camera_dev}.CCD_TEMPERATURE.CCD_TEMPERATURE_VALUE",
        "cooler_on": f"{camera_dev}.CCD_COOLER.COOLER_ON",
        "cooler_power": f"{camera_dev}.CCD_COOLER_POWER.CCD_COOLER_VALUE",
    }
    if focuser_dev:
        props["focuser_temp"] = f"{focuser_dev}.FOCUS_TEMPERATURE.TEMPERATURE"

    with ThreadPoolExecutor(max_workers=len(props)) as pool:
        values = dict(zip(props, pool.map(indi_service.indi_get, props.values())))

    park, err = values["park"]
    if err:
        out["error"] = err
    out["mount"]["parked"] = "PARKED" if park == "On" else ("UNPARKED" if park == "Off" else "UNKNOWN")

    ra, err = values["ra"]
    dec, err2 = values["dec"]
    out["mount"]["ra"] = hms(ra)
    out["mount"]["dec"] = dms(dec)
    if (err or err2) and not out["error"]:
        out["error"] = err or err2

    temp, err = values["temp"]
    cooler_on, err2 = values["cooler_on"]
    cooler_power, err3 = values["cooler_power"]
    out["camera"]["temperature"] = temp
    out["camera"]["cooler"] = "ON" if cooler_on == "On" else ("OFF" if cooler_on == "Off" else "UNKNOWN")
    out["camera"]["cooler_power"] = cooler_power

    if "focuser_temp" in values:
        focuser_temp, err4 = values["focuser_temp"]
        out["camera"]["focuser_temperature"] = focuser_temp
        if err4 and not out["error"]:
            out["error"] = err4
    else:
        out["camera"]["focuser_temperature"] = None

    if (err or err2 or err3) and not out["error"]:
        out["error"] = err or err2 or err3

    return out


def _resolve_indi_for_action() -> tuple[str | None, str | None, str | None, str | None]:
    """Return (mount, camera, focuser, error). Error set if snapshot fails or a required role is missing."""
    raw, err = indi_service.indi_getprop_snapshot()
    if err:
        return None, None, None, err
    if not raw.strip():
        return None, None, None, "empty indi_getprop snapshot"
    r = indi_service.resolve_all_roles(raw, _MOUNT_ALIASES, _CAMERA_ALIASES, _FOCUSER_ALIASES)
    return r["mount"], r["camera"], r["focuser"], None


def build_status() -> dict:
    power_out = []
    perr = None
    if not _DLI.password:
        perr = "DLI_PASSWORD not set"
    else:
        for i, oid in enumerate(_DLI_OUTLETS):
            label = _DLI_LABELS[i] if i < len(_DLI_LABELS) else f"Outlet {oid}"
            st, e = _DLI.get_outlet_state(oid)
            power_out.append(
                {"id": oid, "label": label, "on": st, "reachable": st is not None, "detail": e or None}
            )
            if st is None and e and not perr:
                perr = e[:200]

    roof_block: dict = {
        "state": "UNKNOWN",
        "message": None,
        "timestamp": None,
        "note": None,
    }
    tok, cid = _read_discord_creds()
    if not tok or not cid:
        roof_block["note"] = "Discord token or channel id missing"
    else:
        msgs, err = fetch_discord_messages(tok, cid, 3)
        if err:
            roof_block["note"] = err
        elif not msgs:
            roof_block["note"] = "No messages returned"
        else:
            msg, txt = pick_roof_from_messages(msgs)
            if not msg:
                roof_block["note"] = "No roof status found in latest 3 messages"
            else:
                roof_block["message"] = txt[:500] if txt else None
                roof_block["timestamp"] = msg.get("timestamp")
                roof_block["state"] = infer_roof_state(txt or "")

    indi_block: dict = {
        "server_running": indi_service.indiserver_running(),
        "devices": [],
        "connections": {},
        "raw_error": None,
        "resolved_devices": {"mount": None, "camera": None, "focuser": None},
        "device_resolution_note": None,
    }
    raw, ierr = indi_service.indi_getprop_snapshot()
    resolved_devices: dict[str, str | None] = {"mount": None, "camera": None, "focuser": None}
    if ierr:
        indi_block["raw_error"] = ierr
    elif raw.strip():
        devs, conns = indi_service.parse_indi_getprop(raw)
        indi_block["devices"] = devs
        indi_block["connections"] = conns
        resolved_devices = indi_service.resolve_all_roles(
            raw, _MOUNT_ALIASES, _CAMERA_ALIASES, _FOCUSER_ALIASES
        )
        indi_block["resolved_devices"] = resolved_devices
        unresolved = [k for k, v in resolved_devices.items() if not v]
        if unresolved:
            indi_block["device_resolution_note"] = (
                "No INDI device matched configured alias(es) for: "
                + ", ".join(unresolved)
                + ". Add names under devices.* in sky_scripter.json (string or list)."
            )

    return {
        "power": {"outlets": power_out, "error": perr},
        "roof": roof_block,
        "indi": indi_block,
        "mount_camera": mount_camera_status(
            snapshot_err=ierr or None,
            snapshot_raw=raw if not ierr else "",
            resolved=resolved_devices if not ierr else None,
        ),
        "host": build_host_status(_CAPTURE_DIR),
        "last_command": dict(_last_cmd),
    }


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict | None:
    n = int(handler.headers.get("Content-Length", "0") or "0")
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        if self.path == "/api/status":
            try:
                data = json.dumps(build_status(), default=str).encode("utf-8")
                self._send(HTTPStatus.OK, data)
            except Exception as e:
                logger.exception("GET /api/status failed: %s", e)
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, json.dumps({"error": str(e)}).encode())
            return
        if self.path in ("/", "/index.html"):
            static = os.path.join(os.path.dirname(__file__), "static", "index.html")
            try:
                html = open(static, "rb").read()
                self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
            except OSError:
                self._send(HTTPStatus.NOT_FOUND, b"missing static/index.html")
            return
        self._send(HTTPStatus.NOT_FOUND, b"{\"error\":\"not found\"}")

    def do_POST(self) -> None:
        global _last_cmd
        body = _read_json_body(self)
        if body is None:
            self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"invalid json\"}")
            return
        client = self.client_address[0] if self.client_address else "?"

        if self.path == "/api/power":
            with _cmd_lock:
                if not _DLI.password:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"{\"error\":\"DLI_PASSWORD not set\"}")
                    return
                on = body.get("on")
                if not isinstance(on, bool):
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"on must be boolean\"}")
                    return
                if body.get("all") is True:
                    ok_all = True
                    details = []
                    for oid in _DLI_OUTLETS:
                        oks, msg = _DLI.set_outlet_state(oid, on)
                        details.append(f"{oid}:{oks}")
                        ok_all = ok_all and oks
                    _last_cmd = {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "what": f"power all on={on}",
                        "ok": ok_all,
                        "detail": ";".join(details),
                    }
                    if ok_all:
                        logger.info("POST /api/power all on=%s client=%s ok", on, client)
                    else:
                        logger.warning("POST /api/power all on=%s client=%s failed %s", on, client, details)
                    self._send(
                        HTTPStatus.OK if ok_all else HTTPStatus.BAD_GATEWAY,
                        json.dumps({"ok": ok_all, "detail": details}).encode(),
                    )
                    return
                if "outlet" not in body:
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"need outlet or all\"}")
                    return
                try:
                    oid = int(body["outlet"])
                except (TypeError, ValueError):
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"bad outlet\"}")
                    return
                if oid not in _DLI_OUTLETS:
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"outlet not allowed\"}")
                    return
                oks, msg = _DLI.set_outlet_state(oid, on)
                _last_cmd = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "what": f"power outlet {oid} on={on}",
                    "ok": oks,
                    "detail": msg,
                }
                if oks:
                    logger.info("POST /api/power outlet=%s on=%s client=%s ok", oid, on, client)
                else:
                    logger.warning("POST /api/power outlet=%s on=%s client=%s failed %s", oid, on, client, msg)
                self._send(
                    HTTPStatus.OK if oks else HTTPStatus.BAD_GATEWAY,
                    json.dumps({"ok": oks, "detail": msg}).encode(),
                )
            return

        if self.path == "/api/indi":
            act = body.get("action")
            if act not in ("start", "stop", "connect", "park", "unpark", "set_temp", "cooler_off"):
                self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"bad action\"}")
                return
            with _cmd_lock:
                if act == "start":
                    oks, msg = indi_service.start_indiserver(_INDI_DRIVERS)
                    what = "indi start"
                elif act == "stop":
                    oks, msg = indi_service.stop_indiserver()
                    what = "indi stop"
                elif act == "connect":
                    device = str(body.get("device") or "").strip()
                    if not device:
                        self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"device required\"}")
                        return
                    oks, msg = indi_service.connect_indi_device(device)
                    what = f"indi connect {device}"
                elif act == "park":
                    mdev, _, _, rerr = _resolve_indi_for_action()
                    if rerr or not mdev:
                        oks, msg = False, rerr or "could not resolve mount device"
                    else:
                        oks, msg = indi_service.indi_set(f"{mdev}.TELESCOPE_PARK.PARK=On")
                    what = "mount park"
                elif act == "unpark":
                    mdev, _, _, rerr = _resolve_indi_for_action()
                    if rerr or not mdev:
                        oks, msg = False, rerr or "could not resolve mount device"
                    else:
                        oks, msg = indi_service.indi_set(f"{mdev}.TELESCOPE_PARK.UNPARK=On")
                    what = "mount unpark"
                elif act == "set_temp":
                    try:
                        temp = float(body["temperature"])
                    except (KeyError, TypeError, ValueError):
                        self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"temperature required\"}")
                        return
                    if temp not in (-10.0, 0.0, 5.0):
                        self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"temperature must be -10, 0, or 5\"}")
                        return
                    _, cdev, _, rerr = _resolve_indi_for_action()
                    if rerr or not cdev:
                        oks, msg = False, rerr or "could not resolve camera device"
                    else:
                        oks, msg = indi_service.indi_set(
                            f"{cdev}.CCD_TEMPERATURE.CCD_TEMPERATURE_VALUE={temp:g}"
                        )
                    what = f"camera set temp {temp:g}"
                else:
                    _, cdev, _, rerr = _resolve_indi_for_action()
                    if rerr or not cdev:
                        oks, msg = False, rerr or "could not resolve camera device"
                    else:
                        oks, msg = indi_service.indi_set(f"{cdev}.CCD_COOLER.COOLER_OFF=On")
                    what = "camera cooler off"
                _last_cmd = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "what": what,
                    "ok": oks,
                    "detail": msg,
                }
                if oks:
                    logger.info("POST /api/indi %s client=%s ok", what, client)
                else:
                    logger.warning("POST /api/indi %s client=%s failed %s", what, client, msg)
                self._send(
                    HTTPStatus.OK if oks else HTTPStatus.BAD_GATEWAY,
                    json.dumps({"ok": oks, "detail": msg}).encode(),
                )
            return

        self._send(HTTPStatus.NOT_FOUND, b"{\"error\":\"not found\"}")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main() -> None:
    os.makedirs(os.path.join(_REPO_ROOT, ".logs"), exist_ok=True)
    _log_file = os.path.join(
        _REPO_ROOT,
        ".logs",
        f"observatory_panel-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log",
    )
    _fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=_fmt,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(_log_file, encoding="utf-8"),
        ],
        force=True,
    )
    if not _BIND_HOST:
        logger.info(
            "Set OBSERVATORY_BIND_HOST to the IPv4 address to bind (e.g. your WireGuard address)."
        )
        raise SystemExit(1)
    logger.info("Using config file %s", _CONFIG_PATH if os.path.isfile(_CONFIG_PATH) else "defaults only")
    host = _BIND_HOST
    try:
        httpd = ThreadingHTTPServer((host, _HTTP_PORT), _Handler)
    except OSError as e:
        logger.error("Failed to bind %s:%s: %s", host, _HTTP_PORT, e)
        raise SystemExit(1)
    logger.info("Observatory panel http://%s:%s/ log file %s", host, _HTTP_PORT, _log_file)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
