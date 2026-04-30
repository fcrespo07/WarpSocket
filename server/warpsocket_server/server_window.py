from __future__ import annotations

import logging
import queue
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable

import customtkinter as ctk
from PIL import Image

from warpsocket_server.config import (
    ConfigError,
    ServerConfig,
    default_config_dir,
    default_config_path,
)
from warpsocket_server.logs import MemoryLogHandler
from warpsocket_server.server_manager import ServerManager, ServerState

log = logging.getLogger(__name__)

_RESOURCES = Path(__file__).parent / "resources"

_STATE_COLORS: dict[ServerState, str] = {
    ServerState.STOPPED:  "#808080",
    ServerState.STARTING: "#FFC800",
    ServerState.RUNNING:  "#00C800",
    ServerState.ERROR:    "#DC0000",
}

_STATE_LABELS: dict[ServerState, str] = {
    ServerState.STOPPED:  "Detenido",
    ServerState.STARTING: "Iniciando…",
    ServerState.RUNNING:  "Activo",
    ServerState.ERROR:    "Error",
}


class ServerWindow(ctk.CTk):
    """Main server window.

    Shows either the first-run setup wizard (no config) or the dashboard.
    Closing with X hides to tray; full quit requires 'Parar y salir'.
    """

    def __init__(
        self,
        config: ServerConfig | None,
        manager: ServerManager | None,
        memory_handler: MemoryLogHandler,
        on_setup_complete: Callable[[ServerConfig, ServerManager], None],
        on_quit: Callable[[], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._manager = manager
        self._memory_handler = memory_handler
        self._on_setup_complete_cb = on_setup_complete
        self._on_quit_cb = on_quit
        self._log_last_count = 0
        self._log_refresh_running = False

        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self.title("WarpSocket Server")
        self.geometry("800x580")
        self.minsize(560, 440)
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
            ico = _RESOURCES / "app_icon.ico"
            if ico.exists():
                self.iconbitmap(str(ico))
        except Exception:
            pass

    # ── UI skeleton ───────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 0))

        try:
            img = Image.open(_RESOURCES / "app_icon.png")
            logo = ctk.CTkImage(light_image=img, dark_image=img, size=(44, 44))
            ctk.CTkLabel(top, image=logo, text="").pack(side="left")
        except Exception:
            pass

        ctk.CTkLabel(
            top,
            text="WarpSocket Server",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(side="left", padx=(12, 0))

        self._status_dot = ctk.CTkLabel(
            top, text="●", font=ctk.CTkFont(size=15), text_color="#808080"
        )
        self._status_dot.pack(side="right", padx=(0, 4))

        self._status_label = ctk.CTkLabel(
            top, text="Sin configuración", font=ctk.CTkFont(size=13)
        )
        self._status_label.pack(side="right", padx=(0, 8))

        ctk.CTkFrame(self, height=1, fg_color=("gray75", "gray30")).pack(
            fill="x", padx=20, pady=(12, 0)
        )

        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20, pady=(12, 16))

        self._setup_frame = ctk.CTkFrame(self._content, fg_color="transparent")
        self._dashboard_frame = ctk.CTkFrame(self._content, fg_color="transparent")

        self._build_setup_frame()
        self._build_dashboard_frame()

    # ── Setup wizard ──────────────────────────────────────────────────────────

    def _build_setup_frame(self) -> None:
        f = self._setup_frame
        # We swap child frames as we navigate through wizard pages.
        self._wizard_pages: list[ctk.CTkFrame] = []
        self._wizard_page_idx = 0

        self._wizard_container = ctk.CTkFrame(f, fg_color="transparent")
        self._wizard_container.pack(fill="both", expand=True)

        self._page_check = self._make_page_check()
        self._page_config = self._make_page_config()
        self._page_installing = self._make_page_installing()
        self._page_done = self._make_page_done()

    def _show_wizard_page(self, page: ctk.CTkFrame) -> None:
        for w in self._wizard_container.winfo_children():
            w.pack_forget()
        page.pack(fill="both", expand=True)

    # Page 1: dependency check

    def _make_page_check(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._wizard_container, fg_color="transparent")

        ctk.CTkLabel(
            page,
            text="Configuración inicial",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(30, 6))
        ctk.CTkLabel(
            page,
            text="Verificando dependencias necesarias…",
            font=ctk.CTkFont(size=12),
            text_color=("gray50", "gray60"),
        ).pack(pady=(0, 20))

        self._dep_wstunnel = ctk.CTkLabel(page, text="  wstunnel  …", anchor="w")
        self._dep_wstunnel.pack(fill="x", padx=80)
        self._dep_wg = ctk.CTkLabel(page, text="  wg  …", anchor="w")
        self._dep_wg.pack(fill="x", padx=80, pady=(4, 0))

        self._dep_error = ctk.CTkLabel(
            page, text="", text_color="#DC4444",
            font=ctk.CTkFont(size=12), wraplength=480,
        )
        self._dep_error.pack(pady=(14, 0))

        self._btn_next_check = ctk.CTkButton(
            page, text="Continuar →", width=160, state="disabled",
            command=self._wizard_go_config,
        )
        self._btn_next_check.pack(pady=(20, 0))

        return page

    # Page 2: network configuration

    def _make_page_config(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._wizard_container, fg_color="transparent")

        ctk.CTkLabel(
            page,
            text="Configuración de red",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(20, 14))

        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.pack(fill="x", padx=60)
        grid.columnconfigure(1, weight=1)

        def row(label: str, r: int) -> ctk.CTkEntry:
            ctk.CTkLabel(grid, text=label, anchor="e", width=180).grid(
                row=r, column=0, sticky="e", padx=(0, 10), pady=4
            )
            entry = ctk.CTkEntry(grid, width=240)
            entry.grid(row=r, column=1, sticky="w", pady=4)
            return entry

        self._entry_endpoint = row("Endpoint (IP o dominio)", 0)
        self._entry_port = row("Puerto WSS", 1)
        self._entry_wg_port = row("Puerto WireGuard (loopback)", 2)
        self._entry_subnet = row("Subred WireGuard (CIDR)", 3)
        self._entry_srv_addr = row("Dirección del servidor WG", 4)

        # Defaults
        self._entry_port.insert(0, "443")
        self._entry_wg_port.insert(0, "51820")
        self._entry_subnet.insert(0, "10.0.0.0/24")
        self._entry_srv_addr.insert(0, "10.0.0.1/24")

        btn_bar = ctk.CTkFrame(page, fg_color="transparent")
        btn_bar.pack(pady=(20, 0))
        ctk.CTkButton(
            btn_bar, text="← Atrás", width=120,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            command=lambda: self._show_wizard_page(self._page_check),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_bar, text="Instalar →", width=140,
            command=self._wizard_start_install,
        ).pack(side="left", padx=6)

        return page

    # Page 3: installation progress

    def _make_page_installing(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._wizard_container, fg_color="transparent")

        ctk.CTkLabel(
            page,
            text="Instalando…",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(30, 10))

        self._install_log = ctk.CTkTextbox(
            page,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
            state="disabled",
            height=240,
        )
        self._install_log.pack(fill="x", padx=40, pady=(0, 10))

        self._btn_done_install = ctk.CTkButton(
            page, text="Finalizado →", width=160, state="disabled",
            command=self._wizard_finish,
        )
        self._btn_done_install.pack(pady=(10, 0))

        return page

    # Page 4: done

    def _make_page_done(self) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._wizard_container, fg_color="transparent")

        ctk.CTkLabel(
            page, text="✓  Servidor configurado",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#00C800",
        ).pack(pady=(60, 10))
        ctk.CTkLabel(
            page,
            text=(
                "El servidor se ha instalado y está listo para recibir conexiones.\n"
                "Usa 'Añadir cliente' para generar el primer .warpcfg."
            ),
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=(0, 30))
        ctk.CTkButton(
            page, text="Ir al panel →", width=160,
            command=self._wizard_finish,
        ).pack()

        return page

    # Wizard navigation

    def _show_setup(self) -> None:
        self._dashboard_frame.pack_forget()
        self._setup_frame.pack(fill="both", expand=True)
        self._status_dot.configure(text_color="#808080")
        self._status_label.configure(text="Sin configuración")
        self._show_wizard_page(self._page_check)
        self.after(100, self._check_deps)

    def _check_deps(self) -> None:
        wstunnel = shutil.which("wstunnel")
        wg = shutil.which("wg")

        def _fmt(path: str | None, name: str) -> str:
            return f"  ✓  {name}: {path}" if path else f"  ✗  {name}: no encontrado en PATH"

        self._dep_wstunnel.configure(text=_fmt(wstunnel, "wstunnel"))
        self._dep_wg.configure(text=_fmt(wg, "wg"))

        if wstunnel and wg:
            self._dep_error.configure(text="")
            self._btn_next_check.configure(state="normal")
            # Pre-fill endpoint with detected public IP
            threading.Thread(target=self._detect_ip, daemon=True).start()
        else:
            missing = [n for n, p in [("wstunnel", wstunnel), ("wg", wg)] if not p]
            self._dep_error.configure(
                text=f"Faltan: {', '.join(missing)}. "
                     "Instálalos y reinicia la aplicación."
            )
            self._btn_next_check.configure(state="disabled")

    def _detect_ip(self) -> None:
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
                ip = r.read().decode().strip()
            self.after(0, lambda: self._entry_endpoint.insert(0, ip))
        except Exception:
            pass

    def _wizard_go_config(self) -> None:
        self._show_wizard_page(self._page_config)

    def _wizard_start_install(self) -> None:
        endpoint = self._entry_endpoint.get().strip()
        port_s = self._entry_port.get().strip()
        wg_port_s = self._entry_wg_port.get().strip()
        subnet = self._entry_subnet.get().strip()
        srv_addr = self._entry_srv_addr.get().strip()

        if not endpoint:
            messagebox.showerror("Error", "El endpoint no puede estar vacío.", parent=self)
            return
        try:
            port = int(port_s)
            wg_port = int(wg_port_s)
            if not (1 <= port <= 65535) or not (1 <= wg_port <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Los puertos deben ser números entre 1 y 65535.", parent=self)
            return

        self._show_wizard_page(self._page_installing)
        self._install_log.configure(state="normal")
        self._install_log.delete("1.0", "end")
        self._install_log.configure(state="disabled")

        threading.Thread(
            target=self._run_install,
            args=(endpoint, port, wg_port, subnet, srv_addr),
            daemon=True,
            name="server-install",
        ).start()

    def _log_install(self, msg: str, ok: bool = True) -> None:
        tag = "✓" if ok else "✗"
        line = f"  [{tag}]  {msg}\n"
        self.after(0, lambda: self._append_install_log(line))

    def _append_install_log(self, line: str) -> None:
        self._install_log.configure(state="normal")
        self._install_log.insert("end", line)
        self._install_log.see("end")
        self._install_log.configure(state="disabled")

    def _run_install(
        self, endpoint: str, port: int, wg_port: int, subnet: str, srv_addr: str
    ) -> None:
        from warpsocket_server.crypto import generate_tls_cert, generate_wg_keypair
        from warpsocket_server.platforms import get_server_platform
        from warpsocket_server.wireguard import build_server_wg_conf

        config_dir = default_config_dir()
        cert_dir = config_dir / "tls"

        try:
            # Generate secrets
            self._log_install("Generando ruta HTTP upgrade…")
            upgrade_path = secrets.token_urlsafe(32)

            self._log_install("Generando certificado TLS…")
            cert_path, key_path, fingerprint = generate_tls_cert(endpoint, cert_dir)
            self._log_install(f"Certificado TLS listo ({fingerprint[:23]}…)")

            self._log_install("Generando par de claves WireGuard…")
            wg_bin = shutil.which("wg")
            wg_priv, wg_pub = generate_wg_keypair(Path(wg_bin) if wg_bin else None)
            self._log_install("Par de claves WG listo")

            # Build and save config
            config = ServerConfig(
                schema_version=1,
                endpoint=endpoint,
                port=port,
                http_upgrade_path_prefix=upgrade_path,
                cert_path=str(cert_path),
                key_path=str(key_path),
                cert_fingerprint_sha256=fingerprint,
                wg_private_key=wg_priv,
                wg_public_key=wg_pub,
                subnet=subnet,
                server_address=srv_addr,
                wg_listen_port=wg_port,
                clients=[],
            )
            config_path = default_config_path()
            config.save(config_path)
            self._log_install(f"Configuración guardada en {config_path}")

            # Platform setup (IP forwarding, NAT, firewall)
            platform = get_server_platform()
            try:
                platform.prepare_system(subnet, port)
                self._log_install("Sistema configurado (reenvío IP, NAT, firewall)")
            except Exception as exc:
                self._log_install(f"Advertencia sistema: {exc}", ok=False)

            # WireGuard
            if sys.platform == "win32":
                from warpsocket_server.wireguard import build_server_wg_conf_windows
                wg_conf = build_server_wg_conf_windows(config)
            else:
                wg_conf = build_server_wg_conf(config)
            platform.install_wg_config(wg_conf)
            self._log_install("Interfaz WireGuard activa")

            # Connectivity probe
            self._log_install("Comprobando conectividad local…")
            try:
                with socket.create_connection(("127.0.0.1", wg_port), timeout=3):
                    self._log_install(f"WireGuard escuchando en el puerto {wg_port}")
            except OSError:
                self._log_install(f"WireGuard no responde en {wg_port} (aún iniciando)", ok=False)

            self._log_install("Instalación completada")
            self.after(0, lambda: self._install_succeeded(config))

        except Exception as exc:
            log.exception("Setup wizard install failed")
            self._log_install(f"Error fatal: {exc}", ok=False)
            self.after(0, lambda: self._btn_done_install.configure(
                state="normal", text="Cerrar", fg_color="#C0392B"
            ))

    def _install_succeeded(self, config: ServerConfig) -> None:
        self._config = config
        self._btn_done_install.configure(state="normal")
        self._show_wizard_page(self._page_done)

    def _wizard_finish(self) -> None:
        if self._config is None:
            return
        from warpsocket_server.server_manager import ServerManager
        manager = ServerManager(self._config)
        self._manager = manager
        manager.add_listener(self._on_state_change)
        self._on_setup_complete_cb(self._config, manager)
        self._show_dashboard()
        manager.start()

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def _build_dashboard_frame(self) -> None:
        f = self._dashboard_frame

        self._tabview = ctk.CTkTabview(f, height=0)
        self._tabview.pack(fill="both", expand=True)

        self._build_tab_estado(self._tabview.add("Estado"))
        self._build_tab_clientes(self._tabview.add("Clientes"))
        self._build_tab_logs(self._tabview.add("Logs"))

        btn_bar = ctk.CTkFrame(f, fg_color="transparent")
        btn_bar.pack(fill="x", pady=(10, 0))

        self._btn_start = ctk.CTkButton(
            btn_bar, text="Iniciar", width=110,
            command=self._handle_start,
        )
        self._btn_start.pack(side="left")

        self._btn_stop = ctk.CTkButton(
            btn_bar, text="Parar", width=110,
            fg_color="#C0392B", hover_color="#922B21",
            command=self._handle_stop_services,
        )
        self._btn_stop.pack(side="left", padx=(10, 0))

        ctk.CTkButton(
            btn_bar, text="Salir",
            width=90,
            fg_color="transparent", border_width=1,
            text_color=("gray10", "gray90"),
            hover_color=("gray85", "gray25"),
            command=self._on_quit_cb,
        ).pack(side="right")

    def _build_tab_estado(self, parent: ctk.CTkFrame) -> None:
        self._estado_box = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="none",
            state="disabled",
            height=200,
        )
        self._estado_box.pack(fill="both", expand=True, pady=(6, 0))

    def _build_tab_clientes(self, parent: ctk.CTkFrame) -> None:
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.pack(fill="x", pady=(6, 4))

        ctk.CTkButton(
            top, text="Añadir cliente…", width=160,
            command=self._handle_add_client,
        ).pack(side="left")

        self._btn_revoke = ctk.CTkButton(
            top, text="Revocar", width=110,
            fg_color="#C0392B", hover_color="#922B21",
            state="disabled",
            command=self._handle_revoke_client,
        )
        self._btn_revoke.pack(side="left", padx=(10, 0))

        self._clients_box = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="none",
            state="disabled",
        )
        self._clients_box.pack(fill="both", expand=True)
        self._selected_client: str | None = None

    def _build_tab_logs(self, parent: ctk.CTkFrame) -> None:
        self._log_box = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="none",
            state="disabled",
            activate_scrollbars=True,
        )
        self._log_box.pack(fill="both", expand=True, pady=(6, 0))

    def _show_dashboard(self) -> None:
        self._setup_frame.pack_forget()
        self._dashboard_frame.pack(fill="both", expand=True)
        if self._manager:
            self._manager.add_listener(self._on_state_change)
            self._on_state_change(self._manager.state)
        self._refresh_estado()
        self._refresh_clients()

    def set_manager(self, manager: ServerManager) -> None:
        self._manager = manager
        if self._dashboard_frame.winfo_ismapped():
            manager.add_listener(self._on_state_change)
            self._on_state_change(manager.state)

    # ── Estado tab ────────────────────────────────────────────────────────────

    def _refresh_estado(self) -> None:
        if self._config is None:
            return
        c = self._config
        lines = [
            f"Endpoint       {c.endpoint}:{c.port}",
            f"Subred WG      {c.subnet}",
            f"IP servidor    {c.server_address}",
            f"Puerto WG      {c.wg_listen_port}",
            f"Clientes       {len(c.clients)} registrados",
            "",
            f"Fingerprint TLS  {c.cert_fingerprint_sha256[:47]}…",
        ]
        self._estado_box.configure(state="normal")
        self._estado_box.delete("1.0", "end")
        self._estado_box.insert("1.0", "\n".join(lines))
        self._estado_box.configure(state="disabled")

    # ── Clientes tab ──────────────────────────────────────────────────────────

    def _refresh_clients(self) -> None:
        if self._config is None:
            return
        try:
            from warpsocket_server.wireguard import get_live_peers
            live = get_live_peers()
        except Exception:
            live = {}

        import time as _time
        now = int(_time.time())
        _ONLINE_WINDOW = 180

        header = f"{'Nombre':<20}  {'IP':<16}  {'Estado':<10}  Último handshake\n"
        sep = "-" * 64 + "\n"
        lines = [header, sep]
        for client in self._config.clients:
            peer = live.get(client.public_key)
            if peer is None:
                status = "desconocido"
                hs = "—"
            elif peer.latest_handshake is None:
                status = "inactivo"
                hs = "nunca"
            else:
                ago = now - peer.latest_handshake
                status = "online" if ago < _ONLINE_WINDOW else "offline"
                hs = f"{ago}s atrás" if ago < 60 else f"{ago // 60}m atrás"
            lines.append(f"{client.name:<20}  {client.address:<16}  {status:<10}  {hs}\n")

        self._clients_box.configure(state="normal")
        self._clients_box.delete("1.0", "end")
        self._clients_box.insert("1.0", "".join(lines))
        self._clients_box.configure(state="disabled")

    # ── State listener ────────────────────────────────────────────────────────

    def _on_state_change(self, state: ServerState) -> None:
        self.ui_queue.put(lambda s=state: self._apply_state(s))

    def _apply_state(self, state: ServerState) -> None:
        color = _STATE_COLORS.get(state, "#808080")
        label = _STATE_LABELS.get(state, str(state))
        self._status_dot.configure(text_color=color)
        self._status_label.configure(text=label)
        if self._dashboard_frame.winfo_ismapped():
            is_running = state == ServerState.RUNNING
            is_stopped = state in (ServerState.STOPPED, ServerState.ERROR)
            self._btn_start.configure(state="normal" if is_stopped else "disabled")
            self._btn_stop.configure(state="normal" if is_running else "disabled")

    # ── Button handlers ───────────────────────────────────────────────────────

    def _handle_start(self) -> None:
        if self._manager:
            self._manager.start()

    def _handle_stop_services(self) -> None:
        if self._manager:
            threading.Thread(
                target=self._manager.stop, daemon=True, name="server-stop"
            ).start()

    def _handle_add_client(self) -> None:
        dialog = ctk.CTkInputDialog(text="Nombre del nuevo cliente:", title="Añadir cliente")
        name = dialog.get_input()
        if not name or not name.strip():
            return
        name = name.strip()

        threading.Thread(
            target=self._do_add_client, args=(name,), daemon=True, name="add-client"
        ).start()

    def _do_add_client(self, name: str) -> None:
        if self._manager is None:
            return
        try:
            warpcfg_path = self._manager.add_client(name)
            self._config = self._manager.config
            self.after(0, lambda: self._after_add_client(name, warpcfg_path, None))
        except Exception as exc:
            self.after(0, lambda e=exc: self._after_add_client(name, None, e))

    def _after_add_client(self, name: str, path: Path | None, err: Exception | None) -> None:
        if err:
            messagebox.showerror("Error", f"No se pudo añadir el cliente:\n{err}", parent=self)
            return
        self._refresh_clients()
        self._refresh_estado()

        answer = messagebox.askyesno(
            "Cliente añadido",
            f"Cliente '{name}' creado correctamente.\n\n"
            f"Archivo .warpcfg en:\n{path}\n\n"
            "¿Abrir la carpeta?",
            parent=self,
        )
        if answer and path:
            import os, subprocess as _sp
            _sp.Popen(["explorer", "/select,", str(path)], creationflags=_sp.CREATE_NO_WINDOW)

    def _handle_revoke_client(self) -> None:
        if self._selected_client is None or self._manager is None:
            return
        name = self._selected_client
        if not messagebox.askyesno(
            "Revocar cliente",
            f"¿Seguro que quieres revocar el cliente '{name}'?\n"
            "Se desconectará inmediatamente.",
            parent=self,
        ):
            return
        threading.Thread(
            target=self._do_revoke, args=(name,), daemon=True
        ).start()

    def _do_revoke(self, name: str) -> None:
        try:
            self._manager.revoke_client(name)
            self._config = self._manager.config
            self.after(0, lambda: (
                self._refresh_clients(),
                self._refresh_estado(),
            ))
        except Exception as exc:
            self.after(0, lambda e=exc: messagebox.showerror("Error", str(e), parent=self))

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
        self.deiconify()
        self.lift()
        self.focus_force()

    def stop_log_refresh(self) -> None:
        self._log_refresh_running = False
