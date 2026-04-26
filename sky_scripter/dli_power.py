"""Digital Loggers (DLI) network power switch — REST relay outlet API over HTTP digest."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class DliOutlet:
    """One relay outlet on a DLI power switch."""

    outlet_id: int
    label: str


class DliPowerSwitch:
    """HTTP digest client for ``/restapi/relay/outlets/{id}/state/``."""

    def __init__(self, host: str, user: str, password: str):
        self.host = host.strip()
        self.user = user.strip()
        self.password = password

    def _base_url(self) -> str:
        return f"http://{self.host}/"

    def _outlet_url(self, outlet_id: int) -> str:
        return f"http://{self.host}/restapi/relay/outlets/{outlet_id}/state/"

    def request(self, outlet_id: int, method: str = "GET", on: bool | None = None) -> tuple[int, str]:
        url = self._outlet_url(outlet_id)
        body = None
        if on is not None:
            body = f"value={'true' if on else 'false'}".encode("ascii")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self._base_url(), self.user, self.password)
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

    def get_outlet_state(self, outlet_id: int) -> tuple[bool | None, str]:
        code, body = self.request(outlet_id)
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

    def set_outlet_state(self, outlet_id: int, on: bool) -> tuple[bool, str]:
        code, raw = self.request(outlet_id, "PUT", on)
        ok = code in (200, 201, 204)
        return ok, f"http {code} {raw[:120]}"
