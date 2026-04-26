"""Host metrics (disk, load, memory, uptime) for dashboards and watchdogs."""

from __future__ import annotations

import os
import shutil
from datetime import datetime


def host_status(capture_dir: str) -> dict:
    """Return a JSON-serializable snapshot for a path used as capture/storage root."""
    out: dict = {
        "disk_path": capture_dir,
        "disk_free_gb": None,
        "disk_total_gb": None,
        "disk_pct_used": None,
    }
    try:
        u = shutil.disk_usage(capture_dir)
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
                    mem_total = int(line.split()[1])
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
