"""
startup.py — Supervisor for Grafana on Databricks Apps (Lakebase backend).

Sequence:
  1. Configure logging.
  2. Load config from env.
  3. Mint a Lakebase token; run preflight (direct psycopg to Lakebase).
  4. Generate per-boot loopback password; write pgbouncer.ini + userlist.txt (chmod 0600).
  5. Launch PgBouncer.
  6. Write provisioning/datasources/lakebase.yaml (chmod 0600).
  7. Launch Grafana.
  8. Start token-refresher background thread.
  9. Install SIGTERM/SIGINT handlers.
 10. Supervisor loop: wait on Grafana; handle PgBouncer unexpected exit.

Import-safety contract: all real work is inside main(); top-level module scope
is pure (constants, imports, function definitions only) so that
`python -c "import startup"` succeeds without side effects.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

from lib.config import load_config
from lib.grafana_env import build_env, render_lakebase_datasource
from lib.lakebase import mint_token, resolve_endpoint
from lib.pgbouncer import launch as pgb_launch, reload as pgb_reload, render_ini, render_userlist
from lib.preflight import check_create_privilege
from lib.refresher import RefreshState, run_loop

log = logging.getLogger("startup")

# ---------------------------------------------------------------------------
# Module-level path constants (configurable, not hard-wired to subprocess args)
# ---------------------------------------------------------------------------

# Paths are relative to the repo root (cwd when startup.py is executed).
# They are defined here so callers can override in tests or by env if needed.
PGBOUNCER_BINARY = os.environ.get("PGBOUNCER_BINARY", "bin/pgbouncer")
PGBOUNCER_INI_PATH = os.environ.get("PGBOUNCER_INI_PATH", "pgbouncer.ini")
USERLIST_PATH = os.environ.get("USERLIST_PATH", "userlist.txt")
GRAFANA_HOME = os.environ.get("GRAFANA_HOME", "bin/grafana")
PROVISIONING_DIR = os.environ.get("PROVISIONING_DIR", "provisioning")
DATASOURCE_YAML_PATH = os.environ.get(
    "DATASOURCE_YAML_PATH", "provisioning/datasources/lakebase.yaml"
)

# psql is used to reach the PgBouncer admin console (RELOAD; RECONNECT <db>;).
# NOTE: The deployed Databricks App image must include a `psql` binary on PATH.
# The fetch_binaries.sh script (Task 9) fetches Grafana and PgBouncer; a psql
# binary from the same distro package (e.g. libpq-dev / postgresql-client) must
# also be staged into bin/ and added to PATH in the app environment (Task 11).
# For now we assume `psql` is on PATH; if it moves we'll set PSQL_BINARY env.
PSQL_BINARY = os.environ.get("PSQL_BINARY", "psql")


# ---------------------------------------------------------------------------
# write_server_cred — rewrites password= in [databases] line, preserves 0600
# ---------------------------------------------------------------------------

def write_server_cred(tok: str, ini_path: str = PGBOUNCER_INI_PATH) -> None:
    """Rewrite the password= value on the [databases] connection string line.

    This function is called by the refresher thread on every token rotation.
    It rewrites only the password= field so as not to disturb other ini content.
    The file permissions are preserved at 0600 (set at initial write; re-applied
    here defensively in case an umask race widens them).
    """
    path = Path(ini_path)
    content = path.read_text()

    # Match the [databases] section line that contains password=<value>.
    # The pattern targets only the password= token within the db connstring line.
    new_content = re.sub(r"(password=)[^\s]+", rf"\g<1>{tok}", content, count=1)

    if new_content == content:
        log.warning("write_server_cred: password= pattern not found in %s; "
                    "ini may be malformed", ini_path)

    path.write_text(new_content)
    os.chmod(ini_path, 0o600)


# ---------------------------------------------------------------------------
# main — all side-effecting work lives here
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Load config from environment.
    cfg = load_config(os.environ)
    log.info("Config loaded: instance=%s db=%s pgbouncer_port=%d refresh_interval=%ds",
             cfg.instance_name, cfg.database_name, cfg.pgbouncer_port, cfg.refresh_interval_s)

    # 2. SDK client + resolve Lakebase endpoint + mint initial token.
    from databricks.sdk import WorkspaceClient  # imported here: heavy; import-safe at module top
    client = WorkspaceClient()

    log.info("Resolving Lakebase endpoint for instance '%s'", cfg.instance_name)
    ep = resolve_endpoint(client, cfg.instance_name)
    log.info("Lakebase endpoint: %s:%d", ep.host, ep.port)

    log.info("Minting initial Lakebase token")
    token = mint_token(client, cfg.instance_name)

    # 3. Preflight: verify CREATE privilege directly against Lakebase (PgBouncer not yet up).
    log.info("Running preflight: checking CREATE privilege on database '%s'", cfg.database_name)
    try:
        with psycopg.connect(
            host=ep.host,
            port=ep.port,
            dbname=cfg.database_name,
            user=cfg.db_user,
            password=token,
            sslmode="require",
        ) as preflight_conn:
            check_create_privilege(preflight_conn, cfg.database_name)
    except PermissionError as exc:
        log.error("Preflight failed: %s", exc)
        sys.exit(1)
    log.info("Preflight passed: service principal has CREATE on '%s'", cfg.database_name)

    # 4. Per-boot loopback password — static for the lifetime of this process.
    #    secrets.token_urlsafe uses base64url alphabet: A-Z a-z 0-9 - _
    #    No double-quote character is possible; we assert the contract explicitly.
    client_pw = secrets.token_urlsafe(24)
    assert '"' not in client_pw, (
        "client_pw contains a double-quote character, which would break "
        "PgBouncer's userlist.txt format. This should never happen with "
        "token_urlsafe but is asserted defensively."
    )

    # 5. Write pgbouncer.ini and userlist.txt; chmod 0600 (contain token + loopback password).
    ini_content = render_ini(
        listen_port=cfg.pgbouncer_port,
        db_name=cfg.database_name,
        server_host=ep.host,
        server_port=ep.port,
        server_user=cfg.db_user,
        server_token=token,
        client_user=cfg.db_user,
    )
    Path(PGBOUNCER_INI_PATH).write_text(ini_content)
    os.chmod(PGBOUNCER_INI_PATH, 0o600)

    userlist_content = render_userlist(cfg.db_user, client_pw)
    Path(USERLIST_PATH).write_text(userlist_content)
    os.chmod(USERLIST_PATH, 0o600)

    log.info("Wrote %s and %s (chmod 0600)", PGBOUNCER_INI_PATH, USERLIST_PATH)

    # 6. Launch PgBouncer.
    log.info("Launching PgBouncer: %s %s", PGBOUNCER_BINARY, PGBOUNCER_INI_PATH)
    pgbouncer_proc = pgb_launch(PGBOUNCER_BINARY, PGBOUNCER_INI_PATH)
    log.info("PgBouncer pid=%d", pgbouncer_proc.pid)

    # 7. Write datasource provisioning file; chmod 0600 (contains loopback password).
    Path(DATASOURCE_YAML_PATH).parent.mkdir(parents=True, exist_ok=True)
    datasource_yaml = render_lakebase_datasource(
        pgbouncer_port=cfg.pgbouncer_port,
        db_name=cfg.database_name,
        client_user=cfg.db_user,
        client_password=client_pw,
    )
    Path(DATASOURCE_YAML_PATH).write_text(datasource_yaml)
    os.chmod(DATASOURCE_YAML_PATH, 0o600)
    log.info("Wrote %s (chmod 0600)", DATASOURCE_YAML_PATH)

    # 8. Launch Grafana.
    grafana_env = {**os.environ, **build_env(
        app_port=cfg.app_port,
        pgbouncer_port=cfg.pgbouncer_port,
        db_name=cfg.database_name,
        client_user=cfg.db_user,
        client_password=client_pw,
        root_url=cfg.root_url,
        provisioning_dir=str(Path(PROVISIONING_DIR).resolve()),
    )}
    grafana_cmd = [f"{GRAFANA_HOME}/bin/grafana", "server", "--homepath", GRAFANA_HOME]
    log.info("Launching Grafana: %s", " ".join(grafana_cmd))
    grafana_proc = subprocess.Popen(grafana_cmd, env=grafana_env)
    log.info("Grafana pid=%d", grafana_proc.pid)

    # 9. Refresher: shared state so PgBouncer relaunch can use last_good_token.
    state = RefreshState(last_good_token=token)
    stop_event = threading.Event()

    def _write_server_cred(tok: str) -> None:
        write_server_cred(tok, PGBOUNCER_INI_PATH)

    def _reload() -> None:
        pgb_reload(PSQL_BINARY, cfg.pgbouncer_port, cfg.db_user, cfg.database_name)

    refresher_thread = threading.Thread(
        target=run_loop,
        kwargs=dict(
            state=state,
            mint=lambda: mint_token(client, cfg.instance_name),
            write_server_cred=_write_server_cred,
            reload=_reload,
            interval_s=cfg.refresh_interval_s,
            stop=stop_event,
        ),
        daemon=True,
        name="token-refresher",
    )
    refresher_thread.start()
    log.info("Token refresher thread started (interval=%ds)", cfg.refresh_interval_s)

    # 10. Signal handling: SIGTERM / SIGINT → clean shutdown.
    def _shutdown(signum: int, _frame) -> None:  # type: ignore[type-arg]
        log.info("Received signal %d; initiating shutdown", signum)
        stop_event.set()
        if grafana_proc.poll() is None:
            log.info("Terminating Grafana (pid=%d)", grafana_proc.pid)
            grafana_proc.terminate()
        if pgbouncer_proc.poll() is None:
            log.info("Terminating PgBouncer (pid=%d)", pgbouncer_proc.pid)
            pgbouncer_proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # 11. Supervisor loop.
    #
    # Strategy:
    #   - Grafana is the primary process: when it exits, we shut everything down
    #     and propagate its exit code.
    #   - PgBouncer is a support process: if it exits unexpectedly we relaunch it
    #     using state.last_good_token so the re-rendered ini has a valid password.
    #     We use a nonlocal reference so the signal handler can terminate the
    #     current pgbouncer_proc even if it has been relaunched.

    # Wrap pgbouncer_proc in a mutable container so the closure can rebind it.
    pgb_state = {"proc": pgbouncer_proc}

    log.info("Supervisor loop started; waiting on Grafana (pid=%d)", grafana_proc.pid)

    while True:
        # Poll Grafana.
        rc = grafana_proc.poll()
        if rc is not None:
            log.info("Grafana exited with code %d; shutting down", rc)
            stop_event.set()
            p = pgb_state["proc"]
            if p.poll() is None:
                log.info("Terminating PgBouncer (pid=%d)", p.pid)
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.warning("PgBouncer did not exit cleanly; killing")
                    p.kill()
            sys.exit(rc)

        # Poll PgBouncer.
        p = pgb_state["proc"]
        pgb_rc = p.poll()
        if pgb_rc is not None:
            log.warning("PgBouncer (pid=%d) exited unexpectedly with code %d; relaunching",
                        p.pid, pgb_rc)
            # Re-render ini with the last successfully minted token (NOT the boot
            # token, which may have expired if this is a late crash).
            tok = state.last_good_token
            new_ini = render_ini(
                listen_port=cfg.pgbouncer_port,
                db_name=cfg.database_name,
                server_host=ep.host,
                server_port=ep.port,
                server_user=cfg.db_user,
                server_token=tok,
                client_user=cfg.db_user,
            )
            Path(PGBOUNCER_INI_PATH).write_text(new_ini)
            os.chmod(PGBOUNCER_INI_PATH, 0o600)
            new_proc = pgb_launch(PGBOUNCER_BINARY, PGBOUNCER_INI_PATH)
            log.info("PgBouncer relaunched (pid=%d)", new_proc.pid)
            pgb_state["proc"] = new_proc

        time.sleep(1)


if __name__ == "__main__":
    main()
