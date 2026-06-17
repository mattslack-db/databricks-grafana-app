from unittest.mock import patch

import pytest

from lib.pgbouncer import reload, render_ini, render_userlist

def test_render_userlist_quotes_user_and_password():
    out = render_userlist("grafana_local", "secretpw")
    assert out.strip() == '"grafana_local" "secretpw"'

def test_render_ini_targets_local_stunnel_with_tls_disabled():
    # PgBouncer connects in plaintext to the local stunnel shim (which adds
    # TLS+SNI to Lakebase), so server_tls_sslmode must be disable and the db
    # connstring must NOT carry sslmode= (PgBouncer rejects that key).
    ini = render_ini(
        listen_port=6432, db_name="grafana",
        server_host="127.0.0.1", server_port=5433,
        server_user="grafana_sp", server_token="tok-123",
        client_user="grafana_local", server_lifetime=1800)
    assert "listen_addr = 127.0.0.1" in ini
    assert "listen_port = 6432" in ini
    assert "host=127.0.0.1 port=5433" in ini
    assert "server_tls_sslmode = disable" in ini
    assert "sslmode=" not in ini.split("[pgbouncer]")[0]  # not in [databases] line
    assert "ignore_startup_parameters = extra_float_digits" in ini
    assert "user=grafana_sp" in ini
    assert "password=tok-123" in ini
    assert "server_lifetime = 1800" in ini
    assert "auth_type" in ini


def test_admin_user_matches_userlist_entry_for_reload_auth():
    # The reload() admin console auth must use an identity that is BOTH listed
    # in admin_users (pgbouncer.ini) AND present in userlist.txt. Both are keyed
    # on client_user, so they must agree for RELOAD/RECONNECT to authenticate.
    client_user = "grafana_local"
    client_pw = "loopback-pw"
    ini = render_ini(
        listen_port=6432, db_name="grafana",
        server_host="h.example.com", server_port=5432,
        server_user="grafana_sp", server_token="tok-123",
        client_user=client_user)
    userlist = render_userlist(client_user, client_pw)
    assert f"admin_users = {client_user}" in ini
    assert f'"{client_user}"' in userlist


class _FakeCompleted:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_reload_issues_reload_and_reconnect_as_separate_queries():
    # The PgBouncer admin console rejects a multi-statement simple query, so
    # RELOAD and RECONNECT must be sent as SEPARATE -c flags (two queries).
    # Combining them ("RELOAD; RECONNECT x;") made psql exit 1 and silently
    # broke token rotation in production.
    with patch("lib.pgbouncer.subprocess.run", return_value=_FakeCompleted()) as run:
        reload("bin/psql", 6432, "admin", "pw", "grafana")
    args = run.call_args
    argv = args.args[0]
    # Two -c flags, RELOAD; first then RECONNECT grafana;
    assert argv.count("-c") == 2
    ci = [i for i, a in enumerate(argv) if a == "-c"]
    assert argv[ci[0] + 1] == "RELOAD;"
    assert argv[ci[1] + 1] == "RECONNECT grafana;"
    # Password passed via env, never argv.
    assert "pw" not in argv
    assert args.kwargs["env"]["PGPASSWORD"] == "pw"
    assert args.kwargs["capture_output"] is True


def test_reload_raises_with_stderr_on_failure():
    # A failed reload must be loud (not swallowed) — otherwise the server token
    # silently goes stale and every connection fails after the ~1h token TTL.
    failed = _FakeCompleted(returncode=1, stderr="permission denied for RELOAD")
    with patch("lib.pgbouncer.subprocess.run", return_value=failed):
        with pytest.raises(RuntimeError, match="permission denied for RELOAD"):
            reload("bin/psql", 6432, "admin", "pw", "grafana")
