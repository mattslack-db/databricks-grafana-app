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
from lib.stunnel import launch as stunnel_launch, render_conf as render_stunnel_conf

log = logging.getLogger("startup")

# ---------------------------------------------------------------------------
# Module-level path constants (configurable, not hard-wired to subprocess args)
# ---------------------------------------------------------------------------

# Paths are relative to the repo root (cwd when startup.py is executed).
# They are defined here so callers can override in tests or by env if needed.
PGBOUNCER_BINARY = os.environ.get("PGBOUNCER_BINARY", "bin/pgbouncer")
PGBOUNCER_INI_PATH = os.environ.get("PGBOUNCER_INI_PATH", "pgbouncer.ini")
USERLIST_PATH = os.environ.get("USERLIST_PATH", "userlist.txt")

# stunnel: TLS+SNI shim that PgBouncer connects to in plaintext over loopback.
# PgBouncer -> stunnel (127.0.0.1:STUNNEL_PORT) -> TLS+SNI -> Lakebase:5432.
# Default points at the bundled binary; override with STUNNEL_BINARY env var.
STUNNEL_BINARY = os.environ.get("STUNNEL_BINARY", "bin/stunnel")
STUNNEL_CONF_PATH = os.environ.get("STUNNEL_CONF_PATH", "stunnel.conf")
STUNNEL_PORT = int(os.environ.get("STUNNEL_PORT", "5433"))
# Verify the Lakebase server certificate chain (recommended for production).
STUNNEL_VERIFY_CHAIN = os.environ.get("STUNNEL_VERIFY_CHAIN", "false").lower() == "true"
GRAFANA_HOME = os.environ.get("GRAFANA_HOME", "bin/grafana")
PROVISIONING_DIR = os.environ.get("PROVISIONING_DIR", "provisioning")
DATASOURCE_YAML_PATH = os.environ.get(
    "DATASOURCE_YAML_PATH", "provisioning/datasources/lakebase.yaml"
)

# psql is used to reach the PgBouncer admin console (RELOAD; RECONNECT <db>;).
# Default points at the bundled binary staged by scripts/fetch_binaries.sh.
# Override with PSQL_BINARY env var (the local harness may override this).
PSQL_BINARY = os.environ.get("PSQL_BINARY", "bin/psql")


# ---------------------------------------------------------------------------
# write_server_cred — rewrites password= in [databases] line, preserves 0600
# ---------------------------------------------------------------------------

# Relaunch crash-loop ceiling: if PgBouncer dies more than this many times
# within the window, give up and exit non-zero so the platform restarts the app.
PGB_MAX_RELAUNCHES = 5
PGB_RELAUNCH_WINDOW_S = 60
# How long to wait after a launch before deciding the binary came up at all.
PGB_LAUNCH_SETTLE_S = 0.5


def write_server_cred(tok: str, ini_path: str, ini_lock: threading.Lock) -> None:
    """Rewrite the password= value on the [databases] connection string line.

    This function is called by the refresher thread on every token rotation.
    It rewrites only the password= field so as not to disturb other ini content.
    The file permissions are preserved at 0600 (set at initial write; re-applied
    here defensively in case an umask race widens them).

    The ini_lock serialises this write against the supervisor's relaunch
    re-render and shutdown teardown so the ini file is never read or written
    mid-tear.
    """
    with ini_lock:
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

def _prepend_lib_path() -> None:
    """Prepend bin/lib/ (absolute) to LD_LIBRARY_PATH.

    This must be called before any child process is launched so that pgbouncer,
    stunnel, and psql all inherit the bundled shared libraries from bin/lib/
    (staged by scripts/fetch_binaries.sh) rather than requiring apt-installed
    runtime packages. The Databricks Apps runtime has no apt access, so all
    non-glibc .so files are bundled there.

    Preserves any pre-existing LD_LIBRARY_PATH value (prepend, colon-separated).
    """
    lib_dir = str(Path(__file__).resolve().parent / "bin" / "lib")
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    new_val = f"{lib_dir}:{existing}" if existing else lib_dir
    os.environ["LD_LIBRARY_PATH"] = new_val
    log.info("LD_LIBRARY_PATH set to: %s", new_val)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 0. Prepend bin/lib/ to LD_LIBRARY_PATH so all child processes (pgbouncer,
    #    stunnel, psql) find their bundled shared libraries.  Must happen before
    #    any subprocess.Popen call.
    _prepend_lib_path()

    # 1. Load config from environment.
    cfg = load_config(os.environ)
    log.info("Config loaded: endpoint_path=%s db=%s pgbouncer_port=%d refresh_interval=%ds",
             cfg.endpoint_path, cfg.database_name, cfg.pgbouncer_port, cfg.refresh_interval_s)

    # 2. SDK client + resolve Lakebase endpoint + mint initial token.
    from databricks.sdk import WorkspaceClient  # imported here: heavy; import-safe at module top
    client = WorkspaceClient()

    if cfg.host:
        log.info("Using pre-configured Lakebase host: %s", cfg.host)
        from lib.lakebase import LakebaseEndpoint
        ep = LakebaseEndpoint(host=cfg.host, port=5432)
    else:
        log.info("Resolving Lakebase endpoint for path '%s'", cfg.endpoint_path)
        ep = resolve_endpoint(client, cfg.endpoint_path)
    log.info("Lakebase endpoint: %s:%d", ep.host, ep.port)

    log.info("Minting initial Lakebase token")
    token = mint_token(client, cfg.endpoint_path)

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

    # 5a. Write stunnel.conf and launch stunnel (TLS+SNI shim to Lakebase).
    #     No secrets in this file — just the Lakebase host/port + SNI.
    stunnel_conf = render_stunnel_conf(
        accept_port=STUNNEL_PORT,
        server_host=ep.host,
        server_port=ep.port,
        verify_chain=STUNNEL_VERIFY_CHAIN,
    )
    Path(STUNNEL_CONF_PATH).write_text(stunnel_conf)
    os.chmod(STUNNEL_CONF_PATH, 0o644)
    log.info("Launching stunnel: %s %s (accept 127.0.0.1:%d -> %s:%d, sni=%s)",
             STUNNEL_BINARY, STUNNEL_CONF_PATH, STUNNEL_PORT, ep.host, ep.port, ep.host)
    stunnel_proc = stunnel_launch(STUNNEL_BINARY, STUNNEL_CONF_PATH)
    log.info("stunnel pid=%d", stunnel_proc.pid)

    # 5b. Write pgbouncer.ini and userlist.txt; chmod 0600 (contain token + loopback password).
    #     PgBouncer's server points at the LOCAL stunnel, not Lakebase directly.
    ini_content = render_ini(
        listen_port=cfg.pgbouncer_port,
        db_name=cfg.database_name,
        server_host="127.0.0.1",
        server_port=STUNNEL_PORT,
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
    # Serialises ini writes (refresher) against relaunch re-render / teardown.
    ini_lock = threading.Lock()

    def _write_server_cred(tok: str) -> None:
        write_server_cred(tok, PGBOUNCER_INI_PATH, ini_lock)

    def _reload() -> None:
        # Admin console auth: reload must authenticate AS the admin identity,
        # which is client_user/client_pw (admin_users in the ini + userlist.txt
        # entry both use client_user). PGPASSWORD is set inside pgb_reload.
        pgb_reload(PSQL_BINARY, cfg.pgbouncer_port, cfg.db_user, client_pw,
                   cfg.database_name)

    refresher_thread = threading.Thread(
        target=run_loop,
        kwargs=dict(
            state=state,
            mint=lambda: mint_token(client, cfg.endpoint_path),
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

    # 10. Signal handling: SIGTERM / SIGINT.
    #
    # The handler does the MINIMUM safe work: set stop_event. It does NOT call
    # sys.exit() and does NOT tear down children — doing so from a signal frame
    # could fire mid-write of pgbouncer.ini in the refresher thread, tearing the
    # file. The supervisor loop owns the single teardown path: it sees the flag
    # at the top of the next tick and runs terminate/wait/kill there.
    def _on_signal(signum: int, _frame) -> None:  # type: ignore[type-arg]
        log.info("Received signal %d; requesting shutdown", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # 11. Supervisor loop.
    #
    # Strategy:
    #   - Grafana is the primary process: when it exits, we shut everything down
    #     and propagate its exit code.
    #   - PgBouncer is a support process: if it exits unexpectedly we relaunch it
    #     using state.last_good_token so the re-rendered ini has a valid password.
    #     A crash-loop ceiling prevents spinning forever on a broken binary.
    #   - A single teardown path is shared by the signal, Grafana-exit, and
    #     crash-loop branches. It holds ini_lock so it never races the refresher.

    # Mutable container so the teardown closure always sees the current proc
    # even after a relaunch rebinds it.
    pgb_state = {"proc": pgbouncer_proc}
    stunnel_state = {"proc": stunnel_proc}
    relaunch_times: list[float] = []
    stunnel_relaunch_times: list[float] = []

    def _terminate_children() -> None:
        # Hold ini_lock so we don't race the refresher's write_server_cred.
        with ini_lock:
            stop_event.set()
            for name, proc in (("Grafana", grafana_proc),
                               ("PgBouncer", pgb_state["proc"]),
                               ("stunnel", stunnel_state["proc"])):
                if proc.poll() is None:
                    log.info("Terminating %s (pid=%d)", name, proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        log.warning("%s did not exit cleanly; killing", name)
                        proc.kill()

    def _relaunch_pgbouncer() -> subprocess.Popen:
        # Re-render ini with the last good token (NOT the boot token, which may
        # have expired if this is a late crash). Guard the ini write with the
        # lock so it cannot tear against a concurrent refresher write.
        with ini_lock:
            tok = state.last_good_token
            new_ini = render_ini(
                listen_port=cfg.pgbouncer_port,
                db_name=cfg.database_name,
                server_host="127.0.0.1",
                server_port=STUNNEL_PORT,
                server_user=cfg.db_user,
                server_token=tok,
                client_user=cfg.db_user,
            )
            Path(PGBOUNCER_INI_PATH).write_text(new_ini)
            os.chmod(PGBOUNCER_INI_PATH, 0o600)
        return pgb_launch(PGBOUNCER_BINARY, PGBOUNCER_INI_PATH)

    log.info("Supervisor loop started; waiting on Grafana (pid=%d)", grafana_proc.pid)

    while True:
        # Shutdown requested via signal → single teardown path, then exit.
        if stop_event.is_set():
            log.info("Shutdown requested; tearing down children")
            _terminate_children()
            sys.exit(0)

        # Grafana is primary: its exit drives overall shutdown + exit code.
        rc = grafana_proc.poll()
        if rc is not None:
            log.info("Grafana exited with code %d; shutting down", rc)
            _terminate_children()
            sys.exit(rc)

        # stunnel is a support process: relaunch on unexpected exit. Its config
        # is static (no token), so relaunch just re-runs the same conf file.
        s = stunnel_state["proc"]
        s_rc = s.poll()
        if s_rc is not None:
            now = time.monotonic()
            stunnel_relaunch_times[:] = [t for t in stunnel_relaunch_times
                                         if now - t < PGB_RELAUNCH_WINDOW_S]
            if len(stunnel_relaunch_times) >= PGB_MAX_RELAUNCHES:
                log.error("stunnel crash-looped (%d relaunches within %ds); "
                          "giving up so the platform can restart the app",
                          len(stunnel_relaunch_times), PGB_RELAUNCH_WINDOW_S)
                _terminate_children()
                sys.exit(1)
            log.warning("stunnel (pid=%d) exited unexpectedly with code %d; relaunching",
                        s.pid, s_rc)
            stunnel_relaunch_times.append(now)
            new_stunnel = stunnel_launch(STUNNEL_BINARY, STUNNEL_CONF_PATH)
            time.sleep(PGB_LAUNCH_SETTLE_S)
            if new_stunnel.poll() is not None:
                log.error("stunnel relaunch (pid=%d) died immediately with code %s",
                          new_stunnel.pid, new_stunnel.poll())
            else:
                log.info("stunnel relaunched (pid=%d)", new_stunnel.pid)
            stunnel_state["proc"] = new_stunnel

        # PgBouncer is a support process: relaunch on unexpected exit.
        p = pgb_state["proc"]
        pgb_rc = p.poll()
        if pgb_rc is not None:
            now = time.monotonic()
            relaunch_times[:] = [t for t in relaunch_times if now - t < PGB_RELAUNCH_WINDOW_S]
            if len(relaunch_times) >= PGB_MAX_RELAUNCHES:
                log.error("PgBouncer crash-looped (%d relaunches within %ds); "
                          "giving up so the platform can restart the app",
                          len(relaunch_times), PGB_RELAUNCH_WINDOW_S)
                _terminate_children()
                sys.exit(1)

            log.warning("PgBouncer (pid=%d) exited unexpectedly with code %d; relaunching",
                        p.pid, pgb_rc)
            relaunch_times.append(now)
            new_proc = _relaunch_pgbouncer()

            # Detect a binary that died instantly — don't claim success on a corpse.
            time.sleep(PGB_LAUNCH_SETTLE_S)
            if new_proc.poll() is not None:
                log.error("PgBouncer relaunch (pid=%d) died immediately with code %s",
                          new_proc.pid, new_proc.poll())
            else:
                log.info("PgBouncer relaunched (pid=%d)", new_proc.pid)
            pgb_state["proc"] = new_proc

        time.sleep(1)


if __name__ == "__main__":
    main()
