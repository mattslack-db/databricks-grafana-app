"""stunnel — TLS+SNI shim between PgBouncer and Lakebase.

PgBouncer never sends TLS SNI on server connections (any version), but Lakebase
(Neon-based) routes by SNI. stunnel runs as a local TLS client: PgBouncer
connects to it in plaintext over loopback, and stunnel opens a TLS connection to
Lakebase WITH the SNI hostname set, then pipes bytes. The Postgres protocol
(startup + cleartext-token auth) flows over that tunnel transparently.

The config contains no secrets (just hostnames/ports) — the OAuth token lives in
PgBouncer's config, not here.
"""
from __future__ import annotations

import subprocess


def render_conf(*, accept_port: int, server_host: str, server_port: int,
                verify_chain: bool = False, accept_host: str = "127.0.0.1") -> str:
    """Render an stunnel client config that adds TLS+SNI to the Lakebase hop.

    verify_chain=False skips server-certificate verification (functional but
    unauthenticated TLS). For production, set True to verify the chain against
    the system CA store and pin the hostname — Lakebase uses a public CA cert.
    """
    if verify_chain:
        # CAfile (the concatenated Ubuntu/Debian system bundle) is more robust
        # than CApath, which requires c_rehash hashed symlinks to be present.
        # checkHost pins the Lakebase hostname (matches the cert SAN, incl.
        # wildcard). Lakebase presents a publicly-trusted certificate.
        verify_block = (
            "verifyChain = yes\n"
            "CAfile = /etc/ssl/certs/ca-certificates.crt\n"
            f"checkHost = {server_host}\n"
        )
    else:
        verify_block = "verifyChain = no\n"
    return (
        "foreground = yes\n"
        "pid =\n"
        "\n"
        "[lakebase]\n"
        "client = yes\n"
        f"accept = {accept_host}:{accept_port}\n"
        f"connect = {server_host}:{server_port}\n"
        # Postgres uses STARTTLS-style negotiation (send SSLRequest, get 'S',
        # THEN TLS) — NOT TLS-on-connect. Without this, Lakebase receives a raw
        # TLS ClientHello where it expects the Postgres SSLRequest packet and
        # drops the connection ("unexpected eof while reading").
        "protocol = pgsql\n"
        # SNI hostname Lakebase routes on (sent during the TLS handshake).
        f"sni = {server_host}\n"
        f"{verify_block}"
    )


def launch(binary: str, conf_path: str) -> subprocess.Popen:
    return subprocess.Popen([binary, conf_path])
