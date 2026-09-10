from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from outwarp.platforms import Platform, PlatformError, get_platform
from outwarp.platforms.linux import LinuxPlatform
from outwarp.platforms.windows import WindowsPlatform


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --- factory ---

def test_get_platform_returns_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(get_platform(), WindowsPlatform)


def test_get_platform_returns_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(get_platform(), LinuxPlatform)


def test_get_platform_raises_for_macos(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(PlatformError, match="Unsupported platform"):
        get_platform()


def test_get_platform_raises_for_unknown(monkeypatch):
    monkeypatch.setattr("sys.platform", "freebsd")
    with pytest.raises(PlatformError, match="Unsupported platform"):
        get_platform()


# --- WindowsPlatform: install / uninstall WG tunnel ---

def test_install_wg_tunnel_writes_conf_and_invokes_wireguard(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")

    # install_wg_tunnel does several subprocess.run calls in sequence:
    #   1. `sc query …` — stale-check (rc != 0 = no stale service, take fast path)
    #   2. `wireguard.exe /installtunnelservice <conf>` — actual install
    #   3+. `sc query …` polled until stdout contains "RUNNING"
    install_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        install_calls.append(list(cmd))
        if cmd[0] == "sc" and cmd[1] == "query":
            # No stale service before install; reports RUNNING during poll.
            if any("/installtunnelservice" in str(prev[1] if len(prev) > 1 else "")
                   for prev in install_calls[:-1]):
                return _mock_run(0, stdout="STATE : 4 RUNNING")
            return _mock_run(1060, stderr="service not found")
        return _mock_run(0)

    with patch("subprocess.run", side_effect=fake_run):
        path = p.install_wg_tunnel("MyTunnel", "[Interface]\nPrivateKey=...")

    assert path == tmp_path / "MyTunnel.conf"
    assert path.read_text(encoding="utf-8").startswith("[Interface]")
    install_call = next(
        c for c in install_calls
        if "/installtunnelservice" in (c[1] if len(c) > 1 else "")
    )
    assert install_call[2] == str(path)


def test_install_wg_tunnel_raises_on_failure(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
    (tmp_path / "wireguard.exe").write_text("fake")

    with patch("subprocess.run", return_value=_mock_run(1, stderr="access denied")), \
            pytest.raises(PlatformError, match="access denied"):
        p.install_wg_tunnel("MyTunnel", "...")


def test_install_wg_tunnel_raises_when_wireguard_missing(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "missing.exe")
    with pytest.raises(PlatformError, match="WireGuard for Windows not found"):
        p.install_wg_tunnel("MyTunnel", "...")


def test_uninstall_wg_tunnel_idempotent_when_wireguard_missing(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "missing.exe")
    p.uninstall_wg_tunnel("MyTunnel")  # must not raise


def test_uninstall_wg_tunnel_calls_wireguard(tmp_path, monkeypatch):
    p = WindowsPlatform()
    monkeypatch.setattr(p, "_conf_dir", tmp_path)
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
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
    monkeypatch.setattr("outwarp.platforms.windows._WIREGUARD_EXE", tmp_path / "wireguard.exe")
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


def test_is_wg_tunnel_active_true_with_spanish_sc_output():
    # Regression: `sc query` translates the state name on non-English Windows
    # (e.g. "EN EJECUCIÓN" on Spanish locales). We must rely on the numeric
    # state code, not the localised string, or the watchdog will wrongly
    # decide the tunnel has died on every poll.
    spanish_output = (
        "NOMBRE_DE_SERVICIO: WireGuardTunnel$MyTunnel\n"
        "        TIPO               : 30  WIN32_OWN_PROCESS\n"
        "        ESTADO             : 4  EN EJECUCIÓN\n"
        "                                (STOPPABLE, NOT_PAUSABLE, ACCEPTS_SHUTDOWN)\n"
        "        WIN32_EXIT_CODE    : 0  (0x0)\n"
    )
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout=spanish_output)):
        assert p.is_wg_tunnel_active("MyTunnel") is True


def test_is_wg_tunnel_active_false_with_spanish_stopped_output():
    spanish_output = (
        "NOMBRE_DE_SERVICIO: WireGuardTunnel$MyTunnel\n"
        "        TIPO               : 30  WIN32_OWN_PROCESS\n"
        "        ESTADO             : 1  DETENIDO\n"
        "        WIN32_EXIT_CODE    : 0  (0x0)\n"
    )
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout=spanish_output)):
        assert p.is_wg_tunnel_active("MyTunnel") is False


def test_is_wg_tunnel_active_skips_type_and_exit_code_lines():
    # The TYPE line has a multi-digit code (e.g. "30") and the EXIT_CODE line
    # is "0  (0x...)" — the parser must skip both and pick the STATE line.
    # If the regex ever matched TYPE we'd get state=3 (StopPending) and the
    # tunnel would look like it's mid-shutdown.
    output = (
        "        TYPE               : 30  WIN32_OWN_PROCESS\n"
        "        STATE              : 4  RUNNING\n"
        "        WIN32_EXIT_CODE    : 0  (0x0)\n"
    )
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout=output)):
        assert p.is_wg_tunnel_active("MyTunnel") is True


# --- WindowsPlatform: gateway ---

def test_get_default_gateway_parses_ipv4():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="192.168.1.1\n")):
        assert p.get_default_gateway() == "192.168.1.1"


def test_get_default_gateway_raises_on_invalid_output():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(0, stdout="not-an-ip\n")), \
            pytest.raises(PlatformError, match="Could not parse"):
        p.get_default_gateway()


def test_get_default_gateway_raises_on_command_failure():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1, stderr="boom")), \
            pytest.raises(PlatformError, match="Failed to query default gateway"):
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
    with patch("subprocess.run", return_value=_mock_run(1, stderr="access denied")), \
            pytest.raises(PlatformError, match="Failed to add host route"):
        p.add_host_route("203.0.113.42", "192.168.1.1")


def test_remove_host_route_idempotent():
    p = WindowsPlatform()
    with patch("subprocess.run", return_value=_mock_run(1, stderr="not found")):
        p.remove_host_route("203.0.113.42")  # must not raise


def test_platform_is_abstract():
    with pytest.raises(TypeError):
        Platform()


# --- LinuxPlatform: helper-based privileged ops ---

@pytest.fixture
def linux_helper(tmp_path, monkeypatch):
    """Stand up a fake helper script so existence checks pass without sudo."""
    helper = tmp_path / "outwarp-priv"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("OUTWARP_HELPER", str(helper))
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
        path = p.install_wg_tunnel("OutWarp", "[Interface]\nPrivateKey=k\n")

    # Returned path is OutWarp's private conf dir, not /etc/wireguard — other
    # WireGuard tools (e.g. omarchy-vpn) scan that directory and adopt any
    # .conf they find, fighting OutWarp for control of its own interface.
    from pathlib import Path
    assert path == Path("/etc/wireguard-outwarp/OutWarp.conf")

    cmd = mock_run.call_args[0][0]
    assert cmd == [str(linux_helper), "up", "OutWarp"]
    # Conf text is piped via stdin so it never lives on the user's filesystem.
    assert mock_run.call_args.kwargs.get("input") == "[Interface]\nPrivateKey=k\n"


def test_linux_install_wg_tunnel_raises_on_helper_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1, stderr="Address already in use")), \
            pytest.raises(PlatformError, match="Address already in use"):
        p.install_wg_tunnel("OutWarp", "...")


def test_linux_install_wg_tunnel_raises_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTWARP_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    with pytest.raises(PlatformError, match="Privileged helper not found"):
        p.install_wg_tunnel("OutWarp", "...")


def test_linux_uninstall_wg_tunnel_no_op_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTWARP_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    p.uninstall_wg_tunnel("OutWarp")  # must not raise


def test_linux_uninstall_wg_tunnel_calls_helper_down(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.uninstall_wg_tunnel("OutWarp")
    assert mock_run.call_args[0][0] == [str(linux_helper), "down", "OutWarp"]


# --- LinuxPlatform: tunnel status ---

def test_linux_is_wg_tunnel_active_true_when_helper_succeeds(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)):
        assert p.is_wg_tunnel_active("OutWarp") is True


def test_linux_is_wg_tunnel_active_false_when_helper_fails(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1)):
        assert p.is_wg_tunnel_active("OutWarp") is False


def test_linux_is_wg_tunnel_active_false_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTWARP_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    assert p.is_wg_tunnel_active("OutWarp") is False


# --- LinuxPlatform: kill switch (helper-backed nftables) ---

def test_linux_engage_kill_switch_invokes_helper_with_allowlist(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.engage_kill_switch(["203.0.113.42", "198.51.100.7"])
    assert mock_run.call_args[0][0] == [
        str(linux_helper), "killswitch-on", "203.0.113.42", "198.51.100.7",
    ]


def test_linux_engage_kill_switch_rejects_empty_allowlist(linux_helper):
    p = _linux_platform(linux_helper)
    with pytest.raises(PlatformError, match="empty allowlist"):
        p.engage_kill_switch([])


def test_linux_engage_kill_switch_raises_on_helper_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with (
        patch("subprocess.run", return_value=_mock_run(2, stderr="nft not installed")),
        pytest.raises(PlatformError, match="Failed to engage kill switch"),
    ):
        p.engage_kill_switch(["203.0.113.42"])


def test_linux_release_kill_switch_calls_helper_off(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.release_kill_switch()
    assert mock_run.call_args[0][0] == [str(linux_helper), "killswitch-off"]


def test_linux_release_kill_switch_no_op_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTWARP_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    p.release_kill_switch()  # must not raise


def test_linux_is_kill_switch_engaged_reflects_helper_status(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)):
        assert p.is_kill_switch_engaged() is True
    with patch("subprocess.run", return_value=_mock_run(1)):
        assert p.is_kill_switch_engaged() is False


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
    with patch("subprocess.run", return_value=_mock_run(0, stdout="default dev eth0\n")), \
            pytest.raises(PlatformError, match="Could not parse"):
        p.get_default_gateway()


def test_linux_get_default_gateway_raises_on_command_failure(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(1, stderr="boom")), \
            pytest.raises(PlatformError, match="Failed to query default gateway"):
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
    with patch("subprocess.run", return_value=_mock_run(1, stderr="permission denied")), \
            pytest.raises(PlatformError, match="Failed to add host route"):
        p.add_host_route("203.0.113.42", "192.168.1.1")


def test_linux_remove_host_route_idempotent(linux_helper):
    p = _linux_platform(linux_helper)
    with patch("subprocess.run", return_value=_mock_run(0)) as mock_run:
        p.remove_host_route("203.0.113.42")
    assert mock_run.call_args[0][0] == [str(linux_helper), "route-del", "203.0.113.42"]


def test_linux_remove_host_route_no_op_when_helper_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTWARP_HELPER", raising=False)
    p = LinuxPlatform(helper=tmp_path / "missing-helper", sudo=False)
    p.remove_host_route("203.0.113.42")  # must not raise


# --- WindowsPlatform: autostart (HKCU\…\Run) ---

class _FakeWinreg:
    """In-memory stand-in for the stdlib winreg module.

    Only the surface the autostart code actually touches is implemented:
    OpenKey as a context manager, SetValueEx, QueryValueEx, DeleteValue."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 0x2
    KEY_READ = 0x20019
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def OpenKey(self, hive, subkey, reserved, access):  # noqa: N802
        winreg_self = self

        class _CtxKey:
            def __enter__(self):
                return winreg_self
            def __exit__(self, *exc):
                return False

        return _CtxKey()

    def SetValueEx(self, key, name, reserved, value_type, value):  # noqa: N802
        self.values[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_windows_install_autostart_writes_run_key():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    with patch.dict(sys.modules, {"winreg": fake}):
        p.install_autostart([r"C:\Program Files\OutWarp\outwarp.exe"])
    # Path with a space gets quoted so cmd.exe parses it as one argv item.
    assert fake.values["OutWarp"] == r'"C:\Program Files\OutWarp\outwarp.exe"'


def test_windows_install_autostart_joins_multi_arg_command():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    with patch.dict(sys.modules, {"winreg": fake}):
        p.install_autostart([r"C:\Python\python.exe", "-m", "outwarp"])
    # No spaces in any argv item → no quoting added; just space-joined for cmd.
    assert fake.values["OutWarp"] == r"C:\Python\python.exe -m outwarp"


def test_windows_install_autostart_quotes_argv_item_with_space():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    with patch.dict(sys.modules, {"winreg": fake}):
        p.install_autostart([r"C:\Program Files\Python\python.exe", "-m", "outwarp"])
    # The path with a space must be quoted so cmd.exe parses it as one item.
    assert fake.values["OutWarp"] == r'"C:\Program Files\Python\python.exe" -m outwarp'


def test_windows_install_autostart_overwrites_existing():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    fake.values["OutWarp"] = "old-value"
    with patch.dict(sys.modules, {"winreg": fake}):
        p.install_autostart(["new.exe"])
    assert fake.values["OutWarp"] == "new.exe"


def test_windows_uninstall_autostart_removes_value():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    fake.values["OutWarp"] = "x"
    with patch.dict(sys.modules, {"winreg": fake}):
        p.uninstall_autostart()
    assert "OutWarp" not in fake.values


def test_windows_uninstall_autostart_idempotent_when_absent():
    p = WindowsPlatform()
    fake = _FakeWinreg()  # empty
    with patch.dict(sys.modules, {"winreg": fake}):
        p.uninstall_autostart()  # must not raise


def test_windows_is_autostart_installed_reflects_value_presence():
    p = WindowsPlatform()
    fake = _FakeWinreg()
    with patch.dict(sys.modules, {"winreg": fake}):
        assert p.is_autostart_installed() is False
        fake.values["OutWarp"] = "anything"
        assert p.is_autostart_installed() is True


def test_windows_install_autostart_rejects_empty_command():
    p = WindowsPlatform()
    with pytest.raises(PlatformError, match="empty command"):
        p.install_autostart([])


# --- LinuxPlatform: autostart (~/.config/autostart/outwarp.desktop) ---

def test_linux_install_autostart_writes_desktop_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    p.install_autostart(["/usr/bin/outwarp"])
    f = tmp_path / "autostart" / "outwarp.desktop"
    assert f.exists()
    body = f.read_text(encoding="utf-8")
    assert "[Desktop Entry]" in body
    assert "Type=Application" in body
    assert "Exec=/usr/bin/outwarp" in body
    assert "X-GNOME-Autostart-enabled=true" in body


def test_linux_install_autostart_quotes_paths_with_spaces(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    p.install_autostart(["/opt/with space/outwarp", "--silent"])
    body = (tmp_path / "autostart" / "outwarp.desktop").read_text(encoding="utf-8")
    # shlex.join emits POSIX-quoted items so XDG handlers don't split on the space.
    assert "Exec='/opt/with space/outwarp' --silent" in body


def test_linux_install_autostart_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "autostart").mkdir()
    (tmp_path / "autostart" / "outwarp.desktop").write_text("STALE\n")
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    p.install_autostart(["/usr/bin/outwarp"])
    body = (tmp_path / "autostart" / "outwarp.desktop").read_text(encoding="utf-8")
    assert "STALE" not in body
    assert "Exec=/usr/bin/outwarp" in body


def test_linux_uninstall_autostart_removes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "autostart").mkdir()
    f = tmp_path / "autostart" / "outwarp.desktop"
    f.write_text("...")
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    p.uninstall_autostart()
    assert not f.exists()


def test_linux_uninstall_autostart_idempotent_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    p.uninstall_autostart()  # must not raise


def test_linux_is_autostart_installed_reflects_file_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = LinuxPlatform(helper=tmp_path / "h", sudo=False)
    assert p.is_autostart_installed() is False
    p.install_autostart(["/usr/bin/outwarp"])
    assert p.is_autostart_installed() is True


# --- WindowsPlatform: kill switch (netsh advfirewall) ---

def _is_netsh_add_rule(cmd, rule_name):
    return (
        len(cmd) >= 6
        and cmd[:5] == ["netsh", "advfirewall", "firewall", "add", "rule"]
        and any(a == f"name={rule_name}" for a in cmd)
    )


def _is_netsh_delete_rule(cmd, rule_name):
    return (
        len(cmd) >= 6
        and cmd[:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]
        and any(a == f"name={rule_name}" for a in cmd)
    )


def _is_netsh_show_rule(cmd, rule_name):
    return (
        len(cmd) >= 6
        and cmd[:5] == ["netsh", "advfirewall", "firewall", "show", "rule"]
        and any(a == f"name={rule_name}" for a in cmd)
    )


def test_windows_engage_kill_switch_adds_allow_then_block():
    p = WindowsPlatform()
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        # delete-rule probes report "no such rule" (rc != 0) so the wipe is
        # a no-op; both add-rules succeed.
        if cmd[3] == "delete":
            return _mock_run(1, stderr="No rules match the specified criteria.")
        return _mock_run(0)

    with patch("subprocess.run", side_effect=fake_run):
        p.engage_kill_switch(["203.0.113.42", "203.0.113.43"])

    add_calls = [c for c in calls if c[3] == "add"]
    assert len(add_calls) == 2
    allow, block = add_calls
    assert _is_netsh_add_rule(allow, "OutWarp-KillSwitch-Allow")
    assert "remoteip=203.0.113.42,203.0.113.43" in allow
    assert "action=allow" in allow
    assert "dir=out" in allow
    assert _is_netsh_add_rule(block, "OutWarp-KillSwitch-Block")
    assert "action=block" in block


def test_windows_engage_kill_switch_wipes_stale_rules_first():
    """Idempotent engage: a half-applied previous run must not double-add."""
    p = WindowsPlatform()
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _mock_run(0)  # everything succeeds, including the wipe

    with patch("subprocess.run", side_effect=fake_run):
        p.engage_kill_switch(["1.1.1.1"])

    delete_calls = [c for c in calls if c[3] == "delete"]
    add_calls = [c for c in calls if c[3] == "add"]
    # Two deletes (block then allow) before the two adds.
    assert len(delete_calls) == 2
    assert len(add_calls) == 2
    # And they ran in that order.
    first_add_idx = next(i for i, c in enumerate(calls) if c[3] == "add")
    last_delete_idx = max(i for i, c in enumerate(calls) if c[3] == "delete")
    assert last_delete_idx < first_add_idx


def test_windows_engage_kill_switch_rejects_empty_allowlist():
    p = WindowsPlatform()
    with pytest.raises(PlatformError, match="empty allowlist"):
        p.engage_kill_switch([])


def test_windows_engage_kill_switch_rolls_back_allow_when_block_fails():
    p = WindowsPlatform()
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if cmd[3] == "delete":
            return _mock_run(1, stderr="No rules match the specified criteria.")
        if cmd[3] == "add" and any("OutWarp-KillSwitch-Block" in s for s in cmd):
            return _mock_run(1, stderr="boom")
        return _mock_run(0)  # the allow add succeeds first

    with patch("subprocess.run", side_effect=fake_run), \
            pytest.raises(PlatformError, match="block rule"):
        p.engage_kill_switch(["1.1.1.1"])

    # After the block-rule add failed we should have deleted the allow rule
    # we added, otherwise we leave a half-engaged switch behind.
    rollback_deletes = [
        c for c in calls
        if c[3] == "delete" and any("OutWarp-KillSwitch-Allow" in s for s in c)
    ]
    # 1 from the wipe before the engage attempt + 1 rollback delete = 2.
    assert len(rollback_deletes) == 2


def test_windows_release_kill_switch_deletes_both_rules():
    p = WindowsPlatform()
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _mock_run(0)

    with patch("subprocess.run", side_effect=fake_run):
        p.release_kill_switch()

    deleted = [c for c in calls if c[3] == "delete"]
    names = {a for c in deleted for a in c if a.startswith("name=")}
    assert "name=OutWarp-KillSwitch-Block" in names
    assert "name=OutWarp-KillSwitch-Allow" in names


def test_windows_release_kill_switch_idempotent_when_absent():
    p = WindowsPlatform()
    with patch(
        "subprocess.run",
        return_value=_mock_run(1, stderr="No rules match the specified criteria."),
    ):
        p.release_kill_switch()  # must not raise


def test_windows_is_kill_switch_engaged_reflects_block_rule_presence():
    p = WindowsPlatform()
    # show-rule rc=0 means present
    with patch("subprocess.run", return_value=_mock_run(0, stdout="...")):
        assert p.is_kill_switch_engaged() is True
    # rc=1 means absent
    with patch("subprocess.run", return_value=_mock_run(1, stderr="No rules match")):
        assert p.is_kill_switch_engaged() is False
