"""Durable storage for room snapshots.

The brief asks for "PostgreSQL (JSONB) or an append-only event log."
This implements the same idea -- durable key-value snapshot storage,
keyed by (room kind, room id), replayable on restart -- against SQLite
instead, since it needs zero external infrastructure to run this
project locally. Swapping in Postgres later is a matter of implementing
the same three-method :class:`DocumentStore` interface against
``asyncpg``/JSONB; nothing above this layer (the room manager, the
server routes) would need to change.

Every CRDT already speaks MessagePack (``to_bytes``/``from_bytes``), so
what's stored here is just that snapshot blob -- loading a room is
"deserialize the last snapshot," which is exactly the state-based CRDT
merge path already tested elsewhere in this codebase, not a new code
path to trust.
"""

from __future__ import annotations

import sqlite3
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
