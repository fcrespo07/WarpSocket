from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from warpsocket.platforms import Platform, PlatformError, get_platform
from warpsocket.platforms.linux import LinuxPlatform
from warpsocket.platforms.macos import MacOSPlatform
from warpsocket.platforms.windows import WindowsPlatform


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- factory ---

def test_get_platform_returns_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(get_platform(), WindowsPlatform)


def test_get_platform_returns_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(get_platform(), LinuxPlatform)


def test_get_platform_returns_macos(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert isinstance(get_platform(), MacOSPlatform)


def test_get_platform_raises_for_unknown(monkeypatch):
    monkeypatch.setattr("sys.platform", "freebsd")
    with pytest.raises(PlatformError, match="Unsupported platform"):
        get_platform()


# --- WindowsPlatform: install / uninstall WG tunnel ---

def test_install_wg_tunnel_writes_conf_and_invokes_wireguard(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")

    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        path = p.install_wg_tunnel("MyTunnel", "[Interface]\nPrivateKey=...")

    assert path == tmp_path / "MyTunnel.conf"
    assert path.read_text(encoding="utf-8").startswith("[Interface]")
    args = mock_run.call_args[0][0]
    assert args[1] == "/installtunnelservice"
    assert args[2] == str(path)


def test_install_wg_tunnel_raises_on_failure(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")

    with patch("subprocess.run", return_value=_mock_run(1, stderr="access denied")):
        with pytest.raises(PlatformError, match="access denied"):
            p.install_wg_tunnel("MyTunnel", "...")


def test_install_wg_tunnel_raises_when_wireguard_missing(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "missing.exe")
    with pytest.raises(PlatformError, match="WireGuard for Windows not found"):
        p.install_wg_tunnel("MyTunnel", "...")


def test_uninstall_wg_tunnel_idempotent_when_wireguard_missing(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "missing.exe")
    p.uninstall_wg_tunnel("MyTunnel")  # must not raise


def test_uninstall_wg_tunnel_calls_wireguard(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.uninstall_wg_tunnel("MyTunnel")
    args = mock_run.call_args[0][0]
    assert args[1] == "/uninstalltunnelservice"
    assert args[2] == "MyTunnel"


# --- WindowsPlatform: tunnel status ---

def test_is_wg_tunnel_active_true_when_running():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="STATE : 4 RUNNING")):
        assert p.is_wg_tunnel_active("MyTunnel") is True


def test_is_wg_tunnel_active_false_when_stopped():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="STATE : 1 STOPPED")):
        assert p.is_wg_tunnel_active("MyTunnel") is False


def test_is_wg_tunnel_active_false_when_service_not_found():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1060, stderr="service not found")):
        assert p.is_wg_tunnel_active("MyTunnel") is False


# --- WindowsPlatform: gateway ---

def test_get_default_gateway_parses_ipv4():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="192.168.1.1\n")):
        assert p.get_default_gateway() == "192.168.1.1"


def test_get_default_gateway_raises_on_invalid_output():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="not-an-ip\n")):
        with pytest.raises(PlatformError, match="Could not parse"):
            p.get_default_gateway()


def test_get_default_gateway_raises_on_command_failure():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1, stderr="boom")):
        with pytest.raises(PlatformError, match="Failed to query default gateway"):
            p.get_default_gateway()


# --- WindowsPlatform: routes ---

def test_add_host_route_calls_route_add():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.add_host_route("203.0.113.42", "192.168.1.1")
    args = mock_run.call_args[0][0]
    assert args == ["route", "add", "203.0.113.42", "MASK", "255.255.255.255", "192.168.1.1"]


def test_add_host_route_raises_on_failure():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1, stderr="access denied")):
        with pytest.raises(PlatformError, match="Failed to add host route"):
            p.add_host_route("203.0.113.42", "192.168.1.1")


def test_remove_host_route_idempotent():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1, stderr="not found")):
        p.remove_host_route("203.0.113.42")  # must not raise


# --- Stubs raise on Linux/macOS ---

@pytest.mark.parametrize("cls", [LinuxPlatform, MacOSPlatform])
def test_stub_platforms_raise_for_install(cls):
    p = cls()
    with pytest.raises(PlatformError, match="not implemented"):
        p.install_wg_tunnel("x", "...")


def test_platform_is_abstract():
    with pytest.raises(TypeError):
        Platform()
