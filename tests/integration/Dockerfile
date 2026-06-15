# Local boot harness for Task 10 — runs the linux/amd64 stack on a Mac via Docker.
# NOT a deployment artifact; the real app runs on Databricks Apps' own runtime.
FROM --platform=linux/amd64 python:3.11-slim-bookworm

# Runtime deps:
# - postgresql-client: `psql` for the PgBouncer admin console (RELOAD/RECONNECT)
# - binutils, curl, ca-certificates, xz-utils: for scripts/fetch_binaries.sh
# - libevent-2.1-7, libc-ares2, libssl3: PgBouncer .deb runtime libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client binutils curl ca-certificates xz-utils \
        libevent-2.1-7 libc-ares2 libssl3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NOTE: Python deps + binaries are installed at RUN time by boot_local.sh, because
# colima's docker-BUILD network can't fetch them (the internal pip proxy 404s on
# PEP 658 .metadata at build time, and pypi.org is null-routed). Run-time network
# is reliable. Deps install once; binaries persist on a named volume at /app/bin.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Repo is bind-mounted at /app at run time; binaries go to a named volume at
# /app/bin (fast container fs, persisted across runs). Entry runs the harness.
CMD ["bash", "tests/integration/boot_local.sh"]
