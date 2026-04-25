from __future__ import annotations

import hashlib
import socket
import ssl


class NetworkError(RuntimeError):
    pass


def tcp_probe(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def get_tls_fingerprint(host: str, port: int, timeout: float = 5.0) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
    except OSError as exc:
        raise NetworkError(f"Could not establish TLS connection to {host}:{port}: {exc}")
    if not der:
        raise NetworkError(f"Server at {host}:{port} did not present a TLS certificate")
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def verify_tls_fingerprint(host: str, port: int, expected: str, timeout: float = 5.0) -> None:
    actual = get_tls_fingerprint(host, port, timeout)
    if actual.upper() != expected.upper():
        raise NetworkError(
            f"TLS certificate fingerprint mismatch for {host}:{port}.\n"
            f"  Expected: {expected.upper()}\n"
            f"  Got:      {actual}\n"
            "The server certificate has changed (possible MITM, server reinstall, "
            "or wrong .warpcfg). Re-import a fresh .warpcfg from the server admin."
        )
