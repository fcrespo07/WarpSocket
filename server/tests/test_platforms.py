from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket_server.platforms import ServerPlatform, get_server_platform
from warpsocket_server.platforms.base import PlatformError
from warpsocket_server.platforms.linux import LinuxServerPlatform


def test_get_server_platform_returns_subclass() -> None:
    p = get_server_platform()
    assert isinstance(p, ServerPlatform)


def test_server_platform_is_abstract() -> None:
    with pytest.raises(TypeError):
        ServerPlatform()  # type: ignore[abstract]


class TestLinuxPlatform:
    def test_wg_config_dir(self) -> None:
        assert LinuxServerPlatform().wg_config_dir() == Path("/etc/wireguard")

    @patch("warpsocket_server.platforms.linux._SERVICE_PATH")
    @patch("warpsocket_server.platforms.linux.os.chmod")
    @patch("warpsocket_server.platforms.linux._run")
    def test_install_wstunnel_writes_unit_and_enables(
        self,
        mock_run: MagicMock,
        mock_chmod: MagicMock,
        mock_path: MagicMock,
    ) -> None:
        platform = LinuxServerPlatform()
        platform.install_wstunnel_service(
            port=443,
            cert_path=Path("/etc/warpsocket/cert.pem"),
            key_path=Path("/etc/warpsocket/key.pem"),
            upgrade_path="secret",
            wg_listen_port=51820,
            wstunnel_bin=Path("/usr/local/bin/wstunnel"),
        )
        mock_path.write_text.assert_called_once()
        unit_text = mock_path.write_text.call_args[0][0]
        assert "wss://0.0.0.0:443" in unit_text
        assert "127.0.0.1:51820" in unit_text
        # Path separator is platform-dependent in str(Path), so check for the exe name
        assert "wstunnel server" in unit_text
        assert "cert.pem" in unit_text

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert ["systemctl", "daemon-reload"] in commands
        assert any("enable" in cmd for cmd in commands)

    @patch("warpsocket_server.platforms.linux._run")
    def test_is_wstunnel_running_true(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="active\n")
        assert LinuxServerPlatform().is_wstunnel_running() is True

    @patch("warpsocket_server.platforms.linux._run")
    def test_is_wstunnel_running_false(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="inactive\n")
        assert LinuxServerPlatform().is_wstunnel_running() is False

    @patch("warpsocket_server.platforms.linux.Path")
    def test_is_wg_active(self, mock_path_cls: MagicMock) -> None:
        instance = MagicMock()
        mock_path_cls.return_value = instance

        instance.exists.return_value = True
        assert LinuxServerPlatform().is_wg_active() is True
        mock_path_cls.assert_called_with("/sys/class/net/wg0")

        instance.exists.return_value = False
        assert LinuxServerPlatform().is_wg_active() is False

    @patch("warpsocket_server.platforms.linux._run")
    def test_install_wstunnel_raises_on_systemctl_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "systemctl", stderr="fail")
        with patch("warpsocket_server.platforms.linux._SERVICE_PATH"):
            with patch("warpsocket_server.platforms.linux.os.chmod"):
                with pytest.raises(PlatformError, match="Failed to enable"):
                    LinuxServerPlatform().install_wstunnel_service(
                        port=443,
                        cert_path=Path("/x"),
                        key_path=Path("/y"),
                        upgrade_path="s",
                        wg_listen_port=51820,
                        wstunnel_bin=Path("/usr/bin/wstunnel"),
                    )


    @patch("warpsocket_server.platforms.linux._SYSCTL_DROP_IN")
    @patch("warpsocket_server.platforms.linux._run")
    def test_uninstall_wg_config_disables_unit_and_removes_file(
        self, mock_run: MagicMock, mock_sysctl: MagicMock, tmp_path: Path
    ) -> None:
        conf = tmp_path / "wg0.conf"
        conf.write_text("[Interface]\n")
        mock_sysctl.exists.return_value = False
        with patch.object(LinuxServerPlatform, "wg_config_dir", return_value=tmp_path):
            LinuxServerPlatform().uninstall_wg_config()
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert any("disable" in c for c in cmds)
        assert not conf.exists()

    @patch("warpsocket_server.platforms.linux._SYSCTL_DROP_IN")
    @patch("warpsocket_server.platforms.linux._run")
    def test_uninstall_wg_config_removes_sysctl_drop_in(
        self, mock_run: MagicMock, mock_sysctl: MagicMock, tmp_path: Path
    ) -> None:
        mock_sysctl.exists.return_value = True
        with patch.object(LinuxServerPlatform, "wg_config_dir", return_value=tmp_path):
            LinuxServerPlatform().uninstall_wg_config()
        mock_sysctl.unlink.assert_called_once()
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert ["sysctl", "--system"] in cmds

    def test_install_prefix_and_bin_link_match_installer(self) -> None:
        p = LinuxServerPlatform()
        assert p.install_prefix() == Path("/opt/warpsocket-server")
        assert p.bin_link() == Path("/usr/local/bin/warpsocket-server")

    @patch("warpsocket_server.platforms.linux._run")
    def test_restart_wstunnel_service(self, mock_run: MagicMock) -> None:
        LinuxServerPlatform().restart_wstunnel_service()
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert ["systemctl", "restart", "wstunnel-warpsocket.service"] in cmds

    @patch("warpsocket_server.platforms.linux._run")
    def test_restart_wstunnel_raises_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "systemctl", stderr="fail")
        with pytest.raises(PlatformError, match="Failed to restart"):
            LinuxServerPlatform().restart_wstunnel_service()

    @patch("warpsocket_server.platforms.linux._run")
    def test_uninstall_wstunnel_service(self, mock_run: MagicMock) -> None:
        with patch("warpsocket_server.platforms.linux._SERVICE_PATH") as mock_path:
            mock_path.exists.return_value = True
            LinuxServerPlatform().uninstall_wstunnel_service()
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert any("disable" in c for c in cmds)

    @patch("warpsocket_server.platforms.linux._run")
    def test_restart_wg(self, mock_run: MagicMock) -> None:
        LinuxServerPlatform().restart_wg()
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert ["systemctl", "restart", "wg-quick@wg0.service"] in cmds

    @patch("warpsocket_server.platforms.linux._run")
    def test_restart_wg_raises_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "systemctl", stderr="fail")
        with pytest.raises(PlatformError, match="Failed to restart"):
            LinuxServerPlatform().restart_wg()


class TestStubPlatforms:
    def test_macos_raises_not_implemented(self) -> None:
        from warpsocket_server.platforms.macos import MacOSServerPlatform

        p = MacOSServerPlatform()
        with pytest.raises(PlatformError, match="not implemented"):
            p.is_wstunnel_running()

    def test_windows_wstunnel_service_is_noop(self) -> None:
        from warpsocket_server.platforms.windows import WindowsServerPlatform
        from unittest.mock import patch

        p = WindowsServerPlatform()
        # install/uninstall are no-ops on Windows (ServerManager owns wstunnel)
        p.install_wstunnel_service(443, Path("/c"), Path("/k"), "x", 51820, Path("/b"))
        p.uninstall_wstunnel_service()

        # is_wstunnel_running checks via tasklist; mock subprocess so it works off-platform
        with patch(
            "warpsocket_server.platforms.windows._run",
            return_value=type("R", (), {"stdout": "", "returncode": 0})(),
        ):
            assert p.is_wstunnel_running() is False
