# Grafana on Databricks Apps (Lakebase-backed) — Design

**Date:** 2026-06-14
**Status:** Approved (design), pending spec review
**Owner:** matt.slack@databricks.com

## Goal

Run **Grafana OSS** (a Go binary) inside a **Databricks App** in a **classic
workspace**, using **Lakebase (managed Postgres)** as Grafana's backend store
and as its first dashboard datasource. The Databricks SQL warehouse datasource
is an additive option for a later phase.

This follows the "custom language execution" method for Databricks Apps
(Ivan Trusov, Apps SME Office Hours EMEA 2026-05-27): native runtimes are
Python and Node.js only; **compiled languages (Go, etc.) must be built/obtained
externally as a `linux-amd64` binary, uploaded with the app, and run as the
startup command** — never compiled inside the app environment.

## Decisions (locked)

| Decision | Choice |
|----------|--------|
| Backend store | Lakebase (managed Postgres) |
| Purpose | Real dashboards over data (not just a PoC) |
| Primary datasource | Lakebase Postgres (native Grafana Postgres datasource) |
| Secondary datasource | Databricks SQL warehouse plugin — additive, phase 2 |
| Auth model | Lean on the Databricks Apps SSO proxy; Grafana runs anonymous with org role `Admin`, login form disabled |
| Binary delivery | **A** — pre-stage Grafana + PgBouncer binaries in the app folder, uploaded with the app |
| Credential rotation | **Y** — local PgBouncer + Python token-refresher so Grafana survives Lakebase token rotation |

## Key constraints driving the design

1. **Lakebase auth is a short-lived OAuth token used as the Postgres password
   (~1h TTL).** Grafana is a long-running process with a connection pool and
   **cannot reload its DB password** without a restart. A naive single-token
   approach fails ~1h after boot when a pooled connection re-authenticates.
2. **`os.execv` is incompatible with the rotation requirement.** The doc's R
   Shiny pattern uses `execv` to replace the Python process with the target
   binary. That would kill the Python token-refresher thread. Therefore the
   entrypoint is a **Python supervisor** managing child processes, not an
   `execv` hand-off. (`execv` would only fit a simpler single-token variant.)
3. **A + Y requires two prebuilt `linux-amd64` binaries**: Grafana (Go) and
   PgBouncer (C). Both are obtained externally and uploaded with the app,
   consistent with the doc's method.

## Architecture

A single Databricks App. `app.yaml` command = `python startup.py`.
`startup.py` is a long-running **supervisor**:

```
Databricks App container (serverless, behind workspace SSO proxy)
┌──────────────────────────────────────────────────────────────┐
│  startup.py  (Python supervisor + token refresher thread)      │
│     │ spawns                                                   │
│     ├──► pgbouncer  (127.0.0.1:6432)  ──TLS──► Lakebase :5432  │
│     └──► grafana server (0.0.0.0:$DATABRICKS_APP_PORT)         │
│             └── DB conns + Postgres datasource → 127.0.0.1:6432│
└──────────────────────────────────────────────────────────────┘
```

- Grafana connects **only** to local PgBouncer using a **static loopback
  password**. This password is **generated randomly once per boot** by
  `startup.py` and shared between `userlist.txt` and `GF_DATABASE_PASSWORD`; it
  never rotates during the process lifetime.
- PgBouncer holds the **rotating** Lakebase token as its server-side credential.
- The refresher re-mints the token every ~50 min and updates PgBouncer's
  **server-side** credential (the connection-string password PgBouncer uses to
  reach Lakebase — distinct from the client-side `userlist.txt` Grafana
  authenticates against). It then issues a PgBouncer `RELOAD` via the admin
  console. `server_lifetime` is kept **shorter than the token TTL** (e.g. ~30
  min vs ~60 min) so pooled server connections are recycled and re-authenticate
  with the fresh token before the old one expires. Grafana never observes a
  credential change.

**Load-bearing spike (resolve early in implementation):** confirm the exact
PgBouncer re-auth mechanism — whether `RELOAD` alone causes new server
connections to pick up the rewritten server password, and the precise interplay
of `server_lifetime` / `server_idle_timeout` with the refresh interval. This is
the single most critical behaviour in the design and must be verified before
building the rest.
- Both Grafana's metadata store **and** the Lakebase dashboard datasource point
  at `127.0.0.1:6432`, so all Lakebase traffic flows through the rotating pool.

## Components / file layout

```
databricks-grafana-app/
├── app.yaml                      # command: ["python","startup.py"]
├── requirements.txt              # databricks-sdk, psycopg[binary]
├── startup.py                    # supervisor: env → mint → pgbouncer → grafana → refresher → wait
├── lib/
│   ├── lakebase.py               # mint Postgres token + resolve host/port/db via SDK
│   ├── pgbouncer.py              # render pgbouncer.ini + userlist.txt, launch, RELOAD
│   ├── refresher.py              # background thread: re-mint → rewrite server cred → RELOAD
│   └── grafana_env.py            # build GF_* env + render provisioning files
├── provisioning/datasources/
│   ├── lakebase.yaml             # native Postgres datasource → 127.0.0.1:6432
│   └── databricks-sql.yaml.disabled  # additive SQL-warehouse datasource (phase 2)
├── bin/                          # GITIGNORED, staged at deploy time
│   ├── grafana/                  # full OSS linux-amd64 dist (bin/ conf/ public/ …)
│   └── pgbouncer                 # linux-amd64 binary
├── scripts/fetch_binaries.sh     # downloads + extracts Grafana + PgBouncer into bin/ (version-pinned + checksum-verified)
└── README.md
```

**On choice "A":** the ~300 MB of binaries are **gitignored** and pulled into
`bin/` by `fetch_binaries.sh` before `databricks sync`/deploy, so they ship with
the app (the doc's intent) without bloating git history.

## Boot data flow

1. Read env: `DATABRICKS_APP_PORT`, Lakebase instance name, DB name. App service
   principal credentials are auto-injected by the Apps runtime.
2. `lakebase.mint()` → SDK generates a Postgres OAuth token and resolves the
   instance host/port.
3. Write `pgbouncer.ini` (listen `127.0.0.1:6432`; server = Lakebase
   `host:5432` with `sslmode=require`; server password = token) and
   `userlist.txt` (static local credential for Grafana). Launch PgBouncer.
4. `grafana_env.build()` sets:
   - `GF_DATABASE_TYPE=postgres`, `GF_DATABASE_HOST=127.0.0.1:6432`,
     `GF_DATABASE_NAME`, `GF_DATABASE_USER`, `GF_DATABASE_PASSWORD=<static
     local>`, `GF_DATABASE_SSL_MODE=disable` (loopback; PgBouncer→Lakebase is
     TLS).
   - `GF_SERVER_HTTP_PORT=$DATABRICKS_APP_PORT`, `GF_SERVER_ROOT_URL`,
     `GF_SERVER_SERVE_FROM_SUB_PATH=true` (Apps serve under a path prefix).
   - `GF_AUTH_ANONYMOUS_ENABLED=true`, `GF_AUTH_ANONYMOUS_ORG_ROLE=Admin`,
     `GF_AUTH_DISABLE_LOGIN_FORM=true`.
   - `GF_PATHS_PROVISIONING` → `provisioning/`.
   - Render `provisioning/datasources/lakebase.yaml` (native Postgres datasource
     → `127.0.0.1:6432`).
5. Launch Grafana as a child process. Start the refresher thread.
6. Supervisor `wait()`s. On Grafana exit or SIGTERM: tear down children, exit.

## Error handling

- **Boot token mint fails** → fail fast with a clear log; the Apps platform
  restarts the app.
- **Refresher error** → exponential backoff + retry, keep last-good token, emit
  a loud log; never crash the supervisor on a transient refresh failure.
- **PgBouncer dies** → supervisor restarts it, re-rendering its config with the
  refresher's **current last-good token**, not the boot token, to avoid
  restarting onto a stale credential.
- **Grafana dies** → supervisor exits (platform restarts the app).
- **First-boot schema creation** → Grafana auto-migrates its tables into the
  Lakebase database, so the app service principal's Postgres role needs `CREATE`
  on that database/schema. (This is the known Lakebase "GRANT CREATE ON
  DATABASE" gotcha — a missing grant surfaces as a misleading Postgres error.)
  Include a one-time bootstrap SQL snippet and a preflight check that fails with
  a precise, actionable message if the grant is missing.

## Testing strategy

- **Local boot:** run `startup.py` with a fake `DATABRICKS_APP_PORT` against
  Lakebase from a laptop; assert Grafana boots, persists a dashboard across a
  restart, and the Lakebase datasource returns rows.
- **Rotation (core risk):** set the refresh interval to ~1 min in a test; confirm
  Grafana DB connections and datasource queries survive past the token TTL. This
  test exists to prove the entire reason for the PgBouncer layer.
- **Deploy:** `databricks bundle deploy` (or apps deploy) → open the app URL
  through the SSO proxy and confirm Grafana loads under the sub-path.

## Prerequisites to confirm before/while building

- Databricks **Apps** enabled in the classic workspace; the workspace host URL.
- **Lakebase** instance provisioned: instance name, database name, and the app
  service principal granted a Postgres role with `CREATE` on the Grafana DB.
- Grafana version pin (default: latest Grafana **OSS** `linux-amd64`).
- PgBouncer `linux-amd64` binary source (static build or distro package
  extracted; obtained externally per the doc's method).

## Phase 2 (out of scope for the first plan)

- Enable the Databricks SQL warehouse datasource
  (`databricks-sql.yaml.disabled` → active) via the official
  `databricks-grafana-datasource` plugin, including warehouse selection and
  plugin delivery (pre-bundled in `bin/grafana/plugins` to avoid boot-time
  egress).
- Dashboard provisioning (committed dashboard JSON) once datasources are stable.
```

---

## Addendum 2026-06-14 — Autoscaling Lakebase (supersedes provisioned-API assumptions)

The Lakebase instance was provisioned on the **Autoscaling** tier, not the
provisioned `databricks database` tier the body of this spec assumed. The
following supersedes the earlier `instance_name` / `w.database` references.

**Provisioned instance (workspace `fevm-classic`):**
- Project / branch / endpoint: `grafana-app` / `production` / `primary`
- Endpoint resource path: `projects/grafana-app/branches/production/endpoints/primary`
- Host: `ep-lucky-tree-d25raxtv.database.us-east-1.cloud.databricks.com`, port `5432`
- Database: `grafana` (created), engine **PostgreSQL 17.10**

**Runtime API (databricks-sdk, verified against installed version):**
- Token: `w.postgres.generate_database_credential(endpoint_path).token`
  (also returns `.expire_time`). `endpoint_path` is the string above.
- Host (optional resolve): `w.postgres.get_endpoint(endpoint_path).status.hosts.host`.
  Host is stable, so it may instead be supplied via env var.
- **Connection user is the Databricks identity**, not a Postgres role: the app's
  service principal application-id at runtime (a human email when testing
  locally). There is no `grafana_sp` role.

**Impact on the build:**
- `lib/lakebase.py` → use `client.postgres.generate_database_credential(...)` and
  `client.postgres.get_endpoint(...)`; key off the endpoint path, not an instance name.
- `lib/config.py` → carry the endpoint path (or project/branch/endpoint parts) and
  the connection identity (`db_user` = SP app-id / email), plus an optional explicit host.
- `lib/grafana_env.py` → datasource `postgresVersion: 1700` (PG 17), was 1500.
- Deploy: grant the app SP `CREATE` on the `grafana` DB (preflight enforces it).
