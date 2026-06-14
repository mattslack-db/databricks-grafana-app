from __future__ import annotations
import uuid
from dataclasses import dataclass

@dataclass(frozen=True)
class LakebaseEndpoint:
    host: str
    port: int

def resolve_endpoint(client, instance_name: str) -> LakebaseEndpoint:
    inst = client.database.get_database_instance(name=instance_name)
    return LakebaseEndpoint(host=inst.read_write_dns, port=int(getattr(inst, "pg_port", 5432)))

def mint_token(client, instance_name: str) -> str:
    cred = client.database.generate_database_credential(
        request_id=str(uuid.uuid4()), instance_names=[instance_name])
    return cred.token
