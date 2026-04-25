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

    @patch("warpsocket_server.platforms.linux._run")
    def test_is_wg_active(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert LinuxServerPlatform().is_wg_active() is True

        mock_run.return_value = MagicMock(returncode=1)
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


class TestStubPlatforms:
    def test_macos_raises_not_implemented(self) -> None:
        from warpsocket_server.platforms.macos import MacOSServerPlatform

        p = MacOSServerPlatform()
        with pytest.raises(PlatformError, match="not implemented"):
            p.is_wstunnel_running()

    def test_windows_raises_not_implemented(self) -> None:
        from warpsocket_server.platforms.windows import WindowsServerPlatform

        p = WindowsServerPlatform()
        with pytest.raises(PlatformError, match="not implemented"):
            p.is_wstunnel_running()
