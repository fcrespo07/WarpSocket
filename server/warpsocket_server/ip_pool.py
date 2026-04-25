from __future__ import annotations

import ipaddress


class PoolExhaustedError(RuntimeError):
    pass


def next_available_ip(
    subnet: str,
    server_address: str,
    allocated: list[str],
) -> str:
    """Return the next available IP in the subnet as 'X.X.X.X/32'.

    Skips the network/broadcast addresses, the server IP, and already allocated IPs.
    """
    network = ipaddress.IPv4Network(subnet, strict=False)
    server_ip = ipaddress.IPv4Address(server_address.split("/")[0])
    used = {server_ip}
    for addr in allocated:
        used.add(ipaddress.IPv4Address(addr.split("/")[0]))

    for host in network.hosts():
        if host not in used:
            return f"{host}/32"

    raise PoolExhaustedError(
        f"No available IPs in subnet {subnet} "
        f"({len(used)} addresses in use, {network.num_addresses - 2} usable)"
    )
