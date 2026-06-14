from __future__ import annotations

def check_create_privilege(conn, db_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT has_database_privilege(current_user, %s, 'CREATE')", (db_name,))
        has = cur.fetchone()[0]
    if not has:
        raise PermissionError(
            f"Service principal lacks CREATE on database '{db_name}'. "
            f"Grafana cannot run its first-boot schema migration. "
            f"Fix: connect as an owner and run: GRANT CREATE ON DATABASE {db_name} TO <sp_role>;")
