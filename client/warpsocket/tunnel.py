from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from platformdirs import user_data_dir

from warpsocket.config import ClientConfig
from warpsocket.network import NetworkError, tcp_probe, verify_tls_fingerprint
from warpsocket.platforms import Platform, get_platform
from warpsocket.wireguard import build_wg_conf

_APP_NAME = "WarpSocket"
_ENV_OVERRIDE = "WARPSOCKET_WSTUNNEL"

log = logging.getLogger(__name__)


class TunnelState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


StateListener = Callable[[TunnelState], None]


class TunnelError(RuntimeError):
    pass


def find_wstunnel() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        p = Path(override)
        if not p.exists():
            raise TunnelError(f"{_ENV_OVERRIDE} points to {override}, which does not exist")
        return p

    binary_name = "wstunnel.exe" if sys.platform == "win32" else "wstunnel"
    standard = Path(user_data_dir(_APP_NAME)) / "bin" / binary_name
    if standard.exists():
        return standard

    on_path = shutil.which("wstunnel")
    if on_path:
        return Path(on_path)

    raise TunnelError(
        f"wstunnel binary not found. Looked at: ${_ENV_OVERRIDE}, {standard}, $PATH. "
        "Install it via the WarpSocket installer or download from "
        "https://github.com/erebe/wstunnel/releases"
    )


def build_wstunnel_command(config: ClientConfig, wstunnel_bin: Path) -> list[str]:
    t = config.tunnel
    s = config.server
    forward = (
        f"udp://127.0.0.1:{t.local_port}:{t.remote_host}:{t.remote_port}?timeout_sec=0"
    )
    # TLS cert verification is disabled by default in wstunnel v10+; fingerprint
    # pinning via verify_tls_fingerprint() is the actual identity check.
    return [
        str(wstunnel_bin),
        "client",
        "-L",
        forward,
        "--http-upgrade-path-prefix",
        s.http_upgrade_path_prefix,
        f"wss://{s.endpoint}:{s.port}",
    ]


class Tunnel:
    def __init__(
        self,
        config: ClientConfig,
        platform: Platform | None = None,
        wstunnel_bin: Path | None = None,
    ) -> None:
        self._config = config
        self._platform = platform or get_platform()
        self._wstunnel_bin = wstunnel_bin or find_wstunnel()
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_thread: Thread | None = None
        self._installed_routes: list[str] = []
        self._wg_installed = False

    def _drain_stdout(self) -> None:
        assert self._proc is not None
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                stripped = line.rstrip()
                if stripped:
                    log.info("wstunnel: %s", stripped)
        except Exception:
            pass

    def connect(self) -> None:
        s = self._config.server
        if not tcp_probe(s.endpoint, s.port):
            raise TunnelError(
                f"Cannot reach {s.endpoint}:{s.port}. The server may be down, the port "
                "may not be open in the server firewall, or your network may block "
                "outbound connections to that port."
            )

        try:
            verify_tls_fingerprint(s.endpoint, s.port, self._config.tls.cert_fingerprint_sha256)
        except NetworkError as exc:
            raise TunnelError(str(exc)) from exc

        gateway = self._platform.get_default_gateway()
        for ip in self._config.routing.bypass_ips:
            self._platform.add_host_route(ip, gateway)
            self._installed_routes.append(ip)

        try:
            wg_conf = build_wg_conf(self._config)
            self._platform.install_wg_tunnel(self._config.wireguard.tunnel_name, wg_conf)
            self._wg_installed = True

            cmd = build_wstunnel_command(self._config, self._wstunnel_bin)
            log.info("Starting wstunnel: %s", " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._stdout_thread = Thread(
                target=self._drain_stdout, daemon=True, name="wstunnel-stdout"
            )
            self._stdout_thread.start()
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            except Exception as exc:
                log.warning("Error terminating wstunnel: %s", exc)
            finally:
                if self._stdout_thread is not None:
                    self._stdout_thread.join(timeout=2)
                    self._stdout_thread = None
            self._proc = None

        if self._wg_installed:
            try:
                self._platform.uninstall_wg_tunnel(self._config.wireguard.tunnel_name)
            except Exception as exc:
                log.warning("Error uninstalling WG tunnel: %s", exc)
            self._wg_installed = False

        for ip in self._installed_routes:
            try:
                self._platform.remove_host_route(ip)
            except Exception as exc:
                log.warning("Error removing host route %s: %s", ip, exc)
        self._installed_routes.clear()

    @property
    def is_active(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        return self._platform.is_wg_tunnel_active(self._config.wireguard.tunnel_name)


def _pick_delay(delays: list[int], attempt_just_failed: int) -> int:
    if not delays:
        return 5
    idx = min(max(attempt_just_failed - 1, 0), len(delays) - 1)
    return delays[idx]


class TunnelManager:
    def __init__(
        self,
        config: ClientConfig,
        tunnel: Tunnel | None = None,
        *,
        stability_seconds: float = 30.0,
        poll_interval: float = 1.0,
    ) -> None:
        self._config = config
        self._tunnel = tunnel or Tunnel(config)
        self._stability = stability_seconds
        self._poll = poll_interval
        self._state = TunnelState.DISCONNECTED
        self._lock = Lock()
        self._listeners: list[StateListener] = []
        self._stop_event = Event()
        self._thread: Thread | None = None

    @property
    def state(self) -> TunnelState:
        with self._lock:
            return self._state

    def add_listener(self, callback: StateListener) -> None:
        with self._lock:
            self._listeners.append(callback)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = Thread(target=self._run, daemon=True, name="warpsocket-tunnel")
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        try:
            self._tunnel.disconnect()
        except Exception:
            log.exception("Error while disconnecting tunnel during stop()")
        self._set_state(TunnelState.DISCONNECTED)

    def _set_state(self, state: TunnelState) -> None:
        with self._lock:
            if self._state == state:
                return
            self._state = state
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(state)
            except Exception:
                log.exception("State listener raised")

    def _run(self) -> None:
        attempt = 0
        max_attempts = self._config.reconnect.max_attempts
        delays = self._config.reconnect.delays_seconds

        while not self._stop_event.is_set():
            self._set_state(
                TunnelState.CONNECTING if attempt == 0 else TunnelState.RECONNECTING
            )
            try:
                self._tunnel.connect()
            except Exception as exc:
                log.warning("Connect attempt %d failed: %s", attempt + 1, exc)
                attempt += 1
                if attempt >= max_attempts:
                    self._set_state(TunnelState.FAILED)
                    return
                if self._stop_event.wait(_pick_delay(delays, attempt)):
                    return
                continue

            self._set_state(TunnelState.CONNECTED)
            connected_at = time.monotonic()
            stability_reset = False

            while not self._stop_event.is_set():
                if not self._tunnel.is_active:
                    break
                if not stability_reset and time.monotonic() - connected_at >= self._stability:
                    attempt = 0
                    stability_reset = True
                if self._stop_event.wait(self._poll):
                    return

            if self._stop_event.is_set():
                return

            log.warning("Tunnel died unexpectedly; cleaning up before retry")
            try:
                self._tunnel.disconnect()
            except Exception:
                log.exception("Cleanup after unexpected tunnel death failed")

            attempt += 1
            if attempt >= max_attempts:
                self._set_state(TunnelState.FAILED)
                return
            if self._stop_event.wait(_pick_delay(delays, attempt)):
                return
