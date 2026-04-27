from __future__ import annotations

import logging
import queue
import sys
import tempfile
from pathlib import Path
from typing import Callable

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


_log_window: dict[str, object] = {}  # singleton: at most one log window at a time


def _show_log_window(memory_handler: object, root: object) -> None:
    """Open a live-updating log window. MUST be called from the tkinter main thread."""
    import customtkinter as ctk

    from warpsocket.logs import MemoryLogHandler

    assert isinstance(memory_handler, MemoryLogHandler)
    assert isinstance(root, ctk.CTk)

    # Raise existing window instead of opening a second one.
    existing = _log_window.get("win")
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_force()
                return
        except Exception:
            pass
        _log_window.pop("win", None)

    # Ensure the hidden root is fully initialised before spawning a child window.
    # On some Windows/customtkinter versions, CTkToplevel silently fails to appear
    # if the root hasn't processed its first idle tasks.
    root.update_idletasks()

    win = ctk.CTkToplevel(root)
    _log_window["win"] = win
    win.title("WarpSocket — Logs")
    win.geometry("700x420")
    try:
        win.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(200, lambda: win.attributes("-topmost", False))
        win.focus_force()
    except Exception:
        log.exception("Could not raise log window")

    text = ctk.CTkTextbox(
        win, wrap="word", font=ctk.CTkFont(family="Consolas", size=11)
    )
    text.pack(fill="both", expand=True, padx=8, pady=8)

    last_count = [0]

    def _refresh() -> None:
        if not win.winfo_exists():
            return
        lines = memory_handler.snapshot()
        if len(lines) > last_count[0]:
            text.configure(state="normal")
            for line in lines[last_count[0]:]:
                text.insert("end", line + "\n")
            text.see("end")
            text.configure(state="disabled")
            last_count[0] = len(lines)
        win.after(500, _refresh)

    _refresh()


def _pump_ui_queue(root: object, ui_queue: queue.Queue) -> None:
    """Process pending UI actions on the tkinter main thread.

    Tcl/tkinter is not thread-safe; calling root.after() from a non-main
    thread (e.g. pystray's tray thread) silently fails on Windows. The
    queue + main-thread polling pattern is the canonical workaround.
    """
    try:
        while True:
            action: Callable[[], None] = ui_queue.get_nowait()
            try:
                action()
            except Exception:
                log.exception("UI action raised")
    except queue.Empty:
        pass
    root.after(50, lambda: _pump_ui_queue(root, ui_queue))


def _ensure_elevated() -> None:
    """On Windows, re-launch with UAC elevation if not already running as admin."""
    if sys.platform != "win32":
        return
    import ctypes

    if ctypes.windll.shell32.IsUserAnAdmin():
        return

    # Determine how to relaunch elevated.
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — sys.executable IS the .exe, no extra args needed.
        executable = sys.executable
        params = None
    elif sys.argv[0].endswith(".exe"):
        # pip-installed entry point: 'warpsocket' becomes warpsocket.exe in Scripts/.
        # Relaunching the .exe directly is cleaner than python.exe + script path.
        executable = sys.argv[0]
        params = None
    else:
        # Running directly via 'python app.py' or 'python -m warpsocket'.
        executable = sys.executable
        params = " ".join(f'"{a}"' for a in sys.argv)

    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    # ShellExecuteW returns > 32 on success; <= 32 means error or user cancelled.
    sys.exit(0 if ret > 32 else 1)


def main() -> int:
    _ensure_elevated()
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

        import customtkinter as ctk

        from warpsocket.tray import TrayApp
        from warpsocket.tunnel import TunnelManager

        # Hidden root window — owns the tkinter event loop on the main thread.
        # All CTkToplevel windows (log viewer, dialogs) must be children of this root.
        root = ctk.CTk()
        root.withdraw()

        # Queue for actions that must run on the main thread. Tray callbacks
        # (which run in pystray's thread) push closures here; _pump_ui_queue
        # drains them from the tkinter mainloop.
        ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        manager = TunnelManager(config)

        def on_import_warpcfg() -> None:
            def _do() -> None:
                from warpsocket.wizard import run_wizard

                new_config = run_wizard()
                if new_config is not None:
                    log.info("Re-imported config; restart WarpSocket to apply.")
                    from tkinter import messagebox

                    messagebox.showinfo(
                        "WarpSocket",
                        "Configuración actualizada.\nReinicia WarpSocket para aplicar los cambios.",
                    )

            ui_queue.put(_do)

        def on_view_logs() -> None:
            ui_queue.put(lambda: _show_log_window(memory_handler, root))

        def on_quit() -> None:
            log.info("User quit — stopping tunnel")
            manager.stop()
            ui_queue.put(root.quit)

        tray = TrayApp(
            manager=manager,
            on_import_warpcfg=on_import_warpcfg,
            on_view_logs=on_view_logs,
            on_quit=on_quit,
        )

        manager.start()
        tray.run()  # Starts pystray in background thread (non-blocking)
        log.info("Tray running — entering UI event loop")
        root.after(50, lambda: _pump_ui_queue(root, ui_queue))
        root.mainloop()  # Main thread: blocks here until on_quit pushes root.quit

        manager.stop()
        log.info("WarpSocket shut down cleanly")
        return 0

    finally:
        lock.release()
