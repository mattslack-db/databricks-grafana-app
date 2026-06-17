from __future__ import annotations

def check_create_privilege(conn, db_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT has_database_privilege(current_user, %s, 'CREATE')", (db_name,))
        row = cur.fetchone()
    if row is None:
        # No row means the database isn't visible to current_user — surface a
        # clear permission error instead of an opaque TypeError on None[0].
        raise PermissionError(
            f"Database '{db_name}' not found or not accessible to the service principal.")
    if not row[0]:
        raise PermissionError(
            f"Service principal lacks CREATE on database '{db_name}'. "
            f"Grafana cannot run its first-boot schema migration. "
            f"Fix: connect as an owner and run: GRANT CREATE ON DATABASE {db_name} TO <sp_role>;")
