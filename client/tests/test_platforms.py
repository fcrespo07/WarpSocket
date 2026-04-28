from __future__ import annotations

import subprocess
import sys
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
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")
    # First call: wireguard /uninstalltunnelservice → success
    # Second call: sc query → service already gone (rc != 0) → stop polling
    with patch("subprocess.run", side_effect=[_mock_run(0), _mock_run(1)]) as mock_run:
        p.uninstall_wg_tunnel("MyTunnel")
    first_args = mock_run.call_args_list[0][0][0]
    assert first_args[1] == "/uninstalltunnelservice"
    assert first_args[2] == "MyTunnel"


def test_uninstall_wg_tunnel_deletes_conf_file(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("warpsocket.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")
    conf = tmp_path / "MyTunnel.conf"
    conf.write_text("[Interface]")
    with patch("subprocess.run", side_effect=[_mock_run(0), _mock_run(1)]):
        p.uninstall_wg_tunnel("MyTunnel")
    assert not conf.exists()


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


# --- Stubs raise on macOS ---

def test_macos_stub_raises_for_install():
    p = MacOSPlatform()
    with pytest.raises(PlatformError, match="not implemented"):
        p.install_wg_tunnel("x", "...")


def test_platform_is_abstract():
    with pytest.raises(TypeError):
        Platform()


# --- LinuxPlatform: helper-based privileged ops ---

@pytest.fixture
def linux_helper(tmp_path, monkeypatch):
    """Stand up a fake helper script so existence checks pass without sudo."""
    helper = tmp_path / "warpsocket-priv"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("WARPSOCKET_HELPER", str(helper))
    return helper


def _linux_platform(helper, *, sudo=False):
    return LinuxPlatform(helper=helper, sudo=sudo)


def test_linux_prepends_sudo_when_not_root(linux_helper):
    p = _linux_platform(linux_helper, sudo=True)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.add_host_route("203.0.113.42", "192.168.1.1")
    assert mock_run.call_args[0][0] == [
        "sudo", "-n", str(linux_helper), "route-add", "203.0.113.42", "192.168.1.1",
    ]


def test_linux_skips_sudo_when_already_root(linux_helper):
    p = _linux_platform(linux_helper, sudo=False)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.add_host_route("203.0.113.42", "192.168.1.1")
    assert mock_run.call_args[0][0][0] == str(linux_helper)


def test_linux_install_wg_tunnel_invokes_helper_with_conf_on_stdin(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        path = p.install_wg_tunnel("WarpSocket", "[Interface]\nPrivateKey=k\n")

    # Returned path is the canonical /etc/wireguard location written by the helper.
    from pathlib import Path as _P
    assert path == _P("/etc/wireguard/WarpSocket.conf")

    cmd = mock_run.call_args[0][0]
    assert cmd == [str(linux_helper), "up", "WarpSocket"]
    # Conf text is piped via stdin so it never lives on the user's filesystem.
    assert mock_run.call_args.kwargs.get("input") == "[Interface]\nPrivateKey=k\n"


def test_linux_install_wg_tunnel_raises_on_helper_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1, stderr="Address already in use")):
        with pytest.raises(PlatformError, match="Address already in use"):
            p.install_wg_tunnel("WarpSocket", "...")


def test_linux_install_wg_tunnel_raises_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WARPSOCKET_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    with pytest.raises(PlatformError, match="Privileged helper not found"):
        p.install_wg_tunnel("WarpSocket", "...")


def test_linux_uninstall_wg_tunnel_no_op_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WARPSOCKET_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    p.uninstall_wg_tunnel("WarpSocket")  # must not raise


def test_linux_uninstall_wg_tunnel_calls_helper_down(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.uninstall_wg_tunnel("WarpSocket")
    assert mock_run.call_args[0][0] == [str(linux_helper), "down", "WarpSocket"]


# --- LinuxPlatform: tunnel status ---

def test_linux_is_wg_tunnel_active_true_when_helper_succeeds(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)):
        assert p.is_wg_tunnel_active("WarpSocket") is True


def test_linux_is_wg_tunnel_active_false_when_helper_fails(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1)):
        assert p.is_wg_tunnel_active("WarpSocket") is False


def test_linux_is_wg_tunnel_active_false_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WARPSOCKET_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    assert p.is_wg_tunnel_active("WarpSocket") is False


# --- LinuxPlatform: gateway (unprivileged, calls `ip` directly) ---

def test_linux_get_default_gateway_parses_first_route(linux_helper):
    p = _linux_platform(linux_helper)
    output = "default via 192.168.1.1 dev eth0 proto dhcp metric 100\n"
    with patch("subprocess.run", return_value=_mock_run(0, stdout=output)) as mock_run:
        assert p.get_default_gateway() == "192.168.1.1"
    # Confirm no helper / sudo prefix on the unprivileged read.
    assert mock_run.call_args[0][0] == ["ip", "-4", "route", "show", "default"]


def test_linux_get_default_gateway_skips_non_ipv4_lines(linux_helper):
    p = _linux_platform(linux_helper)
    output = (
        "default proto static metric 600\n"
        "default via 10.0.0.1 dev wlan0 proto dhcp metric 600\n"
    )
    with patch("subprocess.run", return_value=_mock_run(0, stdout=output)):
        assert p.get_default_gateway() == "10.0.0.1"


def test_linux_get_default_gateway_raises_on_invalid_output(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0, stdout="default dev eth0\n")):
        with pytest.raises(PlatformError, match="Could not parse"):
            p.get_default_gateway()


def test_linux_get_default_gateway_raises_on_command_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1, stderr="boom")):
        with pytest.raises(PlatformError, match="Failed to query default gateway"):
            p.get_default_gateway()


# --- LinuxPlatform: routes ---

def test_linux_add_host_route_calls_helper_route_add(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.add_host_route("203.0.113.42", "192.168.1.1")
    assert mock_run.call_args[0][0] == [
        str(linux_helper), "route-add", "203.0.113.42", "192.168.1.1",
    ]


def test_linux_add_host_route_idempotent_when_already_exists(linux_helper):
    p = _linux_platform(linux_helper)
    with patch(
        "subprocess.run",
        return_value=_mock_run(2, stderr="RTNETLINK answers: File exists\n"),
    ):
        p.add_host_route("203.0.113.42", "192.168.1.1")  # must not raise


def test_linux_add_host_route_raises_on_other_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1, stderr="permission denied")):
        with pytest.raises(PlatformError, match="Failed to add host route"):
            p.add_host_route("203.0.113.42", "192.168.1.1")


def test_linux_remove_host_route_idempotent(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.remove_host_route("203.0.113.42")
    assert mock_run.call_args[0][0] == [str(linux_helper), "route-del", "203.0.113.42"]


def test_linux_remove_host_route_no_op_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("WARPSOCKET_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    p.remove_host_route("203.0.113.42")  # must not raise
