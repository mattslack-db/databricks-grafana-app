import pytest
from lib.config import AppConfig, load_config

BASE_ENV = {
    "DATABRICKS_APP_PORT": "8080",
    "LAKEBASE_ENDPOINT_PATH": "projects/grafana-app/branches/production/endpoints/primary",
    "LAKEBASE_DATABASE_NAME": "grafana",
    "LAKEBASE_DB_USER": "user@example.com",
    "GRAFANA_ROOT_URL": "https://app.example.com/grafana",
}


def test_load_config_parses_required_fields():
    cfg = load_config(BASE_ENV)
    assert cfg.app_port == 8080
    assert cfg.endpoint_path == "projects/grafana-app/branches/production/endpoints/primary"
    assert cfg.database_name == "grafana"
    assert cfg.db_user == "user@example.com"
    assert cfg.root_url == "https://app.example.com/grafana"
    assert cfg.refresh_interval_s == 3000  # default ~50 min
    assert cfg.pgbouncer_port == 6432


def test_load_config_missing_endpoint_path_raises():
    env = dict(BASE_ENV)
    del env["LAKEBASE_ENDPOINT_PATH"]
    with pytest.raises(ValueError) as e:
        load_config(env)
    assert "LAKEBASE_ENDPOINT_PATH" in str(e.value)


def test_load_config_host_defaults_to_none_when_unset():
    cfg = load_config(BASE_ENV)
    assert cfg.host is None


def test_load_config_host_set_when_env_provided():
    env = {**BASE_ENV, "LAKEBASE_HOST": "ep-example-00000000.database.us-east-1.cloud.databricks.com"}
    cfg = load_config(env)
    assert cfg.host == "ep-example-00000000.database.us-east-1.cloud.databricks.com"


def test_app_config_is_frozen():
    cfg = load_config(BASE_ENV)
    with pytest.raises(Exception):
        cfg.app_port = 9090
