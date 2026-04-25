from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warpsocket_server.config import ServerConfig


def build_warpcfg(
    server_config: ServerConfig,
    client_name: str,
    client_private_key: str,
    client_address: str,
) -> dict[str, Any]:
    """Build the .warpcfg dict that the client expects."""
    return {
        "schema_version": 1,
        "server": {
            "endpoint": server_config.endpoint,
            "port": server_config.port,
            "http_upgrade_path_prefix": server_config.http_upgrade_path_prefix,
        },
        "tls": {
            "cert_fingerprint_sha256": server_config.cert_fingerprint_sha256,
        },
        "tunnel": {
            "local_port": server_config.wg_listen_port,
            "remote_host": server_config.server_address.split("/")[0],
            "remote_port": server_config.wg_listen_port,
        },
        "wireguard": {
            "tunnel_name": "WarpSocket",
            "client_address": client_address,
            "client_private_key": client_private_key,
            "server_public_key": server_config.wg_public_key,
            "dns": ["1.1.1.1"],
        },
        "routing": {
            "bypass_ips": [server_config.endpoint],
        },
        "reconnect": {
            "max_attempts": 5,
            "delays_seconds": [5, 10, 20, 30, 60],
        },
    }


def write_warpcfg(warpcfg: dict[str, Any], path: Path) -> None:
    """Write a .warpcfg file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(warpcfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
