FROM python:3.12-slim AS builder

WORKDIR /app

# System deps: build-essential is needed here (compiled wheels for
# numba/llvmlite on slim images without a prebuilt match) but must never
# reach the final image -- it's a real attack-surface/size cost with no
# runtime purpose once the wheels are built.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

# Installed into an isolated prefix (not system site-packages) so the
# final stage can copy over exactly this directory tree and nothing else
# the builder's apt packages left behind. The cache mount (not
# --no-cache-dir) persists pip's downloaded wheels across separate
# `docker build` invocations even though this whole stage is discarded
# after copying /install out -- a dependency-only change re-downloads
# nothing that was already fetched, only what actually changed.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout=180 --retries=5 --prefix=/install ".[postgres,redis]"


FROM python:3.12-slim AS final

WORKDIR /app

# Runs as an unprivileged user -- the base image's build-essential-free
# final stage plus this USER directive means a compromised app process
# has no compiler and no root, unlike the previous single-stage image.
RUN groupadd --system app && useradd --system --gid app --home /app app

COPY --from=builder /install /usr/local
COPY demo ./demo

ENV CRDT_CAD_DB_PATH=/data/crdt_cad.db
# `pip install .` (not editable) copies the package into site-packages, so
# the demo static assets must be located explicitly -- see the comment on
# DEMO_STATIC_DIR in crdt_cad/server/app.py.
ENV CRDT_CAD_STATIC_DIR=/app/demo/static

RUN mkdir -p /data && chown -R app:app /data /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "crdt_cad.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
