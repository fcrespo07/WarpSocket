from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from platformdirs import user_data_dir

from warpsocket.config import ClientConfig
from warpsocket.network import NetworkError, tcp_probe, verify_tls_fingerprint
from warpsocket.platforms import Platform, get_platform
from warpsocket.wireguard import build_wg_conf

_APP_NAME = "WarpSocket"
_ENV_OVERRIDE = "WARPSOCKET_WSTUNNEL"

log = logging.getLogger(__name__)


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
        self._installed_routes: list[str] = []
        self._wg_installed = False

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
