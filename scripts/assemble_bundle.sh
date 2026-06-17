#!/usr/bin/env bash
# scripts/assemble_bundle.sh
#
# Assemble the bin/ bundle (Grafana + PgBouncer + stunnel + psql + their shared
# libraries) INSIDE an Ubuntu 22.04 builder container, then tar it to /out.
#
# WHY UBUNTU 22.04: the Databricks Apps runtime is Ubuntu 22.04 LTS (glibc 2.35).
# glibc is backward- but NOT forward-compatible, so every native binary and
# every bundled .so must be built against glibc <= 2.35. Building the bundle in
# this exact base guarantees that. (An earlier bookworm/glibc-2.36 build crashed
# the deployed app: "version `GLIBC_2.36' not found".)
#
# Grafana is a static Go binary (glibc-independent) so it is downloaded directly.
# PgBouncer, stunnel4, and psql are apt-installed (apt verifies repo GPG
# signatures) so their binaries AND the libs ldd resolves for them are all jammy.
#
# Expects (provided by Dockerfile.bundlebuilder):
#   - apt packages: pgbouncer stunnel4 postgresql-client-16 (binaries on PATH)
#   - tools: curl, sha256sum, tar, ldd, readlink, file
#   - /out mounted (the tarball is written to /out/bin.tar.gz)
set -euo pipefail

OUT_DIR="/out"
STAGE="/tmp/bundle"          # assemble bin/ here, then tar into /out
BIN_DIR="${STAGE}/bin"
LIB_DIR="${BIN_DIR}/lib"

GRAFANA_VERSION="12.0.2"
GRAFANA_TARBALL="grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz"
GRAFANA_URL="https://dl.grafana.com/oss/release/${GRAFANA_TARBALL}"
GRAFANA_SHA256="c1755b4da918edfd298d5c8d5f1ffce35982ad10e1640ec356570cfb8c34b3e8"

info() { echo "[assemble] $*"; }
error() { echo "[assemble] ERROR: $*" >&2; exit 1; }

rm -rf "${STAGE}"
mkdir -p "${BIN_DIR}" "${LIB_DIR}" "${OUT_DIR}"

# ---------------------------------------------------------------------------
# Grafana (static Go binary; distro-independent)
# ---------------------------------------------------------------------------
info "Downloading Grafana ${GRAFANA_VERSION} …"
curl -fsSL -o "/tmp/${GRAFANA_TARBALL}" "${GRAFANA_URL}"
echo "${GRAFANA_SHA256}  /tmp/${GRAFANA_TARBALL}" | sha256sum -c - \
    || error "Grafana SHA256 mismatch"
mkdir -p "${BIN_DIR}/grafana"
tar -xzf "/tmp/${GRAFANA_TARBALL}" --strip-components=1 -C "${BIN_DIR}/grafana"
[[ -x "${BIN_DIR}/grafana/bin/grafana" ]] || error "grafana binary missing after extract"
info "Grafana staged ($(file -b "${BIN_DIR}/grafana/bin/grafana" | cut -c1-40)…)"

# ---------------------------------------------------------------------------
# Databricks SQL warehouse datasource plugin (OSS community plugin,
# mullerpeter-databricks-datasource). Bundled into the Grafana plugins dir so
# it ships with the app (the Apps runtime has no plugin-catalog access). It is
# unsigned — startup.py allow-lists it via GF_PLUGINS_ALLOW_LOADING_UNSIGNED.
# The plugin's backend is a Go binary; building/bundling on jammy keeps it
# glibc-compatible with the runtime.
# ---------------------------------------------------------------------------
DBX_PLUGIN_ID="mullerpeter-databricks-datasource"
DBX_PLUGIN_URL="https://github.com/mullerpeter/databricks-grafana/releases/latest/download/${DBX_PLUGIN_ID}.zip"
PLUGINS_DIR="${BIN_DIR}/grafana/data/plugins"
mkdir -p "${PLUGINS_DIR}"
info "Downloading Databricks datasource plugin (${DBX_PLUGIN_ID}) …"
curl -fsSL -o "/tmp/${DBX_PLUGIN_ID}.zip" "${DBX_PLUGIN_URL}"
( cd "${PLUGINS_DIR}" && unzip -q -o "/tmp/${DBX_PLUGIN_ID}.zip" )
# The zip extracts to a top-level dir; normalize to <plugins>/<id>/plugin.json.
if [[ ! -f "${PLUGINS_DIR}/${DBX_PLUGIN_ID}/plugin.json" ]]; then
    found="$(find "${PLUGINS_DIR}" -maxdepth 2 -name plugin.json | head -1)"
    [[ -n "${found}" ]] || error "plugin.json not found after extracting ${DBX_PLUGIN_ID}"
    src="$(dirname "${found}")"
    [[ "${src}" != "${PLUGINS_DIR}/${DBX_PLUGIN_ID}" ]] && mv "${src}" "${PLUGINS_DIR}/${DBX_PLUGIN_ID}"
fi
info "Databricks plugin staged at grafana/data/plugins/${DBX_PLUGIN_ID}"

# ---------------------------------------------------------------------------
# apt-installed binaries (jammy / glibc 2.35)
# ---------------------------------------------------------------------------
copy_binary() {
    local src="$1" dest="$2"
    [[ -x "${src}" ]] || error "expected binary not found: ${src}"
    install -m 0755 "${src}" "${dest}"
    info "staged $(basename "${dest}") <- ${src}"
}

copy_binary /usr/sbin/pgbouncer "${BIN_DIR}/pgbouncer"
copy_binary /usr/bin/stunnel4   "${BIN_DIR}/stunnel"
# PGDG postgresql-client-16 installs the real psql here (not /usr/bin/psql,
# which is the postgresql-client-common wrapper).
copy_binary /usr/lib/postgresql/16/bin/psql "${BIN_DIR}/psql"

# ---------------------------------------------------------------------------
# Bundle shared libraries (exclude glibc core — those must come from the
# runtime's own glibc 2.35). Same exclusion set as the original fetch script.
# ---------------------------------------------------------------------------
GLIBC_SKIP=( "ld-linux*.so*" "libc.so*" "libm.so*" "libdl.so*"
             "libpthread.so*" "librt.so*" "libresolv.so*" )

is_glibc_core() {
    local base="$1" pat
    for pat in "${GLIBC_SKIP[@]}"; do
        case "${base}" in ${pat}) return 0 ;; esac
    done
    return 1
}

bundle_libs_for() {
    local binary="$1"
    info "scanning deps of $(basename "${binary}") …"
    ldd "${binary}" 2>/dev/null | awk '/=>/{print $3}' | while read -r libpath; do
        [[ -n "${libpath}" && "${libpath}" != "not" && -e "${libpath}" ]] || continue
        local base; base="$(basename "${libpath}")"
        is_glibc_core "${base}" && { info "  skip (glibc-core): ${base}"; continue; }
        local realfile; realfile="$(readlink -f "${libpath}")"
        local realbase; realbase="$(basename "${realfile}")"
        if [[ ! -f "${LIB_DIR}/${realbase}" ]]; then
            cp -p "${realfile}" "${LIB_DIR}/${realbase}"
            info "  bundled: ${realbase}"
        fi
        if [[ "${base}" != "${realbase}" && ! -e "${LIB_DIR}/${base}" ]]; then
            ln -s "${realbase}" "${LIB_DIR}/${base}"
        fi
    done
}

bundle_libs_for "${BIN_DIR}/pgbouncer"
bundle_libs_for "${BIN_DIR}/stunnel"
bundle_libs_for "${BIN_DIR}/psql"

LIB_COUNT="$(find "${LIB_DIR}" -maxdepth 1 -name '*.so*' | wc -l | tr -d ' ')"
info "bundled ${LIB_COUNT} shared libraries into bin/lib/"

# ---------------------------------------------------------------------------
# Tar it (paths relative to bin/, mirroring the staged layout)
# ---------------------------------------------------------------------------
info "creating ${OUT_DIR}/bin.tar.gz …"
tar -czf "${OUT_DIR}/bin.tar.gz" -C "${BIN_DIR}" .
SIZE="$(du -h "${OUT_DIR}/bin.tar.gz" | cut -f1)"
info "Done: ${OUT_DIR}/bin.tar.gz (${SIZE})"
info "glibc of build base: $(ldd --version | head -1)"
