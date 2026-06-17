# Databricks notebook source

# MAGIC %md
# MAGIC # Lakebase Demo Data Generator
# MAGIC
# MAGIC Streams synthetic app-metrics rows into `demo.app_metrics` in the Grafana Lakebase database.
# MAGIC The Grafana dashboard at https://grafana-app-000000000000.aws.databricksapps.com
# MAGIC refreshes every 30 s and shows the last 24 h — run this cell and watch the graphs move.
# MAGIC
# MAGIC **Schema:** `demo.app_metrics(ts TIMESTAMPTZ, service TEXT, metric TEXT, value FLOAT8)`
# MAGIC
# MAGIC **Metrics generated:**
# MAGIC - `requests_per_min` — 50–250 req/min per service, with a sinusoidal daily pattern + noise
# MAGIC - `p95_latency_ms`  — 40–160 ms, inversely correlated with traffic (busier = slightly slower)

# COMMAND ----------

# Config — adjust if you've changed the Lakebase project or database name
ENDPOINT_PATH = "projects/grafana-app/branches/production/endpoints/primary"
DATABASE      = "grafana"

# How long to run and how often to insert a new data point
INTERVAL_S    = 30   # seconds between inserts (matches Grafana refresh)
RUN_FOR_S     = 3600 # total run time in seconds; set to None to run until manually stopped

# COMMAND ----------

import math, random, time
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

# Mint a fresh OAuth token for Lakebase
cred = w.postgres.generate_database_credential(endpoint_path=ENDPOINT_PATH)
token = cred.token

# Resolve the endpoint host
endpoints = list(w.postgres.list_endpoints(
    parent=f"projects/grafana-app/branches/production"
))
host = endpoints[0].status.hosts.host

print(f"Connecting to {host} / {DATABASE}")

# COMMAND ----------

import psycopg

SERVICES = ["search", "checkout", "catalog"]

# Base traffic levels per service (req/min)
BASE_RPM = {"search": 180, "checkout": 90, "catalog": 130}

def _hour_factor(ts: datetime) -> float:
    """Sinusoidal factor: peaks at ~14:00 UTC, troughs at ~02:00 UTC."""
    h = ts.hour + ts.minute / 60
    return 0.6 + 0.4 * math.sin(math.pi * (h - 2) / 12)

def generate_row(ts: datetime, service: str):
    factor = _hour_factor(ts)
    noise  = random.gauss(1.0, 0.08)

    rpm    = max(10, BASE_RPM[service] * factor * noise)
    # latency rises slightly under load
    lat    = max(20, 60 + (rpm / BASE_RPM[service]) * 30 + random.gauss(0, 8))

    return [
        (ts, service, "requests_per_min", round(rpm, 2)),
        (ts, service, "p95_latency_ms",   round(lat, 2)),
    ]

# COMMAND ----------

deadline = (time.monotonic() + RUN_FOR_S) if RUN_FOR_S else None
iteration = 0

with psycopg.connect(
    host=host, port=5432, dbname=DATABASE,
    user=w.current_user.me().user_name,
    password=token,
    sslmode="require",
) as conn:
    conn.autocommit = True
    print("Connected. Inserting rows every", INTERVAL_S, "s …")

    while True:
        now = datetime.now(timezone.utc)
        rows = []
        for svc in SERVICES:
            rows.extend(generate_row(now, svc))

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO demo.app_metrics(ts, service, metric, value) VALUES (%s, %s, %s, %s)",
                rows,
            )

        iteration += 1
        print(f"[{now:%H:%M:%S}] inserted {len(rows)} rows (iteration {iteration})")

        if deadline and time.monotonic() >= deadline:
            print("Run complete.")
            break

        time.sleep(INTERVAL_S)
