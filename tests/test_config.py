import pytest
from lib.config import AppConfig, load_config

BASE_ENV = {
    "DATABRICKS_APP_PORT": "8080",
    "LAKEBASE_INSTANCE_NAME": "grafana-pg",
    "LAKEBASE_DATABASE_NAME": "grafana",
    "LAKEBASE_DB_USER": "grafana_sp",
    "GRAFANA_ROOT_URL": "https://app.example.com/grafana",
}

def test_load_config_parses_required_fields():
    cfg = load_config(BASE_ENV)
    assert cfg.app_port == 8080
    assert cfg.instance_name == "grafana-pg"
    assert cfg.database_name == "grafana"
    assert cfg.db_user == "grafana_sp"
    assert cfg.root_url == "https://app.example.com/grafana"
    assert cfg.refresh_interval_s == 3000  # default ~50 min
    assert cfg.pgbouncer_port == 6432

def test_load_config_missing_required_raises():
    env = dict(BASE_ENV); del env["LAKEBASE_INSTANCE_NAME"]
    with pytest.raises(ValueError) as e:
        load_config(env)
    assert "LAKEBASE_INSTANCE_NAME" in str(e.value)

def test_app_config_is_frozen():
    cfg = load_config(BASE_ENV)
    with pytest.raises(Exception):
        cfg.app_port = 9090
