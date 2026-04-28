from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable

import customtkinter as ctk
from PIL import Image

from warpsocket.config import ClientConfig, ConfigError, import_warpcfg
from warpsocket.logs import MemoryLogHandler
from warpsocket.tunnel import TunnelManager, TunnelState

log = logging.getLogger(__name__)

_STATE_COLORS: dict[TunnelState, str] = {
    TunnelState.DISCONNECTED: "#808080",
    TunnelState.CONNECTING:   "#FFC800",
    TunnelState.CONNECTED:    "#00C800",
    TunnelState.RECONNECTING: "#FF9600",
    TunnelState.FAILED:       "#DC0000",
}

_STATE_LABELS: dict[TunnelState, str] = {
    TunnelState.DISCONNECTED: "Desconectado",
    TunnelState.CONNECTING:   "Conectando…",
    TunnelState.CONNECTED:    "Conectado",
    TunnelState.RECONNECTING: "Reconectando…",
    TunnelState.FAILED:       "Error de conexión",
}


class MainWindow(ctk.CTk):
    """Single-window WarpSocket client UI.

    Shows a setup page (no config) or dashboard (config loaded).
    Closing with the X button hides to tray; full quit requires Parar or tray menu.
    """

    def __init__(
        self,
        config: ClientConfig | None,
        manager: TunnelManager | None,
        memory_handler: MemoryLogHandler,
        on_import: Callable[[ClientConfig], None],
        on_quit: Callable[[], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._manager = manager
        self._memory_handler = memory_handler
        self._on_import_cb = on_import
        self._on_quit_cb = on_quit

        # Thread-safe queue for actions that must run on the tkinter main thread.
        # Pystray callbacks push closures here; _pump_ui_queue drains them.
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._log_last_count = 0
        self._log_refresh_running = False

        self.title("WarpSocket")
        self.geometry("720x540")
        self.minsize(520, 420)
        self.resizable(True, True)

        # X button hides to tray instead of quitting.
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self._load_icon()
        self._build_ui()

        if config and manager:
            self._show_dashboard()
        else:
            self._show_setup()

        self._start_log_refresh()
        self._pump_ui_queue()

    # ── Icon ──────────────────────────────────────────────────────────────────

    def _load_icon(self) -> None:
        try:
            ico = Path(__file__).parent / "resources" / "app_icon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Top bar: logo + app name + connection status ──
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 0))

        try:
            img = Image.open(Path(__file__).parent / "resources" / "app_icon.png")
            logo = ctk.CTkImage(light_image=img, dark_image=img, size=(44, 44))
            ctk.CTkLabel(top, image=logo, text="").pack(side="left")
        except Exception:
            pass

        ctk.CTkLabel(
            top,
            text="WarpSocket",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(side="left", padx=(12, 0))

        self._status_dot = ctk.CTkLabel(
            top,
            text="●",
            font=ctk.CTkFont(size=15),
            text_color="#808080",
        )
        self._status_dot.pack(side="right", padx=(0, 4))

        self._status_label = ctk.CTkLabel(
            top,
            text="Sin configuración",
            font=ctk.CTkFont(size=13),
        )
        self._status_label.pack(side="right", padx=(0, 8))

        # Horizontal divider
        ctk.CTkFrame(self, height=1, fg_color=("gray75", "gray30")).pack(
            fill="x", padx=20, pady=(12, 0)
        )

        # ── Content area (setup / dashboard frames swap here) ──
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(12, 16))

        self._setup_frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._dashboard_frame = ctk.CTkFrame(self._content, fg_color="transparent")

        self._build_setup_frame()
        self._build_dashboard_frame()

    def _build_setup_frame(self) -> None:
        f = self._setup_frame

        ctk.CTkLabel(
            f,
            text="No se encontró ninguna configuración.",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(pady=(50, 6))

        ctk.CTkLabel(
            f,
            text="Importa un archivo .warpcfg para comenzar.",
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
        ).pack(pady=(0, 28))

        ctk.CTkButton(
            f,
            text="Importar .warpcfg…",
            width=220,
            height=44,
            command=self._handle_import,
        ).pack()

        self._setup_error = ctk.CTkLabel(
            f,
            text="",
            text_color="#DC4444",
            font=ctk.CTkFont(size=12),
            wraplength=440,
        )
        self._setup_error.pack(pady=(14, 0))

    def _build_dashboard_frame(self) -> None:
        f = self._dashboard_frame

        # TabView: Logs | Configuración
        self._tabview = ctk.CTkTabview(f, height=0)
        self._tabview.pack(fill="both", expand=True)

        logs_tab = self._tabview.add("Logs")
        self._log_box = ctk.CTkTextbox(
            logs_tab,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="none",
            state="disabled",
            activate_scrollbars=True,
        )
        self._log_box.pack(fill="both", expand=True, pady=(6, 0))

        cfg_tab = self._tabview.add("Configuración")
        self._cfg_box = ctk.CTkTextbox(
            cfg_tab,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
            state="disabled",
        )
        self._cfg_box.pack(fill="both", expand=True, pady=(6, 0))

        # Button bar: Parar | Reconectar | (right) Importar .warpcfg…
        btn_bar = ctk.CTkFrame(f, fg_color="transparent")
        btn_bar.pack(fill="x", pady=(10, 0))

        self._btn_stop = ctk.CTkButton(
            btn_bar,
            text="Parar",
            width=110,
            fg_color="#C0392B",
            hover_color="#922B21",
            command=self._handle_stop,
        )
        self._btn_stop.pack(side="left")

        self._btn_reconnect = ctk.CTkButton(
            btn_bar,
            text="Reconectar",
            width=120,
            command=self._handle_reconnect,
        )
        self._btn_reconnect.pack(side="left", padx=(10, 0))

        ctk.CTkButton(
            btn_bar,
            text="Importar .warpcfg…",
            width=165,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            command=self._handle_import,
        ).pack(side="right")

    # ── Page switching ────────────────────────────────────────────────────────

    def _show_setup(self) -> None:
        self._dashboard_frame.pack_forget()
        self._setup_frame.pack(fill="both", expand=True)
        self._status_dot.configure(text_color="#808080")
        self._status_label.configure(text="Sin configuración")

    def _show_dashboard(self) -> None:
        self._setup_frame.pack_forget()
        self._dashboard_frame.pack(fill="both", expand=True)
        self._refresh_cfg_tab()
        if self._manager:
            self._manager.add_listener(self._on_state_change)
            self._on_state_change(self._manager.state)

    def set_manager(self, manager: TunnelManager) -> None:
        """Wire a newly-created TunnelManager after a config import."""
        self._manager = manager
        if self._dashboard_frame.winfo_ismapped():
            manager.add_listener(self._on_state_change)
            self._on_state_change(manager.state)

    def _refresh_cfg_tab(self) -> None:
        c = self._config
        if not c:
            return
        lines = [
            f"Servidor      {c.server.endpoint}:{c.server.port}",
            f"Túnel WG      {c.wireguard.tunnel_name}",
            f"IP cliente    {c.wireguard.client_address}",
            f"DNS           {', '.join(c.wireguard.dns)}",
            f"Puerto local  {c.tunnel.local_port}",
            f"Reconexión    máx {c.reconnect.max_attempts} intentos, "
            f"backoff {c.reconnect.delays_seconds} s",
        ]
        self._cfg_box.configure(state="normal")
        self._cfg_box.delete("1.0", "end")
        self._cfg_box.insert("1.0", "\n".join(lines))
        self._cfg_box.configure(state="disabled")

    # ── State listener (tunnel thread → safe via ui_queue) ────────────────────

    def _on_state_change(self, state: TunnelState) -> None:
        self.ui_queue.put(lambda s=state: self._apply_state(s))

    def _apply_state(self, state: TunnelState) -> None:
        color = _STATE_COLORS.get(state, "#808080")
        label = _STATE_LABELS.get(state, str(state))
        self._status_dot.configure(text_color=color)
        self._status_label.configure(text=label)
        stopped = state in (TunnelState.DISCONNECTED, TunnelState.FAILED)
        self._btn_stop.configure(state="disabled" if stopped else "normal")
        self._btn_reconnect.configure(state="normal" if stopped else "disabled")

    # ── Button handlers ───────────────────────────────────────────────────────

    def _handle_import(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            parent=self,
            title="Importar configuración",
            filetypes=[
                ("WarpSocket config", "*.warpcfg *.json"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if not path:
            return

        try:
            cfg = import_warpcfg(Path(path))
        except ConfigError as exc:
            if self._setup_frame.winfo_ismapped():
                self._setup_error.configure(text=f"Error: {exc}")
            else:
                from tkinter import messagebox
                messagebox.showerror("Error al importar", str(exc), parent=self)
            return

        self._config = cfg
        self._setup_error.configure(text="")
        self._on_import_cb(cfg)

        if not self._dashboard_frame.winfo_ismapped():
            self._refresh_cfg_tab()
            self._show_dashboard()
        else:
            self._refresh_cfg_tab()

    def _handle_stop(self) -> None:
        log.info("User clicked Parar — shutting down")
        self._on_quit_cb()

    def _handle_reconnect(self) -> None:
        if self._manager:
            threading.Thread(
                target=lambda: (self._manager.stop(), self._manager.start()),
                daemon=True,
                name="warpsocket-manual-reconnect",
            ).start()

    # ── Log refresh ───────────────────────────────────────────────────────────

    def _start_log_refresh(self) -> None:
        self._log_refresh_running = True
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        if not self._log_refresh_running:
            return
        try:
            if self._dashboard_frame.winfo_ismapped():
                lines = self._memory_handler.snapshot()
                new = lines[self._log_last_count:]
                if new:
                    self._log_box.configure(state="normal")
                    for line in new:
                        self._log_box.insert("end", line + "\n")
                    self._log_box.see("end")
                    self._log_box.configure(state="disabled")
                    self._log_last_count = len(lines)
        except Exception:
            pass
        self.after(500, self._refresh_logs)

    # ── UI queue pump ─────────────────────────────────────────────────────────

    def _pump_ui_queue(self) -> None:
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("UI queue action raised")
        except queue.Empty:
            pass
        self.after(50, self._pump_ui_queue)

    # ── Tray / window management ──────────────────────────────────────────────

    def _hide_to_tray(self) -> None:
        self.withdraw()

    def show_from_tray(self) -> None:
        """Restore from tray. Must be called from main thread (push to ui_queue from tray)."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def stop_log_refresh(self) -> None:
        self._log_refresh_running = False
