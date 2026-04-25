from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket_server.cli import build_parser, main
from warpsocket_server.config import ClientEntry, ServerConfig


def _write_server_config(tmp_path: Path, **overrides: object) -> Path:
    defaults = {
        "schema_version": 1,
        "endpoint": "203.0.113.42",
        "port": 443,
        "http_upgrade_path_prefix": "s3cr3t",
        "cert_path": str(tmp_path / "cert.pem"),
        "key_path": str(tmp_path / "key.pem"),
        "cert_fingerprint_sha256": "AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89",
        "wg_private_key": "server_priv",
        "wg_public_key": "server_pub",
        "subnet": "10.0.0.0/24",
        "server_address": "10.0.0.1/24",
        "wg_listen_port": 51820,
        "clients": [],
    }
    defaults.update(overrides)
    config_path = tmp_path / "server_config.json"
    config_path.write_text(json.dumps(defaults), encoding="utf-8")
    return tmp_path


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        main(["--version"])
    assert "0.0.1" in capsys.readouterr().out


def test_no_command_raises() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_parser_has_config_dir_option() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config-dir", "/tmp/ws", "status"])
    assert args.config_dir == "/tmp/ws"


class TestAddClient:
    @patch("warpsocket_server.cli.add_peer_live")
    @patch("warpsocket_server.cli.generate_wg_keypair", return_value=("client_priv", "client_pub"))
    def test_add_client_creates_warpcfg(
        self, mock_keygen: MagicMock, mock_add: MagicMock, tmp_path: Path
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        ret = main(["--config-dir", str(config_dir), "add-client", "laptop"])
        assert ret == 0

        warpcfg_path = Path.cwd() / "laptop.warpcfg"
        assert warpcfg_path.exists()
        warpcfg = json.loads(warpcfg_path.read_text(encoding="utf-8"))
        assert warpcfg["wireguard"]["client_private_key"] == "client_priv"
        assert warpcfg["wireguard"]["client_address"] == "10.0.0.2/32"
        warpcfg_path.unlink()  # cleanup

    @patch("warpsocket_server.cli.add_peer_live")
    @patch("warpsocket_server.cli.generate_wg_keypair", return_value=("priv", "pub"))
    def test_add_client_updates_server_config(
        self, mock_keygen: MagicMock, mock_add: MagicMock, tmp_path: Path
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        main(["--config-dir", str(config_dir), "add-client", "phone"])
        cfg = ServerConfig.load(config_dir / "server_config.json")
        assert len(cfg.clients) == 1
        assert cfg.clients[0].name == "phone"
        (Path.cwd() / "phone.warpcfg").unlink(missing_ok=True)

    @patch("warpsocket_server.cli.add_peer_live")
    @patch("warpsocket_server.cli.generate_wg_keypair", return_value=("priv", "pub"))
    def test_add_duplicate_client_fails(
        self, mock_keygen: MagicMock, mock_add: MagicMock, tmp_path: Path
    ) -> None:
        clients = [{"name": "laptop", "public_key": "key", "address": "10.0.0.2/32"}]
        config_dir = _write_server_config(tmp_path, clients=clients)
        ret = main(["--config-dir", str(config_dir), "add-client", "laptop"])
        assert ret == 1


class TestListClients:
    def test_list_empty(self, tmp_path: Path) -> None:
        config_dir = _write_server_config(tmp_path)
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0

    def test_list_with_clients(self, tmp_path: Path) -> None:
        clients = [
            {"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"},
            {"name": "phone", "public_key": "key2", "address": "10.0.0.3/32"},
        ]
        config_dir = _write_server_config(tmp_path, clients=clients)
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0


class TestStatus:
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_status_shows_table(self, mock_platform: MagicMock, tmp_path: Path) -> None:
        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.is_wstunnel_running.return_value = True
        fake.is_wg_active.return_value = True
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "status"])
        assert ret == 0

    @patch("warpsocket_server.platforms.get_server_platform")
    def test_status_handles_platform_errors(
        self, mock_platform: MagicMock, tmp_path: Path
    ) -> None:
        from warpsocket_server.platforms.base import PlatformError

        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.is_wstunnel_running.side_effect = PlatformError("not supported")
        fake.is_wg_active.side_effect = PlatformError("not supported")
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "status"])
        assert ret == 0


class TestRevokeClient:
    @patch("warpsocket_server.cli.remove_peer_live")
    def test_revoke_removes_client(self, mock_remove: MagicMock, tmp_path: Path) -> None:
        clients = [{"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"}]
        config_dir = _write_server_config(tmp_path, clients=clients)
        ret = main(["--config-dir", str(config_dir), "revoke-client", "laptop"])
        assert ret == 0
        cfg = ServerConfig.load(config_dir / "server_config.json")
        assert len(cfg.clients) == 0

    def test_revoke_nonexistent_fails(self, tmp_path: Path) -> None:
        config_dir = _write_server_config(tmp_path)
        ret = main(["--config-dir", str(config_dir), "revoke-client", "ghost"])
        assert ret == 1
