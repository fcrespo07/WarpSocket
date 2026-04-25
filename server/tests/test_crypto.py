from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from warpsocket_server.crypto import (
    CryptoError,
    compute_cert_fingerprint,
    generate_tls_cert,
    generate_wg_keypair,
)

_FINGERPRINT_RE = re.compile(r"^([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}$")


class TestTlsCert:
    def test_generates_cert_and_key_files(self, tmp_path: Path) -> None:
        cert_path, key_path, fp = generate_tls_cert("203.0.113.42", tmp_path)
        assert cert_path.exists()
        assert key_path.exists()
        assert cert_path.read_text().startswith("-----BEGIN CERTIFICATE-----")
        assert key_path.read_text().startswith("-----BEGIN")

    def test_fingerprint_format(self, tmp_path: Path) -> None:
        _, _, fp = generate_tls_cert("example.com", tmp_path)
        assert _FINGERPRINT_RE.match(fp), f"Fingerprint format invalid: {fp}"

    def test_compute_fingerprint_matches(self, tmp_path: Path) -> None:
        cert_path, _, fp = generate_tls_cert("10.0.0.1", tmp_path)
        fp2 = compute_cert_fingerprint(cert_path)
        assert fp == fp2

    def test_ip_san_for_ip_address(self, tmp_path: Path) -> None:
        from cryptography import x509

        cert_path, _, _ = generate_tls_cert("203.0.113.42", tmp_path)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        ips = san.value.get_values_for_type(x509.IPAddress)
        assert len(ips) == 1

    def test_dns_san_for_domain(self, tmp_path: Path) -> None:
        from cryptography import x509

        cert_path, _, _ = generate_tls_cert("vpn.example.com", tmp_path)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        names = san.value.get_values_for_type(x509.DNSName)
        assert "vpn.example.com" in names

    def test_creates_dest_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "nested" / "dir"
        cert_path, key_path, _ = generate_tls_cert("10.0.0.1", dest)
        assert cert_path.exists()


class TestWgKeypair:
    def test_generates_keypair_with_mocked_wg(self) -> None:
        mock_genkey = MagicMock()
        mock_genkey.stdout = "cHJpdmF0ZWtleQ==\n"
        mock_pubkey = MagicMock()
        mock_pubkey.stdout = "cHVibGlja2V5\n"

        with patch("warpsocket_server.crypto.subprocess.run") as mock_run:
            mock_run.side_effect = [mock_genkey, mock_pubkey]
            priv, pub = generate_wg_keypair(wg_bin=Path("/usr/bin/wg"))

        assert priv == "cHJpdmF0ZWtleQ=="
        assert pub == "cHVibGlja2V5"
        assert mock_run.call_count == 2

    def test_raises_on_missing_binary(self) -> None:
        with patch("warpsocket_server.crypto.shutil.which", return_value=None):
            with pytest.raises(CryptoError, match="WireGuard tools not found"):
                generate_wg_keypair()

    def test_raises_on_subprocess_failure(self) -> None:
        import subprocess

        with patch("warpsocket_server.crypto.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "wg", stderr="error"
            )
            with pytest.raises(CryptoError, match="key generation failed"):
                generate_wg_keypair(wg_bin=Path("/usr/bin/wg"))
