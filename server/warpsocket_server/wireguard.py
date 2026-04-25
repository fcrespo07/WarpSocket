from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from warpsocket_server.config import ServerConfig

log = logging.getLogger(__name__)


class WireGuardError(RuntimeError):
    pass


def build_server_wg_conf(config: ServerConfig) -> str:
    """Build the server-side wg0.conf content."""
    lines = [
        "[Interface]",
        f"PrivateKey = {config.wg_private_key}",
        f"Address = {config.server_address}",
        f"ListenPort = {config.wg_listen_port}",
    ]

    for client in config.clients:
        lines.append("")
        lines.append("[Peer]")
        lines.append(f"# {client.name}")
        lines.append(f"PublicKey = {client.public_key}")
        lines.append(f"AllowedIPs = {client.address}")

    lines.append("")
    return "\n".join(lines)


def add_peer_live(
    public_key: str,
    allowed_ips: str,
    interface: str = "wg0",
    wg_bin: Path | None = None,
) -> None:
    """Hot-add a peer to a running WireGuard interface."""
    wg = str(wg_bin or "wg")
    try:
        subprocess.run(
            [wg, "set", interface, "peer", public_key, "allowed-ips", allowed_ips],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise WireGuardError(f"Failed to add peer: {exc.stderr.strip()}") from exc
    log.info("Added peer %s with allowed-ips %s", public_key[:16] + "...", allowed_ips)


def remove_peer_live(
    public_key: str,
    interface: str = "wg0",
    wg_bin: Path | None = None,
) -> None:
    """Hot-remove a peer from a running WireGuard interface."""
    wg = str(wg_bin or "wg")
    try:
        subprocess.run(
            [wg, "set", interface, "peer", public_key, "remove"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise WireGuardError(f"Failed to remove peer: {exc.stderr.strip()}") from exc
    log.info("Removed peer %s", public_key[:16] + "...")
