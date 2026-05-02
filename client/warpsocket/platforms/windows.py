from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from .base import Platform, PlatformError

log = logging.getLogger(__name__)

_WIREGUARD_EXE = Path(r"C:\Program Files\WireGuard\wireguard.exe")
# C:\ProgramData\WireGuard is where wireguard.exe /installtunnelservice expects
# to find conf files — the service runs as LocalSystem and cannot access %LOCALAPPDATA%.
_WG_CONF_DIR = Path(r"C:\ProgramData\WireGuard")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(*args, capture_output=True, text=True,
                          creationflags=_NO_WINDOW, **kwargs)


class WindowsPlatform(Platform):
    def __init__(self) -> None:
        self._conf_dir = _WG_CONF_DIR

    def install_wg_tunnel(self, name: str, config_text: str) -> Path:
        self._require_wireguard()
        # Clean up any stale service left over from a previous session.
        stale = _run(["sc", "query", f"WireGuardTunnel${name}"])
        if stale.returncode == 0:
            log.warning("Stale WireGuard service found for '%s'; uninstalling before reinstall", name)
            self.uninstall_wg_tunnel(name)

        self._conf_dir.mkdir(parents=True, exist_ok=True)
        conf_path = self._conf_dir / f"{name}.conf"
        conf_path.write_text(config_text, encoding="utf-8")
        result = _run([str(_WIREGUARD_EXE), "/installtunnelservice", str(conf_path)])
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to install WireGuard tunnel '{name}': "
                f"{(result.stderr or result.stdout).strip()}"
            )
        if result.stdout.strip():
            log.info("wireguard /installtunnelservice: %s", result.stdout.strip())
        # /installtunnelservice returns before the service reaches RUNNING; poll until it does
        # so that is_wg_tunnel_active() returns True by the time wstunnel starts.
        deadline = time.monotonic() + 15.0
        last_state = "(no output)"
        while time.monotonic() < deadline:
            query = _run(["sc", "query", f"WireGuardTunnel${name}"])
            last_state = query.stdout.strip() or f"(rc={query.returncode})"
            if "RUNNING" in query.stdout:
                break
            time.sleep(0.25)
        else:
            log.warning("WireGuard service state at timeout:\n%s", last_state)
            _run([str(_WIREGUARD_EXE), "/uninstalltunnelservice", name])
            raise PlatformError(
                f"WireGuard tunnel '{name}' did not reach RUNNING within 15 s — "
                "ensure the client runs as Administrator and WireGuard is fully installed."
            )
        return conf_path

    def uninstall_wg_tunnel(self, name: str) -> None:
        if not _WIREGUARD_EXE.exists():
            return
        _run([str(_WIREGUARD_EXE), "/uninstalltunnelservice", name])
        # /uninstalltunnelservice is async — poll until SCM confirms the service is gone.
        # After 5 s send an explicit sc stop in case the service is stuck in STOP_PENDING.
        deadline = time.monotonic() + 20.0
        nudge_at = time.monotonic() + 5.0
        nudged = False
        while time.monotonic() < deadline:
            if _run(["sc", "query", f"WireGuardTunnel${name}"]).returncode != 0:
                break
            if not nudged and time.monotonic() >= nudge_at:
                log.warning("WireGuard service '%s' still stopping; sending sc stop nudge", name)
                _run(["sc", "stop", f"WireGuardTunnel${name}"])
                nudged = True
            time.sleep(0.25)
        else:
            log.warning("WireGuard tunnel '%s' did not stop within timeout; routes may flap", name)
        conf_path = self._conf_dir / f"{name}.conf"
        conf_path.unlink(missing_ok=True)

    def is_wg_tunnel_active(self, name: str) -> bool:
        return "RUNNING" in _run(["sc", "query", f"WireGuardTunnel${name}"]).stdout

    def get_default_gateway(self) -> str:
        result = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | "
            "Sort-Object -Property RouteMetric | Select-Object -First 1).NextHop",
        ])
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to query default gateway: {(result.stderr or result.stdout).strip()}"
            )
        gateway = result.stdout.strip()
        if not _IPV4_RE.match(gateway):
            raise PlatformError(f"Could not parse default gateway from output: {result.stdout!r}")
        return gateway

    def add_host_route(self, ip: str, gateway: str) -> None:
        result = _run(["route", "add", ip, "MASK", "255.255.255.255", gateway])
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to add host route {ip} via {gateway}: "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def remove_host_route(self, ip: str) -> None:
        _run(["route", "delete", ip])

    def _require_wireguard(self) -> None:
        if not _WIREGUARD_EXE.exists():
            raise PlatformError(
                f"WireGuard for Windows not found at {_WIREGUARD_EXE}. "
                "Install it from https://www.wireguard.com/install/ and retry."
            )
