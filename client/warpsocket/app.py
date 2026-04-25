from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from warpsocket import __version__
from warpsocket.config import ClientConfig, ConfigError, default_config_path
from warpsocket.logs import setup_logging

log = logging.getLogger(__name__)

_LOCK_NAME = "warpsocket-client.lock"


class _SingleInstanceLock:
    """Cross-platform mutex to prevent running two instances."""

    def __init__(self) -> None:
        self._handle: object | None = None
        self._lock_path: Path | None = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def release(self) -> None:
        if sys.platform == "win32":
            self._release_windows()
        else:
            self._release_posix()

    def _acquire_windows(self) -> bool:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\WarpSocketClient")
        last_error = ctypes.windll.kernel32.GetLastError()
        if last_error == 183:  # ERROR_ALREADY_EXISTS
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _release_windows(self) -> None:
        if self._handle is not None:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None

    def _acquire_posix(self) -> bool:
        import fcntl
        self._lock_path = Path(tempfile.gettempdir()) / _LOCK_NAME
        try:
            self._handle = open(self._lock_path, "w")  # noqa: SIM115
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self._handle:
                self._handle.close()
                self._handle = None
            return False

    def _release_posix(self) -> None:
        if self._handle is not None:
            import fcntl
            try:
                fcntl.flock(self._handle, fcntl.LOCK_UN)
                self._handle.close()
            except Exception:
                pass
            self._handle = None
            if self._lock_path and self._lock_path.exists():
                try:
                    self._lock_path.unlink()
                except Exception:
                    pass


def _load_or_wizard() -> ClientConfig | None:
    """Load config.json, or run the wizard if it doesn't exist. Returns None if user cancels."""
    config_path = default_config_path()
    if config_path.exists():
        try:
            return ClientConfig.load(config_path)
        except ConfigError as exc:
            log.error("Config file corrupt: %s — launching wizard to re-import", exc)

    from warpsocket.wizard import run_wizard

    return run_wizard()


def _open_log_viewer(memory_handler: object) -> None:
    """Open the live log window in a new thread."""
    import threading

    import customtkinter as ctk

    from warpsocket.logs import MemoryLogHandler

    assert isinstance(memory_handler, MemoryLogHandler)

    def _show() -> None:
        win = ctk.CTkToplevel()
        win.title("WarpSocket — Logs")
        win.geometry("700x420")

        text = ctk.CTkTextbox(win, state="normal", wrap="word", font=ctk.CTkFont(family="Consolas", size=11))
        text.pack(fill="both", expand=True, padx=8, pady=8)

        for line in memory_handler.snapshot():
            text.insert("end", line + "\n")
        text.see("end")
        text.configure(state="disabled")

    threading.Thread(target=_show, daemon=True, name="warpsocket-log-viewer").start()


def main() -> int:
    memory_handler = setup_logging()
    log.info("WarpSocket client v%s starting", __version__)

    lock = _SingleInstanceLock()
    if not lock.acquire():
        log.error("Another instance is already running — exiting")
        from tkinter import messagebox

        messagebox.showwarning(
            "WarpSocket",
            "Ya hay otra instancia de WarpSocket ejecutándose.",
        )
        return 1

    try:
        config = _load_or_wizard()
        if config is None:
            log.info("User cancelled wizard — exiting")
            return 0

        log.info(
            "Config loaded: server=%s:%d tunnel=%s",
            config.server.endpoint,
            config.server.port,
            config.wireguard.tunnel_name,
        )

        from warpsocket.tray import TrayApp
        from warpsocket.tunnel import TunnelManager

        manager = TunnelManager(config)

        def on_import_warpcfg() -> None:
            from warpsocket.wizard import run_wizard

            new_config = run_wizard()
            if new_config is not None:
                log.info("Re-imported config; restart WarpSocket to apply.")
                from tkinter import messagebox

                messagebox.showinfo(
                    "WarpSocket",
                    "Configuración actualizada.\nReinicia WarpSocket para aplicar los cambios.",
                )

        def on_view_logs() -> None:
            _open_log_viewer(memory_handler)

        def on_quit() -> None:
            log.info("User quit — stopping tunnel")
            manager.stop()

        tray = TrayApp(
            manager=manager,
            on_import_warpcfg=on_import_warpcfg,
            on_view_logs=on_view_logs,
            on_quit=on_quit,
        )

        manager.start()
        log.info("Tray running — waiting for user interaction")
        tray.run()  # Blocking — returns when user quits

        manager.stop()
        log.info("WarpSocket shut down cleanly")
        return 0

    finally:
        lock.release()
