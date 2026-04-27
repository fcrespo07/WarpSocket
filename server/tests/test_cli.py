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


class TestRequireRoot:
    def test_require_root_called_for_privileged_command(self) -> None:
        # When --config-dir is omitted, main() should invoke _require_root.
        # We patch _require_root to assert it's called and to short-circuit
        # before the real handler tries to read /etc paths.
        with patch("warpsocket_server.cli._require_root") as mock_req:
            mock_req.side_effect = SystemExit(1)
            with pytest.raises(SystemExit):
                main(["status"])
            mock_req.assert_called_once_with("status")

    def test_require_root_skipped_with_config_dir(self, tmp_path: Path) -> None:
        # Tests pass --config-dir and should NOT trip the root check.
        config_dir = _write_server_config(tmp_path)
        with patch("warpsocket_server.cli._require_root") as mock_req:
            with patch("warpsocket_server.platforms.get_server_platform") as mock_platform:
                mock_platform.return_value = MagicMock()
                main(["--config-dir", str(config_dir), "status"])
            mock_req.assert_not_called()


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

    @patch("warpsocket_server.cli.get_live_peers", return_value={})
    def test_list_with_clients(self, _mock_live: MagicMock, tmp_path: Path) -> None:
        clients = [
            {"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"},
            {"name": "phone", "public_key": "key2", "address": "10.0.0.3/32"},
        ]
        config_dir = _write_server_config(tmp_path, clients=clients)
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0

    @patch("warpsocket_server.cli.get_live_peers")
    def test_list_shows_online_status(
        self, mock_live: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import time

        from warpsocket_server.wireguard import LivePeer

        clients = [{"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"}]
        config_dir = _write_server_config(tmp_path, clients=clients)
        mock_live.return_value = {
            "key1": LivePeer(
                public_key="key1",
                endpoint="192.168.1.50:54321",
                allowed_ips="10.0.0.2/32",
                latest_handshake=int(time.time()) - 30,  # 30s ago = online
                transfer_rx=2048,
                transfer_tx=1024,
            )
        }
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "online" in out
        assert "laptop" in out

    @patch("warpsocket_server.cli.get_live_peers")
    def test_list_shows_offline_when_handshake_stale(
        self, mock_live: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import time

        from warpsocket_server.wireguard import LivePeer

        clients = [{"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"}]
        config_dir = _write_server_config(tmp_path, clients=clients)
        mock_live.return_value = {
            "key1": LivePeer(
                public_key="key1",
                endpoint="192.168.1.50:54321",
                allowed_ips="10.0.0.2/32",
                latest_handshake=int(time.time()) - 600,  # 10 min ago = offline
                transfer_rx=0,
                transfer_tx=0,
            )
        }
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0
        assert "offline" in capsys.readouterr().out

    @patch("warpsocket_server.cli.get_live_peers", return_value={})
    def test_list_shows_unknown_when_wg_down(
        self, _mock_live: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clients = [{"name": "laptop", "public_key": "key1", "address": "10.0.0.2/32"}]
        config_dir = _write_server_config(tmp_path, clients=clients)
        ret = main(["--config-dir", str(config_dir), "list-clients"])
        assert ret == 0
        assert "unknown" in capsys.readouterr().out


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


class TestRestart:
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_restart_calls_install_wg_and_restarts_services(
        self, mock_platform: MagicMock, tmp_path: Path
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "restart"])
        assert ret == 0
        fake.install_wg_config.assert_called_once()
        fake.restart_wg.assert_called_once()
        fake.restart_wstunnel_service.assert_called_once()

    @patch("warpsocket_server.platforms.get_server_platform")
    def test_restart_returns_1_on_wg_failure(
        self, mock_platform: MagicMock, tmp_path: Path
    ) -> None:
        from warpsocket_server.platforms.base import PlatformError

        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.restart_wg.side_effect = PlatformError("boom")
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "restart"])
        assert ret == 1


class TestUninstall:
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_with_yes_flag(self, mock_platform: MagicMock, tmp_path: Path) -> None:
        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.install_prefix.return_value = None
        fake.bin_link.return_value = None
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "uninstall", "--yes"])
        assert ret == 0
        fake.uninstall_wstunnel_service.assert_called_once()
        fake.uninstall_wg_config.assert_called_once()
        assert not config_dir.exists()

    @patch("warpsocket_server.cli.console")
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_aborts_without_confirmation(
        self, mock_platform: MagicMock, mock_console: MagicMock, tmp_path: Path
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.install_prefix.return_value = None
        fake.bin_link.return_value = None
        mock_platform.return_value = fake
        mock_console.input.return_value = "no"
        ret = main(["--config-dir", str(config_dir), "uninstall"])
        assert ret == 1
        fake.uninstall_wstunnel_service.assert_not_called()

    @patch("warpsocket_server.cli.console")
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_confirms_with_yes(
        self, mock_platform: MagicMock, mock_console: MagicMock, tmp_path: Path
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        mock_console.input.return_value = "yes"
        mock_console.print = MagicMock()
        fake = MagicMock()
        fake.install_prefix.return_value = None
        fake.bin_link.return_value = None
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "uninstall"])
        assert ret == 0
        fake.uninstall_wstunnel_service.assert_called_once()

    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_returns_1_on_platform_error(
        self, mock_platform: MagicMock, tmp_path: Path
    ) -> None:
        from warpsocket_server.platforms.base import PlatformError

        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.install_prefix.return_value = None
        fake.bin_link.return_value = None
        fake.uninstall_wstunnel_service.side_effect = PlatformError("service not found")
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "uninstall", "--yes"])
        assert ret == 1

    @patch("warpsocket_server.cli._spawn_deferred_cleanup")
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_schedules_deferred_cleanup_when_install_prefix_exists(
        self,
        mock_platform: MagicMock,
        mock_spawn: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Use a sibling dir for config so that removing it doesn't also wipe
        # install_prefix (which would make .exists() fail before we schedule).
        etc_dir = tmp_path / "etc"
        etc_dir.mkdir()
        config_dir = _write_server_config(etc_dir)
        prefix = tmp_path / "opt-warpsocket"
        prefix.mkdir()
        bin_link = tmp_path / "warpsocket-server"
        fake = MagicMock()
        fake.install_prefix.return_value = prefix
        fake.bin_link.return_value = bin_link
        mock_platform.return_value = fake
        ret = main(["--config-dir", str(config_dir), "uninstall", "--yes"])
        assert ret == 0
        mock_spawn.assert_called_once_with(prefix, bin_link)

    @patch("warpsocket_server.cli._spawn_deferred_cleanup")
    @patch("warpsocket_server.platforms.get_server_platform")
    def test_uninstall_skips_deferred_cleanup_when_no_install_prefix(
        self,
        mock_platform: MagicMock,
        mock_spawn: MagicMock,
        tmp_path: Path,
    ) -> None:
        config_dir = _write_server_config(tmp_path)
        fake = MagicMock()
        fake.install_prefix.return_value = None
        fake.bin_link.return_value = None
        mock_platform.return_value = fake
        main(["--config-dir", str(config_dir), "uninstall", "--yes"])
        mock_spawn.assert_not_called()


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
