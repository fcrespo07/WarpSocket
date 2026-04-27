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
    subnet = config.subnet
    lines = [
        "[Interface]",
        f"PrivateKey = {config.wg_private_key}",
        f"Address = {config.server_address}",
        f"ListenPort = {config.wg_listen_port}",
        # Enable IP forwarding and NAT so connected clients can reach the internet.
        # %i is replaced by wg-quick with the interface name (e.g. wg0).
        f"PostUp = sysctl -w net.ipv4.ip_forward=1; "
        f"iptables -A FORWARD -i %i -j ACCEPT; "
        f"iptables -A FORWARD -o %i -j ACCEPT; "
        f"iptables -t nat -A POSTROUTING -s {subnet} -j MASQUERADE",
        f"PostDown = iptables -D FORWARD -i %i -j ACCEPT; "
        f"iptables -D FORWARD -o %i -j ACCEPT; "
        f"iptables -t nat -D POSTROUTING -s {subnet} -j MASQUERADE",
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
