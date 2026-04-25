from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket.config import ClientConfig, ConfigError
from warpsocket.wizard import try_import

# Re-use the VALID fixture from test_config
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


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "test.warpcfg"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_try_import_valid(tmp_path: Path) -> None:
    src = _write(tmp_path, VALID)
    dest = tmp_path / "config.json"
    cfg = try_import(src, dest)
    assert isinstance(cfg, ClientConfig)
    assert cfg.server.endpoint == "203.0.113.42"
    assert dest.exists()


def test_try_import_invalid_raises(tmp_path: Path) -> None:
    src = tmp_path / "bad.warpcfg"
    src.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        try_import(src, tmp_path / "config.json")


def test_try_import_missing_field(tmp_path: Path) -> None:
    data = {**VALID}
    del data["server"]
    src = _write(tmp_path, data)
    with pytest.raises(ConfigError, match="server"):
        try_import(src, tmp_path / "config.json")


def test_try_import_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        try_import(tmp_path / "nonexistent.warpcfg", tmp_path / "config.json")


def test_pick_warpcfg_file_returns_none_on_cancel() -> None:
    with patch("warpsocket.wizard.filedialog") as mock_fd:
        mock_fd.askopenfilename.return_value = ""
        from warpsocket.wizard import pick_warpcfg_file
        result = pick_warpcfg_file()
        assert result is None


def test_pick_warpcfg_file_returns_path() -> None:
    with patch("warpsocket.wizard.filedialog") as mock_fd:
        mock_fd.askopenfilename.return_value = "/some/path.warpcfg"
        from warpsocket.wizard import pick_warpcfg_file
        result = pick_warpcfg_file()
        assert result == Path("/some/path.warpcfg")
