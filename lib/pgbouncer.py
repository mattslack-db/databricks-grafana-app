from __future__ import annotations
import os
import subprocess

def render_userlist(client_user: str, client_password: str) -> str:
    return f'"{client_user}" "{client_password}"\n'

def render_ini(*, listen_port: int, db_name: str, server_host: str,
               server_port: int, server_user: str, server_token: str,
               client_user: str, server_lifetime: int = 1800,
               server_idle_timeout: int = 300) -> str:
    return f"""[databases]
{db_name} = host={server_host} port={server_port} dbname={db_name} user={server_user} password={server_token} sslmode=require

[pgbouncer]
listen_addr = 127.0.0.1
listen_port = {listen_port}
auth_type = scram-sha-256
auth_file = userlist.txt
admin_users = {client_user}
pool_mode = transaction
server_lifetime = {server_lifetime}
server_idle_timeout = {server_idle_timeout}
max_client_conn = 200
default_pool_size = 20
logfile =
pidfile = pgbouncer.pid
"""

def scram_or_plain(_token: str) -> str:  # placeholder if client auth needs md5/scram tweaks
    return "scram-sha-256"

def launch(binary: str, ini_path: str) -> subprocess.Popen:
    return subprocess.Popen([binary, ini_path])

def reload(psql_binary: str, port: int, admin_user: str, admin_password: str,
           db_name: str) -> None:
    # Task 0 spike finding: RELOAD alone is INSUFFICIENT — if the server
    # password rotates before pooled server conns expire, PgBouncer enters
    # server_login_retry and rejects clients for several seconds. ALWAYS
    # follow RELOAD with RECONNECT <db> to clear that state immediately.
    #
    # The admin console requires authentication. admin_user must appear in
    # userlist.txt (with admin_password) AND in admin_users in pgbouncer.ini.
    # The password is passed via PGPASSWORD in the subprocess env — NEVER on
    # argv — so it cannot leak via `ps`.
    env = {**os.environ, "PGPASSWORD": admin_password}
    subprocess.run([psql_binary, "-h", "127.0.0.1", "-p", str(port),
                    "-U", admin_user, "-d", "pgbouncer",
                    "-c", f"RELOAD; RECONNECT {db_name};"],
                   check=True, env=env)
