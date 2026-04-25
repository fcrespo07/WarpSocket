from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import shutil
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

log = logging.getLogger(__name__)

_CERT_VALIDITY_DAYS = 3650  # ~10 years


class CryptoError(RuntimeError):
    pass


def generate_tls_cert(
    common_name: str,
    dest_dir: Path,
    cert_name: str = "cert.pem",
    key_name: str = "key.pem",
) -> tuple[Path, Path, str]:
    """Generate a self-signed EC P-256 TLS certificate.

    Returns (cert_path, key_path, sha256_fingerprint).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    cert_path = dest_dir / cert_name
    key_path = dest_dir / key_name

    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=_CERT_VALIDITY_DAYS)
        )
    )

    # Add SAN — IP if it looks like an IP, otherwise DNS
    try:
        ip = ipaddress.ip_address(common_name)
        san = x509.SubjectAlternativeName([x509.IPAddress(ip)])
    except ValueError:
        san = x509.SubjectAlternativeName([x509.DNSName(common_name)])
    builder = builder.add_extension(san, critical=False)

    cert = builder.sign(private_key, hashes.SHA256())

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    fingerprint = compute_cert_fingerprint(cert_path)
    log.info("Generated TLS cert: %s (fingerprint: %s)", cert_path, fingerprint)
    return cert_path, key_path, fingerprint


def compute_cert_fingerprint(cert_path: Path) -> str:
    """Read a PEM certificate and return its SHA-256 fingerprint as colon-separated hex."""
    cert_pem = cert_path.read_bytes()
    cert = x509.load_pem_x509_certificate(cert_pem)
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def find_wg_binary() -> Path:
    """Locate the `wg` binary on PATH."""
    wg = shutil.which("wg")
    if wg is None:
        raise CryptoError(
            "WireGuard tools not found. Install wireguard-tools "
            "(e.g. 'apt install wireguard-tools' or 'brew install wireguard-tools')"
        )
    return Path(wg)


def generate_wg_keypair(wg_bin: Path | None = None) -> tuple[str, str]:
    """Generate a WireGuard keypair via `wg genkey` / `wg pubkey`.

    Returns (private_key, public_key) as base64 strings.
    """
    wg = str(wg_bin or find_wg_binary())

    try:
        genkey = subprocess.run(
            [wg, "genkey"],
            capture_output=True,
            text=True,
            check=True,
        )
        private_key = genkey.stdout.strip()

        pubkey = subprocess.run(
            [wg, "pubkey"],
            input=private_key,
            capture_output=True,
            text=True,
            check=True,
        )
        public_key = pubkey.stdout.strip()
    except FileNotFoundError:
        raise CryptoError(f"wg binary not found at {wg}")
    except subprocess.CalledProcessError as exc:
        raise CryptoError(f"WireGuard key generation failed: {exc.stderr.strip()}") from exc

    return private_key, public_key
