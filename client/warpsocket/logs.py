from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from pathlib import Path
from threading import Lock

from platformdirs import user_log_dir

_APP_NAME = "WarpSocket"
_MAX_BYTES = 512 * 1024
_BACKUP_COUNT = 1
_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def default_log_path() -> Path:
    return Path(user_log_dir(_APP_NAME)) / "warpsocket.log"


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        with self._lock:
            self._buf.append(msg)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


def setup_logging(
    level: int = logging.INFO,
    log_path: Path | None = None,
    memory_capacity: int = 2000,
) -> MemoryLogHandler:
    log_path = log_path or default_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(_DEFAULT_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    memory_handler = MemoryLogHandler(capacity=memory_capacity)
    memory_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(memory_handler)

    return memory_handler
