from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

REQUIRED = ["DATABRICKS_APP_PORT", "LAKEBASE_ENDPOINT_PATH",
            "LAKEBASE_DATABASE_NAME", "LAKEBASE_DB_USER", "GRAFANA_ROOT_URL"]


@dataclass(frozen=True)
class AppConfig:
    app_port: int
    endpoint_path: str
    database_name: str
    db_user: str
    root_url: str
    host: Optional[str] = None
    pgbouncer_port: int = 6432
    refresh_interval_s: int = 3000


def load_config(env: Mapping[str, str]) -> AppConfig:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")
    return AppConfig(
        app_port=int(env["DATABRICKS_APP_PORT"]),
        endpoint_path=env["LAKEBASE_ENDPOINT_PATH"],
        database_name=env["LAKEBASE_DATABASE_NAME"],
        db_user=env["LAKEBASE_DB_USER"],
        root_url=env["GRAFANA_ROOT_URL"],
        host=env.get("LAKEBASE_HOST") or None,
        pgbouncer_port=int(env.get("PGBOUNCER_PORT", "6432")),
        refresh_interval_s=int(env.get("TOKEN_REFRESH_INTERVAL_S", "3000")),
    )
