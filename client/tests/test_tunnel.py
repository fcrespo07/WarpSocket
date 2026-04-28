from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket.config import (
    ClientConfig,
    ReconnectConfig,
    RoutingConfig,
    ServerConfig,
    TlsConfig,
    TunnelConfig,
    WireguardConfig,
)
from warpsocket.network import NetworkError
from warpsocket.platforms.base import Platform, PlatformError
from warpsocket.tunnel import (
    Tunnel,
    TunnelError,
    build_wstunnel_command,
    find_wstunnel,
)


def _make_config() -> ClientConfig:
    return ClientConfig(
        schema_version=1,
        server=ServerConfig(endpoint="203.0.113.42", port=443, http_upgrade_path_prefix="s3cret"),
        tls=TlsConfig(cert_fingerprint_sha256="A" * 95),
        tunnel=TunnelConfig(local_port=51820, remote_host="10.0.0.1", remote_port=51820),
        wireguard=WireguardConfig(
            tunnel_name="WarpSocket",
            client_address="10.0.0.42/32",
            client_private_key="priv",
            server_public_key="pub",
        ),
        routing=RoutingConfig(bypass_ips=["203.0.113.42", "203.0.113.43"]),
        reconnect=ReconnectConfig(),
    )


class FakePlatform(Platform):
    def __init__(self) -> None:
        self.installed = False
        self.routes: list[str] = []
        self.gateway = "192.168.1.1"
        self.active = True

    def install_wg_tunnel(self, name, config_text):
        self.installed = True
        return Path("/fake/path.conf")

    def uninstall_wg_tunnel(self, name):
        self.installed = False

    def is_wg_tunnel_active(self, name):
        return self.active

    def get_default_gateway(self):
        return self.gateway

    def add_host_route(self, ip, gateway):
        self.routes.append(ip)

    def remove_host_route(self, ip):
        if ip in self.routes:
            self.routes.remove(ip)


# --- find_wstunnel ---

def test_find_wstunnel_uses_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "wstunnel.exe"
    fake.write_text("x")
    monkeypatch.setenv("WARPSOCKET_WSTUNNEL", str(fake))
    assert find_wstunnel() == fake


def test_find_wstunnel_raises_when_env_override_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WARPSOCKET_WSTUNNEL", str(tmp_path / "nope"))
    with pytest.raises(TunnelError, match="does not exist"):
        find_wstunnel()


def test_find_wstunnel_falls_back_to_path(monkeypatch, tmp_path):
    monkeypatch.delenv("WARPSOCKET_WSTUNNEL", raising=False)
    fake = tmp_path / "wstunnel"
    fake.write_text("x")
    with patch("warpsocket.tunnel.Path") as mock_path:
        # standard location does NOT exist
        mock_path.return_value.__truediv__.return_value.__truediv__.return_value.exists.return_value = False
        mock_path.side_effect = lambda x: Path(x)
        with patch("warpsocket.tunnel.shutil.which", return_value=str(fake)):
            assert find_wstunnel() == fake


def test_find_wstunnel_raises_when_nowhere(monkeypatch):
    monkeypatch.delenv("WARPSOCKET_WSTUNNEL", raising=False)
    with patch("warpsocket.tunnel.shutil.which", return_value=None):
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(TunnelError, match="wstunnel binary not found"):
                find_wstunnel()


# --- build_wstunnel_command ---

def test_build_wstunnel_command_has_expected_structure():
    cfg = _make_config()
    bin_path = Path("/usr/bin/wstunnel")
    cmd = build_wstunnel_command(cfg, bin_path)
    assert cmd[0] == str(bin_path)
    assert cmd[1] == "client"
    assert "--dangerous-disable-certificate-verification" in cmd
    assert "-L" in cmd
    forward = cmd[cmd.index("-L") + 1]
    assert forward == "udp://127.0.0.1:51820:10.0.0.1:51820?timeout_sec=0"
    assert "--http-upgrade-path-prefix" in cmd
    assert cmd[cmd.index("--http-upgrade-path-prefix") + 1] == "s3cret"
    assert cmd[-1] == "wss://203.0.113.42:443"


# --- Tunnel.connect / disconnect ---

def test_connect_happy_path():
    cfg = _make_config()
    plat = FakePlatform()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None

    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", return_value=fake_proc),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        t.connect()

    assert plat.installed is True
    assert sorted(plat.routes) == ["203.0.113.42", "203.0.113.43"]
    assert t._proc is fake_proc


def test_connect_aborts_when_endpoint_unreachable():
    cfg = _make_config()
    plat = FakePlatform()
    with patch("warpsocket.tunnel.tcp_probe", return_value=False):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        with pytest.raises(TunnelError, match="Cannot reach"):
            t.connect()
    # No state should leak through
    assert plat.installed is False
    assert plat.routes == []


def test_connect_aborts_on_fingerprint_mismatch():
    cfg = _make_config()
    plat = FakePlatform()
    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch(
            "warpsocket.tunnel.verify_tls_fingerprint",
            side_effect=NetworkError("fingerprint mismatch boom"),
        ),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        with pytest.raises(TunnelError, match="fingerprint mismatch"):
            t.connect()
    assert plat.installed is False
    assert plat.routes == []


def test_connect_rolls_back_when_wstunnel_fails_to_launch():
    cfg = _make_config()
    plat = FakePlatform()
    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", side_effect=OSError("exec failed")),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        with pytest.raises(OSError):
            t.connect()
    assert plat.installed is False
    assert plat.routes == []


def test_disconnect_reverses_state():
    cfg = _make_config()
    plat = FakePlatform()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None

    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", return_value=fake_proc),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        t.connect()
        t.disconnect()

    assert plat.installed is False
    assert plat.routes == []
    fake_proc.terminate.assert_called_once()


def test_disconnect_kills_process_if_terminate_times_out():
    cfg = _make_config()
    plat = FakePlatform()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    import subprocess
    fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="wstunnel", timeout=5), None]

    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", return_value=fake_proc),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        t.connect()
        t.disconnect()

    fake_proc.terminate.assert_called_once()
    fake_proc.kill.assert_called_once()


def test_disconnect_tolerates_platform_errors():
    cfg = _make_config()
    plat = FakePlatform()

    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", return_value=fake_proc),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        t.connect()

    # Simulate platform errors during disconnect — disconnect must not raise
    plat.uninstall_wg_tunnel = MagicMock(side_effect=PlatformError("boom"))
    plat.remove_host_route = MagicMock(side_effect=PlatformError("boom"))
    t.disconnect()  # must complete


def test_is_active_false_when_not_started():
    cfg = _make_config()
    plat = FakePlatform()
    t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
    assert t.is_active is False


def test_is_active_false_when_proc_died():
    cfg = _make_config()
    plat = FakePlatform()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None

    with (
        patch("warpsocket.tunnel.tcp_probe", return_value=True),
        patch("warpsocket.tunnel.verify_tls_fingerprint"),
        patch("warpsocket.tunnel.subprocess.Popen", return_value=fake_proc),
    ):
        t = Tunnel(cfg, platform=plat, wstunnel_bin=Path("/fake/wstunnel"))
        t.connect()

    fake_proc.poll.return_value = 1  # process exited
    assert t.is_active is False
