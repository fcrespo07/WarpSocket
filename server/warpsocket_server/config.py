from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_APP_NAME = "WarpSocket"
_SCHEMA_VERSION = 1


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ClientEntry:
    name: str
    public_key: str
    address: str


@dataclass(frozen=True)
class ServerConfig:
    schema_version: int
    endpoint: str
    port: int
    http_upgrade_path_prefix: str
    cert_path: str
    key_path: str
    cert_fingerprint_sha256: str
    wg_private_key: str
    wg_public_key: str
    subnet: str
    server_address: str
    wg_listen_port: int
    clients: list[ClientEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> ServerConfig:
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


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(r"C:\ProgramData")
    elif sys.platform == "darwin":
        base = Path("/Library/Application Support")
    else:
        base = Path("/etc")
    return base / "warpsocket"


def default_config_path() -> Path:
    return default_config_dir() / "server_config.json"


# --- internal helpers ---


def _require(data: dict[str, Any], key: str, section: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required field '{key}' in section '{section}'")
    return data[key]


def _parse(raw: dict[str, Any]) -> ServerConfig:
    version = raw.get("schema_version", 1)
    if version != _SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version {version!r} (expected {_SCHEMA_VERSION})"
        )

    port = _require(raw, "port", "root")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError(f"port must be an integer between 1 and 65535, got {port!r}")

    wg_listen_port = _require(raw, "wg_listen_port", "root")
    if not isinstance(wg_listen_port, int) or not (1 <= wg_listen_port <= 65535):
        raise ConfigError(
            f"wg_listen_port must be an integer between 1 and 65535, got {wg_listen_port!r}"
        )

    clients_raw = raw.get("clients", [])
    if not isinstance(clients_raw, list):
        raise ConfigError("clients must be a list")
    clients = [
        ClientEntry(
            name=str(_require(c, "name", "clients[]")),
            public_key=str(_require(c, "public_key", "clients[]")),
            address=str(_require(c, "address", "clients[]")),
        )
        for c in clients_raw
    ]

    return ServerConfig(
        schema_version=version,
        endpoint=str(_require(raw, "endpoint", "root")),
        port=port,
        http_upgrade_path_prefix=str(_require(raw, "http_upgrade_path_prefix", "root")),
        cert_path=str(_require(raw, "cert_path", "root")),
        key_path=str(_require(raw, "key_path", "root")),
        cert_fingerprint_sha256=str(_require(raw, "cert_fingerprint_sha256", "root")),
        wg_private_key=str(_require(raw, "wg_private_key", "root")),
        wg_public_key=str(_require(raw, "wg_public_key", "root")),
        subnet=str(_require(raw, "subnet", "root")),
        server_address=str(_require(raw, "server_address", "root")),
        wg_listen_port=wg_listen_port,
        clients=clients,
    )


def _to_dict(cfg: ServerConfig) -> dict[str, Any]:
    return {
        "schema_version": cfg.schema_version,
        "endpoint": cfg.endpoint,
        "port": cfg.port,
        "http_upgrade_path_prefix": cfg.http_upgrade_path_prefix,
        "cert_path": cfg.cert_path,
        "key_path": cfg.key_path,
        "cert_fingerprint_sha256": cfg.cert_fingerprint_sha256,
        "wg_private_key": cfg.wg_private_key,
        "wg_public_key": cfg.wg_public_key,
        "subnet": cfg.subnet,
        "server_address": cfg.server_address,
        "wg_listen_port": cfg.wg_listen_port,
        "clients": [
            {"name": c.name, "public_key": c.public_key, "address": c.address}
            for c in cfg.clients
        ],
    }
