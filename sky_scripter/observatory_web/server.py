#!/usr/bin/env python3
"""Minimal observatory status + control HTTP server.

Run (from repo root; bind to your WireGuard IPv4 on the astropc):
  OBSERVATORY_BIND_HOST=10.x.x.x PDU_PASSWORD=... python3 -m sky_scripter.observatory_web.server

Env: OBSERVATORY_HTTP_PORT, PDU_HOST, PDU_USER, PDU_PASSWORD, PDU_OUTLETS, PDU_LABELS,
CAPTURE_DIR, INDI_DRIVERS. Discord reads .discord_token / .discord_channel_id in repo root.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logger = logging.getLogger(__name__)

# --- config (env) ---
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BIND_HOST = os.environ.get("OBSERVATORY_BIND_HOST", "").strip()
_HTTP_PORT = int(os.environ.get("OBSERVATORY_HTTP_PORT", "8080"))

_PDU_HOST = os.environ.get("PDU_HOST", "192.168.0.100").strip()
_PDU_USER = os.environ.get("PDU_USER", "admin").strip()
_PDU_PASSWORD = os.environ.get("PDU_PASSWORD", "").strip()
_PDU_OUTLETS = [
    int(x.strip())
    for x in os.environ.get("PDU_OUTLETS", "0,1,2").split(",")
    if x.strip().isdigit()
]
_labels = os.environ.get("PDU_LABELS", "Dew Heater,Camera,Mount").split(",")
_PDU_LABELS = [_labels[i].strip() if i < len(_labels) else f"Outlet {i}" for i in range(len(_PDU_OUTLETS))]

_CAPTURE_DIR = os.path.expanduser(os.environ.get("CAPTURE_DIR", "~/Pictures")).strip()

_INDI_DRIVERS = os.environ.get(
    "INDI_DRIVERS",
    "indi_lx200am5 indi_asi_focuser indi_qhy_ccd indi_playerone_ccd",
).strip()
with open(os.path.join(_REPO_ROOT, "sky_scripter.json"), encoding="utf-8") as f:
    _CONFIG = json.load(f)
_MOUNT_DEVICE = _CONFIG["devices"]["mount"]
_CAMERA_DEVICE = _CONFIG["devices"]["camera"]
_FOCUSER_DEVICE = _CONFIG["devices"]["focuser"]


_cmd_lock = threading.Lock()
_last_cmd: dict = {"time": None, "what": "", "ok": None, "detail": ""}


def _read_discord_creds() -> tuple[str | None, str | None]:
    p = os.path.join(_REPO_ROOT, ".discord_token")
    tok = open(p, encoding="utf-8").read().strip() or None
    p = os.path.join(_REPO_ROOT, ".discord_channel_id")
    cid = open(p, encoding="utf-8").read().strip() or None
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


def parse_indi_getprop(stdout: str) -> tuple[list[str], dict[str, str]]:
    devices: set[str] = set()
    connect_on: dict[str, bool | None] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        left, val = line.split("=", 1)
        val = val.strip()
        parts = left.split(".")
        if len(parts) < 3:
            continue
        dev = ".".join(parts[:-2])
        if not dev.strip():
            continue
        elem = parts[-1]
        prop = parts[-2]
        devices.add(dev)
        if prop == "CONNECTION" and elem == "CONNECT":
            connect_on[dev] = val.lower() == "on"
    conns: dict[str, str] = {}
    for d in sorted(devices):
        co = connect_on.get(d)
        if co is True:
            conns[d] = "CONNECTED"
        elif co is False:
            conns[d] = "DISCONNECTED"
        else:
            conns[d] = "UNKNOWN"
    return sorted(devices), conns


def pdu_request(outlet: int, method: str = "GET", on: bool | None = None) -> tuple[int, str]:
    url = f"http://{_PDU_HOST}/restapi/relay/outlets/{outlet}/state/"
    body = None
    if on is not None:
        body = f"value={'true' if on else 'false'}".encode("ascii")
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, f"http://{_PDU_HOST}/", _PDU_USER, _PDU_PASSWORD)
    auth = urllib.request.HTTPDigestAuthHandler(mgr)
    opener = urllib.request.build_opener(auth)
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("X-CSRF", "x")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(req, timeout=8.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.getcode() or 200, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, raw
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def pdu_get_state(outlet: int) -> tuple[bool | None, str]:
    code, body = pdu_request(outlet)
    if code == 0:
        return None, body
    try:
        data = json.loads(body) if body.strip().startswith("{") else {}
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict) and "value" in data:
        v = data["value"]
        if isinstance(v, bool):
            return v, ""
        if str(v).lower() in ("true", "1", "on"):
            return True, ""
        if str(v).lower() in ("false", "0", "off"):
            return False, ""
    if "true" in body.lower():
        return True, ""
    if "false" in body.lower():
        return False, ""
    return None, f"http {code}: {body[:200]}"


def pdu_set_state(outlet: int, on: bool) -> tuple[bool, str]:
    code, raw = pdu_request(outlet, "PUT", on)
    ok = code in (200, 201, 204)
    return ok, f"http {code} {raw[:120]}"


def fetch_discord_messages(token: str, channel_id: str, limit: int) -> tuple[list[dict] | None, str]:
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bot {token}")
    req.add_header("User-Agent", "sky-scripter-observatory-web (urllib)")
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


def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -1, "", str(e)


def indiserver_running() -> bool:
    code, out, _ = _run(["pgrep", "-x", "indiserver"], timeout=3)
    return code == 0 and bool(out.strip())


def start_indiserver() -> tuple[bool, str]:
    if indiserver_running():
        return True, "already running"
    screen = shutil.which("screen")
    if not screen:
        return False, "screen not found on PATH"
    drivers = _INDI_DRIVERS.split()
    if not drivers:
        return False, "INDI_DRIVERS empty"
    cmd = [screen, "-mdS", "indi", "indiserver", *drivers]
    code, out, err = _run(cmd, timeout=5)
    if code != 0:
        return False, (err or out or f"exit {code}")[:500]
    time.sleep(1)
    return indiserver_running(), "started" if indiserver_running() else "start failed"


def stop_indiserver() -> tuple[bool, str]:
    killall = shutil.which("killall")
    if not killall:
        return False, "killall not found on PATH"
    code, _, err = _run([killall, "indiserver"], timeout=10)
    if code not in (0, 1):
        return False, (err or f"exit {code}")[:300]
    return not indiserver_running(), "stopped" if not indiserver_running() else "still running?"


def connect_indi_device(device: str) -> tuple[bool, str]:
    raw, err = indi_getprop_snapshot()
    if err:
        return False, err
    devices, connections = parse_indi_getprop(raw)
    if device not in devices:
        return False, f"device not found: {device}"
    if connections.get(device) == "CONNECTED":
        return True, "already connected"
    code, out, err = _run(["indi_setprop", f"{device}.CONNECTION.CONNECT=On"], timeout=10)
    if code != 0:
        return False, (err or out or f"indi_setprop exit {code}")[:500]
    return True, "connect requested"


def indi_getprop_snapshot() -> tuple[str, str]:
    code, out, err = _run(["indi_getprop", "-t", "2"], timeout=6)
    if code != 0:
        return "", (err or out or f"indi_getprop exit {code}")[:500]
    return out, ""


def indi_get(prop: str) -> tuple[str | None, str | None]:
    code, out, err = _run(["indi_getprop", "-t", "2", prop], timeout=4)
    if code != 0:
        return None, (err or out or f"indi_getprop exit {code}")[:300]
    if "=" not in out:
        return None, "missing value"
    return out.strip().split("=", 1)[1], None


def indi_set(prop: str) -> tuple[bool, str]:
    code, out, err = _run(["indi_setprop", prop], timeout=10)
    if code != 0:
        return False, (err or out or f"indi_setprop exit {code}")[:500]
    return True, "ok"


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


def mount_camera_status() -> dict:
    out: dict = {"mount": {}, "camera": {}, "error": None}

    park, err = indi_get(f"{_MOUNT_DEVICE}.TELESCOPE_PARK.PARK")
    if err:
        out["error"] = err
    out["mount"]["parked"] = "PARKED" if park == "On" else ("UNPARKED" if park == "Off" else "UNKNOWN")

    ra, err = indi_get(f"{_MOUNT_DEVICE}.EQUATORIAL_EOD_COORD.RA")
    dec, err2 = indi_get(f"{_MOUNT_DEVICE}.EQUATORIAL_EOD_COORD.DEC")
    out["mount"]["ra"] = hms(ra)
    out["mount"]["dec"] = dms(dec)
    if (err or err2) and not out["error"]:
        out["error"] = err or err2

    temp, err = indi_get(f"{_CAMERA_DEVICE}.CCD_TEMPERATURE.CCD_TEMPERATURE_VALUE")
    cooler_on, err2 = indi_get(f"{_CAMERA_DEVICE}.CCD_COOLER.COOLER_ON")
    cooler_power, err3 = indi_get(f"{_CAMERA_DEVICE}.CCD_COOLER_POWER.CCD_COOLER_VALUE")
    focuser_temp, err4 = indi_get(f"{_FOCUSER_DEVICE}.FOCUS_TEMPERATURE.TEMPERATURE")
    out["camera"]["temperature"] = temp
    out["camera"]["cooler"] = "ON" if cooler_on == "On" else ("OFF" if cooler_on == "Off" else "UNKNOWN")
    out["camera"]["cooler_power"] = cooler_power
    out["camera"]["focuser_temperature"] = focuser_temp
    if (err or err2 or err3 or err4) and not out["error"]:
        out["error"] = err or err2 or err3 or err4

    return out


def host_status() -> dict:
    out: dict = {"disk_path": _CAPTURE_DIR, "disk_free_gb": None, "disk_total_gb": None, "disk_pct_used": None}
    try:
        u = shutil.disk_usage(_CAPTURE_DIR)
        out["disk_free_gb"] = round(u.free / (1024**3), 2)
        out["disk_total_gb"] = round(u.total / (1024**3), 2)
        if u.total:
            out["disk_pct_used"] = round(100.0 * (1.0 - u.free / u.total), 1)
    except OSError as e:
        out["disk_error"] = str(e)
    try:
        la = os.getloadavg()
        out["loadavg"] = [round(x, 2) for x in la]
    except OSError:
        out["loadavg"] = None
    mem_total = mem_avail = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])  # kB
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1])
        if mem_total:
            out["mem_total_mb"] = round(mem_total / 1024, 0)
            if mem_avail is not None:
                out["mem_avail_mb"] = round(mem_avail / 1024, 0)
                out["mem_used_mb"] = round((mem_total - mem_avail) / 1024, 0)
    except OSError:
        pass
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            sec = float(f.read().split()[0])
        out["uptime_s"] = int(sec)
    except (OSError, ValueError, IndexError):
        out["uptime_s"] = None
    out["local_time"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return out


def build_status() -> dict:
    power_out = []
    perr = None
    if not _PDU_PASSWORD:
        perr = "PDU_PASSWORD not set"
    else:
        for i, oid in enumerate(_PDU_OUTLETS):
            label = _PDU_LABELS[i] if i < len(_PDU_LABELS) else f"Outlet {oid}"
            st, e = pdu_get_state(oid)
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
        "server_running": indiserver_running(),
        "devices": [],
        "connections": {},
        "raw_error": None,
    }
    raw, ierr = indi_getprop_snapshot()
    if ierr:
        indi_block["raw_error"] = ierr
    elif raw.strip():
        devs, conns = parse_indi_getprop(raw)
        indi_block["devices"] = devs
        indi_block["connections"] = conns

    return {
        "power": {"outlets": power_out, "error": perr},
        "roof": roof_block,
        "indi": indi_block,
        "mount_camera": mount_camera_status(),
        "host": host_status(),
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
                if not _PDU_PASSWORD:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"{\"error\":\"PDU_PASSWORD not set\"}")
                    return
                on = body.get("on")
                if not isinstance(on, bool):
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"on must be boolean\"}")
                    return
                if body.get("all") is True:
                    ok_all = True
                    details = []
                    for oid in _PDU_OUTLETS:
                        oks, msg = pdu_set_state(oid, on)
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
                if oid not in _PDU_OUTLETS:
                    self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"outlet not allowed\"}")
                    return
                oks, msg = pdu_set_state(oid, on)
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
                    oks, msg = start_indiserver()
                    what = "indi start"
                elif act == "stop":
                    oks, msg = stop_indiserver()
                    what = "indi stop"
                elif act == "connect":
                    device = str(body.get("device") or "").strip()
                    if not device:
                        self._send(HTTPStatus.BAD_REQUEST, b"{\"error\":\"device required\"}")
                        return
                    oks, msg = connect_indi_device(device)
                    what = f"indi connect {device}"
                elif act == "park":
                    oks, msg = indi_set(f"{_MOUNT_DEVICE}.TELESCOPE_PARK.PARK=On")
                    what = "mount park"
                elif act == "unpark":
                    oks, msg = indi_set(f"{_MOUNT_DEVICE}.TELESCOPE_PARK.UNPARK=On")
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
                    oks, msg = indi_set(f"{_CAMERA_DEVICE}.CCD_TEMPERATURE.CCD_TEMPERATURE_VALUE={temp:g}")
                    what = f"camera set temp {temp:g}"
                else:
                    oks, msg = indi_set(f"{_CAMERA_DEVICE}.CCD_COOLER.COOLER_OFF=On")
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
        f"observatory_web-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log",
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
    host = _BIND_HOST
    try:
        httpd = ThreadingHTTPServer((host, _HTTP_PORT), _Handler)
    except OSError as e:
        logger.error("Failed to bind %s:%s: %s", host, _HTTP_PORT, e)
        raise SystemExit(1)
    logger.info("Observatory web http://%s:%s/ log file %s", host, _HTTP_PORT, _log_file)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
