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
