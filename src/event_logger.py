"""JSONL event logger with abs/rel timestamps."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class EventLogger:
    """
    Writes newline-delimited JSON events to a .jsonl file.

    Each event has:
      abs  — ISO-8601 UTC timestamp
      rel  — seconds since stream_start (float)
      event — event name string
      ...additional fields...
    """

    def __init__(self, output_dir: str | Path, stream_key: str):
        self._lock = threading.Lock()
        self._stream_start: datetime | None = None
        self._listeners: list[Callable[[dict], None]] = []

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # Use first 8 chars of key or "nokey" for filename safety
        key_part = (stream_key or "nokey")[:8].replace("/", "_")
        self._path = output_dir / f"{date_str}_{key_part}.jsonl"
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    # ------------------------------------------------------------------

    def add_listener(self, cb: Callable[[dict], None]) -> None:
        """Register a callback called on every logged event (from any thread)."""
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[dict], None]) -> None:
        self._listeners.remove(cb)

    # ------------------------------------------------------------------

    def log(self, event: str, **kwargs: Any) -> dict:
        """Log an event and return the written record."""
        now = datetime.now(timezone.utc)
        rel = 0.0
        if self._stream_start is not None:
            rel = (now - self._stream_start).total_seconds()

        record: dict[str, Any] = {
            "abs": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "rel": round(rel, 3),
            "event": event,
        }
        record.update(kwargs)

        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._file.write(line + "\n")

        for cb in list(self._listeners):
            try:
                cb(record)
            except Exception:
                pass

        return record

    def mark_stream_start(self, **kwargs: Any) -> dict:
        self._stream_start = datetime.now(timezone.utc)
        return self.log("stream_start", **kwargs)

    def mark_stream_end(self, **kwargs: Any) -> dict:
        record = self.log("stream_end", **kwargs)
        return record

    def close(self) -> None:
        with self._lock:
            self._file.flush()
            self._file.close()

    @property
    def log_path(self) -> Path:
        return self._path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
