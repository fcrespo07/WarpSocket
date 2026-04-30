from __future__ import annotations

import logging
import sys
from pathlib import Path

from warpsocket_server import __version__
from warpsocket_server.config import ConfigError, ServerConfig, default_config_path
from warpsocket_server.logs import setup_logging

log = logging.getLogger(__name__)

_MUTEX_NAME = "Global\\WarpSocketServer"


class _SingleInstanceLock:
    def __init__(self) -> None:
        self._handle: object | None = None

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
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
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
        import fcntl, tempfile
        self._lock_path = Path(tempfile.gettempdir()) / "warpsocket-server.lock"
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


def _ensure_elevated() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    executable = sys.executable
    params = " ".join(f'"{a}"' for a in sys.argv) if not getattr(sys, "frozen", False) else None
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    sys.exit(0 if ret > 32 else 1)


def _try_load_config() -> ServerConfig | None:
    path = default_config_path()
    if not path.exists():
        return None
    try:
        return ServerConfig.load(path)
    except ConfigError as exc:
        log.warning("Server config corrupt: %s — showing setup wizard", exc)
        return None


def main() -> int:
    _ensure_elevated()
    memory_handler = setup_logging()
    log.info("WarpSocket Server GUI v%s starting", __version__)

    lock = _SingleInstanceLock()
    if not lock.acquire():
        log.error("Another instance is already running")
        from tkinter import messagebox
        messagebox.showwarning(
            "WarpSocket Server",
            "Ya hay otra instancia de WarpSocket Server ejecutándose.",
        )
        return 1

    try:
        import customtkinter as ctk
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        config = _try_load_config()

        from warpsocket_server.server_manager import ServerManager
        from warpsocket_server.server_tray import ServerTrayApp
        from warpsocket_server.server_window import ServerWindow

        manager: ServerManager | None = ServerManager(config) if config else None

        window: ServerWindow
        tray: ServerTrayApp

        def on_setup_complete(cfg: ServerConfig, mgr: ServerManager) -> None:
            nonlocal manager
            manager = mgr
            tray.update_manager(mgr)
            log.info("Setup complete: server=%s:%d", cfg.endpoint, cfg.port)

        def on_quit() -> None:
            log.info("Shutting down WarpSocket Server")
            window.stop_log_refresh()
            if manager:
                manager.stop()
            tray.stop()
            window.quit()

        window = ServerWindow(
            config=config,
            manager=manager,
            memory_handler=memory_handler,
            on_setup_complete=on_setup_complete,
            on_quit=on_quit,
        )

        tray = ServerTrayApp(
            manager=manager,
            ui_queue=window.ui_queue,
            on_show=window.show_from_tray,
            on_quit=on_quit,
        )

        if manager:
            manager.start()
            log.info("Server manager started: server=%s:%d", config.endpoint, config.port)

        tray.run()
        log.info("Tray running — entering UI event loop")
        window.mainloop()

        log.info("WarpSocket Server shut down cleanly")
        return 0

    finally:
        lock.release()
