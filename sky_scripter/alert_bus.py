import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)

ALERT_LEVEL_TO_LOG = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "critical": logging.ERROR,
    "emergency": logging.ERROR,
}


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass
class Alert:
    level: AlertLevel
    source: str
    code: str
    message: str
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)


class AlertBus:
    """Thread-safe alert queue. Watchdogs raise alerts, main thread consumes them."""

    def __init__(self, history_size: int = 200):
        self._lock = threading.Lock()
        self._pending: list[Alert] = []
        self._history: collections.deque[Alert] = collections.deque(maxlen=history_size)
        self._emergency = threading.Event()
        self._callbacks: list[Callable[[Alert], None]] = []

    def raise_alert(self, alert: Alert) -> None:
        with self._lock:
            self._pending.append(alert)
            self._history.append(alert)
        if alert.level == AlertLevel.EMERGENCY:
            self._emergency.set()
        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception:
                logger.exception("Alert callback failed")
        logger.log(
            ALERT_LEVEL_TO_LOG[alert.level.value],
            "[%s] %s: %s – %s",
            alert.level.value.upper(),
            alert.source,
            alert.code,
            alert.message,
        )

    def get_pending(self) -> list[Alert]:
        with self._lock:
            alerts = self._pending
            self._pending = []
        return alerts

    def get_history(self, count: int = 50) -> list[Alert]:
        with self._lock:
            items = list(self._history)
        return items[-count:]

    def clear_emergency(self) -> None:
        self._emergency.clear()

    def on_alert(self, callback: Callable[[Alert], None]) -> None:
        self._callbacks.append(callback)

    @property
    def emergency_event(self) -> threading.Event:
        return self._emergency
