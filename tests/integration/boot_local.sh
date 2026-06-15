#!/usr/bin/env bash
# Task 10 local boot harness (runs INSIDE the linux/amd64 container).
# Ensures the linux binaries are staged, then launches the supervisor.
set -euo pipefail

cd /app

echo "== python deps =="
python -c "import databricks.sdk, psycopg" 2>/dev/null && echo "deps already present (baked in image)" \
  || pip install --no-cache-dir -q -r requirements.txt

echo "== fetch binaries (idempotent, retries transient net) =="
for attempt in 1 2 3; do
  if [[ -x bin/grafana/bin/grafana && -x bin/pgbouncer ]]; then break; fi
  echo "fetch attempt ${attempt}…"
  bash scripts/fetch_binaries.sh && break || { echo "fetch failed (attempt ${attempt}); retrying"; sleep 3; }
done
[[ -x bin/grafana/bin/grafana && -x bin/pgbouncer ]] || { echo "FATAL: binaries missing after retries"; exit 1; }

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
