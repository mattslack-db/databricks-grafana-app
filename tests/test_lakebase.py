from lib.lakebase import LakebaseEndpoint, resolve_endpoint, mint_token


class FakeCred:  # shape of generate_database_credential result
    def __init__(self, token):
        self.token = token


class FakeHosts:
    host = "ep-example-00000000.database.us-east-1.cloud.databricks.com"


class FakeEndpointStatus:
    hosts = FakeHosts()


class FakeEndpoint:
    status = FakeEndpointStatus()


class FakePostgresAPI:
    def __init__(self):
        self.calls = []

    def get_endpoint(self, name):
        self.calls.append(("get_endpoint", name))
        return FakeEndpoint()

    def generate_database_credential(self, endpoint):
        self.calls.append(("generate_database_credential", endpoint))
        return FakeCred("tok-123")


class FakeClient:
    def __init__(self):
        self.postgres = FakePostgresAPI()


def test_resolve_endpoint_reads_status_hosts_host_and_port_5432():
    client = FakeClient()
    ep = resolve_endpoint(client, "projects/grafana-app/branches/production/endpoints/primary")
    assert ep == LakebaseEndpoint(
        host="ep-example-00000000.database.us-east-1.cloud.databricks.com",
        port=5432,
    )
    assert client.postgres.calls[0] == (
        "get_endpoint",
        "projects/grafana-app/branches/production/endpoints/primary",
    )


def test_mint_token_returns_token_string():
    client = FakeClient()
    token = mint_token(client, "projects/grafana-app/branches/production/endpoints/primary")
    assert token == "tok-123"
    assert client.postgres.calls[0] == (
        "generate_database_credential",
        "projects/grafana-app/branches/production/endpoints/primary",
    )
