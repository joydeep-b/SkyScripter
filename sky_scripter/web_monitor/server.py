import asyncio
import json
import logging
import os
import threading
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, HTTPServer

import websockets

logger = logging.getLogger(__name__)


def _make_handler(static_dir: str, ws_port: int):
    """Build an HTTP handler that serves static files and a /config endpoint."""

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=static_dir, **kwargs)

        def do_GET(self):
            if self.path == "/config":
                body = json.dumps({"ws_port": ws_port}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, fmt, *args):
            pass

    return _Handler


class MonitorServer:
    def __init__(self, orchestrator, guide_wd, roof_wd, safety_wd,
                 alert_bus, struct_logger, port=8765, http_port=8080):
        self.orchestrator = orchestrator
        self.guide_wd = guide_wd
        self.roof_wd = roof_wd
        self.safety_wd = safety_wd
        self.alert_bus = alert_bus
        self.struct_logger = struct_logger
        self.port = port
        self.http_port = http_port
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self):
        self.alert_bus.on_alert(self._on_alert)
        self.struct_logger.on_entry(self._on_log)

        ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        ws_thread.start()

        static_dir = os.path.join(os.path.dirname(__file__), "static")
        os.makedirs(static_dir, exist_ok=True)
        ws_port = self.port
        handler = _make_handler(static_dir, ws_port)
        http = HTTPServer(("0.0.0.0", self.http_port), handler)
        http_thread = threading.Thread(target=http.serve_forever, daemon=True)
        http_thread.start()
        logger.info("Monitor: WS on :%d, HTTP on :%d", self.port, self.http_port)

    def _run_ws(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve_ws())

    async def _serve_ws(self):
        async with websockets.serve(self._ws_handler, "0.0.0.0", self.port):
            await asyncio.Future()

    async def _ws_handler(self, websocket, path=None):
        self._clients.add(websocket)
        try:
            await websocket.send(json.dumps(self._build_status()))
            async for raw in websocket:
                self._handle_command(raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    def _handle_command(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        action = msg.get("action")
        if action == "abort":
            logger.warning("Monitor: abort requested by client")
            self.orchestrator._terminate = True
        else:
            logger.info("Monitor: received action=%s (not yet implemented)", action)

    def _on_alert(self, alert):
        self._broadcast({
            "type": "alert",
            "level": alert.level.value,
            "source": alert.source,
            "code": alert.code,
            "message": alert.message,
            "timestamp": alert.timestamp,
        })

    def _on_log(self, entry: dict):
        self._broadcast({"type": "log", **entry})

    def _broadcast(self, message_dict: dict):
        if not self._loop or not self._clients:
            return
        data = json.dumps(message_dict, default=str)
        asyncio.run_coroutine_threadsafe(self._send_all(data), self._loop)

    async def _send_all(self, data: str):
        dead = set()
        for ws in self._clients:
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                dead.add(ws)
        self._clients -= dead

    def _build_status(self) -> dict:
        alerts = self.alert_bus.get_history(20)
        serialized_alerts = [{
            "level": a.level.value, "source": a.source,
            "code": a.code, "message": a.message, "timestamp": a.timestamp,
        } for a in alerts]
        schedule_data = None
        orch = self.orchestrator
        if orch._schedule is not None:
            schedule_data = {
                "timeline": orch._schedule.get_timeline(),
                "active_index": orch._active_session_idx,
                "completed": list(orch._completed),
            }
        return {
            "type": "status",
            "main": {
                "state": orch.state.value,
                "session_id": orch.session_id,
                "focus_position": orch.focus_position,
                "focus_fwhm": orch.focus_fwhm,
            },
            "guide": self.guide_wd.status,
            "roof": self.roof_wd.status,
            "safety": self.safety_wd.status,
            "schedule": schedule_data,
            "recent_logs": self.struct_logger.get_recent(20),
            "recent_alerts": serialized_alerts,
        }
