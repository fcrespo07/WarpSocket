from __future__ import annotations

import logging
from pathlib import Path

from warpsocket.logs import MemoryLogHandler, default_log_path, setup_logging


def test_default_log_path_is_absolute_under_app_dir():
    p = default_log_path()
    assert p.is_absolute()
    assert "WarpSocket" in str(p)
    assert p.name == "warpsocket.log"


def test_setup_logging_creates_log_file(tmp_path):
    log_path = tmp_path / "logs" / "wstunnel.log"
    setup_logging(log_path=log_path)
    logging.getLogger("test").info("hello")
    assert log_path.exists()
    assert "hello" in log_path.read_text(encoding="utf-8")


def test_setup_logging_returns_memory_handler(tmp_path):
    h = setup_logging(log_path=tmp_path / "log")
    assert isinstance(h, MemoryLogHandler)


def test_memory_handler_captures_records(tmp_path):
    h = setup_logging(log_path=tmp_path / "log")
    logging.getLogger("test").info("first")
    logging.getLogger("test").warning("second")
    snap = h.snapshot()
    assert any("first" in line for line in snap)
    assert any("second" in line for line in snap)


def test_memory_handler_capacity_limits_buffer(tmp_path):
    h = setup_logging(log_path=tmp_path / "log", memory_capacity=3)
    for i in range(10):
        logging.getLogger("test").info("msg-%d", i)
    snap = h.snapshot()
    assert len(snap) == 3
    assert "msg-9" in snap[-1]
    assert "msg-7" in snap[0]


def test_memory_handler_clear():
    h = MemoryLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(logging.LogRecord("x", logging.INFO, "f", 0, "hello", None, None))
    assert h.snapshot() == ["hello"]
    h.clear()
    assert h.snapshot() == []


def test_setup_logging_replaces_existing_handlers(tmp_path):
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    setup_logging(log_path=tmp_path / "log")
    assert sentinel not in root.handlers


def test_log_format_includes_level_and_logger_name(tmp_path):
    log_path = tmp_path / "log"
    setup_logging(log_path=log_path)
    logging.getLogger("warpsocket.test").info("hello world")
    content = log_path.read_text(encoding="utf-8")
    assert "INFO" in content
    assert "warpsocket.test" in content
    assert "hello world" in content
