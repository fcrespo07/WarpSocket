from __future__ import annotations

from warpsocket.config import (
    ClientConfig,
    ReconnectConfig,
    RoutingConfig,
    ServerConfig,
    TlsConfig,
    TunnelConfig,
    WireguardConfig,
)
from warpsocket.wireguard import build_wg_conf


def _make_config(dns: list[str] | None = None) -> ClientConfig:
    return ClientConfig(
        schema_version=1,
        server=ServerConfig(endpoint="203.0.113.42", port=443, http_upgrade_path_prefix="x"),
        tls=TlsConfig(cert_fingerprint_sha256="A" * 95),
        tunnel=TunnelConfig(local_port=51820, remote_host="10.0.0.1", remote_port=51820),
        wireguard=WireguardConfig(
            tunnel_name="WarpSocket",
            client_address="10.0.0.42/32",
            client_private_key="cli3ntPriv",
            server_public_key="serv3rPub",
            dns=dns if dns is not None else ["1.1.1.1"],
        ),
        routing=RoutingConfig(bypass_ips=["203.0.113.42"]),
        reconnect=ReconnectConfig(),
    )


def test_build_wg_conf_includes_required_sections():
    text = build_wg_conf(_make_config())
    assert "[Interface]" in text
    assert "[Peer]" in text
    assert "PrivateKey = cli3ntPriv" in text
    assert "PublicKey = serv3rPub" in text


def test_build_wg_conf_endpoint_is_localhost_not_real_server():
    text = build_wg_conf(_make_config())
    assert "Endpoint = 127.0.0.1:51820" in text
    assert "203.0.113.42" not in text  # real endpoint must NOT appear


def test_build_wg_conf_includes_address_and_dns():
    text = build_wg_conf(_make_config(dns=["1.1.1.1", "8.8.8.8"]))
    assert "Address = 10.0.0.42/32" in text
    assert "DNS = 1.1.1.1, 8.8.8.8" in text


def test_build_wg_conf_omits_dns_when_empty():
    text = build_wg_conf(_make_config(dns=[]))
    assert "DNS" not in text


def test_build_wg_conf_routes_all_traffic():
    text = build_wg_conf(_make_config())
    assert "AllowedIPs = 0.0.0.0/0" in text


def test_build_wg_conf_includes_keepalive():
    text = build_wg_conf(_make_config())
    assert "PersistentKeepalive = 25" in text
