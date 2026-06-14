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
    assert "postgresVersion: 1700" in y
