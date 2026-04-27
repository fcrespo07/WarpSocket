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
    get_live_peers,
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

    def test_table_off(self) -> None:
        conf = build_server_wg_conf(_make_config())
        assert "Table = off" in conf

    def test_forward_rules_use_insert_not_append(self) -> None:
        conf = build_server_wg_conf(_make_config())
        assert "iptables -I FORWARD" in conf
        assert "iptables -A FORWARD" not in conf

    def test_postup_includes_masquerade(self) -> None:
        conf = build_server_wg_conf(_make_config())
        assert "MASQUERADE" in conf
        assert "10.0.0.0/24" in conf

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


class TestGetLivePeers:
    # `wg show <iface> dump` format: tab-separated; first line is the interface,
    # subsequent lines are peers. Real example fields per peer:
    # public_key  preshared_key  endpoint  allowed_ips  latest_handshake  rx  tx  keepalive
    _DUMP_OUTPUT = (
        "server_priv\tserver_pub\t51820\toff\n"
        "peer1pubkey\t(none)\t192.168.1.50:54321\t10.0.0.2/32\t1714220000\t1024\t2048\t25\n"
        "peer2pubkey\t(none)\t(none)\t10.0.0.3/32\t0\t0\t0\toff\n"
    )

    def test_parses_two_peers(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = self._DUMP_OUTPUT
            peers = get_live_peers()
        assert set(peers.keys()) == {"peer1pubkey", "peer2pubkey"}

        active = peers["peer1pubkey"]
        assert active.endpoint == "192.168.1.50:54321"
        assert active.latest_handshake == 1714220000
        assert active.transfer_rx == 1024
        assert active.transfer_tx == 2048
        assert active.allowed_ips == "10.0.0.2/32"

        idle = peers["peer2pubkey"]
        assert idle.endpoint is None
        assert idle.latest_handshake is None
        assert idle.transfer_rx == 0

    def test_returns_empty_when_wg_fails(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_live_peers() == {}

    def test_returns_empty_when_wg_not_installed(self) -> None:
        with patch("warpsocket_server.wireguard.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("wg")
            assert get_live_peers() == {}


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
