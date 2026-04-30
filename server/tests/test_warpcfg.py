from __future__ import annotations

import json
from pathlib import Path

from warpsocket_server.config import ServerConfig
from warpsocket_server.warpcfg import build_warpcfg, write_warpcfg


def _make_server_config() -> ServerConfig:
    return ServerConfig(
        schema_version=1,
        endpoint="203.0.113.42",
        port=443,
        http_upgrade_path_prefix="s3cr3t",
        cert_path="/etc/warpsocket/cert.pem",
        key_path="/etc/warpsocket/key.pem",
        cert_fingerprint_sha256="AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89",
        wg_private_key="server_priv",
        wg_public_key="server_pub",
        subnet="10.0.0.0/24",
        server_address="10.0.0.1/24",
        wg_listen_port=51820,
        clients=[],
    )


def test_build_warpcfg_has_all_sections() -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    assert cfg["schema_version"] == 1
    assert "server" in cfg
    assert "tls" in cfg
    assert "tunnel" in cfg
    assert "wireguard" in cfg
    assert "routing" in cfg
    assert "reconnect" in cfg


def test_build_warpcfg_server_section() -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    assert cfg["server"]["endpoint"] == "203.0.113.42"
    assert cfg["server"]["port"] == 443
    assert cfg["server"]["http_upgrade_path_prefix"] == "s3cr3t"


def test_build_warpcfg_tls_fingerprint() -> None:
    scfg = _make_server_config()
    cfg = build_warpcfg(scfg, "laptop", "client_priv", "10.0.0.2/32")
    assert cfg["tls"]["cert_fingerprint_sha256"] == scfg.cert_fingerprint_sha256


def test_build_warpcfg_wireguard_section() -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    wg = cfg["wireguard"]
    assert wg["client_private_key"] == "client_priv"
    assert wg["server_public_key"] == "server_pub"
    assert wg["client_address"] == "10.0.0.2/32"
    assert wg["tunnel_name"] == "WarpSocket"


def test_build_warpcfg_tunnel_points_to_loopback() -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    assert cfg["tunnel"]["remote_host"] == "127.0.0.1"
    assert cfg["tunnel"]["remote_port"] == 51820


def test_build_warpcfg_routing_bypass() -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    assert "203.0.113.42" in cfg["routing"]["bypass_ips"]


def test_write_warpcfg(tmp_path: Path) -> None:
    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    out = tmp_path / "laptop.warpcfg"
    write_warpcfg(cfg, out)
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["server"]["endpoint"] == "203.0.113.42"


def test_warpcfg_compatible_with_client_schema(tmp_path: Path) -> None:
    """Verify the .warpcfg can be loaded by the client's ClientConfig parser."""
    # This import works because the client package is installed in the same venv
    try:
        from warpsocket.config import ClientConfig
    except ImportError:
        import pytest
        pytest.skip("Client package not installed in this environment")

    cfg = build_warpcfg(_make_server_config(), "laptop", "client_priv", "10.0.0.2/32")
    out = tmp_path / "laptop.warpcfg"
    write_warpcfg(cfg, out)
    client_cfg = ClientConfig.load(out)
    assert client_cfg.server.endpoint == "203.0.113.42"
    assert client_cfg.wireguard.client_private_key == "client_priv"
