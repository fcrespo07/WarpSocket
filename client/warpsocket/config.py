from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

_APP_NAME = "WarpSocket"
_SCHEMA_VERSION = 1
_FINGERPRINT_RE = re.compile(r"^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    endpoint: str
    port: int
    http_upgrade_path_prefix: str


@dataclass(frozen=True)
class TlsConfig:
    cert_fingerprint_sha256: str


@dataclass(frozen=True)
class TunnelConfig:
    local_port: int
    remote_host: str
    remote_port: int


@dataclass(frozen=True)
class WireguardConfig:
    tunnel_name: str
    client_address: str
    client_private_key: str
    server_public_key: str
    dns: list[str] = field(default_factory=lambda: ["1.1.1.1"])


@dataclass(frozen=True)
class RoutingConfig:
    bypass_ips: list[str]


@dataclass(frozen=True)
class ReconnectConfig:
    max_attempts: int = 5
    delays_seconds: list[int] = field(default_factory=lambda: [5, 10, 20, 30, 60])


@dataclass(frozen=True)
class ClientConfig:
    schema_version: int
    server: ServerConfig
    tls: TlsConfig
    tunnel: TunnelConfig
    wireguard: WireguardConfig
    routing: RoutingConfig
    reconnect: ReconnectConfig

    @classmethod
    def load(cls, path: Path) -> ClientConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {path}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Config file is not valid JSON: {exc}")

        return _parse(raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_to_dict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def default_config_path() -> Path:
    return Path(user_config_dir(_APP_NAME)) / "config.json"


def import_warpcfg(warpcfg_path: Path, dest: Path | None = None) -> ClientConfig:
    config = ClientConfig.load(warpcfg_path)
    target = dest or default_config_path()
    config.save(target)
    return config


# --- internal helpers ---

def _require(data: dict[str, Any], key: str, section: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required field '{key}' in section '{section}'")
    return data[key]


def _parse(raw: dict[str, Any]) -> ClientConfig:
    version = raw.get("schema_version", 1)
    if version != _SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version {version!r} (expected {_SCHEMA_VERSION}). "
            "Update WarpSocket client to a newer version."
        )

    server = _parse_server(_require(raw, "server", "root"))
    tls = _parse_tls(_require(raw, "tls", "root"))
    tunnel = _parse_tunnel(_require(raw, "tunnel", "root"))
    wireguard = _parse_wireguard(_require(raw, "wireguard", "root"))
    routing = _parse_routing(_require(raw, "routing", "root"))
    reconnect = _parse_reconnect(raw.get("reconnect", {}))

    return ClientConfig(
        schema_version=version,
        server=server,
        tls=tls,
        tunnel=tunnel,
        wireguard=wireguard,
        routing=routing,
        reconnect=reconnect,
    )


def _parse_server(d: Any) -> ServerConfig:
    if not isinstance(d, dict):
        raise ConfigError("Section 'server' must be an object")
    endpoint = _require(d, "endpoint", "server")
    port = _require(d, "port", "server")
    prefix = _require(d, "http_upgrade_path_prefix", "server")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError(f"server.port must be an integer between 1 and 65535, got {port!r}")
    return ServerConfig(endpoint=str(endpoint), port=port, http_upgrade_path_prefix=str(prefix))


def _parse_tls(d: Any) -> TlsConfig:
    if not isinstance(d, dict):
        raise ConfigError("Section 'tls' must be an object")
    fp = str(_require(d, "cert_fingerprint_sha256", "tls"))
    if not _FINGERPRINT_RE.match(fp):
        raise ConfigError(
            f"tls.cert_fingerprint_sha256 must be a colon-separated SHA-256 hex string "
            f"(e.g. 'AB:CD:...'), got {fp!r}"
        )
    return TlsConfig(cert_fingerprint_sha256=fp)


def _parse_tunnel(d: Any) -> TunnelConfig:
    if not isinstance(d, dict):
        raise ConfigError("Section 'tunnel' must be an object")
    local_port = _require(d, "local_port", "tunnel")
    remote_host = _require(d, "remote_host", "tunnel")
    remote_port = _require(d, "remote_port", "tunnel")
    for name, val in (("local_port", local_port), ("remote_port", remote_port)):
        if not isinstance(val, int) or not (1 <= val <= 65535):
            raise ConfigError(f"tunnel.{name} must be an integer between 1 and 65535, got {val!r}")
    return TunnelConfig(
        local_port=local_port,
        remote_host=str(remote_host),
        remote_port=remote_port,
    )


def _parse_wireguard(d: Any) -> WireguardConfig:
    if not isinstance(d, dict):
        raise ConfigError("Section 'wireguard' must be an object")
    return WireguardConfig(
        tunnel_name=str(_require(d, "tunnel_name", "wireguard")),
        client_address=str(_require(d, "client_address", "wireguard")),
        client_private_key=str(_require(d, "client_private_key", "wireguard")),
        server_public_key=str(_require(d, "server_public_key", "wireguard")),
        dns=list(d.get("dns", ["1.1.1.1"])),
    )


def _parse_routing(d: Any) -> RoutingConfig:
    if not isinstance(d, dict):
        raise ConfigError("Section 'routing' must be an object")
    bypass = _require(d, "bypass_ips", "routing")
    if not isinstance(bypass, list) or not all(isinstance(ip, str) for ip in bypass):
        raise ConfigError("routing.bypass_ips must be a list of IP strings")
    return RoutingConfig(bypass_ips=bypass)


def _parse_reconnect(d: Any) -> ReconnectConfig:
    if not isinstance(d, dict):
        return ReconnectConfig()
    return ReconnectConfig(
        max_attempts=int(d.get("max_attempts", 5)),
        delays_seconds=list(d.get("delays_seconds", [5, 10, 20, 30, 60])),
    )


def _to_dict(cfg: ClientConfig) -> dict[str, Any]:
    return {
        "schema_version": cfg.schema_version,
        "server": {
            "endpoint": cfg.server.endpoint,
            "port": cfg.server.port,
            "http_upgrade_path_prefix": cfg.server.http_upgrade_path_prefix,
        },
        "tls": {
            "cert_fingerprint_sha256": cfg.tls.cert_fingerprint_sha256,
        },
        "tunnel": {
            "local_port": cfg.tunnel.local_port,
            "remote_host": cfg.tunnel.remote_host,
            "remote_port": cfg.tunnel.remote_port,
        },
        "wireguard": {
            "tunnel_name": cfg.wireguard.tunnel_name,
            "client_address": cfg.wireguard.client_address,
            "client_private_key": cfg.wireguard.client_private_key,
            "server_public_key": cfg.wireguard.server_public_key,
            "dns": cfg.wireguard.dns,
        },
        "routing": {
            "bypass_ips": cfg.routing.bypass_ips,
        },
        "reconnect": {
            "max_attempts": cfg.reconnect.max_attempts,
            "delays_seconds": cfg.reconnect.delays_seconds,
        },
    }
