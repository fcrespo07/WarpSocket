from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from warpsocket_server.platforms.base import PlatformError, ServerPlatform

log = logging.getLogger(__name__)

_SERVICE_NAME = "wstunnel-warpsocket.service"
_SERVICE_PATH = Path("/etc/systemd/system") / _SERVICE_NAME

_UNIT_TEMPLATE = """\
[Unit]
Description=WarpSocket wstunnel server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={wstunnel_bin} server \
--restrict-to 127.0.0.1:{wg_listen_port} \
--tls-certificate {cert_path} \
--tls-private-key {key_path} \
--restrict-http-upgrade-path-prefix {upgrade_path} \
wss://0.0.0.0:{port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


class LinuxServerPlatform(ServerPlatform):
    def install_wstunnel_service(
        self,
        port: int,
        cert_path: Path,
        key_path: Path,
        upgrade_path: str,
        wg_listen_port: int,
        wstunnel_bin: Path,
    ) -> None:
        unit = _UNIT_TEMPLATE.format(
            wstunnel_bin=wstunnel_bin,
            cert_path=cert_path,
            key_path=key_path,
            upgrade_path=upgrade_path,
            wg_listen_port=wg_listen_port,
            port=port,
        )
        try:
            _SERVICE_PATH.write_text(unit, encoding="utf-8")
            os.chmod(_SERVICE_PATH, 0o644)
        except OSError as exc:
            raise PlatformError(f"Failed to write systemd unit: {exc}") from exc

        try:
            _run(["systemctl", "daemon-reload"])
            _run(["systemctl", "enable", "--now", _SERVICE_NAME])
        except subprocess.CalledProcessError as exc:
            raise PlatformError(
                f"Failed to enable wstunnel service: {exc.stderr.strip()}"
            ) from exc

        log.info("Installed wstunnel service: %s", _SERVICE_PATH)

    def uninstall_wstunnel_service(self) -> None:
        try:
            _run(["systemctl", "disable", "--now", _SERVICE_NAME], check=False)
            if _SERVICE_PATH.exists():
                _SERVICE_PATH.unlink()
            _run(["systemctl", "daemon-reload"], check=False)
        except OSError as exc:
            raise PlatformError(f"Failed to remove unit file: {exc}") from exc

    def is_wstunnel_running(self) -> bool:
        result = _run(["systemctl", "is-active", _SERVICE_NAME], check=False)
        return result.stdout.strip() == "active"

    def restart_wstunnel_service(self) -> None:
        try:
            _run(["systemctl", "restart", _SERVICE_NAME])
        except subprocess.CalledProcessError as exc:
            raise PlatformError(f"Failed to restart wstunnel: {exc.stderr.strip()}") from exc

    def install_wg_config(self, conf_text: str, interface: str = "wg0") -> None:
        conf_path = self.wg_config_dir() / f"{interface}.conf"
        try:
            conf_path.write_text(conf_text, encoding="utf-8")
            os.chmod(conf_path, 0o600)
        except OSError as exc:
            raise PlatformError(f"Failed to write WG config: {exc}") from exc

        unit = f"wg-quick@{interface}.service"
        try:
            if self.is_wg_active(interface):
                self.reload_wg(interface)
                _run(["systemctl", "enable", unit], check=False)
            else:
                _run(["systemctl", "enable", "--now", unit])
        except subprocess.CalledProcessError as exc:
            raise PlatformError(
                f"Failed to bring up WireGuard {interface}: {exc.stderr.strip()}"
            ) from exc

    def reload_wg(self, interface: str = "wg0") -> None:
        conf_path = self.wg_config_dir() / f"{interface}.conf"
        try:
            stripped = _run(["wg-quick", "strip", str(conf_path)])
            subprocess.run(
                ["wg", "syncconf", interface, "/dev/stdin"],
                input=stripped.stdout,
                text=True,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PlatformError(f"wg syncconf failed: {exc.stderr.strip()}") from exc

    def is_wg_active(self, interface: str = "wg0") -> bool:
        return Path(f"/sys/class/net/{interface}").exists()

    def wg_config_dir(self) -> Path:
        return Path("/etc/wireguard")
