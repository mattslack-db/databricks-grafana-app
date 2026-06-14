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
      postgresVersion: 1700
    secureJsonData:
      password: {client_password}
"""
