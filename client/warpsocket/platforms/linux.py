from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .base import Platform, PlatformError

# Directory where wg-quick reads tunnel configs by default. The privileged
# helper writes here; LinuxPlatform itself never touches it directly.
_WG_CONF_DIR = Path("/etc/wireguard")

# Default install location of the privileged helper script. The installer
# drops this; tests / dev environments override via WARPSOCKET_HELPER.
_DEFAULT_HELPER = Path("/usr/local/libexec/warpsocket-priv")

_IP = "ip"
_DEFAULT_ROUTE_RE = re.compile(r"^default\s+via\s+(\S+)")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
# `ip route add` returns this on stderr when the route is already present.
# Treating it as success keeps add_host_route idempotent like its Windows peer.
_ROUTE_EXISTS = "File exists"


class LinuxPlatform(Platform):
    """Linux client platform.

    All privileged operations (writing to /etc/wireguard, wg-quick up/down,
    `ip route add/del`) are funnelled through a single helper script. The
    installer drops a sudoers.d rule whitelisting just that helper for the
    desktop user, so `sudo -n` never prompts.

    Running the tray as root (e.g. `sudo warpsocket`) also works: when
    `os.geteuid() == 0`, the sudo prefix is skipped and the helper is invoked
    directly.
    """

    def __init__(
        self,
        *,
        helper: Path | None = None,
        sudo: bool | None = None,
    ) -> None:
        env_helper = os.environ.get("WARPSOCKET_HELPER")
        self._helper = helper or (Path(env_helper) if env_helper else _DEFAULT_HELPER)
        if sudo is None:
            sudo = hasattr(os, "geteuid") and os.geteuid() != 0
        self._sudo_prefix: list[str] = ["sudo", "-n"] if sudo else []

    # ------------------------------------------------------------------
    # WireGuard tunnel
    # ------------------------------------------------------------------
    def install_wg_tunnel(self, name: str, config_text: str) -> Path:
        self._require_helper()
        result = self._run_helper("up", name, input_text=config_text)
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to bring up WireGuard tunnel '{name}': "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return _WG_CONF_DIR / f"{name}.conf"

    def uninstall_wg_tunnel(self, name: str) -> None:
        if not self._helper.exists() and not os.environ.get("WARPSOCKET_HELPER"):
            return
        self._run_helper("down", name)

    def is_wg_tunnel_active(self, name: str) -> bool:
        if not self._helper.exists() and not os.environ.get("WARPSOCKET_HELPER"):
            return False
        return self._run_helper("is-active", name).returncode == 0

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def get_default_gateway(self) -> str:
        # `ip route show` is unprivileged — no helper needed.
        result = subprocess.run(
            [_IP, "-4", "route", "show", "default"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PlatformError(
                f"Failed to query default gateway: {(result.stderr or result.stdout).strip()}"
            )
        for line in result.stdout.splitlines():
            match = _DEFAULT_ROUTE_RE.match(line.strip())
            if match and _IPV4_RE.match(match.group(1)):
                return match.group(1)
        raise PlatformError(f"Could not parse default gateway from output: {result.stdout!r}")

    def add_host_route(self, ip: str, gateway: str) -> None:
        self._require_helper()
        result = self._run_helper("route-add", ip, gateway)
        if result.returncode == 0:
            return
        if _ROUTE_EXISTS in (result.stderr or ""):
            return
        raise PlatformError(
            f"Failed to add host route {ip} via {gateway}: "
            f"{(result.stderr or result.stdout).strip()}"
        )

    def remove_host_route(self, ip: str) -> None:
        if not self._helper.exists() and not os.environ.get("WARPSOCKET_HELPER"):
            return
        self._run_helper("route-del", ip)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_helper(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = [*self._sudo_prefix, str(self._helper), *args]
        return subprocess.run(cmd, capture_output=True, text=True, input=input_text)

    def _require_helper(self) -> None:
        # Skip the existence check when an env override is set (tests / dev mode).
        if os.environ.get("WARPSOCKET_HELPER"):
            return
        if not self._helper.exists():
            raise PlatformError(
                f"Privileged helper not found at {self._helper}. "
                "Re-run the WarpSocket installer to (re)install it."
            )
        if self._sudo_prefix and not shutil.which("sudo"):
            raise PlatformError(
                "sudo is required when running the tray as a non-root user. "
                "Install sudo or run the tray with elevated privileges."
            )
