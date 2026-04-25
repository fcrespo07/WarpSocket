from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from warpsocket_server.config import ClientEntry, ServerConfig
from warpsocket_server.wireguard import (
    WireGuardError,
    add_peer_live,
    build_server_wg_conf,
    remove_peer_live,
)


def _make_config(**overrides: object) -> ServerConfig:
    defaults = {
        "schema_version": 1,
        "endpoint": "203.0.113.42",
        "port": 443,
        "http_upgrade_path_prefix": "secret",
        "cert_path": "/etc/warpsocket/cert.pem",
        "key_path": "/etc/warpsocket/key.pem",
        "cert_fingerprint_sha256": "AA:" * 31 + "AA",
        "wg_private_key": "server_private_key",
        "wg_public_key": "server_public_key",
        "subnet": "10.0.0.0/24",
        "server_address": "10.0.0.1/24",
        "wg_listen_port": 51820,
        "clients": [],
    }
    defaults.update(overrides)
    return ServerConfig(**defaults)


class TestBuildServerWgConf:
    def test_interface_section(self) -> None:
        conf = build_server_wg_conf(_make_config())
        assert "[Interface]" in conf
        assert "PrivateKey = server_private_key" in conf
        assert "Address = 10.0.0.1/24" in conf
        assert "ListenPort = 51820" in conf

    def test_no_peers_when_empty(self) -> None:
        conf = build_server_wg_conf(_make_config())
        assert "[Peer]" not in conf

    def test_includes_peers(self) -> None:
        clients = [
            ClientEntry(name="laptop", public_key="key1", address="10.0.0.2/32"),
            ClientEntry(name="phone", public_key="key2", address="10.0.0.3/32"),
        ]
        conf = build_server_wg_conf(_make_config(clients=clients))
        assert conf.count("[Peer]") == 2
        assert "# laptop" in conf
        assert "PublicKey = key1" in conf
        assert "AllowedIPs = 10.0.0.2/32" in conf
        assert "# phone" in conf
        assert "PublicKey = key2" in conf


class TestAddPeerLive:
    def test_calls_wg_set(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            add_peer_live("pubkey123", "10.0.0.5/32")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["wg", "set", "wg0", "peer", "pubkey123", "allowed-ips", "10.0.0.5/32"]

    def test_raises_on_failure(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "wg", stderr="fail")
            with pytest.raises(WireGuardError, match="Failed to add peer"):
                add_peer_live("pubkey", "10.0.0.5/32")


class TestRemovePeerLive:
    def test_calls_wg_set_remove(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            remove_peer_live("pubkey123")
            args = mock_run.call_args[0][0]
            assert args == ["wg", "set", "wg0", "peer", "pubkey123", "remove"]

    def test_raises_on_failure(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "wg", stderr="fail")
            with pytest.raises(WireGuardError, match="Failed to remove peer"):
                remove_peer_live("pubkey")
