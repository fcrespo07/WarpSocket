from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Callable

from warpsocket_server.config import ClientEntry, ConfigError, ServerConfig, default_config_path
from warpsocket_server.crypto import generate_wg_keypair
from warpsocket_server.ip_pool import PoolExhaustedError, next_available_ip
from warpsocket_server.platforms import PlatformError, get_server_platform
from warpsocket_server.warpcfg import build_warpcfg, write_warpcfg
from warpsocket_server.wireguard import (
    add_peer_live,
    build_server_wg_conf,
    remove_peer_live,
)

log = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_MONITOR_INTERVAL = 5.0


class ServerState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


def _build_wstunnel_command(config: ServerConfig, wstunnel_bin: Path) -> list[str]:
    return [
        str(wstunnel_bin),
        "server",
        "--restrict-to", f"127.0.0.1:{config.wg_listen_port}",
        "--tls-certificate", config.cert_path,
        "--tls-private-key", config.key_path,
        "--restrict-http-upgrade-path-prefix", config.http_upgrade_path_prefix,
        f"wss://0.0.0.0:{config.port}",
    ]


def _get_wg_conf(config: ServerConfig) -> str:
    if sys.platform == "win32":
        from warpsocket_server.wireguard import build_server_wg_conf_windows
        return build_server_wg_conf_windows(config)
    return build_server_wg_conf(config)


class ServerManager:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config
        self._state = ServerState.STOPPED
        self._wstunnel: subprocess.Popen | None = None
        self._listeners: list[Callable[[ServerState], None]] = []
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> ServerState:
        return self._state

    @property
    def config(self) -> ServerConfig:
        return self._config

    def add_listener(self, fn: Callable[[ServerState], None]) -> None:
        self._listeners.append(fn)

    def start(self) -> None:
        with self._lock:
            if self._state in (ServerState.STARTING, ServerState.RUNNING):
                return
            self._set_state(ServerState.STARTING)
        threading.Thread(target=self._do_start, daemon=True, name="server-start").start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2)
            self._monitor_thread = None

        proc = self._wstunnel
        if proc is not None:
            self._wstunnel = None
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            except Exception:
                log.exception("Error stopping wstunnel")
            log.info("wstunnel stopped")

        try:
            get_server_platform().uninstall_wg_config()
            log.info("WireGuard server interface stopped")
        except PlatformError as exc:
            log.warning("Could not stop WireGuard: %s", exc)

        self._set_state(ServerState.STOPPED)

    def restart(self) -> None:
        self.stop()
        self._stop_event.clear()
        self.start()

    def add_client(self, name: str) -> Path:
        """Generate a new client, update server config, return path to .warpcfg."""
        config = self._config

        for c in config.clients:
            if c.name == name:
                raise ValueError(f"Client '{name}' already exists")

        wg_bin = shutil.which("wg")
        private_key, public_key = generate_wg_keypair(Path(wg_bin) if wg_bin else None)

        allocated = [c.address for c in config.clients]
        try:
            client_address = next_available_ip(config.subnet, config.server_address, allocated)
        except PoolExhaustedError as exc:
            raise ValueError(str(exc)) from exc

        try:
            add_peer_live(public_key, client_address)
        except Exception as exc:
            log.warning("Could not hot-add peer (WireGuard may not be running): %s", exc)

        new_client = ClientEntry(name=name, public_key=public_key, address=client_address)
        updated = replace(config, clients=[*config.clients, new_client])
        updated.save(default_config_path())
        self._config = updated

        # Persist updated WG config (hot-reload or full restart)
        try:
            get_server_platform().install_wg_config(_get_wg_conf(updated))
        except PlatformError as exc:
            log.warning("Could not persist WG config: %s", exc)

        warpcfg = build_warpcfg(config, name, private_key, client_address)
        warpcfg_path = Path.cwd() / f"{name}.warpcfg"
        write_warpcfg(warpcfg, warpcfg_path)
        log.info("Client '%s' added — .warpcfg at %s", name, warpcfg_path)
        return warpcfg_path

    def revoke_client(self, name: str) -> None:
        config = self._config

        target = next((c for c in config.clients if c.name == name), None)
        if target is None:
            raise ValueError(f"Client '{name}' not found")

        try:
            remove_peer_live(target.public_key)
        except Exception as exc:
            log.warning("Could not hot-remove peer: %s", exc)

        updated = replace(config, clients=[c for c in config.clients if c.name != name])
        updated.save(default_config_path())
        self._config = updated

        try:
            get_server_platform().install_wg_config(_get_wg_conf(updated))
        except PlatformError as exc:
            log.warning("Could not persist WG config: %s", exc)

        log.info("Client '%s' revoked", name)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _set_state(self, state: ServerState) -> None:
        if state == self._state:
            return
        self._state = state
        log.debug("Server state → %s", state.value)
        for fn in list(self._listeners):
            try:
                fn(state)
            except Exception:
                log.exception("State listener raised")

    def _do_start(self) -> None:
        try:
            platform = get_server_platform()

            # OS-level setup (IP forwarding, NAT, firewall) — idempotent no-op on Linux/macOS.
            try:
                platform.prepare_system(self._config.subnet, self._config.port)
            except PlatformError as exc:
                log.warning("prepare_system: %s", exc)

            log.info("Installing WireGuard server interface")
            try:
                platform.install_wg_config(_get_wg_conf(self._config))
            except PlatformError as exc:
                log.error("WireGuard setup failed: %s", exc)
                self._set_state(ServerState.ERROR)
                return

            wstunnel_bin = shutil.which("wstunnel")
            if wstunnel_bin is None:
                log.error("wstunnel binary not found in PATH")
                self._set_state(ServerState.ERROR)
                return

            cmd = _build_wstunnel_command(self._config, Path(wstunnel_bin))
            log.info("Starting wstunnel: %s", " ".join(cmd))
            self._wstunnel = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
            threading.Thread(
                target=self._read_output,
                args=(self._wstunnel,),
                daemon=True,
                name="wstunnel-log",
            ).start()

            self._set_state(ServerState.RUNNING)
            log.info("Server running (wstunnel pid=%d)", self._wstunnel.pid)

            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="server-monitor"
            )
            self._monitor_thread.start()

        except Exception:
            log.exception("Unexpected error starting server")
            self._set_state(ServerState.ERROR)

    def _read_output(self, proc: subprocess.Popen) -> None:
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    log.info("[wstunnel] %s", line)
        except Exception:
            pass

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(_MONITOR_INTERVAL):
            if self._state != ServerState.RUNNING:
                break
            proc = self._wstunnel
            if proc is not None and proc.poll() is not None:
                log.error("wstunnel exited unexpectedly (code=%d)", proc.returncode)
                self._set_state(ServerState.ERROR)
                break
