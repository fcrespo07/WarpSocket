from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from platformdirs import user_data_dir

from .base import Platform, PlatformError

log = logging.getLogger(__name__)

_APP_NAME = "WarpSocket"
_WIREGUARD_EXE = Path(r"C:\Program Files\WireGuard\wireguard.exe")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class WindowsPlatform(Platform):
    def __init__(self) -> None:
        self._conf_dir = Path(user_data_dir(_APP_NAME)) / "wireguard"

    def install_wg_tunnel(self, name: str, config_text: str) -> Path:
        self._require_wireguard()
        self._conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = self._conf_dir / f"{name}.conf"
        conf_path.write_text(config_text, encoding="utf-8")
        result = subprocess.run(
            [str(_WIREGUARD_EXE), "/installtunnelservice", str(conf_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to install WireGuard tunnel '{name}': "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return conf_path

    def uninstall_wg_tunnel(self, name: str) -> None:
        if not _WIREGUARD_EXE.exists():
            return
        subprocess.run(
            [str(_WIREGUARD_EXE), "/uninstalltunnelservice", name],
            capture_output=True,
            text=True,
        )
        # /uninstalltunnelservice is async — poll until SCM confirms the service is gone
        # so callers can safely remove bypass routes without the WG interface still routing traffic.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["sc", "query", f"WireGuardTunnel${name}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                break
            time.sleep(0.25)
        else:
            log.warning("WireGuard tunnel '%s' did not stop within timeout; routes may flap", name)
        conf_path = self._conf_dir / f"{name}.conf"
        conf_path.unlink(missing_ok=True)

    def is_wg_tunnel_active(self, name: str) -> bool:
        result = subprocess.run(
            ["sc", "query", f"WireGuardTunnel${name}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and "RUNNING" in result.stdout

    def get_default_gateway(self) -> str:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
                "Sort-Object -Property RouteMetric | Select-Object -First 1).NextHop",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to query default gateway: {(result.stderr or result.stdout).strip()}"
            )
        gateway = result.stdout.strip()
        if not _IPV4_RE.match(gateway):
            raise PlatformError(f"Could not parse default gateway from output: {result.stdout!r}")
        return gateway

    def add_host_route(self, ip: str, gateway: str) -> None:
        result = subprocess.run(
            ["route", "add", ip, "MASK", "255.255.255.255", gateway],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to add host route {ip} via {gateway}: "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def remove_host_route(self, ip: str) -> None:
        subprocess.run(["route", "delete", ip], capture_output=True, text=True)

    def _require_wireguard(self) -> None:
        if not _WIREGUARD_EXE.exists():
            raise PlatformError(
                f"WireGuard for Windows not found at {_WIREGUARD_EXE}. "
                "Install it from https://www.wireguard.com/install/ and retry."
            )
