from lib.lakebase import LakebaseEndpoint, resolve_endpoint, mint_token

class FakeCred:  # shape of generate_database_credential result
    def __init__(self, token): self.token = token

class FakeInstance:
    read_write_dns = "grafana-pg.db.example.com"
    pg_port = 5432

class FakeDatabaseAPI:
    def __init__(self): self.calls = []
    def get_database_instance(self, name): self.calls.append(("get", name)); return FakeInstance()
    def generate_database_credential(self, request_id, instance_names):
        self.calls.append(("gen", instance_names)); return FakeCred("tok-123")

class FakeClient:
    def __init__(self): self.database = FakeDatabaseAPI()

def test_resolve_endpoint_reads_dns_and_port():
    ep = resolve_endpoint(FakeClient(), "grafana-pg")
    assert ep == LakebaseEndpoint(host="grafana-pg.db.example.com", port=5432)

def test_mint_token_returns_token_string():
    assert mint_token(FakeClient(), "grafana-pg") == "tok-123"
