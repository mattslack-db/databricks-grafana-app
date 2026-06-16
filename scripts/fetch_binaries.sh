#!/usr/bin/env bash
# scripts/fetch_binaries.sh
#
# Download, verify, and stage Grafana OSS, PgBouncer, stunnel, and psql
# linux-amd64 binaries into bin/ for use by startup.py.
#
# Usage:
#   bash scripts/fetch_binaries.sh          # fetch everything
#   FORCE=1 bash scripts/fetch_binaries.sh  # re-download even if present
#
# Idempotency: each binary/directory is skipped if already present and its
# SHA256 matches the pinned value.  Set FORCE=1 to bypass.
#
# Output:
#   bin/grafana/          — Grafana home directory (contains bin/grafana, etc.)
#   bin/pgbouncer         — PgBouncer binary (chmod +x)
#   bin/stunnel           — stunnel4 binary (chmod +x)
#   bin/psql              — PostgreSQL client binary (chmod +x)
#   bin/lib/              — Bundled shared libraries consumed at runtime via
#                           LD_LIBRARY_PATH=bin/lib (set by startup.py).
#                           Contains every non-glibc shared-library dependency
#                           of pgbouncer, stunnel, and psql (libssl, libcrypto,
#                           libevent, libwrap, libc-ares, libpq, libkrb5 chain,
#                           etc.) so the app runs without apt-installed runtime
#                           libs on the Databricks Apps runtime.
#
# Requirements (on the build/CI host — met by the boot container):
#   curl, sha256sum (GNU coreutils), tar, ar (binutils — for .deb extraction),
#   ldd (libc-bin), cp, readlink/realpath
#
# Databricks Apps runtime note:
#   The deployed image has no apt access.  All runtime shared libs are bundled
#   into bin/lib/ by this script (run inside the boot container which has them
#   via apt).  startup.py prepends bin/lib/ to LD_LIBRARY_PATH before launching
#   any child process so pgbouncer, stunnel, and psql all find their libs.

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

# stunnel4 3:5.68-2+deb12u1 — Debian 12 (bookworm) amd64 .deb from deb.debian.org
# Source:  https://deb.debian.org/debian/pool/main/s/stunnel4/
# SHA256 verified from the signed Debian bookworm Packages index
# (packages.debian.org/bookworm/amd64/stunnel4/download → SHA256 field):
#   b645fca2329d96d1a51cdf3dc35be8e3fca5cc2e56e23b0d36de152c2fd71d17
# Size verified: content-length 206812 matches packages.debian.org.
STUNNEL_DEB_VERSION="5.68-2+deb12u1"
STUNNEL_DEB="stunnel4_${STUNNEL_DEB_VERSION}_amd64.deb"
STUNNEL_URL="https://deb.debian.org/debian/pool/main/s/stunnel4/${STUNNEL_DEB}"
STUNNEL_SHA256="b645fca2329d96d1a51cdf3dc35be8e3fca5cc2e56e23b0d36de152c2fd71d17"

# postgresql-client-16 16.14-1.pgdg12+1 — from the official pgdg repo
# Source:  https://apt.postgresql.org/pub/repos/apt/pool/main/p/postgresql-16/
# SHA256 verified from the signed pgdg Packages index:
#   curl -s https://apt.postgresql.org/pub/repos/apt/dists/bookworm-pgdg/main/binary-amd64/Packages.gz \
#     | gunzip | grep -A 20 "^Package: postgresql-client-16$" | grep SHA256
# Size verified: content-length 1953996 matches Packages index.
PSQL_VERSION="16.14-1.pgdg12+1"
PSQL_DEB="postgresql-client-16_${PSQL_VERSION}_amd64.deb"
PSQL_URL="https://apt.postgresql.org/pub/repos/apt/pool/main/p/postgresql-16/${PSQL_DEB}"
PSQL_SHA256="acdbd3a7b03d41cd30113455bb04d5135e6fce9320709420af54b3d28b38cec9"

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

check_cmd curl sha256sum tar ar ldd cp readlink

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
# stunnel4
# ---------------------------------------------------------------------------
#
# stunnel4 is in Debian main (not pgdg). We extract /usr/bin/stunnel4 from the
# bookworm .deb using the same ar + tar approach as PgBouncer above.

STUNNEL_DEST="${BIN_DIR}/stunnel"
STUNNEL_DEB_CACHE="${BIN_DIR}/.stunnel_${STUNNEL_DEB_VERSION}.deb"

if [[ "${FORCE:-0}" != "1" ]] && [[ -x "${STUNNEL_DEST}" ]] && \
   already_ok "${STUNNEL_DEB_CACHE}" "${STUNNEL_SHA256}"; then
    info "stunnel4 ${STUNNEL_DEB_VERSION} already present and .deb verified; skipping"
else
    info "Downloading stunnel4 ${STUNNEL_DEB_VERSION} .deb …"
    curl -fL --progress-bar \
        -o "${TMPDIR_WORK}/${STUNNEL_DEB}" \
        "${STUNNEL_URL}"

    verify_sha256 "${TMPDIR_WORK}/${STUNNEL_DEB}" "${STUNNEL_SHA256}"

    info "Extracting stunnel binary from .deb …"
    mkdir -p "${TMPDIR_WORK}/stunnel_deb"
    ( cd "${TMPDIR_WORK}/stunnel_deb" && ar x "${TMPDIR_WORK}/${STUNNEL_DEB}" )

    STUNNEL_DATA_TAR="$(find "${TMPDIR_WORK}/stunnel_deb" -name 'data.tar.*' | head -1)"
    [[ -n "${STUNNEL_DATA_TAR}" ]] || error "data.tar.* not found inside stunnel4 .deb"

    # stunnel4 .deb installs to /usr/bin/stunnel4
    tar -xf "${STUNNEL_DATA_TAR}" -C "${TMPDIR_WORK}/stunnel_deb" ./usr/bin/stunnel4

    STUNNEL_EXTRACTED="${TMPDIR_WORK}/stunnel_deb/usr/bin/stunnel4"
    [[ -f "${STUNNEL_EXTRACTED}" ]] || error "stunnel4 binary not found at expected path in .deb"

    install -m 0755 "${STUNNEL_EXTRACTED}" "${STUNNEL_DEST}"
    cp "${TMPDIR_WORK}/${STUNNEL_DEB}" "${STUNNEL_DEB_CACHE}"

    info "stunnel4 ${STUNNEL_DEB_VERSION} installed at ${STUNNEL_DEST}"
fi

# ---------------------------------------------------------------------------
# psql (postgresql-client-16)
# ---------------------------------------------------------------------------
#
# The postgresql-client-16 .deb provides the real psql binary at
# /usr/lib/postgresql/16/bin/psql (NOT /usr/bin/psql — that path is a wrapper
# shipped by a different package, postgresql-client-common). We extract only the
# real binary; the shared libs it needs (libpq5, libssl, etc.) are bundled by
# the lib-bundling step below.

PSQL_DEST="${BIN_DIR}/psql"
PSQL_DEB_CACHE="${BIN_DIR}/.psql_${PSQL_VERSION}.deb"

if [[ "${FORCE:-0}" != "1" ]] && [[ -x "${PSQL_DEST}" ]] && \
   already_ok "${PSQL_DEB_CACHE}" "${PSQL_SHA256}"; then
    info "psql ${PSQL_VERSION} already present and .deb verified; skipping"
else
    info "Downloading postgresql-client-16 ${PSQL_VERSION} .deb …"
    curl -fL --progress-bar \
        -o "${TMPDIR_WORK}/${PSQL_DEB}" \
        "${PSQL_URL}"

    verify_sha256 "${TMPDIR_WORK}/${PSQL_DEB}" "${PSQL_SHA256}"

    info "Extracting psql binary from .deb …"
    mkdir -p "${TMPDIR_WORK}/psql_deb"
    ( cd "${TMPDIR_WORK}/psql_deb" && ar x "${TMPDIR_WORK}/${PSQL_DEB}" )

    PSQL_DATA_TAR="$(find "${TMPDIR_WORK}/psql_deb" -name 'data.tar.*' | head -1)"
    [[ -n "${PSQL_DATA_TAR}" ]] || error "data.tar.* not found inside postgresql-client-16 .deb"

    tar -xf "${PSQL_DATA_TAR}" -C "${TMPDIR_WORK}/psql_deb" ./usr/lib/postgresql/16/bin/psql

    PSQL_EXTRACTED="${TMPDIR_WORK}/psql_deb/usr/lib/postgresql/16/bin/psql"
    [[ -f "${PSQL_EXTRACTED}" ]] || error "psql binary not found at expected path in .deb"

    install -m 0755 "${PSQL_EXTRACTED}" "${PSQL_DEST}"
    cp "${TMPDIR_WORK}/${PSQL_DEB}" "${PSQL_DEB_CACHE}"

    info "psql ${PSQL_VERSION} installed at ${PSQL_DEST}"
fi

# ---------------------------------------------------------------------------
# Bundle shared libraries into bin/lib/
# ---------------------------------------------------------------------------
#
# For each of the three app binaries (pgbouncer, stunnel, psql) we run `ldd`
# to get the full transitive set of shared-library dependencies (ldd already
# resolves the full tree on Linux — one pass suffices).
#
# We copy every resolved library into bin/lib/, dereferencing symlinks so the
# real .so file is present, then recreate the soname symlink beside it.
#
# Excluded (must NOT be bundled — must come from the host glibc):
#   ld-linux*.so*      — the dynamic linker itself
#   libc.so*           — GNU C library
#   libm.so*           — math library (part of glibc)
#   libdl.so*          — dlopen (part of glibc)
#   libpthread.so*     — POSIX threads (part of glibc; merged into libc in glibc 2.34+)
#   librt.so*          — realtime (part of glibc; merged in glibc 2.34+)
#   libresolv.so*      — resolver (part of glibc)
#
# Everything else — libssl, libcrypto, libevent, libwrap, libc-ares, libpq,
# libkrb5/gssapi chain, libsasl2, libldap, liblz4, libzstd, libreadline,
# libncurses, etc. — is bundled.
#
# startup.py prepends bin/lib/ to LD_LIBRARY_PATH before launching any child
# process so all three binaries find these libs without any apt packages.

LIB_DIR="${BIN_DIR}/lib"

# Glibc-core basenames to skip (glob patterns matched against basename only).
GLIBC_SKIP_PATTERNS=(
    "ld-linux*.so*"
    "libc.so*"
    "libm.so*"
    "libdl.so*"
    "libpthread.so*"
    "librt.so*"
    "libresolv.so*"
)

# is_glibc_core <basename>  →  returns 0 (true) if should be skipped
is_glibc_core() {
    local base="$1"
    local pat
    for pat in "${GLIBC_SKIP_PATTERNS[@]}"; do
        # Use case for glob matching (no external tool needed)
        case "${base}" in
            ${pat}) return 0 ;;
        esac
    done
    return 1
}

# bundle_libs_for <binary>  — copy all non-glibc-core .so deps into LIB_DIR
bundle_libs_for() {
    local binary="$1"
    [[ -x "${binary}" ]] || { info "  (skip: ${binary} not present)"; return; }
    info "  Scanning deps of $(basename "${binary}") …"
    # ldd output lines of interest:
    #   \tsoname => /resolved/path (0xADDR)
    # We capture the resolved path column.
    ldd "${binary}" 2>/dev/null | awk '/=>/{print $3}' | while read -r libpath; do
        [[ -n "${libpath}" ]] || continue
        [[ "${libpath}" == "not" ]] && continue   # "not found" lines
        [[ -e "${libpath}" ]] || continue
        local base
        base="$(basename "${libpath}")"
        if is_glibc_core "${base}"; then
            info "    skip (glibc-core): ${base}"
            continue
        fi
        # Resolve the real file (dereference all symlinks)
        local realfile
        realfile="$(readlink -f "${libpath}")"
        local realbase
        realbase="$(basename "${realfile}")"
        # Copy the real file if not already present
        if [[ ! -f "${LIB_DIR}/${realbase}" ]]; then
            cp -p "${realfile}" "${LIB_DIR}/${realbase}"
            info "    bundled: ${realbase}"
        fi
        # Recreate the soname symlink (e.g. libssl.so.3 -> libssl.so.3.0.9)
        # if the path we scanned has a different name than the real file
        if [[ "${base}" != "${realbase}" ]] && [[ ! -e "${LIB_DIR}/${base}" ]]; then
            ln -s "${realbase}" "${LIB_DIR}/${base}"
            info "    symlink: ${base} -> ${realbase}"
        fi
    done
}

# Only re-bundle if any binary changed or LIB_DIR is missing/empty,
# unless FORCE=1.
NEED_BUNDLE=0
if [[ "${FORCE:-0}" == "1" ]]; then
    NEED_BUNDLE=1
elif [[ ! -d "${LIB_DIR}" ]] || [[ -z "$(ls -A "${LIB_DIR}" 2>/dev/null)" ]]; then
    NEED_BUNDLE=1
fi

if [[ "${NEED_BUNDLE}" == "1" ]]; then
    info "Bundling shared libraries into ${LIB_DIR}/ …"
    mkdir -p "${LIB_DIR}"
    for binary in "${PGBOUNCER_DEST}" "${STUNNEL_DEST}" "${PSQL_DEST}"; do
        bundle_libs_for "${binary}"
    done
    LIB_COUNT="$(find "${LIB_DIR}" -maxdepth 1 -name '*.so*' | wc -l | tr -d ' ')"
    info "Library bundle complete: ${LIB_COUNT} files in ${LIB_DIR}/"
else
    LIB_COUNT="$(find "${LIB_DIR}" -maxdepth 1 -name '*.so*' | wc -l | tr -d ' ')"
    info "bin/lib/ already populated (${LIB_COUNT} files); skipping lib bundle (FORCE=1 to redo)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

info ""
info "Done."
info "  Grafana   ${GRAFANA_VERSION}:           ${GRAFANA_BINARY}"
info "  PgBouncer ${PGBOUNCER_VERSION}:         ${PGBOUNCER_DEST}"
info "  stunnel4  ${STUNNEL_DEB_VERSION}:  ${STUNNEL_DEST}"
info "  psql      ${PSQL_VERSION}: ${PSQL_DEST}"
info "  libs:                          ${LIB_DIR}/ (LD_LIBRARY_PATH at runtime)"
info ""
info "Next: set DATABRICKS_APP_PORT, LAKEBASE_*, GRAFANA_ROOT_URL and run:"
info "  python startup.py"
