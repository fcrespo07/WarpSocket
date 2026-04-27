"""warpsocket-uninstall — removes all client files installed by install.ps1 / install.sh."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _require_admin() -> None:
    if sys.platform == "win32":
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("Error: run this command from an elevated (Administrator) prompt.")
            print("  Right-click PowerShell → 'Run as administrator', then:")
            print("  warpsocket-uninstall")
            raise SystemExit(1)
    else:
        if os.geteuid() != 0:
            print("Error: run with sudo.")
            print("  sudo warpsocket-uninstall")
            raise SystemExit(1)


def _confirm(yes: bool) -> None:
    print()
    print("This will permanently remove:")
    for line in _what_gets_removed():
        print(f"  • {line}")
    print()
    if yes:
        return
    answer = input("Type 'yes' to confirm: ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        raise SystemExit(0)
    print()


def _what_gets_removed() -> list[str]:
    items = []
    prefix = _client_prefix()
    shim = _client_shim()
    config = _config_dir()
    startup = _startup_shortcut()
    desktop = _desktop_shortcut()
    if prefix:
        items.append(f"Install directory: {prefix}")
    if shim and shim.exists():
        items.append(f"CLI shim: {shim}")
    if config and config.exists():
        items.append(f"Config and logs: {config}")
    if startup and startup.exists():
        items.append(f"Startup shortcut: {startup}")
    if desktop and desktop.exists():
        items.append(f"Desktop shortcut: {desktop}")
    if not items:
        items.append("(nothing detected — already uninstalled?)")
    return items


# ---------------------------------------------------------------------------
# Path detection — mirrors what install.ps1 / install.sh write
# ---------------------------------------------------------------------------

def _client_prefix() -> Path | None:
    if sys.platform == "win32":
        prog = os.environ.get("ProgramFiles", "C:\\Program Files")
        p = Path(prog) / "WarpSocket" / "client"
        return p if p.exists() else None
    if sys.platform == "linux":
        p = Path("/opt/warpsocket-client")
        return p if p.exists() else None
    return None


def _client_shim() -> Path | None:
    if sys.platform == "win32":
        prog = os.environ.get("ProgramFiles", "C:\\Program Files")
        return Path(prog) / "WarpSocket" / "warpsocket.bat"
    if sys.platform == "linux":
        return Path("/usr/local/bin/warpsocket")
    return None


def _config_dir() -> Path | None:
    try:
        from platformdirs import user_config_dir
        return Path(user_config_dir("WarpSocket"))
    except Exception:
        return None


def _startup_shortcut() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "WarpSocket.lnk"
    return None


def _desktop_shortcut() -> Path | None:
    if sys.platform == "win32":
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            return Path(userprofile) / "Desktop" / "WarpSocket.lnk"
    return None


# ---------------------------------------------------------------------------
# Kill running instance
# ---------------------------------------------------------------------------

def _kill_running() -> None:
    """Best-effort: terminate any warpsocket process before deleting files."""
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(
                ["taskkill", "/F", "/IM", "warpsocket.exe"],
                capture_output=True,
            )
        else:
            import subprocess
            subprocess.run(["pkill", "-f", "warpsocket"], capture_output=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Deferred cleanup — deletes venv+shim after this process exits
# (we can't delete the tree we're running from while we're in it)
# ---------------------------------------------------------------------------

def _spawn_deferred_cleanup_windows(prefix: Path, shim: Path | None) -> None:
    import subprocess
    import tempfile

    pid = os.getpid()
    targets = [str(prefix)]
    if shim is not None:
        targets.append(str(shim))

    lines = [f'rmdir /s /q "{prefix}"']
    if shim is not None:
        lines.append(f'del /f /q "{shim}"')

    script = (
        "@echo off\r\n"
        f":wait\r\n"
        f"tasklist /FI \"PID eq {pid}\" 2>nul | find \"{pid}\" >nul && goto wait\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        + "\r\n".join(lines)
        + "\r\ndel /f /q \"%~f0\"\r\n"
    )
    fd, script_path = tempfile.mkstemp(prefix="warpsocket-uninstall-", suffix=".bat")
    with os.fdopen(fd, "w") as f:
        f.write(script)

    subprocess.Popen(
        ["cmd.exe", "/c", script_path],
        creationflags=0x00000008,  # DETACHED_PROCESS
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _spawn_deferred_cleanup_posix(prefix: Path, shim: Path | None) -> None:
    import shlex
    import subprocess
    import tempfile

    pid = os.getpid()
    lines = [f"rm -rf {shlex.quote(str(prefix))}"]
    if shim is not None:
        lines.append(f"rm -f {shlex.quote(str(shim))}")

    script = (
        "#!/usr/bin/env bash\n"
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done\n"
        "sleep 1\n"
        + "\n".join(lines)
        + "\nrm -f \"$0\"\n"
    )
    fd, script_path = tempfile.mkstemp(prefix="warpsocket-uninstall-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    subprocess.Popen(
        ["/bin/bash", script_path],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="warpsocket-uninstall",
        description="Remove all WarpSocket client files installed by the official installer.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )
    args = parser.parse_args(argv)

    _require_admin()
    _confirm(args.yes)

    warnings: list[str] = []

    def step(label: str, fn) -> None:
        print(f"  {label}...", end=" ", flush=True)
        try:
            fn()
            print("done")
        except Exception as exc:
            print(f"warning: {exc}")
            warnings.append(f"{label}: {exc}")

    _kill_running()

    config = _config_dir()
    startup = _startup_shortcut()
    desktop = _desktop_shortcut()
    prefix = _client_prefix()
    shim = _client_shim()

    if config and config.exists():
        step(f"Removing config/logs ({config})", lambda: shutil.rmtree(config))

    if startup and startup.exists():
        step("Removing startup shortcut", startup.unlink)

    if desktop and desktop.exists():
        step("Removing desktop shortcut", desktop.unlink)

    # The venv and shim are deferred: we can't delete the tree we're running from.
    if prefix and prefix.exists():
        try:
            if sys.platform == "win32":
                _spawn_deferred_cleanup_windows(prefix, shim if shim and shim.exists() else None)
            else:
                _spawn_deferred_cleanup_posix(prefix, shim if shim and shim.exists() else None)
            shim_note = f" and {shim}" if shim else ""
            print(f"  Scheduled removal of {prefix}{shim_note} after exit... done")
        except Exception as exc:
            warnings.append(f"deferred cleanup: {exc}")
            print(f"  Scheduling deferred cleanup... warning: {exc}")
    elif shim and shim.exists():
        step(f"Removing CLI shim ({shim})", shim.unlink)

    print()
    if warnings:
        print(f"Uninstall completed with {len(warnings)} warning(s) — see above.")
        return 1

    print("WarpSocket client uninstalled successfully.")
    print("You can also remove WireGuard for Windows from Add/Remove Programs if no longer needed.")
    return 0
