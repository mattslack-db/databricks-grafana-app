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
# MAGIC - `requests_per_min` — per-service base traffic (search ~180, checkout ~90, catalog ~130 req/min) scaled by a sinusoidal daily pattern (×0.2–1.0) + noise
# MAGIC - `p95_latency_ms`  — ~60–95 ms, rising slightly with traffic (busier = slightly slower)

# COMMAND ----------

# MAGIC %pip install -q "databricks-sdk>=0.72" "psycopg[binary]>=3.2"

# COMMAND ----------

# MAGIC %restart_python

# COMMAND ----------

# Config — adjust if you've changed the Lakebase project or database name
ENDPOINT_PATH = "projects/grafana-app/branches/production/endpoints/primary"
DATABASE      = "grafana"

# How long to run and how often to insert a new data point
INTERVAL_S    = 5    # seconds between inserts
RUN_FOR_S     = 3600 # total run time in seconds; set to None to run until manually stopped

# COMMAND ----------

import math, random, time
from datetime import datetime, timezone

import psycopg
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
USER = w.current_user.me().user_name

# Resolve the host for the EXACT endpoint the credential is minted for.
# list_endpoints()[0] can return a different endpoint (e.g. a read replica) whose
# host won't match the freshly minted credential and may reject writes, so always
# resolve the specific ENDPOINT_PATH.
HOST = w.postgres.get_endpoint(ENDPOINT_PATH).status.hosts.host
print(f"Connecting to {HOST} / {DATABASE}")

# Lakebase OAuth credentials are short-lived (~1 h), so mint a fresh token on every
# (re)connect rather than reusing one token for the whole run.
RECONNECT_MAX_ATTEMPTS = 5
RECONNECT_BACKOFF_S    = 2

def connect() -> psycopg.Connection:
    """Open an autocommit connection with a freshly minted OAuth token.

    Retries with backoff so a scale-to-zero wakeup or a brief blip doesn't end the
    run; a new token is minted on every attempt because credentials expire in ~1 h.
    """
    last_exc: psycopg.Error | None = None
    for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
        try:
            token = w.postgres.generate_database_credential(ENDPOINT_PATH).token
            conn = psycopg.connect(
                host=HOST, port=5432, dbname=DATABASE,
                user=USER, password=token, sslmode="require",
            )
            conn.autocommit = True
            return conn
        except psycopg.Error as exc:
            last_exc = exc
            if attempt < RECONNECT_MAX_ATTEMPTS:
                wait = RECONNECT_BACKOFF_S * attempt
                print(f"  connect attempt {attempt} failed ({exc!r}); retrying in {wait}s …")
                time.sleep(wait)
    raise RuntimeError(f"could not connect after {RECONNECT_MAX_ATTEMPTS} attempts") from last_exc

# COMMAND ----------

SERVICES = ["search", "checkout", "catalog"]

# Base traffic levels per service (req/min)
BASE_RPM = {"search": 180, "checkout": 90, "catalog": 130}

def _hour_factor(ts: datetime) -> float:
    """Sinusoidal factor in [0.2, 1.0]: peaks at ~08:00 UTC, troughs at ~20:00 UTC."""
    h = ts.hour + ts.minute / 60
    return 0.6 + 0.4 * math.sin(math.pi * (h - 2) / 12)

def generate_batch(window_start: datetime, window_end: datetime, service: str):
    """Generate up to 200 sample rows for a service, spread across the time window."""
    n = random.randint(1, 200)
    rows = []
    window_s = (window_end - window_start).total_seconds()
    for _ in range(n):
        # Spread timestamps randomly across the window
        offset = random.uniform(0, window_s)
        ts = window_start.replace(microsecond=0) if window_s == 0 else \
             datetime.fromtimestamp(window_start.timestamp() + offset, tz=timezone.utc)

        factor = _hour_factor(ts)
        rpm    = max(10, BASE_RPM[service] * factor * random.gauss(1.0, 0.08))
        lat    = max(20, 60 + (rpm / BASE_RPM[service]) * 30 + random.gauss(0, 8))

        rows.append((ts, service, "requests_per_min", round(rpm, 2)))
        rows.append((ts, service, "p95_latency_ms",   round(lat, 2)))
    return rows

# COMMAND ----------

INSERT_SQL = "INSERT INTO demo.app_metrics(ts, service, metric, value) VALUES (%s, %s, %s, %s)"

deadline = (time.monotonic() + RUN_FOR_S) if RUN_FOR_S else None
iteration = 0
prev_tick = datetime.now(timezone.utc)

conn = connect()
print("Connected. Inserting rows every", INTERVAL_S, "s …")
try:
    while True:
        time.sleep(INTERVAL_S)
        now  = datetime.now(timezone.utc)
        rows = []
        for svc in SERVICES:
            rows.extend(generate_batch(prev_tick, now, svc))

        try:
            with conn.cursor() as cur:
                cur.executemany(INSERT_SQL, rows)
        except psycopg.Error as exc:
            # Don't let a transient failure (dropped connection, expired token, brief
            # endpoint unavailability) kill a long-running generator: log, reconnect
            # with a fresh token, and continue. prev_tick is left unchanged so the next
            # successful tick backfills this window's rows.
            print(f"[{now:%H:%M:%S}] insert failed ({exc!r}); reconnecting …")
            try:
                conn.close()
            except psycopg.Error:
                pass
            conn = connect()
            continue

        prev_tick = now
        iteration += 1
        print(f"[{now:%H:%M:%S}] inserted {len(rows)} rows across {len(SERVICES)} services (iteration {iteration})")

        if deadline and time.monotonic() >= deadline:
            print("Run complete.")
            break
finally:
    conn.close()
