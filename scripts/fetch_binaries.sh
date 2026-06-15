#!/usr/bin/env bash
# scripts/fetch_binaries.sh
#
# Download, verify, and stage Grafana OSS and PgBouncer linux-amd64 binaries
# into bin/ for use by startup.py.
#
# Usage:
#   bash scripts/fetch_binaries.sh          # fetch everything
#   FORCE=1 bash scripts/fetch_binaries.sh  # re-download even if present
#
# Idempotency: each binary is skipped if already present and its SHA256
# matches the pinned value.  Set FORCE=1 to bypass the skip.
#
# Output:
#   bin/grafana/          — Grafana home directory (contains bin/grafana, etc.)
#   bin/pgbouncer         — PgBouncer static binary (chmod +x)
#
# Requirements (on the build/CI host):
#   curl, sha256sum (GNU coreutils), tar, ar (binutils — for .deb extraction)
#
# Databricks Apps runtime note:
#   The deployed image must also have 'psql' on PATH for the PgBouncer admin
#   console (RELOAD; RECONNECT <db>;).  The postgresql-client package from the
#   same pgdg repo provides it.  Stage it alongside these binaries in Task 11.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${REPO_ROOT}/bin"

# ---------------------------------------------------------------------------
# Pinned versions and verified checksums
# ---------------------------------------------------------------------------

# Grafana OSS 12.0.2 — linux-amd64 tarball
# Source:  https://dl.grafana.com/oss/release/
# SHA256 verified directly from:
#   curl -s https://dl.grafana.com/oss/release/grafana-12.0.2.linux-amd64.tar.gz.sha256
GRAFANA_VERSION="12.0.2"
GRAFANA_TARBALL="grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz"
GRAFANA_URL="https://dl.grafana.com/oss/release/${GRAFANA_TARBALL}"
GRAFANA_SHA256="c1755b4da918edfd298d5c8d5f1ffce35982ad10e1640ec356570cfb8c34b3e8"

# PgBouncer 1.25.2 — Debian 12 (bookworm) amd64 .deb from the official pgdg repo
# Source:  https://apt.postgresql.org/pub/repos/apt/pool/main/p/pgbouncer/
# SHA256 verified from the signed pgdg Packages index:
#   curl -s https://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/main/binary-amd64/Packages.gz \
#     | gunzip | grep -A 20 "^Package: pgbouncer$" | grep SHA256
# (The pgdg Packages file is itself signed via InRelease GPG; this is the
#  authoritative checksum for the .deb.)
PGBOUNCER_VERSION="1.25.2"
PGBOUNCER_DEB="pgbouncer_${PGBOUNCER_VERSION}-1.pgdg12+1_amd64.deb"
PGBOUNCER_URL="https://apt.postgresql.org/pub/repos/apt/pool/main/p/pgbouncer/${PGBOUNCER_DEB}"
PGBOUNCER_SHA256="6a4fb1e5de7f284978fb56ede570e80ab1a8b2749d87c5ae4d24f1678387ae92"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "[fetch_binaries] $*"; }
error() { echo "[fetch_binaries] ERROR: $*" >&2; exit 1; }

check_cmd() {
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || error "Required command not found: $cmd"
    done
}

# verify_sha256 <file> <expected_hex>
verify_sha256() {
    local file="$1" expected="$2"
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    if [[ "$actual" != "$expected" ]]; then
        error "SHA256 mismatch for $file\n  expected: $expected\n  actual:   $actual"
    fi
    info "SHA256 OK: $(basename "$file")"
}

# already_ok <file> <expected_sha256>  → true if file exists and checksum matches
already_ok() {
    local file="$1" expected="$2"
    [[ "${FORCE:-0}" == "1" ]] && return 1
    [[ -f "$file" ]] || return 1
    local actual
    actual="$(sha256sum "$file" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]]
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

check_cmd curl sha256sum tar ar

mkdir -p "${BIN_DIR}"

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "${TMPDIR_WORK}"' EXIT

# ---------------------------------------------------------------------------
# Grafana OSS
# ---------------------------------------------------------------------------

GRAFANA_DEST_DIR="${BIN_DIR}/grafana"
GRAFANA_BINARY="${GRAFANA_DEST_DIR}/bin/grafana"

if already_ok "${BIN_DIR}/${GRAFANA_TARBALL}" "${GRAFANA_SHA256}" && [[ -x "${GRAFANA_BINARY}" ]]; then
    info "Grafana ${GRAFANA_VERSION} already present and verified; skipping"
else
    info "Downloading Grafana ${GRAFANA_VERSION} …"
    curl -fL --progress-bar \
        -o "${TMPDIR_WORK}/${GRAFANA_TARBALL}" \
        "${GRAFANA_URL}"

    verify_sha256 "${TMPDIR_WORK}/${GRAFANA_TARBALL}" "${GRAFANA_SHA256}"

    info "Extracting Grafana to ${GRAFANA_DEST_DIR}/ …"
    rm -rf "${GRAFANA_DEST_DIR}"
    mkdir -p "${GRAFANA_DEST_DIR}"
    tar -xzf "${TMPDIR_WORK}/${GRAFANA_TARBALL}" \
        --strip-components=1 \
        -C "${GRAFANA_DEST_DIR}"

    [[ -x "${GRAFANA_BINARY}" ]] || error "Expected binary not found after extraction: ${GRAFANA_BINARY}"
    info "Grafana ${GRAFANA_VERSION} installed at ${GRAFANA_DEST_DIR}"
fi

# ---------------------------------------------------------------------------
# PgBouncer
# ---------------------------------------------------------------------------
#
# The official pgbouncer/pgbouncer GitHub releases ship only a Windows binary
# and a source tarball — no pre-built linux-amd64 binary.  We therefore obtain
# the binary by downloading the signed Debian package from the official
# PostgreSQL Global Development Group (pgdg) apt repository and extracting
# /usr/sbin/pgbouncer from it using `ar` + `tar` (no apt/dpkg required).
#
# The .deb SHA256 above was read from the pgdg signed Packages index, which is
# itself GPG-signed via InRelease.  This is the authoritative provenance chain
# for the binary.

PGBOUNCER_DEST="${BIN_DIR}/pgbouncer"

# Idempotency for the extracted binary: the dest is the extracted binary, not
# the .deb, so we can't checksum it against PGBOUNCER_SHA256 directly. Instead
# we cache the verified .deb and re-verify that on subsequent runs.
PGBOUNCER_DEB_CACHE="${BIN_DIR}/.pgbouncer_${PGBOUNCER_VERSION}.deb"

if [[ "${FORCE:-0}" != "1" ]] && [[ -x "${PGBOUNCER_DEST}" ]] && \
   already_ok "${PGBOUNCER_DEB_CACHE}" "${PGBOUNCER_SHA256}"; then
    info "PgBouncer ${PGBOUNCER_VERSION} already present and .deb verified; skipping"
else
    info "Downloading PgBouncer ${PGBOUNCER_VERSION} .deb …"
    curl -fL --progress-bar \
        -o "${TMPDIR_WORK}/${PGBOUNCER_DEB}" \
        "${PGBOUNCER_URL}"

    verify_sha256 "${TMPDIR_WORK}/${PGBOUNCER_DEB}" "${PGBOUNCER_SHA256}"

    info "Extracting pgbouncer binary from .deb …"
    # .deb layout: ar archive containing data.tar.* with the actual files.
    # Use `cd && ar x` (portable): `ar x --output=DIR` needs a newer GNU binutils
    # AND a pre-existing dir, which fails on slim Debian (debian:bookworm/python-slim).
    mkdir -p "${TMPDIR_WORK}/deb_contents"
    ( cd "${TMPDIR_WORK}/deb_contents" && ar x "${TMPDIR_WORK}/${PGBOUNCER_DEB}" )

    # Find data.tar (may be .xz, .gz, .zst depending on dpkg version)
    DATA_TAR="$(find "${TMPDIR_WORK}/deb_contents" -name 'data.tar.*' | head -1)"
    [[ -n "${DATA_TAR}" ]] || error "data.tar.* not found inside .deb"

    tar -xf "${DATA_TAR}" -C "${TMPDIR_WORK}/deb_contents" ./usr/sbin/pgbouncer

    EXTRACTED_BIN="${TMPDIR_WORK}/deb_contents/usr/sbin/pgbouncer"
    [[ -f "${EXTRACTED_BIN}" ]] || error "pgbouncer binary not found at expected path in .deb"

    install -m 0755 "${EXTRACTED_BIN}" "${PGBOUNCER_DEST}"

    # Cache the verified .deb for future idempotency checks.
    cp "${TMPDIR_WORK}/${PGBOUNCER_DEB}" "${PGBOUNCER_DEB_CACHE}"

    info "PgBouncer ${PGBOUNCER_VERSION} installed at ${PGBOUNCER_DEST}"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

info ""
info "Done."
info "  Grafana  ${GRAFANA_VERSION}: ${GRAFANA_BINARY}"
info "  PgBouncer ${PGBOUNCER_VERSION}: ${PGBOUNCER_DEST}"
info ""
info "Next: set DATABRICKS_APP_PORT, LAKEBASE_*, GRAFANA_ROOT_URL and run:"
info "  python startup.py"
