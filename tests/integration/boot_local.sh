#!/usr/bin/env bash
# Task 10 local boot harness (runs INSIDE the linux/amd64 container).
# Ensures the linux binaries are staged, then launches the supervisor.
set -euo pipefail

cd /app

echo "== python deps =="
python -c "import databricks.sdk, psycopg" 2>/dev/null && echo "deps already present (baked in image)" \
  || pip install --no-cache-dir -q -r requirements.txt

have_binaries() {
  [[ -x bin/grafana/bin/grafana && -x bin/pgbouncer && -x bin/stunnel && -x bin/psql ]] \
    && [[ -d bin/lib ]] && [[ -n "$(ls -A bin/lib 2>/dev/null)" ]]
}

# Bundle path: honour GRAFANA_BUNDLE_TARBALL if set, else look in the default
# staging dir used by scripts/assemble_bundle.sh when building on colima.
BUNDLE_TARBALL="${GRAFANA_BUNDLE_TARBALL:-${HOME}/grafana-bundle-stage/bin.tar.gz}"

echo "== stage binaries (idempotent) =="
if have_binaries; then
  echo "bin/ already populated; skipping extraction"
elif [[ -f "${BUNDLE_TARBALL}" ]]; then
  echo "Extracting bundle from ${BUNDLE_TARBALL} …"
  mkdir -p bin
  tar -xzf "${BUNDLE_TARBALL}" -C bin
  have_binaries || { echo "FATAL: bundle extracted but expected binaries still missing"; exit 1; }
  echo "Extraction complete"
else
  cat >&2 <<EOF
FATAL: bin/ is not pre-populated and no bundle tarball found.

Expected at: ${BUNDLE_TARBALL}
Override with: GRAFANA_BUNDLE_TARBALL=/path/to/bin.tar.gz

To build the bundle (requires colima + linux/amd64):
  bash scripts/assemble_bundle.sh   (outputs ~/grafana-bundle-stage/bin.tar.gz)
EOF
  exit 1
fi

echo "== binaries ready =="
echo "== sanity: binary archs =="
file bin/grafana/bin/grafana bin/pgbouncer 2>/dev/null || true

# Run the supervisor from a writable working dir (/work) so PgBouncer's
# relative auth_file/pidfile and the supervisor's pgbouncer.ini/userlist.txt
# land somewhere a NON-ROOT user can write. Binaries stay at /app/bin (passed
# absolutely via GRAFANA_HOME/PGBOUNCER_BINARY). This mirrors the deployed app,
# which runs non-root (PgBouncer refuses to run as root).
mkdir -p /work
echo "== launch supervisor (cwd=/work, user=$(id -un)) =="
cd /work
# Env (LAKEBASE_*, DATABRICKS_*, GRAFANA_ROOT_URL, paths) injected via --env-file.
exec python /app/startup.py
