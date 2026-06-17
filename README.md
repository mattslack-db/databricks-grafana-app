# Grafana on Databricks Apps (Lakebase backend)

Run Grafana OSS as a Databricks App on a classic workspace, using Lakebase (autoscaling Postgres) as the session/state database. Auth comes from the Databricks Apps SSO proxy. Two datasources are provisioned out of the box: Lakebase Postgres and a Databricks SQL warehouse.

---

## How it works

Databricks Apps enforce a **10 MB per-source-file limit**, which rules out shipping the ~360 MB Grafana binary as app source. Instead:

1. `scripts/assemble_bundle.sh` builds a gzipped tarball (`bin.tar.gz`) of all native binaries — Grafana, PgBouncer, stunnel, psql, and their bundled shared libraries — **inside an `ubuntu:22.04` container** (glibc 2.35, matching the Databricks Apps runtime exactly).
2. The tarball is uploaded to a Unity Catalog Volume.
3. On every cold start, `startup.py` downloads and extracts it via the Databricks SDK Files API.

### Startup sequence

```
startup.py
  │
  ├─ Download + extract bin.tar.gz from UC Volume  (lib/staging.py)
  ├─ Mint Lakebase OAuth token                     (lib/lakebase.py)
  ├─ Preflight: verify CREATE privilege            (lib/preflight.py)
  ├─ Write stunnel.conf → launch stunnel           (lib/stunnel.py)
  │    PgBouncer ──plaintext──▶ stunnel ──TLS+SNI──▶ Lakebase:5432
  ├─ Write pgbouncer.ini + userlist.txt → launch PgBouncer  (lib/pgbouncer.py)
  ├─ Write provisioning/datasources/lakebase.yaml
  ├─ Write provisioning/datasources/databricks.yaml  (if warehouse configured)
  ├─ Write provisioning/dashboards/provider.yaml
  ├─ Launch Grafana
  └─ Start token-refresher thread                  (lib/refresher.py)
       every TOKEN_REFRESH_INTERVAL_S seconds:
         mint new token → rewrite pgbouncer.ini → RELOAD + RECONNECT
```

### Why PgBouncer + stunnel?

- **PgBouncer** provides session pooling and holds the Lakebase OAuth token as the server password. When the token rotates (every ~50 min) the refresher rewrites the ini and issues `RELOAD; RECONNECT` without restarting Grafana.
- **stunnel** adds TLS + SNI to the PgBouncer→Lakebase hop. PgBouncer never sends TLS SNI on server connections (any version), but Lakebase routes by SNI. stunnel terminates the Postgres STARTTLS handshake and opens a verified TLS connection with the correct SNI hostname.

### Auth

In the deployed app, `GRAFANA_AUTH_PROXY=true` makes Grafana trust the `X-Forwarded-Email` / `X-Forwarded-Preferred-Username` headers injected by the Databricks Apps SSO proxy. Users are auto-provisioned as **Editor**. The whitelist restricts auth-proxy trust to loopback (`127.0.0.1, ::1`).

Locally (no SSO proxy) the app falls back to anonymous Admin.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Databricks classic workspace | Lakebase requires a workspace with serverless support; FE VM workspaces work |
| Databricks CLI ≥ 0.285.0 | `brew upgrade databricks` |
| Docker + colima (linux/amd64) | For building the binary bundle on a Mac |
| `psql` client | `brew install postgresql@16` |
| Unity Catalog Volume | For hosting `bin.tar.gz` (see setup below) |

---

## One-time setup

### 1. Create the Lakebase project

```bash
databricks postgres create-project grafana-app \
  --json '{"spec": {"display_name": "Grafana App"}}' \
  --profile fevm-classic

# Wait until the endpoint is ACTIVE (~1-2 min)
databricks postgres list-endpoints \
  projects/grafana-app/branches/production \
  --profile fevm-classic -o json | jq '.[0].status'
```

Get the endpoint host:

```bash
databricks postgres list-endpoints \
  projects/grafana-app/branches/production \
  --profile fevm-classic -o json | jq -r '.[0].status.hosts.host'
```

### 2. Create the Grafana database and grant the app SP access

```bash
HOST=<endpoint-host-from-above>
TOKEN=$(databricks postgres generate-database-credential \
  projects/grafana-app/branches/production/endpoints/primary \
  --profile fevm-classic -o json | jq -r '.token')
EMAIL=$(databricks current-user me --profile fevm-classic -o json | jq -r '.userName')

# Create the database
PGPASSWORD=$TOKEN psql "host=$HOST port=5432 dbname=postgres user=$EMAIL sslmode=require" \
  -c "CREATE DATABASE grafana;"

# Provision the extension and grant the app SP (replace UUID with the app's SP applicationId)
PGPASSWORD=$TOKEN psql "host=$HOST port=5432 dbname=grafana user=$EMAIL sslmode=require" \
  -c "CREATE EXTENSION IF NOT EXISTS databricks_auth;" \
  -c "SELECT databricks_create_role('<app-sp-uuid>', 'SERVICE_PRINCIPAL');" \
  -c "GRANT CREATE ON DATABASE grafana TO \"<app-sp-uuid>\";"
```

### 3. Build and upload the binary bundle

The bundle must be built on `ubuntu:22.04` (glibc 2.35 = Apps runtime). From the repo root:

```bash
# Start colima with linux/amd64 if not already running
colima start --arch x86_64 --memory 4

# Build the bundle (outputs ~/grafana-bundle-stage/bin.tar.gz, ~200 MB)
bash scripts/assemble_bundle.sh

# Upload to UC Volume (adjust catalog/schema/volume as needed)
databricks fs cp \
  ~/grafana-bundle-stage/bin.tar.gz \
  dbfs:/Volumes/<catalog>/<schema>/<volume>/bin.tar.gz \
  --profile fevm-classic
```

Grant the app SP READ on the volume:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-sp-uuid>`;
GRANT USE SCHEMA ON SCHEMA <catalog>.<schema> TO `<app-sp-uuid>`;
GRANT READ VOLUME ON VOLUME <catalog>.<schema>.<volume> TO `<app-sp-uuid>`;
```

### 4. Create the Grafana secret scope

The secret scope stores the Grafana session-signing key so sessions survive container restarts:

```bash
databricks secrets create-scope grafana-app --profile fevm-classic

# Generate a random key and store it
KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
databricks secrets put-secret grafana-app grafana_secret_key \
  --string-value "$KEY" --profile fevm-classic

# Grant the app SP READ (use the raw UUID as the principal)
databricks secrets put-acl grafana-app \
  "<app-sp-uuid>" READ --profile fevm-classic
```

### 5. Configure `app.yaml`

Update the values in `app.yaml` to match your environment:

| Variable | What to set |
|---|---|
| `GRAFANA_BUNDLE_VOLUME_PATH` | UC Volume path, e.g. `/Volumes/catalog/schema/volume/bin.tar.gz` |
| `LAKEBASE_ENDPOINT_PATH` | `projects/grafana-app/branches/production/endpoints/primary` |
| `LAKEBASE_DATABASE_NAME` | `grafana` |
| `LAKEBASE_HOST` | Endpoint hostname from step 1 |
| `LAKEBASE_DB_USER` | App SP `applicationId` (UUID) |
| `GRAFANA_ROOT_URL` | Leave blank until first deploy; set to `https://<app>-<workspace-id>.databricksapps.com` after |
| `DATABRICKS_WAREHOUSE_HTTP_PATH` | `sql/1.0/warehouses/<id>` — omit to disable the SQL warehouse datasource |

---

## Deploy

Uses a [Databricks Asset Bundle](https://docs.databricks.com/dev-tools/bundles/index.html):

```bash
# First deploy (GRAFANA_ROOT_URL not yet known)
databricks bundle deploy -t dev --profile fevm-classic
databricks bundle run grafana-app -t dev --profile fevm-classic

# After the first deploy, note the app URL from the output, set GRAFANA_ROOT_URL
# in app.yaml, then redeploy:
databricks apps stop grafana-app --profile fevm-classic
databricks bundle deploy -t dev --profile fevm-classic
databricks bundle run grafana-app -t dev --profile fevm-classic
```

The full deploy recipe (stop → deploy → run) forces a fresh container so the new source is picked up:

```bash
databricks apps stop grafana-app --profile fevm-classic && \
databricks bundle deploy -t dev --profile fevm-classic && \
databricks bundle run grafana-app -t dev --profile fevm-classic
```

---

## Local dev

Two Docker images are provided under `tests/integration/`:

| Image | Purpose |
|---|---|
| `Dockerfile` | Standard harness — installs runtime libs via apt, uses a named volume for `bin/` |
| `Dockerfile.selfcontained` | Self-containment proof — `ubuntu:22.04` base, NO apt runtime libs; proves the bundle runs with only `bin/lib/` on `LD_LIBRARY_PATH` |
| `Dockerfile.bundlebuilder` | Used by `assemble_bundle.sh` — ubuntu:22.04 + pgdg packages for building the bundle |

The `boot_local.sh` entrypoint extracts the bundle from a local tarball if `bin/` is not already populated:

```bash
# Build the selfcontained image
docker build --platform linux/amd64 \
  -f tests/integration/Dockerfile.selfcontained \
  -t grafana-app-selfcontained .

# Run it (bundle tarball bind-mounted in)
docker run --rm --platform linux/amd64 \
  -v "$HOME/grafana-bundle-stage:/bundle:ro" \
  -e GRAFANA_BUNDLE_TARBALL=/bundle/bin.tar.gz \
  --env-file .env.integration \
  grafana-app-selfcontained
```

Create `.env.integration` with local values (no SSO proxy — Grafana runs as anonymous Admin):

```bash
DATABRICKS_HOST=https://fevm-classic-stable-qh34qo.cloud.databricks.com
DATABRICKS_TOKEN=<your-pat>
DATABRICKS_APP_PORT=3000
LAKEBASE_ENDPOINT_PATH=projects/grafana-app/branches/production/endpoints/primary
LAKEBASE_DATABASE_NAME=grafana
LAKEBASE_DB_USER=<app-sp-uuid>
LAKEBASE_HOST=<endpoint-host>
GRAFANA_ROOT_URL=http://localhost:3000
TOKEN_REFRESH_INTERVAL_S=3000
STUNNEL_VERIFY_CHAIN=true
```

---

## Configuration reference

All configuration is via environment variables. The deployed app reads them from `app.yaml`; the local harness uses `--env-file`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABRICKS_APP_PORT` | Yes | — | Injected by Apps runtime; Grafana listens on this port |
| `DATABRICKS_HOST` | Yes | — | Injected by Apps runtime |
| `DATABRICKS_CLIENT_ID` | Yes | — | Injected by Apps runtime (app SP) |
| `DATABRICKS_CLIENT_SECRET` | Yes | — | Injected by Apps runtime; stripped from Grafana env |
| `LAKEBASE_ENDPOINT_PATH` | Yes | — | Resource path for `generate-database-credential` |
| `LAKEBASE_DATABASE_NAME` | Yes | — | Postgres database name (e.g. `grafana`) |
| `LAKEBASE_DB_USER` | Yes | — | App SP `applicationId` UUID |
| `LAKEBASE_HOST` | No | Resolved from endpoint path | Static endpoint hostname; set to skip the API call |
| `GRAFANA_ROOT_URL` | Yes | — | Public app URL; drives `GF_SERVER_ROOT_URL` |
| `GRAFANA_BUNDLE_VOLUME_PATH` | No | — | UC Volume path for `bin.tar.gz`; unset = `bin/` pre-staged |
| `GRAFANA_AUTH_PROXY` | No | `false` | Set `true` in `app.yaml` to enable SSO proxy auth |
| `STUNNEL_VERIFY_CHAIN` | No | `false` | Set `true` to verify Lakebase TLS chain (recommended in production) |
| `TOKEN_REFRESH_INTERVAL_S` | No | `3000` | Token rotation interval (Lakebase tokens expire after ~1h) |
| `PGBOUNCER_PORT` | No | `6432` | PgBouncer listen port (loopback) |
| `PGBOUNCER_BINARY` | No | `bin/pgbouncer` | Path to PgBouncer binary |
| `STUNNEL_BINARY` | No | `bin/stunnel` | Path to stunnel binary |
| `STUNNEL_PORT` | No | `5433` | stunnel accept port (loopback) |
| `DATABRICKS_WAREHOUSE_HTTP_PATH` | No | — | Enables Databricks SQL datasource when set with SP creds |

---

## Datasources and dashboards

Both datasources are file-provisioned — Grafana reads them from `provisioning/datasources/` on startup so they exist on a fresh install without manual UI configuration.

| Datasource | UID | Plugin | Auth |
|---|---|---|---|
| Lakebase | `lakebase` | `postgres` | Per-boot loopback password via PgBouncer |
| Databricks SQL | `databricks-sql` | `mullerpeter-databricks-datasource` | OAuth2 M2M (app SP) |

The Databricks SQL datasource uses the [mullerpeter community plugin](https://github.com/mullerpeter/databricks-grafana), bundled into `bin/grafana/data/plugins/` by `assemble_bundle.sh`. It is unsigned and allow-listed via `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS`.

Two demo dashboards are provisioned from `provisioning/dashboards/json/`:

- **`lakebase-demo.json`** — queries the Lakebase datasource
- **`databricks-sql-demo.json`** — queries the NYC Taxi table via the SQL warehouse

---

## Repo structure

```
├── startup.py                  Python supervisor entrypoint
├── app.yaml                    Databricks Apps configuration
├── databricks.yml              Databricks Asset Bundle (DAB)
├── requirements.txt            Python deps (databricks-sdk, psycopg)
│
├── lib/
│   ├── config.py               Load + validate env config
│   ├── grafana_env.py          Build Grafana GF_* env, render datasource YAML
│   ├── lakebase.py             Mint OAuth token, resolve endpoint host
│   ├── pgbouncer.py            Render ini/userlist, launch, reload
│   ├── preflight.py            Verify CREATE privilege before startup
│   ├── refresher.py            Background token rotation loop
│   ├── staging.py              Download + extract bin.tar.gz from UC Volume
│   └── stunnel.py              Render stunnel.conf, launch
│
├── provisioning/
│   ├── dashboards/json/        Static dashboard JSON files
│   └── datasources/            Datasource templates (runtime files are gitignored)
│
├── scripts/
│   ├── assemble_bundle.sh      Build bin.tar.gz inside ubuntu:22.04 container
│   └── fetch_binaries.sh       Legacy (superseded by assemble_bundle.sh)
│
└── tests/
    ├── test_*.py               Unit tests (35 tests, no network/process required)
    └── integration/
        ├── Dockerfile              Standard local harness
        ├── Dockerfile.selfcontained  Self-containment proof (ubuntu:22.04)
        ├── Dockerfile.bundlebuilder  Bundle builder image
        └── boot_local.sh           Container entrypoint
```

---

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

All 35 unit tests run without network access, live databases, or running processes. Integration tests (full stack via Docker) require the local harness.

---

## Security notes

- **Token isolation:** `DATABRICKS_CLIENT_SECRET` and `DATABRICKS_CLIENT_ID` are stripped from the Grafana process environment — Grafana can only reach the warehouse via the provisioned datasource YAML (chmod 0600).
- **TLS verification:** `STUNNEL_VERIFY_CHAIN=true` (set in `app.yaml`) verifies the Lakebase certificate chain against the system CA bundle and pins the hostname via `checkHost`.
- **Auth-proxy whitelist:** Grafana only trusts `X-Forwarded-*` headers from `127.0.0.1` and `::1`, preventing header spoofing if the Grafana port were somehow directly reachable.
- **Secret key:** `GF_SECURITY_SECRET_KEY` is sourced from a Databricks secret scope (`grafana-app`) so Grafana sessions survive container restarts. Startup soft-fails gracefully if the secret is unavailable.
- **Runtime files:** `pgbouncer.ini`, `userlist.txt`, `stunnel.conf`, and datasource YAML files contain secrets and are chmod 0600. They are gitignored and regenerated on every boot.
