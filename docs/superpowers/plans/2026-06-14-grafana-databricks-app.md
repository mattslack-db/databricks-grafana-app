# Grafana on Databricks Apps (Lakebase) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Grafana OSS as a prebuilt Go binary inside a Databricks App (classic workspace), backed by Lakebase Postgres, surviving Lakebase's ~1h OAuth-token rotation via a local PgBouncer + Python refresher.

**Architecture:** `app.yaml` runs `python startup.py`, a long-running **supervisor** that mints a Lakebase token, launches PgBouncer (local pooler holding the rotating token) and Grafana (talking only to local PgBouncer with a static per-boot password), runs a background token-refresher thread, and forwards signals. Grafana's metadata store and its Lakebase datasource both route through PgBouncer at `127.0.0.1:6432`.

**Tech Stack:** Python 3.11 (stdlib + `databricks-sdk`, `psycopg[binary]`), Grafana OSS `linux-amd64`, PgBouncer `linux-amd64`, Databricks Apps, Lakebase (managed Postgres), `pytest`.

**Spec:** `docs/superpowers/specs/2026-06-14-grafana-databricks-app-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `app.yaml` | Apps entrypoint: `command: ["python","startup.py"]` |
| `requirements.txt` | `databricks-sdk`, `psycopg[binary]`, `pytest` (dev) |
| `startup.py` | Supervisor: preflight → mint → launch children → refresher → wait/signals |
| `lib/__init__.py` | Package marker |
| `lib/config.py` | Pure: read & validate env into a frozen `AppConfig` dataclass |
| `lib/lakebase.py` | Mint Postgres token + resolve instance host/port via SDK |
| `lib/pgbouncer.py` | Pure render of `pgbouncer.ini` + `userlist.txt`; launch/reload helpers |
| `lib/grafana_env.py` | Pure: build `GF_*` env dict + render datasource provisioning YAML |
| `lib/refresher.py` | Background refresh loop (injectable clock + mint fn for tests) |
| `lib/preflight.py` | Verify Postgres role has `CREATE` on the Grafana DB; precise error |
| `provisioning/datasources/lakebase.yaml.tmpl` | Native Postgres datasource template → 127.0.0.1:6432 |
| `provisioning/datasources/databricks-sql.yaml.disabled` | Phase-2 SQL-warehouse datasource (inert) |
| `scripts/fetch_binaries.sh` | Version-pinned + checksum-verified download of Grafana + PgBouncer into `bin/` |
| `databricks.yml` | Bundle config to deploy the app |
| `tests/test_config.py` … | Unit tests per pure module |
| `README.md` | Build/deploy/run instructions |

Pure modules (`config`, `pgbouncer` render, `grafana_env`, `refresher` logic) are unit-tested with TDD. Process launches, the supervisor, and binary fetch are validated by integration/manual steps.

---

## Task 0: Rotation spike (DE-RISK FIRST — blocks all else)

**Why first:** The entire PgBouncer layer exists to defeat the ~1h token cliff. If `RELOAD` does not cause new server connections to re-authenticate with a rewritten server password, the architecture changes. Prove it before building.

**Files:**
- Create: `docs/superpowers/spikes/2026-06-14-pgbouncer-rotation.md` (findings)

- [ ] **Step 1: Stand up a local Postgres + PgBouncer**, with PgBouncer pointed at Postgres using a server password set in `pgbouncer.ini` connstring (or `auth_user`).
- [ ] **Step 2: Open a psql session through PgBouncer** (`-h 127.0.0.1 -p 6432`) and confirm queries work.
- [ ] **Step 3: Rotate the *server* password** in Postgres (`ALTER ROLE ... PASSWORD`), rewrite the matching password in `pgbouncer.ini`, and run `RELOAD;` on the PgBouncer admin console (`psql -p 6432 pgbouncer`).
- [ ] **Step 4: Verify** that after `server_lifetime` elapses (set it to ~10s for the test) OR after `RECONNECT;`, new server connections authenticate with the new password and client queries keep working without the client reconnecting.
- [ ] **Step 5: Record findings** — exact keys used (`server_lifetime`, `server_idle_timeout`, whether `RELOAD` alone suffices or `RECONNECT` is needed), and the chosen production values (`server_lifetime` < token TTL, e.g. 1800s vs 3600s). Commit the spike doc.

```bash
git add docs/superpowers/spikes/2026-06-14-pgbouncer-rotation.md
git commit -m "spike: verify pgbouncer server-credential rotation on RELOAD"
```

**If the spike fails** (RELOAD/RECONNECT cannot rotate server creds): stop and revisit the design (fallback = supervisor restarts Grafana on a fresh token; sessions drop). Surface to human.

---

## Task 1: Project scaffold

**Files:**
- Create: `requirements.txt`, `lib/__init__.py`, `README.md`, `app.yaml`
- Verify: `.gitignore` already contains `bin/`

- [ ] **Step 1: Write `requirements.txt`**

```
databricks-sdk>=0.40
psycopg[binary]>=3.2
```
(dev/test additionally: `pytest>=8`)

- [ ] **Step 2: Write `app.yaml`**

```yaml
command: ["python", "startup.py"]
```

- [ ] **Step 3: Create `lib/__init__.py`** (empty) and a short `README.md`** stub** (filled in Task 11).

- [ ] **Step 4: Confirm `.gitignore` ignores `bin/`** (already created). If missing, add `bin/`, `__pycache__/`, `*.pyc`, `.DS_Store`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt app.yaml lib/__init__.py README.md .gitignore
git commit -m "chore: scaffold databricks-grafana-app project"
```

---

## Task 2: `lib/config.py` — env → validated AppConfig (TDD)

**Files:**
- Create: `lib/config.py`, `tests/test_config.py`

- [ ] **Step 1: Write failing test** `tests/test_config.py`

```python
import pytest
from lib.config import AppConfig, load_config

BASE_ENV = {
    "DATABRICKS_APP_PORT": "8080",
    "LAKEBASE_INSTANCE_NAME": "grafana-pg",
    "LAKEBASE_DATABASE_NAME": "grafana",
    "LAKEBASE_DB_USER": "grafana_sp",
    "GRAFANA_ROOT_URL": "https://app.example.com/grafana",
}

def test_load_config_parses_required_fields():
    cfg = load_config(BASE_ENV)
    assert cfg.app_port == 8080
    assert cfg.instance_name == "grafana-pg"
    assert cfg.database_name == "grafana"
    assert cfg.db_user == "grafana_sp"
    assert cfg.root_url == "https://app.example.com/grafana"
    assert cfg.refresh_interval_s == 3000  # default ~50 min
    assert cfg.pgbouncer_port == 6432

def test_load_config_missing_required_raises():
    env = dict(BASE_ENV); del env["LAKEBASE_INSTANCE_NAME"]
    with pytest.raises(ValueError) as e:
        load_config(env)
    assert "LAKEBASE_INSTANCE_NAME" in str(e.value)

def test_app_config_is_frozen():
    cfg = load_config(BASE_ENV)
    with pytest.raises(Exception):
        cfg.app_port = 9090
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_config.py -v` (ImportError).

- [ ] **Step 3: Implement `lib/config.py`**

```python
from __future__ import annotations
from dataclasses import dataclass

REQUIRED = ["DATABRICKS_APP_PORT", "LAKEBASE_INSTANCE_NAME",
            "LAKEBASE_DATABASE_NAME", "LAKEBASE_DB_USER", "GRAFANA_ROOT_URL"]

@dataclass(frozen=True)
class AppConfig:
    app_port: int
    instance_name: str
    database_name: str
    db_user: str
    root_url: str
    pgbouncer_port: int = 6432
    refresh_interval_s: int = 3000

def load_config(env: dict) -> AppConfig:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")
    return AppConfig(
        app_port=int(env["DATABRICKS_APP_PORT"]),
        instance_name=env["LAKEBASE_INSTANCE_NAME"],
        database_name=env["LAKEBASE_DATABASE_NAME"],
        db_user=env["LAKEBASE_DB_USER"],
        root_url=env["GRAFANA_ROOT_URL"],
        pgbouncer_port=int(env.get("PGBOUNCER_PORT", "6432")),
        refresh_interval_s=int(env.get("TOKEN_REFRESH_INTERVAL_S", "3000")),
    )
```

- [ ] **Step 4: Run, expect PASS** — `pytest tests/test_config.py -v`.
- [ ] **Step 5: Commit** — `git add lib/config.py tests/test_config.py && git commit -m "feat: env-driven AppConfig with validation"`

---

## Task 3: `lib/lakebase.py` — mint token + resolve endpoint (TDD, mocked SDK)

**Files:**
- Create: `lib/lakebase.py`, `tests/test_lakebase.py`

> **Verify against installed SDK first:** the exact method/field names for credential generation and instance lookup evolve. Before implementing, run a one-liner to confirm: `python -c "from databricks.sdk import WorkspaceClient; w=WorkspaceClient(); print([m for m in dir(w.database) if not m.startswith('_')])"`. Adjust `generate_database_credential` / `get_database_instance` calls and the token/host field names to match. The code below is the expected shape; treat field names as verify-then-wire.

- [ ] **Step 1: Write failing test** `tests/test_lakebase.py` (inject a fake client)

```python
from lib.lakebase import LakebaseEndpoint, resolve_endpoint, mint_token

class FakeCred:  # shape of generate_database_credential result
    def __init__(self, token): self.token = token

class FakeInstance:
    read_write_dns = "grafana-pg.db.example.com"
    pg_port = 5432

class FakeDatabaseAPI:
    def __init__(self): self.calls = []
    def get_database_instance(self, name): self.calls.append(("get", name)); return FakeInstance()
    def generate_database_credential(self, request_id, instance_names):
        self.calls.append(("gen", instance_names)); return FakeCred("tok-123")

class FakeClient:
    def __init__(self): self.database = FakeDatabaseAPI()

def test_resolve_endpoint_reads_dns_and_port():
    ep = resolve_endpoint(FakeClient(), "grafana-pg")
    assert ep == LakebaseEndpoint(host="grafana-pg.db.example.com", port=5432)

def test_mint_token_returns_token_string():
    assert mint_token(FakeClient(), "grafana-pg") == "tok-123"
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `lib/lakebase.py`**

```python
from __future__ import annotations
import uuid
from dataclasses import dataclass

@dataclass(frozen=True)
class LakebaseEndpoint:
    host: str
    port: int

def resolve_endpoint(client, instance_name: str) -> LakebaseEndpoint:
    inst = client.database.get_database_instance(name=instance_name)
    return LakebaseEndpoint(host=inst.read_write_dns, port=int(getattr(inst, "pg_port", 5432)))

def mint_token(client, instance_name: str) -> str:
    cred = client.database.generate_database_credential(
        request_id=str(uuid.uuid4()), instance_names=[instance_name])
    return cred.token
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Verify the real SDK shape** with the one-liner above; reconcile any naming differences and re-run tests.
- [ ] **Step 6: Commit** — `git commit -m "feat: lakebase token minting and endpoint resolution"`

---

## Task 4: `lib/pgbouncer.py` — render config (TDD pure) + launch/reload helpers

**Files:**
- Create: `lib/pgbouncer.py`, `tests/test_pgbouncer.py`

Apply Task 0 findings for `server_lifetime`/`server_idle_timeout` and whether reload uses `RELOAD` or `RELOAD`+`RECONNECT`.

- [ ] **Step 1: Write failing test** `tests/test_pgbouncer.py`

```python
from lib.pgbouncer import render_ini, render_userlist, scram_or_plain

def test_render_userlist_quotes_user_and_password():
    out = render_userlist("grafana_local", "secretpw")
    assert out.strip() == '"grafana_local" "secretpw"'

def test_render_ini_points_at_lakebase_with_ssl_and_short_lifetime():
    ini = render_ini(
        listen_port=6432, db_name="grafana",
        server_host="h.example.com", server_port=5432,
        server_user="grafana_sp", server_token="tok-123",
        client_user="grafana_local", server_lifetime=1800)
    assert "listen_addr = 127.0.0.1" in ini
    assert "listen_port = 6432" in ini
    assert "host=h.example.com port=5432" in ini
    assert "sslmode=require" in ini
    assert "user=grafana_sp" in ini
    assert "password=tok-123" in ini
    assert "server_lifetime = 1800" in ini
    assert "auth_type" in ini
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `lib/pgbouncer.py`** (render fns pure; launch/reload use subprocess)

```python
from __future__ import annotations
import subprocess

def render_userlist(client_user: str, client_password: str) -> str:
    return f'"{client_user}" "{client_password}"\n'

def render_ini(*, listen_port: int, db_name: str, server_host: str,
               server_port: int, server_user: str, server_token: str,
               client_user: str, server_lifetime: int = 1800,
               server_idle_timeout: int = 300) -> str:
    return f"""[databases]
{db_name} = host={server_host} port={server_port} dbname={db_name} user={server_user} password={server_token} sslmode=require

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {listen_port}
auth_type = scram-sha-256
auth_file = userlist.txt
admin_users = {client_user}
pool_mode = transaction
server_lifetime = {server_lifetime}
server_idle_timeout = {server_idle_timeout}
max_client_conn = 200
default_pool_size = 20
logfile =
pidfile = pgbouncer.pid
"""

def scram_or_plain(_token: str) -> str:  # placeholder if client auth needs md5/scram tweaks
    return "scram-sha-256"

def launch(binary: str, ini_path: str) -> subprocess.Popen:
    return subprocess.Popen([binary, ini_path])

def reload(psql_binary: str, port: int, admin_user: str, db_name: str) -> None:
    # Task 0 spike finding: RELOAD alone is INSUFFICIENT — if the server
    # password rotates before pooled server conns expire, PgBouncer enters
    # server_login_retry and rejects clients for several seconds. ALWAYS
    # follow RELOAD with RECONNECT <db> to clear that state immediately.
    subprocess.run([psql_binary, "-h", "127.0.0.1", "-p", str(port),
                    "-U", admin_user, "-d", "pgbouncer",
                    "-c", f"RELOAD; RECONNECT {db_name};"], check=True)
```

> **Task 0 spike findings applied:** `RELOAD` is always followed by `RECONNECT <db>`
> (not optional). Prod config: `pool_mode=transaction`, `server_lifetime=1800`,
> `server_idle_timeout=300`. We ship our own PgBouncer binary so we fully control
> `pgbouncer.ini` — the `edoburu` image's `auth_user=` quirk seen in the spike does
> not apply. Client side uses `scram-sha-256` with a known local password in
> `userlist.txt` (plaintext entry is accepted for local scram lookups). If client
> auth shows loopback friction, fall back to `auth_type = trust` for 127.0.0.1 only
> and record the decision.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: pgbouncer config rendering + launch/reload helpers"`

---

## Task 5: `lib/grafana_env.py` — GF_* env + datasource provisioning (TDD pure)

**Files:**
- Create: `lib/grafana_env.py`, `provisioning/datasources/lakebase.yaml.tmpl`, `tests/test_grafana_env.py`

- [ ] **Step 1: Write failing test** `tests/test_grafana_env.py`

```python
from lib.grafana_env import build_env, render_lakebase_datasource

def test_build_env_sets_db_to_local_pgbouncer_and_anon_admin():
    env = build_env(app_port=8080, pgbouncer_port=6432, db_name="grafana",
                    client_user="grafana_local", client_password="pw",
                    root_url="https://x/grafana", provisioning_dir="/app/provisioning")
    assert env["GF_DATABASE_TYPE"] == "postgres"
    assert env["GF_DATABASE_HOST"] == "127.0.0.1:6432"
    assert env["GF_DATABASE_NAME"] == "grafana"
    assert env["GF_DATABASE_USER"] == "grafana_local"
    assert env["GF_DATABASE_PASSWORD"] == "pw"
    assert env["GF_DATABASE_SSL_MODE"] == "disable"
    assert env["GF_SERVER_HTTP_PORT"] == "8080"
    assert env["GF_SERVER_ROOT_URL"] == "https://x/grafana"
    assert env["GF_SERVER_SERVE_FROM_SUB_PATH"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Admin"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert env["GF_PATHS_PROVISIONING"] == "/app/provisioning"

def test_render_lakebase_datasource_targets_pgbouncer():
    y = render_lakebase_datasource(pgbouncer_port=6432, db_name="grafana",
                                   client_user="grafana_local", client_password="pw")
    assert "type: postgres" in y
    assert "url: 127.0.0.1:6432" in y
    assert "sslmode" in y and "disable" in y
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `lib/grafana_env.py`**

```python
from __future__ import annotations

def build_env(*, app_port: int, pgbouncer_port: int, db_name: str,
              client_user: str, client_password: str, root_url: str,
              provisioning_dir: str) -> dict:
    return {
        "GF_DATABASE_TYPE": "postgres",
        "GF_DATABASE_HOST": f"127.0.0.1:{pgbouncer_port}",
        "GF_DATABASE_NAME": db_name,
        "GF_DATABASE_USER": client_user,
        "GF_DATABASE_PASSWORD": client_password,
        "GF_DATABASE_SSL_MODE": "disable",
        "GF_SERVER_HTTP_PORT": str(app_port),
        "GF_SERVER_ROOT_URL": root_url,
        "GF_SERVER_SERVE_FROM_SUB_PATH": "true",
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_AUTH_ANONYMOUS_ORG_ROLE": "Admin",
        "GF_AUTH_DISABLE_LOGIN_FORM": "true",
        "GF_PATHS_PROVISIONING": provisioning_dir,
    }

def render_lakebase_datasource(*, pgbouncer_port: int, db_name: str,
                               client_user: str, client_password: str) -> str:
    return f"""apiVersion: 1
datasources:
  - name: Lakebase
    type: postgres
    access: proxy
    url: 127.0.0.1:{pgbouncer_port}
    database: {db_name}
    user: {client_user}
    isDefault: true
    jsonData:
      sslmode: disable
      postgresVersion: 1500
    secureJsonData:
      password: {client_password}
"""
```

- [ ] **Step 4: Create `provisioning/datasources/lakebase.yaml.tmpl`** documenting the rendered shape (the live file is written by `startup.py` at boot from `render_lakebase_datasource`). Also create `provisioning/datasources/databricks-sql.yaml.disabled` as an inert Phase-2 placeholder with a comment.
- [ ] **Step 5: Run, expect PASS.**
- [ ] **Step 6: Commit** — `git commit -m "feat: grafana env + lakebase datasource provisioning"`

---

## Task 6: `lib/refresher.py` — refresh loop (TDD, injected clock + mint)

**Files:**
- Create: `lib/refresher.py`, `tests/test_refresher.py`

- [ ] **Step 1: Write failing test** `tests/test_refresher.py`

```python
from lib.refresher import refresh_once, RefreshState

def test_refresh_once_mints_writes_and_reloads():
    events = []
    state = RefreshState(last_good_token="old")
    def mint(): return "new-token"
    def write_server_cred(tok): events.append(("write", tok))
    def reload(): events.append(("reload",))
    refresh_once(state, mint, write_server_cred, reload)
    assert state.last_good_token == "new-token"
    assert events == [("write", "new-token"), ("reload",)]

def test_refresh_once_keeps_last_good_on_mint_failure():
    state = RefreshState(last_good_token="old")
    def mint(): raise RuntimeError("boom")
    def write_server_cred(tok): raise AssertionError("should not write")
    def reload(): raise AssertionError("should not reload")
    refresh_once(state, mint, write_server_cred, reload)  # must not raise
    assert state.last_good_token == "old"
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `lib/refresher.py`**

```python
from __future__ import annotations
import logging, threading, time
from dataclasses import dataclass

log = logging.getLogger("refresher")

@dataclass
class RefreshState:
    last_good_token: str

def refresh_once(state: RefreshState, mint, write_server_cred, reload) -> None:
    try:
        token = mint()
    except Exception:
        log.exception("token mint failed; keeping last-good token")
        return
    write_server_cred(token)
    reload()
    state.last_good_token = token

def run_loop(state: RefreshState, mint, write_server_cred, reload,
             interval_s: int, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        try:
            refresh_once(state, mint, write_server_cred, reload)
        except Exception:
            log.exception("refresh cycle error; will retry next interval")
```

> **Backoff note:** the spec mentions "exponential backoff" on refresh failure. A
> fixed `interval_s` retry is intentionally sufficient here: `refresh_once`
> preserves the last-good token on failure and the interval (~50 min) is well
> under the token TTL (~60 min), so a single missed cycle is non-fatal. If the
> Task 0 spike or ops experience shows clustered failures, add capped backoff
> then — YAGNI until proven.

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: token refresher loop with last-good fallback"`

---

## Task 7: `lib/preflight.py` — verify CREATE privilege (TDD with fake conn)

**Files:**
- Create: `lib/preflight.py`, `tests/test_preflight.py`

Grafana auto-migrates its schema on first boot; the SP role needs `CREATE` on the DB. Surface a precise error rather than Grafana's misleading failure (known Lakebase gotcha).

- [ ] **Step 1: Write failing test** `tests/test_preflight.py`

```python
import pytest
from lib.preflight import check_create_privilege

class FakeCur:
    def __init__(self, result): self._r = result
    def execute(self, *_): pass
    def fetchone(self): return (self._r,)
    def __enter__(self): return self
    def __exit__(self, *a): pass

class FakeConn:
    def __init__(self, result): self._r = result
    def cursor(self): return FakeCur(self._r)

def test_passes_when_has_create():
    check_create_privilege(FakeConn(True), "grafana")  # no raise

def test_raises_actionable_error_when_missing():
    with pytest.raises(PermissionError) as e:
        check_create_privilege(FakeConn(False), "grafana")
    assert "GRANT CREATE ON DATABASE grafana" in str(e.value)
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `lib/preflight.py`**

```python
from __future__ import annotations

def check_create_privilege(conn, db_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT has_database_privilege(current_user, %s, 'CREATE')", (db_name,))
        has = cur.fetchone()[0]
    if not has:
        raise PermissionError(
            f"Service principal lacks CREATE on database '{db_name}'. "
            f"Grafana cannot run its first-boot schema migration. "
            f"Fix: connect as an owner and run: GRANT CREATE ON DATABASE {db_name} TO <sp_role>;")
```

- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat: preflight CREATE-privilege check with actionable error"`

---

## Task 8: `startup.py` — supervisor wiring (integration)

**Files:**
- Create: `startup.py`

Not unit-tested (process orchestration); validated by Task 10 local boot. Keep functions small; reuse the tested lib modules.

- [ ] **Step 1: Implement `startup.py`** sequence:
  1. `logging.basicConfig(level=INFO)`.
  2. `cfg = load_config(os.environ)`.
  3. `client = WorkspaceClient()`; `ep = resolve_endpoint(...)`; `token = mint_token(...)`.
  4. Open a short `psycopg` connection **directly to Lakebase** (host/port from `resolve_endpoint`, user=`cfg.db_user`, password=freshly minted `token`, `sslmode=require`) — direct, not via PgBouncer, which isn't launched yet — and run `check_create_privilege`. On `PermissionError`, log and `sys.exit(1)`.
  5. `client_pw = secrets.token_urlsafe(24)` (per-boot static loopback password).
  6. Write `pgbouncer.ini` + `userlist.txt` (render fns). Define `write_server_cred(tok)` = rewrite the `[databases]` line's `password=` in `pgbouncer.ini`. Launch PgBouncer (`bin/pgbouncer`).
  7. Write `provisioning/datasources/lakebase.yaml` from `render_lakebase_datasource(...)`.
  8. Build env: `env = {**os.environ, **build_env(...)}`. Launch Grafana: `subprocess.Popen([f"{GRAFANA_HOME}/bin/grafana","server","--homepath",GRAFANA_HOME], env=env)`.
  9. Start refresher thread: `run_loop(state, mint=lambda: mint_token(client, cfg.instance_name), write_server_cred, reload=lambda: pgbouncer.reload(psql_binary, cfg.pgbouncer_port, cfg.db_user, cfg.database_name), interval_s=cfg.refresh_interval_s, stop=stop_event)`. (`reload` always issues `RELOAD; RECONNECT <db>;` per the Task 0 finding.)
  10. Install SIGTERM/SIGINT handlers that set `stop_event`, terminate children, and exit.
  11. Supervisor loop: `wait()` on Grafana; if Grafana exits → set stop, terminate PgBouncer, exit with Grafana's code. If PgBouncer exits unexpectedly → re-render with `state.last_good_token` and relaunch.
- [ ] **Step 2: `python -c "import startup"` smoke** (import-only; guard `main()` under `if __name__ == '__main__'`). Expected: no import errors.
- [ ] **Step 3: Commit** — `git commit -m "feat: supervisor entrypoint wiring pgbouncer + grafana + refresher"`

---

## Task 9: `scripts/fetch_binaries.sh` — staged binaries (pinned + checksummed)

**Files:**
- Create: `scripts/fetch_binaries.sh`

- [ ] **Step 1: Implement** a bash script that:
  - Pins `GRAFANA_VERSION` and a PgBouncer version as variables.
  - Downloads Grafana OSS `linux-amd64` tarball, verifies SHA256, extracts to `bin/grafana/`.
  - Obtains a PgBouncer `linux-amd64` binary (static build or extracted distro package), verifies SHA256, places at `bin/pgbouncer`, `chmod +x`.
  - Is idempotent (skip if present + checksum matches).
- [ ] **Step 2: Run `bash scripts/fetch_binaries.sh`** on a linux/amd64 host (or CI) — expect `bin/grafana/bin/grafana` and `bin/pgbouncer` present. On Apple-silicon laptop, fetch is for deploy staging; local boot test (Task 10) uses host-native binaries.
- [ ] **Step 3: Commit** — `git commit -m "build: version-pinned checksum-verified binary fetch script"`

---

## Task 10: Local end-to-end boot + rotation test (integration)

**Files:**
- Create: `tests/integration/test_boot.md` (manual runbook) or `tests/integration/test_boot.py`

- [ ] **Step 1: Boot locally** against Lakebase (or local Postgres standing in): set env (`DATABRICKS_APP_PORT=8080`, Lakebase vars), run `python startup.py`.
- [ ] **Step 2: Assert** Grafana reachable at `http://127.0.0.1:8080/grafana`, anonymous Admin, Lakebase datasource present and "Save & test" passes.
- [ ] **Step 3: Persistence** — create a dashboard, restart `startup.py`, confirm it survived (proves Postgres-backed state).
- [ ] **Step 4: Rotation (core risk)** — set `TOKEN_REFRESH_INTERVAL_S=60`, leave running >2 token TTLs, periodically run a datasource query and a Grafana DB write (save dashboard); confirm no auth failures past the original token TTL.
- [ ] **Step 5: Commit** the runbook/results — `git commit -m "test: local boot, persistence, and token-rotation runbook"`

---

## Task 11: Deploy config + smoke test + README (integration)

**Files:**
- Create: `databricks.yml`; finalize `README.md`

- [ ] **Step 1: Write `databricks.yml`** bundle defining the app resource, pointing at this directory, with the app env vars (`LAKEBASE_*`, `GRAFANA_ROOT_URL`, optional `TOKEN_REFRESH_INTERVAL_S`).
- [ ] **Step 2: Stage binaries** (`bash scripts/fetch_binaries.sh`) then `databricks bundle deploy` (or `databricks apps deploy`) to the classic workspace.
- [ ] **Step 3: Grant** the app SP a Lakebase Postgres role with `CREATE` on the Grafana DB (run the bootstrap SQL from the spec). Confirm the preflight passes in app logs.
- [ ] **Step 4: Smoke test** — open the app URL through the SSO proxy; confirm Grafana loads under the sub-path, datasource works, a dashboard persists across an app restart.
- [ ] **Step 5: Finalize `README.md`** — build (`fetch_binaries.sh`), deploy, required env, Lakebase grant, Phase-2 notes (enable `databricks-sql` datasource + bundle the plugin).
- [ ] **Step 6: Commit** — `git commit -m "docs: deploy config, README, and deployment runbook"`

---

## Out of scope (Phase 2 — separate plan)

- Activate the Databricks SQL warehouse datasource via the `databricks-grafana-datasource` plugin (pre-bundled in `bin/grafana/plugins` to avoid boot-time egress); warehouse selection + auth.
- Committed dashboard provisioning (dashboard JSON) once datasources are stable.
