import json
import pytest
from pathlib import Path
from warpsocket.config import ClientConfig, ConfigError, default_config_path, import_warpcfg

VALID = {
    "schema_version": 1,
    "server": {
        "endpoint": "203.0.113.42",
        "port": 443,
        "http_upgrade_path_prefix": "s3cr3t",
    },
    "tls": {
        "cert_fingerprint_sha256": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89",
    },
    "tunnel": {"local_port": 51820, "remote_host": "10.0.0.1", "remote_port": 51820},
    "wireguard": {
        "tunnel_name": "WarpSocket",
        "client_address": "10.0.0.42/32",
        "client_private_key": "dGVzdGtleQ==",
        "server_public_key": "c2VydmVya2V5",
        "dns": ["1.1.1.1"],
    },
    "routing": {"bypass_ips": ["203.0.113.42"]},
    "reconnect": {"max_attempts": 5, "delays_seconds": [5, 10, 20, 30, 60]},
}


def write_cfg(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "test.warpcfg"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_valid(tmp_path):
    cfg = ClientConfig.load(write_cfg(tmp_path, VALID))
    assert cfg.server.endpoint == "203.0.113.42"
    assert cfg.server.port == 443
    assert cfg.tunnel.local_port == 51820
    assert cfg.wireguard.tunnel_name == "WarpSocket"
    assert cfg.routing.bypass_ips == ["203.0.113.42"]
    assert cfg.reconnect.max_attempts == 5


def test_reconnect_defaults_when_missing(tmp_path):
    data = {k: v for k, v in VALID.items() if k != "reconnect"}
    cfg = ClientConfig.load(write_cfg(tmp_path, data))
    assert cfg.reconnect.max_attempts == 5
    assert cfg.reconnect.delays_seconds == [5, 10, 20, 30, 60]


def test_missing_required_field(tmp_path):
    data = {**VALID, "server": {"port": 443, "http_upgrade_path_prefix": "x"}}
    with pytest.raises(ConfigError, match="endpoint"):
        ClientConfig.load(write_cfg(tmp_path, data))


def test_invalid_port(tmp_path):
    data = {**VALID, "server": {**VALID["server"], "port": 99999}}
    with pytest.raises(ConfigError, match="port"):
        ClientConfig.load(write_cfg(tmp_path, data))


def test_bad_fingerprint(tmp_path):
    data = {**VALID, "tls": {"cert_fingerprint_sha256": "notafingerprint"}}
    with pytest.raises(ConfigError, match="cert_fingerprint_sha256"):
        ClientConfig.load(write_cfg(tmp_path, data))


def test_unsupported_schema_version(tmp_path):
    data = {**VALID, "schema_version": 99}
    with pytest.raises(ConfigError, match="schema_version"):
        ClientConfig.load(write_cfg(tmp_path, data))


def test_file_not_found():
    with pytest.raises(ConfigError, match="not found"):
        ClientConfig.load(Path("/nonexistent/path.warpcfg"))


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.warpcfg"
    p.write_text("{ not json }", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        ClientConfig.load(p)


def test_save_roundtrip(tmp_path):
    src = write_cfg(tmp_path, VALID)
    cfg = ClientConfig.load(src)
    dest = tmp_path / "out.json"
    cfg.save(dest)
    cfg2 = ClientConfig.load(dest)
    assert cfg == cfg2


def test_import_warpcfg(tmp_path):
    src = write_cfg(tmp_path, VALID)
    dest = tmp_path / "config.json"
    cfg = import_warpcfg(src, dest)
    assert dest.exists()
    assert cfg.server.port == 443


def test_default_config_path_is_absolute():
    assert default_config_path().is_absolute()
