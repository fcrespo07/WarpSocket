from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from rich.console import Console
from rich.table import Table

from warpsocket_server import __version__
from warpsocket_server.config import (
    ClientEntry,
    ConfigError,
    ServerConfig,
    default_config_dir,
    default_config_path,
)
from warpsocket_server.crypto import generate_wg_keypair
from warpsocket_server.ip_pool import PoolExhaustedError, next_available_ip
from warpsocket_server.warpcfg import build_warpcfg, write_warpcfg
from warpsocket_server.wireguard import (
    add_peer_live,
    build_server_wg_conf,
    get_live_peers,
    remove_peer_live,
)

log = logging.getLogger(__name__)
console = Console()


# Commands that read/modify privileged state (systemd, /etc/wireguard, wg interface).
# Anything not in this set runs without a root check (--version, --help).
_PRIVILEGED_COMMANDS = frozenset({
    "setup", "add-client", "list-clients", "revoke-client",
    "status", "restart", "uninstall",
})


def _require_root(command: str) -> None:
    """Exit with a friendly error if the current user can't run privileged ops."""
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin() != 0:
                return
        except Exception:
            return
        console.print(
            "[red]Error:[/red] This command must be run as Administrator.\n"
            f"  Open an elevated PowerShell and run: [bold]warpsocket-server {command}[/bold]"
        )
        raise SystemExit(1)

    if os.geteuid() == 0:
        return
    console.print(
        f"[red]Error:[/red] This command must be run as root.\n"
        f"  Try: [bold]sudo warpsocket-server {command}[/bold]"
    )
    raise SystemExit(1)


def _resolve_config_path(args: argparse.Namespace) -> Path:
    if args.config_dir:
        return Path(args.config_dir) / "server_config.json"
    return default_config_path()


def _load_config(args: argparse.Namespace) -> ServerConfig:
    path = _resolve_config_path(args)
    try:
        return ServerConfig.load(path)
    except ConfigError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        console.print(
            "Run [bold]warpsocket-server setup[/bold] first to configure the server."
        )
        raise SystemExit(1)


def _cmd_setup(args: argparse.Namespace) -> int:
    from warpsocket_server.setup_wizard import run_setup

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    return run_setup(config_dir)


def _cmd_add_client(args: argparse.Namespace) -> int:
    config = _load_config(args)
    name = args.name

    # Check for duplicate name
    for c in config.clients:
        if c.name == name:
            console.print(f"[red]Error:[/red] Client '{name}' already exists.")
            return 1

    # Generate WG keypair for the client
    try:
        client_private_key, client_public_key = generate_wg_keypair()
    except Exception as exc:
        console.print(f"[red]Error generating WireGuard keys:[/red] {exc}")
        return 1

    # Allocate next IP
    allocated = [c.address for c in config.clients]
    try:
        client_address = next_available_ip(config.subnet, config.server_address, allocated)
    except PoolExhaustedError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        return 1

    # Add peer to running WireGuard
    try:
        add_peer_live(client_public_key, client_address)
    except Exception as exc:
        log.warning("Could not hot-add peer (WireGuard may not be running): %s", exc)

    # Update server config
    new_client = ClientEntry(name=name, public_key=client_public_key, address=client_address)
    updated = replace(config, clients=[*config.clients, new_client])
    config_path = _resolve_config_path(args)
    updated.save(config_path)

    # Persist WG server conf to the OS location wg-quick uses (e.g. /etc/wireguard/wg0.conf).
    # Hot reload via wg syncconf — keeps existing peers connected. PostUp/PostDown won't
    # re-run, but they don't change between calls (only the peer list does).
    from warpsocket_server.platforms import PlatformError, get_server_platform
    try:
        get_server_platform().install_wg_config(build_server_wg_conf(updated))
    except PlatformError as exc:
        log.warning("Could not persist WG config to OS location: %s", exc)

    # Build and write .warpcfg
    warpcfg = build_warpcfg(config, name, client_private_key, client_address)
    warpcfg_path = Path.cwd() / f"{name}.warpcfg"
    write_warpcfg(warpcfg, warpcfg_path)

    console.print(f"\n[green]Client '{name}' added successfully.[/green]")
    console.print(f"  IP: {client_address}")
    console.print(f"  Config: [bold]{warpcfg_path}[/bold]")
    console.print(
        "\nSend this .warpcfg file to the client securely — "
        "it contains the client's private key."
    )
    return 0


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        f /= 1024
        if f < 1024:
            return f"{f:.1f} {u}"
    return f"{f:.1f} PB"


def _format_seconds_ago(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h ago"


# A peer is considered "online" if its last handshake is within this window.
# WireGuard renews handshakes roughly every 2 minutes; 3 minutes leaves margin.
_ONLINE_WINDOW_SECONDS = 180


def _cmd_list_clients(args: argparse.Namespace) -> int:
    import time

    config = _load_config(args)

    if not config.clients:
        console.print("No clients registered. Use [bold]add-client[/bold] to add one.")
        return 0

    live_peers = get_live_peers()
    now = int(time.time())

    table = Table(title="Registered Clients")
    table.add_column("Name", style="bold")
    table.add_column("Address")
    table.add_column("Status")
    table.add_column("Last handshake")
    table.add_column("Endpoint")
    table.add_column("RX / TX")

    for c in config.clients:
        live = live_peers.get(c.public_key)
        if live is None:
            # Peer not even known to running WG (interface down, or peer not synced)
            status = "[dim]unknown[/dim]"
            handshake = "[dim]—[/dim]"
            endpoint = "[dim]—[/dim]"
            transfer = "[dim]—[/dim]"
        elif live.latest_handshake is None:
            status = "[yellow]idle[/yellow]"
            handshake = "[dim]never[/dim]"
            endpoint = "[dim]—[/dim]"
            transfer = "0 B / 0 B"
        else:
            seconds_ago = now - live.latest_handshake
            if seconds_ago < _ONLINE_WINDOW_SECONDS:
                status = "[green]online[/green]"
            else:
                status = "[yellow]offline[/yellow]"
            handshake = _format_seconds_ago(seconds_ago)
            endpoint = live.endpoint or "[dim]—[/dim]"
            transfer = f"{_format_bytes(live.transfer_rx)} / {_format_bytes(live.transfer_tx)}"

        table.add_row(c.name, c.address, status, handshake, endpoint, transfer)

    console.print(table)
    return 0


def _cmd_revoke_client(args: argparse.Namespace) -> int:
    config = _load_config(args)
    name = args.name

    target = None
    for c in config.clients:
        if c.name == name:
            target = c
            break

    if target is None:
        console.print(f"[red]Error:[/red] Client '{name}' not found.")
        return 1

    # Remove peer from running WireGuard
    try:
        remove_peer_live(target.public_key)
    except Exception as exc:
        log.warning("Could not hot-remove peer (WireGuard may not be running): %s", exc)

    # Update config
    updated = replace(config, clients=[c for c in config.clients if c.name != name])
    config_path = _resolve_config_path(args)
    updated.save(config_path)

    # Persist updated peer list to the OS WG config (hot reload, keeps others connected).
    from warpsocket_server.platforms import PlatformError, get_server_platform
    try:
        get_server_platform().install_wg_config(build_server_wg_conf(updated))
    except PlatformError as exc:
        log.warning("Could not persist WG config to OS location: %s", exc)

    console.print(f"[green]Client '{name}' revoked.[/green]")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    """Regenerate wg0.conf from current config and fully restart wg-quick + wstunnel.

    Use this after upgrading WarpSocket or whenever PostUp/PostDown rules change —
    a hot reload (wg syncconf) does NOT re-run those scripts.
    """
    from warpsocket_server.platforms import PlatformError, get_server_platform

    config = _load_config(args)
    platform = get_server_platform()

    console.print("[bold]Regenerating WireGuard config...[/bold]")
    try:
        platform.install_wg_config(build_server_wg_conf(config))
        console.print("  [green]✓[/green] wg0.conf written")
    except PlatformError as exc:
        console.print(f"  [red]✗[/red] WireGuard config: {exc}")
        return 1

    console.print("[bold]Restarting WireGuard interface (PostUp/PostDown will re-run)...[/bold]")
    try:
        platform.restart_wg()
        console.print("  [green]✓[/green] wg-quick restarted")
    except PlatformError as exc:
        console.print(f"  [red]✗[/red] WireGuard restart: {exc}")
        return 1

    console.print("[bold]Restarting wstunnel service...[/bold]")
    try:
        platform.restart_wstunnel_service()
        console.print("  [green]✓[/green] wstunnel restarted")
    except PlatformError as exc:
        console.print(f"  [red]✗[/red] wstunnel restart: {exc}")
        return 1

    console.print("\n[green]Server restarted successfully.[/green]")
    return 0


def _spawn_deferred_cleanup(install_prefix: Path, bin_link: Path | None) -> None:
    """Spawn a detached shell script that removes paths AFTER this process exits.

    Necessary because the running Python interpreter lives inside install_prefix
    (e.g. /opt/warpsocket-server/.venv) — we can't rmtree the dir we're executing
    from. The script polls for our PID to disappear, then deletes itself last.
    """
    import os
    import shlex
    import subprocess
    import tempfile

    pid = os.getpid()
    targets = [str(install_prefix)]
    if bin_link is not None:
        targets.append(str(bin_link))

    # Build rm commands: -rf for the prefix (directory), -f for the symlink.
    rm_lines = [f"rm -rf {shlex.quote(str(install_prefix))}"]
    if bin_link is not None:
        rm_lines.append(f"rm -f {shlex.quote(str(bin_link))}")

    script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"while kill -0 {pid} 2>/dev/null; do sleep 0.3; done\n"
        "sleep 1\n"
        + "\n".join(rm_lines)
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


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from warpsocket_server.platforms import PlatformError, get_server_platform

    config_path = _resolve_config_path(args)
    config_dir = config_path.parent
    platform = get_server_platform()
    install_prefix = platform.install_prefix()
    bin_link = platform.bin_link()

    console.print("[bold red]WARNING:[/bold red] This will permanently remove WarpSocket server:")
    console.print("  • wstunnel systemd service")
    console.print("  • WireGuard interface, config and PostUp/PostDown rules")
    console.print(f"  • Server config and TLS certificates: {config_dir}")
    console.print("  • Persistent IP forwarding sysctl drop-in")
    if install_prefix is not None:
        console.print(f"  • Install directory: {install_prefix}")
    if bin_link is not None:
        console.print(f"  • CLI symlink: {bin_link}")

    if not args.yes:
        answer = console.input("\nType [bold]yes[/bold] to confirm: ")
        if answer.strip().lower() != "yes":
            console.print("Aborted.")
            return 1

    warnings: list[str] = []

    def _step(label: str, fn: callable) -> None:
        console.print(f"  {label}...", end=" ")
        try:
            fn()
            console.print("[green]done[/green]")
        except (PlatformError, OSError) as exc:
            console.print(f"[yellow]warning:[/yellow] {exc}")
            warnings.append(f"{label}: {exc}")

    console.print()
    _step("Stopping & removing wstunnel service", platform.uninstall_wstunnel_service)
    _step("Bringing down WireGuard + removing config", platform.uninstall_wg_config)

    def _rm_config_dir() -> None:
        import shutil
        if config_dir.exists():
            shutil.rmtree(config_dir)

    _step(f"Removing config dir ({config_dir})", _rm_config_dir)

    # Defer venv + symlink removal until after we exit (we live inside install_prefix).
    if install_prefix is not None and install_prefix.exists():
        try:
            _spawn_deferred_cleanup(install_prefix, bin_link)
            console.print(
                f"  Scheduled removal of {install_prefix}"
                + (f" and {bin_link}" if bin_link else "")
                + " after exit... [green]done[/green]"
            )
        except OSError as exc:
            console.print(f"  Scheduling cleanup... [yellow]warning:[/yellow] {exc}")
            warnings.append(f"deferred cleanup: {exc}")
    elif bin_link is not None and bin_link.exists():
        # No install prefix to remove (e.g., dev install) — just unlink the binary.
        _step(f"Removing CLI symlink ({bin_link})", lambda: bin_link.unlink())

    if warnings:
        console.print(
            "\n[yellow]Uninstall completed with warnings.[/yellow] "
            f"({len(warnings)} step(s) reported issues — see above.)"
        )
        return 1

    console.print("\n[green]WarpSocket server uninstalled successfully.[/green]")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from warpsocket_server.platforms import PlatformError, get_server_platform

    config = _load_config(args)
    platform = get_server_platform()

    try:
        wstunnel_active = platform.is_wstunnel_running()
    except PlatformError as exc:
        wstunnel_active = None
        wstunnel_error = str(exc)
    else:
        wstunnel_error = None

    try:
        wg_active = platform.is_wg_active()
    except PlatformError as exc:
        wg_active = None
        wg_error = str(exc)
    else:
        wg_error = None

    table = Table(title="WarpSocket Server Status", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Endpoint", f"{config.endpoint}:{config.port}")
    table.add_row("WireGuard subnet", config.subnet)
    table.add_row("Server WG address", config.server_address)
    table.add_row("WG listen port", str(config.wg_listen_port))
    table.add_row("Registered clients", str(len(config.clients)))

    def _status_cell(active: bool | None, error: str | None) -> str:
        if active is True:
            return "[green]running[/green]"
        if active is False:
            return "[red]stopped[/red]"
        return f"[yellow]unknown[/yellow] ({error})"

    table.add_row("wstunnel service", _status_cell(wstunnel_active, wstunnel_error))
    table.add_row("WireGuard interface", _status_cell(wg_active, wg_error))

    console.print(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warpsocket-server",
        description="WarpSocket server — WireGuard over WebSocket tunnel manager",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config-dir",
        type=str,
        default=None,
        help="Override the default config directory",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Run the interactive setup wizard")

    p_add = sub.add_parser("add-client", help="Register a new client")
    p_add.add_argument("name", help="Client name (used as filename for .warpcfg)")

    sub.add_parser("list-clients", help="List registered clients")

    p_revoke = sub.add_parser("revoke-client", help="Revoke a client")
    p_revoke.add_argument("name", help="Client name to revoke")

    sub.add_parser("status", help="Show server status")

    sub.add_parser(
        "restart",
        help="Regenerate wg0.conf and fully restart wg-quick + wstunnel "
             "(use after upgrading or if PostUp rules changed)",
    )

    p_uninstall = sub.add_parser("uninstall", help="Remove WarpSocket server completely")
    p_uninstall.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation prompt"
    )

    return parser


_COMMANDS: dict[str, callable] = {
    "setup": _cmd_setup,
    "add-client": _cmd_add_client,
    "list-clients": _cmd_list_clients,
    "revoke-client": _cmd_revoke_client,
    "status": _cmd_status,
    "restart": _cmd_restart,
    "uninstall": _cmd_uninstall,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    if args.command in _PRIVILEGED_COMMANDS:
        # Skip the root check when --config-dir points to a writable location
        # (used by tests). Real installations always use the default /etc path.
        if not args.config_dir:
            _require_root(args.command)
    return handler(args)
