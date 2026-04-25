from __future__ import annotations

import argparse
import logging
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
from warpsocket_server.wireguard import add_peer_live, build_server_wg_conf, remove_peer_live

log = logging.getLogger(__name__)
console = Console()


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

    # Write WG server conf
    wg_conf_dir = config_path.parent
    wg_conf_path = wg_conf_dir / "wg0.conf"
    wg_conf_path.write_text(build_server_wg_conf(updated), encoding="utf-8")

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


def _cmd_list_clients(args: argparse.Namespace) -> int:
    config = _load_config(args)

    if not config.clients:
        console.print("No clients registered. Use [bold]add-client[/bold] to add one.")
        return 0

    table = Table(title="Registered Clients")
    table.add_column("Name", style="bold")
    table.add_column("Address")
    table.add_column("Public Key")

    for c in config.clients:
        table.add_row(c.name, c.address, c.public_key[:24] + "...")

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

    # Rewrite WG conf
    wg_conf_path = config_path.parent / "wg0.conf"
    wg_conf_path.write_text(build_server_wg_conf(updated), encoding="utf-8")

    console.print(f"[green]Client '{name}' revoked.[/green]")
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

    return parser


_COMMANDS: dict[str, callable] = {
    "setup": _cmd_setup,
    "add-client": _cmd_add_client,
    "list-clients": _cmd_list_clients,
    "revoke-client": _cmd_revoke_client,
    "status": _cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)
