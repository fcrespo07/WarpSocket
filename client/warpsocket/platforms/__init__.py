from __future__ import annotations

import sys

from .base import Platform, PlatformError


def get_platform() -> Platform:
    if sys.platform == "win32":
        from .windows import WindowsPlatform
        return WindowsPlatform()
    if sys.platform == "darwin":
        from .macos import MacOSPlatform
        return MacOSPlatform()
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatform
        return LinuxPlatform()
    raise PlatformError(f"Unsupported platform: {sys.platform!r}")


__all__ = ["Platform", "PlatformError", "get_platform"]
