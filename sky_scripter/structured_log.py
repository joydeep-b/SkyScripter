import collections
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable


class StructuredLogger:
  """JSON structured logger with in-memory ring buffer."""

  def __init__(self, name: str, log_dir: str = None, buffer_size: int = 500):
    if log_dir is None:
      log_dir = os.path.join(os.path.dirname(__file__), '..', '.logs')
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{name}-{time.strftime('%Y-%m-%d')}.jsonl")
    self._file = open(path, 'a')
    self._buffer = collections.deque(maxlen=buffer_size)
    self._callbacks: list[Callable[[dict], None]] = []
    self._lock = threading.Lock()

  def log(self, subsystem: str, event: str, **data):
    entry = {
      "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
      "subsystem": subsystem,
      "event": event,
      **data,
    }
    line = json.dumps(entry, default=str)
    with self._lock:
      self._file.write(line + '\n')
      self._file.flush()
      self._buffer.append(entry)
      callbacks = list(self._callbacks)
    for cb in callbacks:
      try:
        cb(entry)
      except Exception:
        logging.exception("Structured log callback failed")
    logging.info(f"[{subsystem}] {event}: {json.dumps(data, default=str)}")

  def get_recent(self, count: int = 100) -> list[dict]:
    with self._lock:
      items = list(self._buffer)
    return items[-count:]

  def on_entry(self, callback: Callable[[dict], None]):
    with self._lock:
      self._callbacks.append(callback)
