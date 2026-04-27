from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from warpsocket_server.config import ServerConfig
from warpsocket_server.crypto import generate_tls_cert, generate_wg_keypair
from warpsocket_server.platforms import PlatformError, get_server_platform
from warpsocket_server.wireguard import build_server_wg_conf

log = logging.getLogger(__name__)
console = Console()

_PUBLIC_IP_SERVICE = "https://api.ipify.org"
_PUBLIC_IP_TIMEOUT = 5


def _detect_public_ip() -> str | None:
    try:
        with urllib.request.urlopen(_PUBLIC_IP_SERVICE, timeout=_PUBLIC_IP_TIMEOUT) as resp:
            return resp.read().decode("utf-8").strip()
    except (urllib.error.URLError, TimeoutError) as exc:
        log.warning("Could not detect public IP: %s", exc)
        return None


def _check_root() -> bool:
    if sys.platform == "win32":
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    return os.geteuid() == 0


def _find_wstunnel() -> Path | None:
    found = shutil.which("wstunnel")
    return Path(found) if found else None


def _find_wg() -> Path | None:
    found = shutil.which("wg")
    return Path(found) if found else None


def run_setup(config_dir: Path) -> int:
    """Interactive setup wizard. Returns exit code."""
    console.print(
        Panel.fit(
            "[bold cyan]WarpSocket Server — Setup Wizard[/bold cyan]\n\n"
            "This wizard will configure wstunnel + WireGuard as system services.",
            border_style="cyan",
        )
    )

    if not _check_root():
        console.print(
            "[red]Error:[/red] This wizard must be run as root (or Administrator on Windows).\n"
            "  On Linux/macOS: [bold]sudo warpsocket-server setup[/bold]"
        )
        return 1

    config_path = config_dir / "server_config.json"
    if config_path.exists():
        console.print(f"\n[yellow]Warning:[/yellow] Config already exists at {config_path}.")
        if not Confirm.ask("Overwrite existing configuration?", default=False):
            console.print("Aborted.")
            return 0

    # Check binaries
    console.print("\n[bold]Checking dependencies...[/bold]")
    wstunnel_bin = _find_wstunnel()
    if wstunnel_bin is None:
        console.print(
            "[red]Error:[/red] wstunnel binary not found in PATH.\n"
            "  Install from: https://github.com/erebe/wstunnel/releases"
        )
        return 1
    console.print(f"  [green]✓[/green] wstunnel: {wstunnel_bin}")

    wg_bin = _find_wg()
    if wg_bin is None:
        console.print(
            "[red]Error:[/red] WireGuard tools (wg) not found.\n"
            "  Install: [bold]apt install wireguard-tools[/bold] "
            "(Debian/Ubuntu) or equivalent."
        )
        return 1
    console.print(f"  [green]✓[/green] wg: {wg_bin}")

    # Detect public IP
    console.print("\n[bold]Detecting public IP...[/bold]")
    detected_ip = _detect_public_ip()
    if detected_ip:
        console.print(f"  Detected: [cyan]{detected_ip}[/cyan]")
        endpoint = Prompt.ask("Server endpoint (IP or domain)", default=detected_ip)
    else:
        console.print("  [yellow]Could not auto-detect[/yellow]")
        endpoint = Prompt.ask("Server endpoint (IP or domain)")

    # Ports
    console.print("\n[bold]Network configuration[/bold]")
    port = IntPrompt.ask("wstunnel WSS port", default=443)
    while not (1 <= port <= 65535):
        console.print("[red]Invalid port[/red]")
        port = IntPrompt.ask("wstunnel WSS port", default=443)

    wg_listen_port = IntPrompt.ask("WireGuard listen port (loopback only)", default=51820)

    # Subnet
    subnet = Prompt.ask("WireGuard subnet (CIDR)", default="10.0.0.0/24")
    server_address = Prompt.ask(
        "Server's WireGuard address", default=f"{subnet.split('/')[0].rsplit('.', 1)[0]}.1/24"
    )

    # Generate secrets
    console.print("\n[bold]Generating cryptographic material...[/bold]")
    upgrade_path = secrets.token_urlsafe(32)
    console.print("  [green]✓[/green] HTTP upgrade path prefix")

    cert_dir = config_dir / "tls"
    cert_path, key_path, fingerprint = generate_tls_cert(endpoint, cert_dir)
    console.print(f"  [green]✓[/green] TLS cert ({fingerprint[:23]}...)")

    wg_priv, wg_pub = generate_wg_keypair(wg_bin)
    console.print("  [green]✓[/green] WireGuard server keypair")

    # Build and save server config
    config = ServerConfig(
        schema_version=1,
        endpoint=endpoint,
        port=port,
        http_upgrade_path_prefix=upgrade_path,
        cert_path=str(cert_path),
        key_path=str(key_path),
        cert_fingerprint_sha256=fingerprint,
        wg_private_key=wg_priv,
        wg_public_key=wg_pub,
        subnet=subnet,
        server_address=server_address,
        wg_listen_port=wg_listen_port,
        clients=[],
    )
    config.save(config_path)
    console.print(f"  [green]✓[/green] Server config saved to {config_path}")

    # Enable IP forwarding persistently (survives reboots).
    # PostUp in wg0.conf handles the runtime activation; this covers the
    # window between boot and wg-quick bringing the interface up.
    _enable_ip_forwarding(config_dir)

    # Install services via platform
    platform = get_server_platform()
    console.print("\n[bold]Installing services...[/bold]")

    try:
        wg_conf = build_server_wg_conf(config)
        # If the interface was already up (re-running setup), install_wg_config does
        # a hot reload via wg syncconf which doesn't re-run PostUp. Force a full
        # restart so the iptables/forwarding rules are guaranteed to be applied.
        was_active = platform.is_wg_active()
        platform.install_wg_config(wg_conf)
        if was_active:
            platform.restart_wg()
        console.print("  [green]✓[/green] WireGuard interface up")
    except PlatformError as exc:
        console.print(f"  [red]✗[/red] WireGuard: {exc}")
        return 1

    try:
        platform.install_wstunnel_service(
            port=port,
            cert_path=cert_path,
            key_path=key_path,
            upgrade_path=upgrade_path,
            wg_listen_port=wg_listen_port,
            wstunnel_bin=wstunnel_bin,
        )
        console.print("  [green]✓[/green] wstunnel service enabled")
    except PlatformError as exc:
        console.print(f"  [red]✗[/red] wstunnel: {exc}")
        return 1

    # Connectivity probe (localhost only)
    console.print("\n[bold]Running connectivity probe...[/bold]")
    probe_ok = _probe_localhost(port)
    if probe_ok:
        console.print("  [green]✓[/green] wstunnel is listening on the configured port")
    else:
        console.print(
            "  [yellow]⚠[/yellow]  Could not connect locally — check service logs:\n"
            "     journalctl -u wstunnel-warpsocket -e"
        )

    # Final summary
    console.print(
        Panel.fit(
            f"[bold green]Setup complete![/bold green]\n\n"
            f"Endpoint:  [cyan]{endpoint}:{port}[/cyan]\n"
            f"WireGuard: [cyan]{server_address} (port {wg_listen_port})[/cyan]\n"
            f"Subnet:    [cyan]{subnet}[/cyan]\n\n"
            f"[bold]Next steps:[/bold]\n"
            f"  1. Make sure port {port}/tcp is open in your firewall and router.\n"
            f"  2. Run [bold]warpsocket-server add-client <name>[/bold] to register clients.\n"
            f"  3. Send the generated .warpcfg files to each client.",
            border_style="green",
        )
    )
    return 0


def _enable_ip_forwarding(config_dir: Path) -> None:
    """Write a sysctl drop-in so ip_forward survives reboots (Linux only)."""
    if sys.platform != "linux":
        return
    sysctl_path = Path("/etc/sysctl.d/99-warpsocket.conf")
    try:
        sysctl_path.write_text("net.ipv4.ip_forward = 1\n", encoding="utf-8")
        import subprocess as _sp
        _sp.run(["sysctl", "-p", str(sysctl_path)], capture_output=True, check=False)
        log.info("IP forwarding enabled persistently via %s", sysctl_path)
    except OSError as exc:
        log.warning("Could not write %s: %s — IP forwarding must be enabled manually", sysctl_path, exc)


def _probe_localhost(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except (OSError, TimeoutError):
        return False
