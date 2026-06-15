from lib.pgbouncer import render_ini, render_userlist, scram_or_plain

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
