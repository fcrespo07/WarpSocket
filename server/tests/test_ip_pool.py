from __future__ import annotations

import pytest

from warpsocket_server.ip_pool import PoolExhaustedError, next_available_ip


def test_first_ip_skips_server() -> None:
    ip = next_available_ip("10.0.0.0/24", "10.0.0.1/24", [])
    assert ip == "10.0.0.2/32"


def test_sequential_allocation() -> None:
    ip1 = next_available_ip("10.0.0.0/24", "10.0.0.1/24", [])
    ip2 = next_available_ip("10.0.0.0/24", "10.0.0.1/24", [ip1])
    ip3 = next_available_ip("10.0.0.0/24", "10.0.0.1/24", [ip1, ip2])
    assert ip1 == "10.0.0.2/32"
    assert ip2 == "10.0.0.3/32"
    assert ip3 == "10.0.0.4/32"


def test_skips_allocated_ips() -> None:
    ip = next_available_ip("10.0.0.0/24", "10.0.0.1/24", ["10.0.0.2/32"])
    assert ip == "10.0.0.3/32"


def test_pool_exhaustion() -> None:
    # /30 has 4 addresses: .0 (network), .1 (server), .2 (allocatable), .3 (broadcast)
    with pytest.raises(PoolExhaustedError):
        next_available_ip("10.0.0.0/30", "10.0.0.1/30", ["10.0.0.2/32"])


def test_small_subnet() -> None:
    ip = next_available_ip("10.0.0.0/30", "10.0.0.1/30", [])
    assert ip == "10.0.0.2/32"


def test_server_not_first_host() -> None:
    ip = next_available_ip("10.0.0.0/24", "10.0.0.100/24", [])
    assert ip == "10.0.0.1/32"
