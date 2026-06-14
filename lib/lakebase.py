from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class LakebaseEndpoint:
    host: str
    port: int


def resolve_endpoint(client, endpoint_path: str) -> LakebaseEndpoint:
    ep = client.postgres.get_endpoint(endpoint_path)
    return LakebaseEndpoint(host=ep.status.hosts.host, port=5432)


def mint_token(client, endpoint_path: str) -> str:
    return client.postgres.generate_database_credential(endpoint_path).token
