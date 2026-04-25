from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class PlatformError(RuntimeError):
    pass


class Platform(ABC):
    @abstractmethod
    def install_wg_tunnel(self, name: str, config_text: str) -> Path:
        ...

    @abstractmethod
    def uninstall_wg_tunnel(self, name: str) -> None:
        ...

    @abstractmethod
    def is_wg_tunnel_active(self, name: str) -> bool:
        ...

    @abstractmethod
    def get_default_gateway(self) -> str:
        ...

    @abstractmethod
    def add_host_route(self, ip: str, gateway: str) -> None:
        ...

    @abstractmethod
    def remove_host_route(self, ip: str) -> None:
        ...
