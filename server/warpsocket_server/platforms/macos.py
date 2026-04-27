from __future__ import annotations

from pathlib import Path

from warpsocket_server.platforms.base import PlatformError, ServerPlatform


class MacOSServerPlatform(ServerPlatform):
    def install_wstunnel_service(
        self,
        port: int,
        cert_path: Path,
        key_path: Path,
        upgrade_path: str,
        wg_listen_port: int,
        wstunnel_bin: Path,
    ) -> None:
        raise PlatformError("macOS server support not implemented yet")

    def uninstall_wstunnel_service(self) -> None:
        raise PlatformError("macOS server support not implemented yet")

    def is_wstunnel_running(self) -> bool:
        raise PlatformError("macOS server support not implemented yet")

    def restart_wstunnel_service(self) -> None:
        raise PlatformError("macOS server support not implemented yet")

    def install_wg_config(self, conf_text: str, interface: str = "wg0") -> None:
        raise PlatformError("macOS server support not implemented yet")

    def reload_wg(self, interface: str = "wg0") -> None:
        raise PlatformError("macOS server support not implemented yet")

    def is_wg_active(self, interface: str = "wg0") -> bool:
        raise PlatformError("macOS server support not implemented yet")

    def uninstall_wg_config(self, interface: str = "wg0") -> None:
        raise PlatformError("macOS server support not implemented yet")

    def restart_wg(self, interface: str = "wg0") -> None:
        raise PlatformError("macOS server support not implemented yet")

    def wg_config_dir(self) -> Path:
        return Path("/usr/local/etc/wireguard")
