from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .base import Platform, PlatformError

# Directory where the privileged helper writes tunnel configs.
# Deliberately NOT /etc/wireguard: other WireGuard-aware tools (e.g.
# omarchy-vpn) scan that directory and adopt any .conf they find as one of
# their own managed tunnels, which fights OutWarp for control of its own
# interface. LinuxPlatform itself never touches this path directly — only
# the privileged helper does.
_WG_CONF_DIR = Path("/etc/wireguard-outwarp")

# Default install location of the privileged helper script. The installer
# drops this; tests / dev environments override via OUTWARP_HELPER.
_DEFAULT_HELPER = Path("/usr/local/libexec/outwarp-priv")

_IP = "ip"
_DEFAULT_ROUTE_RE = re.compile(r"^default\s+via\s+(\S+)")
_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
# `ip route add` returns this on stderr when the route is already present.
# Treating it as success keeps add_host_route idempotent like its Windows peer.
_ROUTE_EXISTS = "File exists"

# Autostart spec: any compliant XDG desktop will pick up this file on session
# start. We use ~/.config/autostart (XDG_CONFIG_HOME) — system-wide
# /etc/xdg/autostart would need root and isn't right for a per-user toggle.
_AUTOSTART_FILENAME = "outwarp.desktop"


def _autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart"


class LinuxPlatform(Platform):
    """Linux client platform.

    All privileged operations (writing to /etc/wireguard-outwarp, wg-quick
    up/down, `ip route add/del`) are funnelled through a single helper
    script. The installer drops a sudoers.d rule whitelisting just that
    helper for the desktop user, so `sudo -n` never prompts.

    Running the tray as root (e.g. `sudo outwarp`) also works: when
    `os.geteuid() == 0`, the sudo prefix is skipped and the helper is invoked
    directly.
    """

    def __init__(
        self,
        *,
        helper: Path | None = None,
        sudo: bool | None = None,
    ) -> None:
        env_helper = os.environ.get("OUTWARP_HELPER")
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
        if not self._helper.exists() and not os.environ.get("OUTWARP_HELPER"):
            return
        self._run_helper("down", name)

    def is_wg_tunnel_active(self, name: str) -> bool:
        if not self._helper.exists() and not os.environ.get("OUTWARP_HELPER"):
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
        if not self._helper.exists() and not os.environ.get("OUTWARP_HELPER"):
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
        if os.environ.get("OUTWARP_HELPER"):
            return
        if not self._helper.exists():
            raise PlatformError(
                f"Privileged helper not found at {self._helper}. "
                "Re-run the OutWarp installer to (re)install it."
            )
        if self._sudo_prefix and not shutil.which("sudo"):
            raise PlatformError(
                "sudo is required when running the tray as a non-root user. "
                "Install sudo or run the tray with elevated privileges."
            )

    # ------------------------------------------------------------------
    # Autostart
    # ------------------------------------------------------------------
    def install_autostart(self, command: list[str]) -> None:
        if not command:
            raise PlatformError("install_autostart: empty command")
        path = _autostart_dir() / _AUTOSTART_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_desktop_entry(command), encoding="utf-8")
        except OSError as exc:
            raise PlatformError(f"Failed to write {path}: {exc}") from exc

    def uninstall_autostart(self) -> None:
        path = _autostart_dir() / _AUTOSTART_FILENAME
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise PlatformError(f"Failed to remove {path}: {exc}") from exc

    def is_autostart_installed(self) -> bool:
        return (_autostart_dir() / _AUTOSTART_FILENAME).exists()

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------
    # Backed by an nftables table the privileged helper owns
    # (killswitch-on/off/status). The output chain defaults to drop and only
    # allows loopback, established connections and the allowlist IPs (the
    # server endpoint(s)) so the wstunnel reconnect can still get out.
    def engage_kill_switch(self, allowlist_ips: list[str]) -> None:
        ips = [ip for ip in allowlist_ips if ip]
        if not ips:
            raise PlatformError(
                "engage_kill_switch refusing to run with an empty allowlist — "
                "that would block the very traffic wstunnel needs to reconnect."
            )
        self._require_helper()
        result = self._run_helper("killswitch-on", *ips)
        if result.returncode != 0:
            raise PlatformError(
                "Failed to engage kill switch: "
                f"{(result.stderr or result.stdout).strip() or 'unknown error'}. "
                "If you upgraded OutWarp, re-run the installer so the privileged "
                "helper gains the kill-switch commands; nftables must be installed."
            )

    def release_kill_switch(self) -> None:
        # Best-effort + safe to call when nothing is engaged (app.py calls it
        # unconditionally on startup). Skip silently if the helper is absent.
        if not self._helper.exists() and not os.environ.get("OUTWARP_HELPER"):
            return
        self._run_helper("killswitch-off")

    def is_kill_switch_engaged(self) -> bool:
        if not self._helper.exists() and not os.environ.get("OUTWARP_HELPER"):
            return False
        return self._run_helper("killswitch-status").returncode == 0


def _render_desktop_entry(command: list[str]) -> str:
    # shlex.join produces a POSIX-quoted command line that survives the
    # split-on-whitespace XDG handlers do for Exec= keys.
    exec_line = shlex.join(command)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=OutWarp\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=false\n"
        "Terminal=false\n"
    )
