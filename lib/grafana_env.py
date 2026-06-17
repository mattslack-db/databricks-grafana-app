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


# Default org role granted to users auto-created from the SSO proxy identity.
# Editor lets them view AND build dashboards, but NOT manage server settings,
# users, or datasources (datasources are file-provisioned, so no UI admin is
# needed for normal use). Access to the app is already gated by the Databricks
# Apps SSO proxy, so only authorized workspace users ever reach Grafana.
# To make someone an Admin: grafana-cli admin or promote them in the users DB.
PROXY_DEFAULT_ORG_ROLE = "Editor"


def _auth_env(*, auth_proxy: bool) -> dict:
    """Auth-related GF_* vars.

    auth_proxy=True (deployed Databricks App): trust the SSO proxy's forwarded
    identity headers (X-Forwarded-Email / X-Forwarded-Preferred-Username) and
    auto-provision users as Editor. Anonymous access is disabled.

    auth_proxy=False (local docker harness): there is no SSO proxy to set the
    headers, so fall back to anonymous Admin for easy local testing.
    """
    if not auth_proxy:
        return {
            "GF_AUTH_ANONYMOUS_ENABLED": "true",
            "GF_AUTH_ANONYMOUS_ORG_ROLE": "Admin",
            "GF_AUTH_DISABLE_LOGIN_FORM": "true",
        }
    return {
        # Identify the user by the email the Databricks SSO proxy forwards.
        "GF_AUTH_PROXY_ENABLED": "true",
        "GF_AUTH_PROXY_HEADER_NAME": "X-Forwarded-Email",
        "GF_AUTH_PROXY_HEADER_PROPERTY": "email",
        "GF_AUTH_PROXY_AUTO_SIGN_UP": "true",
        # Populate the Grafana display name + login from the forwarded headers.
        "GF_AUTH_PROXY_HEADERS": "Name:X-Forwarded-Preferred-Username Login:X-Forwarded-Email",
        # Re-sync identity at most once a minute (cheap; headers are stable).
        "GF_AUTH_PROXY_SYNC_TTL": "60",
        "GF_AUTH_PROXY_ENABLE_LOGIN_TOKEN": "false",
        # No anonymous access and no login form — identity comes only from the
        # trusted SSO proxy in front of the app.
        "GF_AUTH_ANONYMOUS_ENABLED": "false",
        "GF_AUTH_DISABLE_LOGIN_FORM": "true",
        # Auto-provisioned proxy users land in the default org as Editor.
        "GF_USERS_AUTO_ASSIGN_ORG": "true",
        "GF_USERS_AUTO_ASSIGN_ORG_ROLE": PROXY_DEFAULT_ORG_ROLE,
    }


def build_env(*, app_port: int, pgbouncer_port: int, db_name: str,
              client_user: str, client_password: str, root_url: str,
              provisioning_dir: str, auth_proxy: bool = False) -> dict:
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
        "GF_PATHS_PROVISIONING": provisioning_dir,
        **_auth_env(auth_proxy=auth_proxy),
    }

# Stable datasource UID so provisioned dashboards can reference Lakebase by a
# fixed id (instead of a name that could change). Dashboards use this UID.
LAKEBASE_DATASOURCE_UID = "lakebase"


def render_lakebase_datasource(*, pgbouncer_port: int, db_name: str,
                               client_user: str, client_password: str) -> str:
    return f"""apiVersion: 1
datasources:
  - name: Lakebase
    uid: {LAKEBASE_DATASOURCE_UID}
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


def render_dashboard_provider(*, json_dir: str) -> str:
    """Grafana dashboard provisioning provider config.

    Points Grafana at a directory of dashboard JSON files. json_dir must be an
    ABSOLUTE path (it differs between the local harness and the deployed app's
    source dir), so startup.py computes it and writes this file at boot. The
    dashboard JSON files themselves are static and shipped in the app source.
    """
    return f"""apiVersion: 1
providers:
  - name: lakebase-dashboards
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    editable: true
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: {json_dir}
      foldersFromFilesStructure: false
"""
