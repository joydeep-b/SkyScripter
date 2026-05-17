import json
import os
import threading
import time
import urllib.error
import urllib.request

from sky_scripter.alert_bus import Alert, AlertBus, AlertLevel
from sky_scripter.structured_log import StructuredLogger


def read_discord_creds(repo_root: str) -> tuple[str | None, str | None]:
  token = channel_id = None
  try:
    with open(os.path.join(repo_root, ".discord_token"), encoding="utf-8") as f:
      token = f.read().strip() or None
  except OSError:
    token = None
  try:
    with open(os.path.join(repo_root, ".discord_channel_id"), encoding="utf-8") as f:
      channel_id = f.read().strip() or None
  except OSError:
    channel_id = None
  return token, channel_id


def discord_message_text(msg: dict) -> str:
  parts = []
  if msg.get("content"):
    parts.append(str(msg["content"]))
  for embed in msg.get("embeds") or []:
    if not isinstance(embed, dict):
      continue
    for key in ("title", "description"):
      if embed.get(key):
        parts.append(str(embed[key]))
    for field in embed.get("fields") or []:
      if isinstance(field, dict):
        if field.get("name"):
          parts.append(str(field["name"]))
        if field.get("value"):
          parts.append(str(field["value"]))
    footer = embed.get("footer") or {}
    if isinstance(footer, dict) and footer.get("text"):
      parts.append(str(footer["text"]))
    author = embed.get("author") or {}
    if isinstance(author, dict) and author.get("name"):
      parts.append(str(author["name"]))
  return "\n".join(parts).strip()


def infer_roof_state(text: str) -> str:
  lowered = text.lower()
  if "roof" not in lowered:
    return "UNKNOWN"
  if "opening" in lowered or "open" in lowered:
    return "OPEN"
  if "closing" in lowered or "closed" in lowered:
    return "CLOSED"
  return "UNKNOWN"


def is_roof_status_text(text: str) -> bool:
  lowered = text.lower()
  return "roof" in lowered and any(
    word in lowered for word in ("opening", "open", "closing", "closed")
  )


def pick_roof_from_messages(messages: list[dict]) -> tuple[dict | None, str | None]:
  for msg in messages:
    if not isinstance(msg, dict):
      continue
    text = discord_message_text(msg)
    if text and is_roof_status_text(text):
      return msg, text
  return None, None


def fetch_discord_messages(token: str, channel_id: str, limit: int = 5):
  url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
  req = urllib.request.Request(url, method="GET")
  req.add_header("Authorization", f"Bot {token}")
  req.add_header("User-Agent", "sky-scripter-sequencer (urllib)")
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:
      data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, list):
      return data, ""
    return None, "unexpected discord json"
  except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")[:300]
    return None, f"discord http {exc.code}: {body}"
  except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
    return None, str(exc)


class DiscordRoofWatchdog(threading.Thread):
  def __init__(self, alert_bus: AlertBus, logger: StructuredLogger,
               repo_root: str, poll_interval: float = 10.0,
               unknown_timeout: float = 60.0, message_limit: int = 5):
    super().__init__(daemon=True)
    self._alert_bus = alert_bus
    self._logger = logger
    self._repo_root = repo_root
    self._poll_interval = poll_interval
    self._unknown_timeout = unknown_timeout
    self._message_limit = message_limit
    self._lock = threading.Lock()
    self._state = "UNKNOWN"
    self._message = None
    self._timestamp = None
    self._note = None
    self._last_check_time = None
    self._last_known_time = None
    self._warned_unknown = False

  def run(self):
    while True:
      self.check_once()
      time.sleep(self._poll_interval)

  def check_once(self) -> str:
    token, channel_id = read_discord_creds(self._repo_root)
    now = time.time()
    note = None
    state = "UNKNOWN"
    text = None
    timestamp = None

    if not token or not channel_id:
      note = "missing .discord_token or .discord_channel_id"
    else:
      messages, error = fetch_discord_messages(token, channel_id, self._message_limit)
      if error:
        note = error
      else:
        msg, text = pick_roof_from_messages(messages or [])
        if msg is None:
          note = f"no roof message in last {self._message_limit} Discord messages"
        else:
          state = infer_roof_state(text)
          timestamp = msg.get("timestamp")

    self._update_state(state, text, timestamp, note, now)
    return state

  def _update_state(self, state, message, timestamp, note, now):
    with self._lock:
      prev = self._state
      self._state = state
      self._message = message
      self._timestamp = timestamp
      self._note = note
      self._last_check_time = now
      if state in ("OPEN", "CLOSED"):
        self._last_known_time = now
        self._warned_unknown = False

    if prev != state:
      self._logger.log("roof_watchdog", "roof_status",
                       state=state, message=message, note=note)

    if prev == "OPEN" and state == "CLOSED":
      self._alert_bus.raise_alert(Alert(
        level=AlertLevel.EMERGENCY,
        source="roof",
        code="ROOF_CLOSING",
        message="Discord roof message indicates roof is closing or closed",
        data={"message": message, "timestamp": timestamp},
      ))

    unknown_for = now - (self._last_known_time or now)
    if state == "UNKNOWN" and unknown_for >= self._unknown_timeout and not self._warned_unknown:
      self._warned_unknown = True
      self._alert_bus.raise_alert(Alert(
        level=AlertLevel.WARNING,
        source="roof",
        code="ROOF_STATUS_UNKNOWN",
        message=f"Discord roof status unknown for >{self._unknown_timeout}s",
        data={"note": note},
      ))

  @property
  def status(self) -> dict:
    with self._lock:
      return {
        "state": self._state,
        "roof_is_open": self._state == "OPEN",
        "message": self._message,
        "timestamp": self._timestamp,
        "note": self._note,
        "last_check": self._last_check_time,
        "last_check_time": self._last_check_time,
      }
