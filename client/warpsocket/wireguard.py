from __future__ import annotations

import ipaddress

from warpsocket.config import ClientConfig


def _allowed_ips_excluding(bypass_ips: list[str]) -> str:
    """Compute 0.0.0.0/0 minus bypass_ips as a comma-separated AllowedIPs string.

    Excluding bypass IPs from AllowedIPs is more reliable than adding host routes
    on top of a WireGuard tunnel, because the WireGuard-NT driver on Windows
    captures traffic before the OS routing table is consulted.
    """
    remaining: list[ipaddress.IPv4Network] = [ipaddress.ip_network("0.0.0.0/0")]
    for ip in bypass_ips:
        excl = ipaddress.ip_network(ip if "/" in ip else f"{ip}/32", strict=False)
        new_remaining: list[ipaddress.IPv4Network] = []
        for net in remaining:
            if excl.overlaps(net):
                new_remaining.extend(net.address_exclude(excl))
            else:
                new_remaining.append(net)
        remaining = new_remaining
    return ", ".join(str(n) for n in sorted(remaining))


def build_wg_conf(config: ClientConfig) -> str:
    wg = config.wireguard
    tunnel = config.tunnel
    bypass = config.routing.bypass_ips
    allowed_ips = _allowed_ips_excluding(bypass) if bypass else "0.0.0.0/0"
    dns_line = f"DNS = {', '.join(wg.dns)}\n" if wg.dns else ""
    return (
        "[Interface]\n"
        f"PrivateKey = {wg.client_private_key}\n"
        f"Address = {wg.client_address}\n"
        f"{dns_line}"
        "\n"
        "[Peer]\n"
        f"PublicKey = {wg.server_public_key}\n"
        f"AllowedIPs = {allowed_ips}\n"
        f"Endpoint = 127.0.0.1:{tunnel.local_port}\n"
        "PersistentKeepalive = 25\n"
    )
