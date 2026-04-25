from __future__ import annotations

import sys

from warpsocket_server.platforms.base import PlatformError, ServerPlatform


def get_server_platform() -> ServerPlatform:
    if sys.platform == "win32":
        from warpsocket_server.platforms.windows import WindowsServerPlatform
        return WindowsServerPlatform()
    if sys.platform == "darwin":
        from warpsocket_server.platforms.macos import MacOSServerPlatform
        return MacOSServerPlatform()
    from warpsocket_server.platforms.linux import LinuxServerPlatform
    return LinuxServerPlatform()


__all__ = ["ServerPlatform", "PlatformError", "get_server_platform"]
