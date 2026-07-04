"""Optional Redis pub/sub fan-out for `Room.broadcast()` across server
*processes* -- the other half of Phase 7's horizontal scaling seam
alongside `crdt_cad.persistence.store.PostgresStore`. A single process
already fans an op out to every locally-connected client; this adds the
missing piece for more than one process (e.g. several k8s replicas
behind one Service) serving the *same* room: ops applied on one process
get published to `room:{kind}:{room_id}`, and every other process
subscribed to that channel relays them to its own local clients.

Selected via `CRDT_CAD_REDIS_URL` (see `app.py`); unset, `create_redis_client()`
returns `None` and `Room.broadcast` never touches Redis at all -- exactly
today's single-process-only behavior, unchanged.

`redis-py` is intentionally *not* a core dependency (see `pyproject.toml`'s
`redis` extra) for the same reason `asyncpg` isn't: the zero-config local
demo has no reason to pull in a Redis client it will never use.
"""

from __future__ import annotations

import os
import uuid

# Identifies *this* server process in every message this process
# publishes, so its own Redis relay loop (subscribed to the same
# channel it just published to) can recognize and skip messages it
# already delivered directly to its local clients -- without this, every
# locally-originated op would be delivered to local clients twice.
PROCESS_ID = uuid.uuid4().hex


def redis_url() -> str | None:
    return os.environ.get("CRDT_CAD_REDIS_URL")


def create_redis_client():
    """Returns a `redis.asyncio.Redis` client if `CRDT_CAD_REDIS_URL` is
    set, else `None`. Raises a clear `ImportError` if the URL is set but
    `redis-py` isn't installed, rather than failing later with a
    confusing `NameError`/`ModuleNotFoundError` deep in a request path.
    """
    url = redis_url()
    if not url:
        return None
    try:
        import redis.asyncio as redis
    except ImportError as exc:
        raise ImportError(
            "CRDT_CAD_REDIS_URL is set but redis-py isn't installed -- "
            "install with `pip install crdt-cad[redis]` (or `pip install redis`), "
            "or unset CRDT_CAD_REDIS_URL to run single-process."
        ) from exc
    return redis.from_url(url)
