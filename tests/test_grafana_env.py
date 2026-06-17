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
    # Default (no auth_proxy) = local anonymous-Admin fallback.
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "true"
    assert env["GF_AUTH_ANONYMOUS_ORG_ROLE"] == "Admin"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    assert env["GF_PATHS_PROVISIONING"] == "/app/provisioning"


def test_build_env_auth_proxy_mode_trusts_sso_headers():
    # auth_proxy=True (deployed app): identity comes from the Databricks SSO
    # proxy headers; anonymous is OFF; users auto-provision as Editor.
    env = build_env(app_port=8080, pgbouncer_port=6432, db_name="grafana",
                    client_user="sp", client_password="pw",
                    root_url="https://grafana-app-123.aws.databricksapps.com",
                    provisioning_dir="/app/provisioning", auth_proxy=True)
    assert env["GF_AUTH_PROXY_ENABLED"] == "true"
    assert env["GF_AUTH_PROXY_HEADER_NAME"] == "X-Forwarded-Email"
    assert env["GF_AUTH_PROXY_HEADER_PROPERTY"] == "email"
    assert env["GF_AUTH_PROXY_AUTO_SIGN_UP"] == "true"
    assert env["GF_USERS_AUTO_ASSIGN_ORG_ROLE"] == "Editor"
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert env["GF_AUTH_DISABLE_LOGIN_FORM"] == "true"
    # Anonymous org-role must NOT be set in proxy mode.
    assert "GF_AUTH_ANONYMOUS_ORG_ROLE" not in env


def test_build_env_serve_from_sub_path_false_for_host_root():
    # A deployed Databricks App owns its whole hostname (no sub-path), so
    # serve_from_sub_path must be false. With it true, Grafana would strip a
    # non-existent path prefix and break routing.
    env = build_env(app_port=8080, pgbouncer_port=6432, db_name="grafana",
                    client_user="sp", client_password="pw",
                    root_url="https://grafana-app-123.aws.databricksapps.com",
                    provisioning_dir="/app/provisioning")
    assert env["GF_SERVER_SERVE_FROM_SUB_PATH"] == "false"


def test_build_env_serve_from_sub_path_false_for_trailing_slash_only():
    # A bare host with a trailing slash is still "root" — no sub-path.
    env = build_env(app_port=8080, pgbouncer_port=6432, db_name="grafana",
                    client_user="sp", client_password="pw",
                    root_url="https://grafana-app-123.aws.databricksapps.com/",
                    provisioning_dir="/app/provisioning")
    assert env["GF_SERVER_SERVE_FROM_SUB_PATH"] == "false"

def test_render_lakebase_datasource_targets_pgbouncer():
    y = render_lakebase_datasource(pgbouncer_port=6432, db_name="grafana",
                                   client_user="grafana_local", client_password="pw")
    assert "type: postgres" in y
    assert "url: 127.0.0.1:6432" in y
    assert "sslmode" in y and "disable" in y
    assert "postgresVersion: 1700" in y
    # Stable uid so dashboards can reference the datasource reliably.
    assert "uid: lakebase" in y


def test_render_dashboard_provider_points_at_json_dir():
    from lib.grafana_env import render_dashboard_provider
    y = render_dashboard_provider(json_dir="/app/src/provisioning/dashboards/json")
    assert "type: file" in y
    assert "path: /app/src/provisioning/dashboards/json" in y
    assert "providers:" in y


def test_render_databricks_datasource_m2m():
    from lib.grafana_env import render_databricks_datasource
    y = render_databricks_datasource(
        hostname="dbc-x.cloud.databricks.com",
        http_path="sql/1.0/warehouses/abc123",
        client_id="sp-client-id", client_secret="sp-secret")
    assert "type: mullerpeter-databricks-datasource" in y
    assert "hostname: dbc-x.cloud.databricks.com" in y
    assert "path: sql/1.0/warehouses/abc123" in y
    assert "authenticationMethod: m2m" in y
    assert "clientId: sp-client-id" in y
    assert "clientSecret: sp-secret" in y
    assert "uid: databricks-sql" in y


def test_build_env_allows_unsigned_databricks_plugin():
    env = build_env(app_port=8080, pgbouncer_port=6432, db_name="grafana",
                    client_user="sp", client_password="pw",
                    root_url="https://x", provisioning_dir="/p")
    assert env["GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS"] == "mullerpeter-databricks-datasource"
