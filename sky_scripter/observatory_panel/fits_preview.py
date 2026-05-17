"""Latest FITS preview generation for the observatory panel."""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astropy.io import fits

from sky_scripter.util import get_siril_path

logger = logging.getLogger(__name__)

_FITS_SUFFIXES = {".fit", ".fits"}
_DARK_RE = re.compile(
    r"master_dark_MODE(?P<readmode>-?\d+)_GAIN(?P<gain>-?\d+)_"
    r"OFFSET(?P<offset>-?\d+)_EXPTIME(?P<exptime>-?\d+)_TEMP(?P<temp>-?\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FitsPreviewConfig:
    enabled: bool
    capture_dir: Path
    dark_dir: Path
    flat_dir: Path
    cache_dir: Path
    stable_seconds: float = 2.0
    stable_timeout_seconds: float = 90.0
    reconcile_interval_seconds: float = 180.0
    siril_timeout_seconds: float = 180.0
    max_previews: int = 5


class FitsPreviewService:
    """Background worker that turns the newest FITS file into a Siril JPEG preview."""

    def __init__(self, config: FitsPreviewConfig):
        self.config = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._queue: queue.Queue[Path] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._watcher_thread: threading.Thread | None = None
        self._watcher_proc: subprocess.Popen[str] | None = None
        self._processed_keys: set[str] = set()
        self._status: dict[str, Any] = {
            "enabled": config.enabled,
            "state": "disabled" if not config.enabled else "stopped",
            "capture_dir": str(config.capture_dir),
            "watcher": None,
            "source_file": None,
            "source_mtime": None,
            "source_size": None,
            "preview_id": None,
            "preview_url": None,
            "generated_at": None,
            "capture_time": None,
            "filter": None,
            "exposure_seconds": None,
            "calibration": {"dark": None, "flat": None, "applied": False},
            "error": None,
            "note": None,
        }

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="fits-preview", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._watcher_proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._watcher_thread:
            self._watcher_thread.join(timeout=3)
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def preview_path(self, preview_id: str) -> Path | None:
        if not re.fullmatch(r"[a-f0-9]{16}\.jpg", preview_id):
            return None
        path = (self.config.cache_dir / preview_id).resolve()
        if path.parent != self.config.cache_dir.resolve():
            return None
        return path if path.is_file() else None

    def _run(self) -> None:
        self._set_status(state="starting", error=None, note=None)
        if not self.config.capture_dir.is_dir():
            self._set_status(state="error", error=f"capture dir not found: {self.config.capture_dir}")
            return
        siril_path = get_siril_path()
        if not siril_path:
            self._set_status(state="error", error="Siril executable not found on PATH")
            return

        newest = self._find_newest_fits()
        if newest:
            self._enqueue(newest)
        self._start_inotify_watcher()
        self._set_status(state="watching", error=None)
        next_reconcile = time.monotonic() + self.config.reconcile_interval_seconds

        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() >= next_reconcile:
                    newest = self._find_newest_fits()
                    if newest:
                        self._enqueue(newest)
                    next_reconcile = time.monotonic() + self.config.reconcile_interval_seconds
                continue
            self._process(path, siril_path)

    def _start_inotify_watcher(self) -> None:
        inotifywait = shutil.which("inotifywait")
        if not inotifywait:
            self._set_status(watcher="reconciliation", note="inotifywait not found; using slow scans")
            return
        self._watcher_thread = threading.Thread(
            target=self._watch_inotify,
            args=(inotifywait,),
            name="fits-preview-inotify",
            daemon=True,
        )
        self._watcher_thread.start()
        self._set_status(watcher="inotifywait")

    def _watch_inotify(self, inotifywait: str) -> None:
        cmd = [
            inotifywait,
            "--monitor",
            "--recursive",
            "--event",
            "close_write,moved_to,create",
            "--format",
            "%w%f",
            str(self.config.capture_dir),
        ]
        try:
            self._watcher_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
            )
        except OSError as e:
            self._set_status(watcher="reconciliation", note=f"could not start inotifywait: {e}")
            return
        assert self._watcher_proc.stdout is not None
        for line in self._watcher_proc.stdout:
            if self._stop.is_set():
                break
            path = Path(line.strip())
            if _is_fits_path(path):
                self._enqueue(path)

    def _enqueue(self, path: Path) -> None:
        if _is_fits_path(path):
            self._queue.put(path)

    def _find_newest_fits(self) -> Path | None:
        newest: Path | None = None
        newest_mtime = -1.0
        try:
            for root, _dirs, files in os.walk(self.config.capture_dir):
                for filename in files:
                    path = Path(root) / filename
                    if not _is_fits_path(path):
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if mtime > newest_mtime:
                        newest = path
                        newest_mtime = mtime
        except OSError as e:
            self._set_status(state="error", error=f"could not scan capture dir: {e}")
        return newest

    def _process(self, path: Path, siril_path: str) -> None:
        try:
            path = path.resolve()
            stat = self._wait_for_stable_file(path)
            cache_key = _cache_key(path, stat.st_size, stat.st_mtime_ns)
            if cache_key in self._processed_keys:
                return
            preview_id = f"{cache_key}.jpg"
            preview_path = self.config.cache_dir / preview_id
            self._set_status(
                state="processing",
                source_file=str(path),
                source_mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                source_size=stat.st_size,
                error=None,
            )

            header = _read_fits_header(path)
            metadata = _extract_metadata(header)
            dark = self._match_dark(header)
            flat = self._match_flat(header)
            self._render_preview(siril_path, path, preview_path, dark, flat)
            self._processed_keys.add(cache_key)
            self._cleanup_cache()
            self._set_status(
                state="ready",
                preview_id=preview_id,
                preview_url=f"/api/fits-preview/{preview_id}?v={cache_key}",
                generated_at=datetime.now(timezone.utc).isoformat(),
                capture_time=metadata["capture_time"],
                filter=metadata["filter"],
                exposure_seconds=metadata["exposure_seconds"],
                calibration={
                    "dark": str(dark) if dark else None,
                    "flat": str(flat) if flat else None,
                    "applied": bool(dark or flat),
                },
                error=None,
            )
            logger.info("Generated FITS preview %s from %s", preview_path, path)
        except Exception as e:
            logger.exception("FITS preview failed for %s: %s", path, e)
            self._set_status(state="error", error=str(e))

    def _wait_for_stable_file(self, path: Path) -> os.stat_result:
        deadline = time.monotonic() + self.config.stable_timeout_seconds
        last_sig: tuple[int, int] | None = None
        stable_since: float | None = None
        last_stat: os.stat_result | None = None
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                st = path.stat()
                sig = (st.st_size, st.st_mtime_ns)
            except OSError:
                time.sleep(0.5)
                continue
            if sig != last_sig:
                last_sig = sig
                stable_since = time.monotonic()
                last_stat = st
                time.sleep(0.5)
                continue
            if stable_since and time.monotonic() - stable_since >= self.config.stable_seconds:
                _read_fits_header(path)
                return st
            last_stat = st
            time.sleep(0.5)
        if last_stat is None:
            raise RuntimeError(f"FITS file did not appear: {path}")
        raise RuntimeError(f"FITS file did not become stable: {path}")

    def _match_dark(self, header: fits.Header) -> Path | None:
        if not self.config.dark_dir.is_dir():
            return None
        exptime = _header_int(header, "EXPTIME")
        ccd_temp = _header_float(header, "CCD-TEMP")
        if exptime is None or ccd_temp is None:
            return None
        wanted = {
            "readmode": _header_int(header, "READMODE"),
            "gain": _header_int(header, "GAIN"),
            "offset": _header_int(header, "OFFSET"),
            "exptime": exptime,
        }
        candidates: list[tuple[float, Path]] = []
        for path in self.config.dark_dir.iterdir():
            if not path.is_file() or not _is_fits_path(path):
                continue
            match = _DARK_RE.search(path.stem)
            if not match:
                continue
            values = {key: int(value) for key, value in match.groupdict().items()}
            if values["temp"] not in (-10, 0) or values["exptime"] != wanted["exptime"]:
                continue
            if any(wanted[k] is not None and values[k] != wanted[k] for k in ("readmode", "gain", "offset")):
                continue
            candidates.append((abs(values["temp"] - ccd_temp), path))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], str(item[1])))
        return candidates[0][1]

    def _match_flat(self, header: fits.Header) -> Path | None:
        if not self.config.flat_dir.is_dir():
            return None
        filter_name = str(header.get("FILTER") or "").strip()
        if not filter_name:
            return None
        for suffix in (".fit", ".fits", ".FIT", ".FITS"):
            path = self.config.flat_dir / f"master_flat_{filter_name}{suffix}"
            if path.is_file():
                return path
        return None

    def _render_preview(self, siril_path: str, source: Path, preview_path: Path, dark: Path | None, flat: Path | None) -> None:
        with tempfile.TemporaryDirectory(prefix="fits-preview-") as tmp:
            work_dir = Path(tmp)
            staged_source = work_dir / f"light{source.suffix.lower()}"
            _link_or_copy(source, staged_source)
            load_path = staged_source
            if dark or flat:
                args = ["calibrate_single", staged_source.name]
                if dark:
                    staged_dark = work_dir / f"master_dark{dark.suffix.lower()}"
                    _link_or_copy(dark, staged_dark)
                    args.append(f"-dark={staged_dark.name}")
                if flat:
                    staged_flat = work_dir / f"master_flat{flat.suffix.lower()}"
                    _link_or_copy(flat, staged_flat)
                    args.append(f"-flat={staged_flat.name}")
                if dark:
                    args.append("-cc=dark")
                script = "requires 1.2.0\n" + " ".join(args) + "\n"
                load_path = work_dir / f"pp_{staged_source.stem}"
            else:
                script = "requires 1.2.0\n"
            script += (
                f"load {_siril_quote(load_path)}\n"
                "autostretch\n"
                f"savejpg {_siril_quote(preview_path.with_suffix(''))} 95\n"
                "close\n"
            )
            result = subprocess.run(
                [siril_path, "-d", str(work_dir), "-s", "-"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=self.config.siril_timeout_seconds,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Siril preview generation failed (code {result.returncode}): {_summarize_output(result)}")
            if not preview_path.is_file():
                raise RuntimeError(f"Siril preview generation did not create JPEG: {preview_path}\n{_summarize_output(result)}")

    def _cleanup_cache(self) -> None:
        previews = sorted(
            (p for p in self.config.cache_dir.glob("*.jpg") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in previews[self.config.max_previews :]:
            try:
                old.unlink()
            except OSError:
                logger.warning("Could not delete old FITS preview %s", old)

    def _set_status(self, **updates: Any) -> None:
        with self._lock:
            self._status.update(updates)


def build_config_from_env(repo_root: str, capture_dir: str) -> FitsPreviewConfig:
    return FitsPreviewConfig(
        enabled=_env_bool("FITS_PREVIEW_ENABLED", True),
        capture_dir=Path(os.path.expanduser(os.environ.get("FITS_PREVIEW_CAPTURE_DIR", capture_dir))),
        dark_dir=Path(os.path.expanduser(os.environ.get("FITS_PREVIEW_MASTER_DARK_DIR", "~/masters/dark"))),
        flat_dir=Path(os.path.expanduser(os.environ.get("FITS_PREVIEW_MASTER_FLAT_DIR", "~/masters/flat"))),
        cache_dir=Path(
            os.path.expanduser(
                os.environ.get(
                    "FITS_PREVIEW_CACHE_DIR",
                    os.path.join(repo_root, ".cache", "observatory_panel", "fits_previews"),
                )
            )
        ),
    )


def _is_fits_path(path: Path) -> bool:
    return path.suffix.lower() in _FITS_SUFFIXES


def _read_fits_header(path: Path) -> fits.Header:
    with fits.open(path, memmap=False) as hdul:
        return hdul[0].header.copy()


def _extract_metadata(header: fits.Header) -> dict[str, Any]:
    return {
        "capture_time": _fits_utc_time(header.get("DATE-OBS")),
        "filter": header.get("FILTER"),
        "exposure_seconds": _header_float(header, "EXPTIME"),
    }


def _fits_utc_time(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _header_float(header: fits.Header, key: str) -> float | None:
    value = header.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _header_int(header: fits.Header, key: str) -> int | None:
    value = _header_float(header, key)
    return int(round(value)) if value is not None else None


def _cache_key(path: Path, size: int, mtime_ns: int) -> str:
    return hashlib.sha256(f"{path}\0{size}\0{mtime_ns}".encode("utf-8")).hexdigest()[:16]


def _link_or_copy(source: Path, dest: Path) -> None:
    try:
        dest.symlink_to(source)
    except OSError:
        shutil.copy2(source, dest)


def _siril_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _summarize_output(result: subprocess.CompletedProcess[str]) -> str:
    parts = []
    if result.stdout:
        parts.append("stdout:\n" + result.stdout[-2000:])
    if result.stderr:
        parts.append("stderr:\n" + result.stderr[-2000:])
    return "\n".join(parts) if parts else "<no Siril output>"


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}
