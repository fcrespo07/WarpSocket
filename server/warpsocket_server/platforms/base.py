from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class PlatformError(RuntimeError):
    pass


class ServerPlatform(ABC):
    """Abstract interface for server-side service management."""

    @abstractmethod
    def install_wstunnel_service(
        self,
        port: int,
        cert_path: Path,
        key_path: Path,
        upgrade_path: str,
        wg_listen_port: int,
        wstunnel_bin: Path,
    ) -> None:
        ...

    @abstractmethod
    def uninstall_wstunnel_service(self) -> None:
        ...

    @abstractmethod
    def is_wstunnel_running(self) -> bool:
        ...

    @abstractmethod
    def restart_wstunnel_service(self) -> None:
        ...

    @abstractmethod
    def install_wg_config(self, conf_text: str, interface: str = "wg0") -> None:
        ...

    @abstractmethod
    def reload_wg(self, interface: str = "wg0") -> None:
        ...

    @abstractmethod
    def is_wg_active(self, interface: str = "wg0") -> bool:
        ...

    @abstractmethod
    def restart_wg(self, interface: str = "wg0") -> None:
        """Bring the WireGuard interface fully down and up again.

        Required when PostUp/PostDown rules in the config have changed —
        `wg syncconf` does a hot reload but doesn't re-run those scripts.
        """
        ...

    @abstractmethod
    def uninstall_wg_config(self, interface: str = "wg0") -> None:
        ...

    @abstractmethod
    def wg_config_dir(self) -> Path:
        ...
