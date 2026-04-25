from __future__ import annotations

from warpsocket.config import ClientConfig


def build_wg_conf(config: ClientConfig) -> str:
    wg = config.wireguard
    tunnel = config.tunnel
    dns_line = f"DNS = {', '.join(wg.dns)}\n" if wg.dns else ""
    return (
        "[Interface]\n"
        f"PrivateKey = {wg.client_private_key}\n"
        f"Address = {wg.client_address}\n"
        f"{dns_line}"
        "\n"
        "[Peer]\n"
        f"PublicKey = {wg.server_public_key}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        f"Endpoint = 127.0.0.1:{tunnel.local_port}\n"
        "PersistentKeepalive = 25\n"
    )
