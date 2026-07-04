FROM python:3.12-slim AS base

WORKDIR /app

# System deps: none beyond build-essential for numba/llvmlite's compiled
# wheels to install cleanly on slim images that lack a prebuilt wheel match.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY demo ./demo

RUN pip install --no-cache-dir .

# Persisted SQLite snapshots live here; mount a volume at this path to
# survive container recreation (see docker-compose.yml).
ENV CRDT_CAD_DB_PATH=/data/crdt_cad.db
# `pip install .` (not editable) copies the package into site-packages, so
# the demo static assets must be located explicitly -- see the comment on
# DEMO_STATIC_DIR in crdt_cad/server/app.py.
ENV CRDT_CAD_STATIC_DIR=/app/demo/static
RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "crdt_cad.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
