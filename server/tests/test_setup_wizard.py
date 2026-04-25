from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket_server.setup_wizard import _detect_public_ip, _probe_localhost, run_setup


class TestProbeLocalhost:
    def test_returns_false_when_nothing_listening(self) -> None:
        # Use a port that's almost certainly not in use
        assert _probe_localhost(1) is False

    @patch("warpsocket_server.setup_wizard.socket.create_connection")
    def test_returns_true_when_connect_succeeds(self, mock_conn: MagicMock) -> None:
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_conn.return_value.__exit__ = MagicMock(return_value=None)
        assert _probe_localhost(443) is True


class TestDetectPublicIp:
    @patch("warpsocket_server.setup_wizard.urllib.request.urlopen")
    def test_returns_ip_string(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"203.0.113.42\n"
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=None)
        assert _detect_public_ip() == "203.0.113.42"

    @patch("warpsocket_server.setup_wizard.urllib.request.urlopen")
    def test_returns_none_on_failure(self, mock_urlopen: MagicMock) -> None:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("fail")
        assert _detect_public_ip() is None


class TestRunSetup:
    @patch("warpsocket_server.setup_wizard._check_root", return_value=False)
    def test_aborts_when_not_root(self, mock_root: MagicMock, tmp_path: Path) -> None:
        ret = run_setup(tmp_path)
        assert ret == 1

    @patch("warpsocket_server.setup_wizard._find_wstunnel", return_value=None)
    @patch("warpsocket_server.setup_wizard._check_root", return_value=True)
    def test_aborts_when_wstunnel_missing(
        self, mock_root: MagicMock, mock_find: MagicMock, tmp_path: Path
    ) -> None:
        ret = run_setup(tmp_path)
        assert ret == 1

    @patch("warpsocket_server.setup_wizard._find_wg", return_value=None)
    @patch(
        "warpsocket_server.setup_wizard._find_wstunnel",
        return_value=Path("/usr/local/bin/wstunnel"),
    )
    @patch("warpsocket_server.setup_wizard._check_root", return_value=True)
    def test_aborts_when_wg_missing(
        self,
        mock_root: MagicMock,
        mock_wst: MagicMock,
        mock_wg: MagicMock,
        tmp_path: Path,
    ) -> None:
        ret = run_setup(tmp_path)
        assert ret == 1

    @patch("warpsocket_server.setup_wizard.Confirm.ask", return_value=False)
    @patch("warpsocket_server.setup_wizard._check_root", return_value=True)
    def test_aborts_when_user_refuses_overwrite(
        self,
        mock_root: MagicMock,
        mock_confirm: MagicMock,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "server_config.json").write_text("{}", encoding="utf-8")
        ret = run_setup(tmp_path)
        assert ret == 0
