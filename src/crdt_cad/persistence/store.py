"""Durable storage for room snapshots.

The brief asks for "PostgreSQL (JSONB) or an append-only event log."
This implements the same idea -- durable key-value snapshot storage,
keyed by (room kind, room id), replayable on restart -- against SQLite
by default, since it needs zero external infrastructure to run this
project locally. :class:`PostgresStore` below implements the exact same
three-method :class:`DocumentStore` interface against a real Postgres
database (via ``asyncpg``), for the one thing SQLite genuinely can't do:
let more than one server *process* (e.g. multiple k8s replicas) share
the same room state. Nothing above this layer (the room manager, the
server routes) needs to know or care which backend is in use.

Every CRDT already speaks MessagePack (``to_bytes``/``from_bytes``), so
what's stored here is just that snapshot blob -- loading a room is
"deserialize the last snapshot," which is exactly the state-based CRDT
merge path already tested elsewhere in this codebase, not a new code
path to trust.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


class DocumentStore:
    """Interface every persistence backend implements."""

    def save(self, kind: str, room_id: str, data: bytes) -> None:
        raise NotImplementedError

    def load(self, kind: str, room_id: str) -> Optional[bytes]:
        raise NotImplementedError

    def list_rooms(self, kind: str) -> list[str]:
        raise NotImplementedError

    def delete(self, kind: str, room_id: str) -> None:
        raise NotImplementedError


class InMemoryStore(DocumentStore):
    """Non-durable store used in tests to avoid touching disk."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    def save(self, kind: str, room_id: str, data: bytes) -> None:
        self._data[(kind, room_id)] = data

    def load(self, kind: str, room_id: str) -> Optional[bytes]:
        return self._data.get((kind, room_id))

    def list_rooms(self, kind: str) -> list[str]:
        return [rid for (k, rid) in self._data if k == kind]

    def delete(self, kind: str, room_id: str) -> None:
        self._data.pop((kind, room_id), None)


class SQLiteStore(DocumentStore):
    """File-backed durable store. One row per (kind, room_id), holding the
    latest full snapshot -- simple last-write-wins at the storage layer,
    which is fine because the *document* is already a CRDT: replaying an
    older snapshot and re-merging incoming ops still converges.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    kind TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    data BLOB NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (kind, room_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, kind: str, room_id: str, data: bytes) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (kind, room_id, data, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, room_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                (kind, room_id, data, time.time()),
            )

    def load(self, kind: str, room_id: str) -> Optional[bytes]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM documents WHERE kind = ? AND room_id = ?", (kind, room_id)
            ).fetchone()
            return row[0] if row else None

    def list_rooms(self, kind: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT room_id FROM documents WHERE kind = ? ORDER BY updated_at DESC", (kind,)
            ).fetchall()
            return [r[0] for r in rows]

    def delete(self, kind: str, room_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE kind = ? AND room_id = ?", (kind, room_id))


class PostgresStore(DocumentStore):
    """Postgres-backed store for real horizontal scaling -- multiple
    server *processes* (e.g. several k8s replicas) sharing the same room
    state, which a per-process SQLite file can never do. Selected via
    ``CRDT_CAD_DATABASE_URL`` (see ``app.py``); SQLite remains the
    zero-config default.

    Implemented with ``asyncpg`` (as the brief asks for), which is
    async-only, bridged behind this same *synchronous* three-method
    ``DocumentStore`` interface every other backend implements, so
    nothing above this layer -- ``Room``, ``RoomManager``, the REST
    routes -- needs to change or know which backend is active. The
    bridge is a dedicated background thread running its own event loop
    (started once, in ``__init__``); every call is handed to that loop
    via ``asyncio.run_coroutine_threadsafe(...).result()``, which blocks
    the *calling* thread until the query completes. That's the same
    trade-off ``SQLiteStore`` already makes -- a blocking call inline
    during room hydration and every persist -- just against a network
    round-trip instead of a local disk read, and persistence is already
    routed through ``asyncio.to_thread`` at the ``Room`` level for
    exactly this reason (see ``Room.persist_async``). Room *hydration*
    (``Room.__init__`` calling ``store.load`` directly, not via
    ``to_thread``) briefly blocks the event loop the same way it already
    does for ``SQLiteStore`` -- a pre-existing, accepted trade-off, not
    one introduced here.

    ``asyncpg`` is intentionally *not* a core dependency (see
    ``pyproject.toml``'s ``postgres`` extra) -- the zero-config local
    demo has no reason to pull in a Postgres driver it will never use,
    the same reasoning ``pymeshlab`` already gets in
    ``crdt_cad.ai.mesh_repair``.
    """

    def __init__(self, dsn: str) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresStore needs asyncpg -- install with `pip install crdt-cad[postgres]` "
                "(or `pip install asyncpg`), or unset CRDT_CAD_DATABASE_URL to use SQLite instead."
            ) from exc
        self._asyncpg = asyncpg
        self._dsn = dsn
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="postgres-store-loop")
        self._thread.start()
        self._pool = self._call(self._init_pool())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _init_pool(self):
        pool = await self._asyncpg.create_pool(self._dsn)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    kind TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    data BYTEA NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (kind, room_id)
                )
                """
            )
        return pool

    def save(self, kind: str, room_id: str, data: bytes) -> None:
        self._call(self._save(kind, room_id, data))

    async def _save(self, kind: str, room_id: str, data: bytes) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents (kind, room_id, data, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (kind, room_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
                """,
                kind, room_id, data, time.time(),
            )

    def load(self, kind: str, room_id: str) -> Optional[bytes]:
        return self._call(self._load(kind, room_id))

    async def _load(self, kind: str, room_id: str) -> Optional[bytes]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data FROM documents WHERE kind = $1 AND room_id = $2", kind, room_id
            )
            return bytes(row["data"]) if row else None

    def list_rooms(self, kind: str) -> list[str]:
        return self._call(self._list_rooms(kind))

    async def _list_rooms(self, kind: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT room_id FROM documents WHERE kind = $1 ORDER BY updated_at DESC", kind
            )
            return [r["room_id"] for r in rows]

    def delete(self, kind: str, room_id: str) -> None:
        self._call(self._delete(kind, room_id))

    async def _delete(self, kind: str, room_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM documents WHERE kind = $1 AND room_id = $2", kind, room_id)

    def close(self) -> None:
        """Tears down the pool and stops the background loop/thread --
        not part of the `DocumentStore` interface (no other backend holds
        resources worth releasing), but needed for clean test teardown
        and a graceful server shutdown."""
        self._call(self._pool.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
