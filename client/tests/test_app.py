from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket.config import ClientConfig, ConfigError

# Re-use the VALID fixture
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


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(VALID), encoding="utf-8")
    return p


class TestLoadOrWizard:
    def test_loads_existing_config(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path)
        with patch("warpsocket.app.default_config_path", return_value=config_path):
            from warpsocket.app import _load_or_wizard
            cfg = _load_or_wizard()
            assert cfg is not None
            assert cfg.server.endpoint == "203.0.113.42"

    def test_runs_wizard_when_no_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nonexistent" / "config.json"
        fake_config = MagicMock(spec=ClientConfig)
        with (
            patch("warpsocket.app.default_config_path", return_value=config_path),
            patch("warpsocket.wizard.run_wizard", return_value=fake_config) as mock_wizard,
        ):
            from warpsocket.app import _load_or_wizard
            result = _load_or_wizard()
            mock_wizard.assert_called_once()
            assert result is fake_config

    def test_returns_none_when_wizard_cancelled(self, tmp_path: Path) -> None:
        config_path = tmp_path / "nonexistent" / "config.json"
        with (
            patch("warpsocket.app.default_config_path", return_value=config_path),
            patch("warpsocket.wizard.run_wizard", return_value=None),
        ):
            from warpsocket.app import _load_or_wizard
            result = _load_or_wizard()
            assert result is None

    def test_runs_wizard_on_corrupt_config(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text("{ not json }", encoding="utf-8")
        fake_config = MagicMock(spec=ClientConfig)
        with (
            patch("warpsocket.app.default_config_path", return_value=config_path),
            patch("warpsocket.wizard.run_wizard", return_value=fake_config),
        ):
            from warpsocket.app import _load_or_wizard
            result = _load_or_wizard()
            assert result is fake_config


class TestSingleInstanceLock:
    def test_acquire_and_release(self) -> None:
        from warpsocket.app import _SingleInstanceLock
        lock = _SingleInstanceLock()
        assert lock.acquire() is True
        lock.release()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only mutex test")
    def test_double_acquire_windows(self) -> None:
        from warpsocket.app import _SingleInstanceLock
        lock1 = _SingleInstanceLock()
        lock2 = _SingleInstanceLock()
        assert lock1.acquire() is True
        assert lock2.acquire() is False
        lock1.release()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only flock test")
    def test_double_acquire_posix(self) -> None:
        from warpsocket.app import _SingleInstanceLock
        lock1 = _SingleInstanceLock()
        lock2 = _SingleInstanceLock()
        assert lock1.acquire() is True
        assert lock2.acquire() is False
        lock1.release()

    def test_release_without_acquire_is_safe(self) -> None:
        from warpsocket.app import _SingleInstanceLock
        lock = _SingleInstanceLock()
        lock.release()  # Should not raise


class TestMainEntryPoint:
    def test_main_returns_zero_on_wizard_cancel(self, tmp_path: Path) -> None:
        from warpsocket.app import _SingleInstanceLock

        config_path = tmp_path / "nonexistent" / "config.json"
        with (
            patch("warpsocket.app.default_config_path", return_value=config_path),
            patch("warpsocket.wizard.run_wizard", return_value=None),
            patch.object(_SingleInstanceLock, "acquire", return_value=True),
            patch.object(_SingleInstanceLock, "release"),
        ):
            from warpsocket.app import main
            assert main() == 0

    def test_main_returns_one_on_lock_fail(self) -> None:
        from warpsocket.app import _SingleInstanceLock

        with (
            patch.object(_SingleInstanceLock, "acquire", return_value=False),
            patch("tkinter.messagebox.showwarning"),
        ):
            from warpsocket.app import main
            assert main() == 1
