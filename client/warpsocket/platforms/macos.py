from __future__ import annotations

from pathlib import Path

from .base import Platform, PlatformError

_NOT_IMPLEMENTED = "macOS platform support is not implemented yet"


class MacOSPlatform(Platform):
    def install_wg_tunnel(self, name: str, config_text: str) -> Path:
        raise PlatformError(_NOT_IMPLEMENTED)

    def uninstall_wg_tunnel(self, name: str) -> None:
        raise PlatformError(_NOT_IMPLEMENTED)

    def is_wg_tunnel_active(self, name: str) -> bool:
        raise PlatformError(_NOT_IMPLEMENTED)

    def get_default_gateway(self) -> str:
        raise PlatformError(_NOT_IMPLEMENTED)

    def add_host_route(self, ip: str, gateway: str) -> None:
        raise PlatformError(_NOT_IMPLEMENTED)

    def remove_host_route(self, ip: str) -> None:
        raise PlatformError(_NOT_IMPLEMENTED)
