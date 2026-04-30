from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from warpsocket_server.platforms.base import PlatformError, ServerPlatform

log = logging.getLogger(__name__)

_WG_EXE = Path(r"C:\Program Files\WireGuard\wireguard.exe")
_WG_DIR = Path(r"C:\ProgramData\WireGuard")
_WG_INTERFACE = "WarpSocket-Server"
_NAT_NAME = "WarpSocket"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def _run(*cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        check=check,
        creationflags=_NO_WINDOW,
    )


def _ps(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a PowerShell -Command snippet."""
    return _run(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", *args,
        check=check,
    )


class WindowsServerPlatform(ServerPlatform):
    # ── wstunnel ─────────────────────────────────────────────────────────────
    # On Windows, wstunnel runs as a subprocess owned by ServerManager.
    # These methods are intentional no-ops / read-only queries.

    def install_wstunnel_service(
        self,
        port: int,
        cert_path: Path,
        key_path: Path,
        upgrade_path: str,
        wg_listen_port: int,
        wstunnel_bin: Path,
    ) -> None:
        log.debug("install_wstunnel_service: no-op on Windows (ServerManager owns wstunnel)")

    def uninstall_wstunnel_service(self) -> None:
        log.debug("uninstall_wstunnel_service: no-op on Windows")

    def is_wstunnel_running(self) -> bool:
        result = _run("tasklist", "/FI", "IMAGENAME eq wstunnel.exe", "/NH", check=False)
        return "wstunnel" in result.stdout.lower()

    def restart_wstunnel_service(self) -> None:
        raise PlatformError(
            "Use ServerManager.restart() to restart wstunnel on Windows"
        )

    # ── WireGuard ─────────────────────────────────────────────────────────────

    def install_wg_config(self, conf_text: str, interface: str = _WG_INTERFACE) -> None:
        self._require_wireguard()
        conf_path = self.wg_config_dir() / f"{interface}.conf"
        try:
            self.wg_config_dir().mkdir(parents=True, exist_ok=True)
            conf_path.write_text(conf_text, encoding="utf-8")
        except OSError as exc:
            raise PlatformError(f"Failed to write WireGuard config: {exc}") from exc

        # Remove any stale service first so /installtunnelservice can succeed.
        if "RUNNING" in _run("sc", "query", f"WireGuardTunnel${interface}", check=False).stdout:
            log.debug("WireGuard tunnel '%s' already running; reinstalling", interface)
            _run(str(_WG_EXE), "/uninstalltunnelservice", interface, check=False)
            self._wait_service_gone(interface)

        result = _run(str(_WG_EXE), "/installtunnelservice", str(conf_path), check=False)
        if result.returncode != 0:
            raise PlatformError(
                f"wireguard.exe /installtunnelservice failed: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        self._wait_service_running(interface)
        log.info("WireGuard server interface '%s' installed and running", interface)

    def reload_wg(self, interface: str = _WG_INTERFACE) -> None:
        # WireGuard for Windows does not ship wg-quick or wg strip; do a full restart.
        self.restart_wg(interface)

    def is_wg_active(self, interface: str = _WG_INTERFACE) -> bool:
        result = _run("sc", "query", f"WireGuardTunnel${interface}", check=False)
        return "RUNNING" in result.stdout

    def uninstall_wg_config(self, interface: str = _WG_INTERFACE) -> None:
        if _WG_EXE.exists():
            _run(str(_WG_EXE), "/uninstalltunnelservice", interface, check=False)
            self._wait_service_gone(interface)
        self._remove_nat()
        conf_path = self.wg_config_dir() / f"{interface}.conf"
        conf_path.unlink(missing_ok=True)

    def restart_wg(self, interface: str = _WG_INTERFACE) -> None:
        conf_path = self.wg_config_dir() / f"{interface}.conf"
        if not conf_path.exists():
            raise PlatformError(f"WireGuard config not found: {conf_path}")
        conf_text = conf_path.read_text(encoding="utf-8")
        self.uninstall_wg_config(interface)
        self.install_wg_config(conf_text, interface)

    def wg_config_dir(self) -> Path:
        return _WG_DIR

    # ── System preparation ────────────────────────────────────────────────────

    def prepare_system(self, subnet: str, wss_port: int) -> None:
        """Enable IP routing and create NAT for the WireGuard subnet."""
        self._enable_ip_forwarding()
        self._create_nat(subnet)
        self._add_firewall_rule(wss_port)

    def _enable_ip_forwarding(self) -> None:
        try:
            import winreg
            key_path = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, key_path, access=winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, "IPEnableRouter", 0, winreg.REG_DWORD, 1)
            log.info("IP routing enabled via registry")
        except OSError as exc:
            log.warning("Could not enable IP routing in registry: %s", exc)

    def _create_nat(self, subnet: str) -> None:
        check = _ps(
            f"Get-NetNat -Name '{_NAT_NAME}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name"
        )
        if _NAT_NAME in check.stdout:
            log.debug("NetNat '%s' already exists", _NAT_NAME)
            return
        result = _ps(
            f"New-NetNat -Name '{_NAT_NAME}' -InternalIPInterfaceAddressPrefix '{subnet}'"
        )
        if result.returncode != 0:
            log.warning("Could not create NetNat for %s: %s", subnet, result.stderr.strip())
        else:
            log.info("Created NetNat '%s' for subnet %s", _NAT_NAME, subnet)

    def _remove_nat(self) -> None:
        _ps(f"Remove-NetNat -Name '{_NAT_NAME}' -Confirm:$false -ErrorAction SilentlyContinue")

    def _add_firewall_rule(self, port: int) -> None:
        _run(
            "netsh", "advfirewall", "firewall", "add", "rule",
            "name=WarpSocket-wstunnel", "dir=in", "action=allow",
            f"localport={port}", "protocol=TCP",
            check=False,
        )
        log.info("Firewall rule added for port %d/tcp", port)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _require_wireguard(self) -> None:
        if not _WG_EXE.exists():
            raise PlatformError(
                f"WireGuard for Windows not found at {_WG_EXE}. "
                "Install it from https://www.wireguard.com/install/ and retry."
            )

    def _wait_service_running(self, interface: str, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if "RUNNING" in _run("sc", "query", f"WireGuardTunnel${interface}", check=False).stdout:
                return
            time.sleep(0.25)
        log.warning("WireGuard '%s' did not reach RUNNING within %.0fs", interface, timeout)

    def _wait_service_gone(self, interface: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _run("sc", "query", f"WireGuardTunnel${interface}", check=False).returncode != 0:
                return
            time.sleep(0.25)
