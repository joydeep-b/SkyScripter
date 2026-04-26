"""Non-fatal INDI CLI helpers (indi_getprop / indi_setprop, indiserver lifecycle).

Used by observatory panel and other tools that must not exit the process on
command failure (unlike ``lib_indi.IndiClient`` which uses ``exec_or_fail``).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Literal

RoleName = Literal["mount", "camera", "focuser"]


def run_cmd(cmd: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except OSError as e:
        return -1, "", str(e)


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


def normalize_device_aliases(value: object) -> list[str]:
    """Normalize ``devices.*`` config: a string or a list of strings -> non-empty aliases in order."""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    return []


def devices_having_property(raw: str, property_name: str) -> list[str]:
    """Return sorted unique device names that expose ``property_name`` (INDI vector name before element)."""
    found: set[str] = set()
    for line in raw.splitlines():
        if "=" not in line:
            continue
        left = line.split("=", 1)[0]
        parts = left.split(".")
        if len(parts) < 3:
            continue
        if parts[-2] == property_name:
            dev = ".".join(parts[:-2])
            if dev.strip():
                found.add(dev)
    return sorted(found)


def _pick_from_candidates(candidates: list[str], aliases: list[str]) -> str | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    cand_set = set(candidates)
    for a in aliases:
        if a in cand_set:
            return a
    return candidates[0]


def resolve_role_device(raw: str, aliases: list[str], role: RoleName) -> str | None:
    """Pick a concrete INDI device name for ``role`` from ``indi_getprop`` snapshot text.

    Order: first configured alias present in the snapshot device list, then capability-based
    inference (mount: TELESCOPE_PARK / EQUATORIAL_EOD_COORD, camera: CCD_TEMPERATURE,
    focuser: FOCUS_TEMPERATURE).
    """
    devices, _ = parse_indi_getprop(raw)
    ds = set(devices)
    for a in aliases:
        if a in ds:
            return a
    if role == "mount":
        for prop in ("TELESCOPE_PARK", "EQUATORIAL_EOD_COORD"):
            picked = _pick_from_candidates(devices_having_property(raw, prop), aliases)
            if picked:
                return picked
        return None
    if role == "camera":
        return _pick_from_candidates(devices_having_property(raw, "CCD_TEMPERATURE"), aliases)
    if role == "focuser":
        return _pick_from_candidates(devices_having_property(raw, "FOCUS_TEMPERATURE"), aliases)
    return None


def resolve_all_roles(
    raw: str,
    mount_aliases: list[str],
    camera_aliases: list[str],
    focuser_aliases: list[str],
) -> dict[str, str | None]:
    return {
        "mount": resolve_role_device(raw, mount_aliases, "mount"),
        "camera": resolve_role_device(raw, camera_aliases, "camera"),
        "focuser": resolve_role_device(raw, focuser_aliases, "focuser"),
    }


def indiserver_running() -> bool:
    code, out, _ = run_cmd(["pgrep", "-x", "indiserver"], timeout=3)
    return code == 0 and bool(out.strip())


def start_indiserver(drivers: str) -> tuple[bool, str]:
    if indiserver_running():
        return True, "already running"
    screen = shutil.which("screen")
    if not screen:
        return False, "screen not found on PATH"
    driver_list = drivers.split()
    if not driver_list:
        return False, "INDI drivers list empty"
    cmd = [screen, "-mdS", "indi", "indiserver", *driver_list]
    code, out, err = run_cmd(cmd, timeout=5)
    if code != 0:
        return False, (err or out or f"exit {code}")[:500]
    time.sleep(1)
    return indiserver_running(), "started" if indiserver_running() else "start failed"


def stop_indiserver() -> tuple[bool, str]:
    killall = shutil.which("killall")
    if not killall:
        return False, "killall not found on PATH"
    code, _, err = run_cmd([killall, "indiserver"], timeout=10)
    if code not in (0, 1):
        return False, (err or f"exit {code}")[:300]
    return not indiserver_running(), "stopped" if not indiserver_running() else "still running?"


def indi_getprop_snapshot(timeout_s: float = 2.0) -> tuple[str, str]:
    t = max(1, int(timeout_s))
    code, out, err = run_cmd(["indi_getprop", "-t", str(t)], timeout=6)
    if code != 0:
        return "", (err or out or f"indi_getprop exit {code}")[:500]
    return out, ""


def indi_get(prop: str, timeout_s: float = 2.0) -> tuple[str | None, str | None]:
    t = max(1, int(timeout_s))
    code, out, err = run_cmd(["indi_getprop", "-t", str(t), prop], timeout=4)
    if code != 0:
        return None, (err or out or f"indi_getprop exit {code}")[:300]
    if "=" not in out:
        return None, "missing value"
    return out.strip().split("=", 1)[1], None


def indi_set(prop: str) -> tuple[bool, str]:
    code, out, err = run_cmd(["indi_setprop", prop], timeout=10)
    if code != 0:
        return False, (err or out or f"indi_setprop exit {code}")[:500]
    return True, "ok"


def connect_indi_device(device: str) -> tuple[bool, str]:
    raw, err = indi_getprop_snapshot()
    if err:
        return False, err
    devices, connections = parse_indi_getprop(raw)
    if device not in devices:
        return False, f"device not found: {device}"
    if connections.get(device) == "CONNECTED":
        return True, "already connected"
    code, out, err = run_cmd(["indi_setprop", f"{device}.CONNECTION.CONNECT=On"], timeout=10)
    if code != 0:
        return False, (err or out or f"indi_setprop exit {code}")[:500]
    return True, "connect requested"
