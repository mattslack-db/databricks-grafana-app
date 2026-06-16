from __future__ import annotations

from urllib.parse import urlparse


def _serve_from_sub_path(root_url: str) -> str:
    """Grafana's serve_from_sub_path must be true ONLY when the root URL has a
    path component (e.g. https://host/grafana) and false when Grafana owns the
    whole host root (e.g. https://app.databricksapps.com). A deployed Databricks
    App owns its entire hostname, so it is served at root; the local docker
    harness uses a /grafana sub-path. Deriving this from root_url keeps both
    correct instead of hard-wiring one mode.
    """
    path = urlparse(root_url).path.strip("/")
    return "true" if path else "false"


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
        "GF_SERVER_SERVE_FROM_SUB_PATH": _serve_from_sub_path(root_url),
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
      postgresVersion: 1700
    secureJsonData:
      password: {client_password}
"""
