"""Durable storage for user accounts and sign-in sessions (Part 6, P1).

Deliberately a *separate* store from :mod:`crdt_cad.persistence.store`
(room snapshots): accounts are optional -- the zero-config deployment
never creates this schema at all (``CRDT_CAD_AUTH_MODE`` defaults to
``tokens``, see :mod:`crdt_cad.server.auth`) -- and they have a
different lifecycle (a user outlives any one room; deleting a user must
never delete a room's CRDT history, only anonymize attribution).

Same backend split as room snapshots, for the same reasons: SQLite by
default (zero infrastructure), Postgres via ``asyncpg`` when several
server processes must share one user base (the k8s Mode B deployment),
and an in-memory implementation for tests.

What is stored, and what deliberately is not:

- ``users``: id, e-mail (unique, lowercased), display name, avatar
  color, created_at. **No passwords, ever** -- sign-in is magic links
  and OAuth only (see the brief's "never store what you can't protect").
- ``sessions``: a **hash** of the session token (never the token itself
  -- a leaked database must not mint working cookies), the user it
  belongs to, expiry, and last-seen. Sessions are server-side so
  "sign out everywhere" is a real operation, not a client-side hope.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def _user_row_to_dict(row) -> dict:
    return {
        "user_id": row[0],
        "email": row[1],
        "display_name": row[2],
        "avatar_color": row[3],
        "created_at": row[4],
    }


class AccountStore:
    """Interface every accounts backend implements. All methods are
    synchronous (callers route through ``asyncio.to_thread`` where it
    matters), mirroring :class:`crdt_cad.persistence.store.DocumentStore`."""

    # -- users ------------------------------------------------------------

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        """Returns the existing user for ``email`` or creates one. E-mail
        is the identity key (magic links and OAuth both resolve to a
        verified e-mail), normalized to lowercase."""
        raise NotImplementedError

    def get_user(self, user_id: str) -> Optional[dict]:
        raise NotImplementedError

    def get_user_by_email(self, email: str) -> Optional[dict]:
        raise NotImplementedError

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        """Updates only the fields given (None = leave unchanged).
        Returns False if the user doesn't exist."""
        raise NotImplementedError

    # -- sessions ---------------------------------------------------------

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        raise NotImplementedError

    def get_session(self, token_hash: str) -> Optional[dict]:
        """Returns ``{"user_id", "expires_at", "created_at", "last_seen"}``
        or None. Expired sessions are treated as absent (and reaped)."""
        raise NotImplementedError

    def touch_session(self, token_hash: str) -> None:
        raise NotImplementedError

    def delete_session(self, token_hash: str) -> None:
        raise NotImplementedError

    def delete_user_sessions(self, user_id: str) -> int:
        """Sign out everywhere. Returns how many sessions were removed."""
        raise NotImplementedError


class InMemoryAccountStore(AccountStore):
    """Non-durable accounts store used in tests."""

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._by_email: dict[str, str] = {}
        self._sessions: dict[str, dict] = {}

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()
        if email in self._by_email:
            return dict(self._users[self._by_email[email]])
        user = {
            "user_id": uuid.uuid4().hex,
            "email": email,
            "display_name": display_name or email.split("@")[0],
            "avatar_color": None,
            "created_at": time.time(),
        }
        self._users[user["user_id"]] = user
        self._by_email[email] = user["user_id"]
        return dict(user)

    def get_user(self, user_id: str) -> Optional[dict]:
        user = self._users.get(user_id)
        return dict(user) if user else None

    def get_user_by_email(self, email: str) -> Optional[dict]:
        user_id = self._by_email.get(email.strip().lower())
        return self.get_user(user_id) if user_id else None

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        if display_name is not None:
            user["display_name"] = display_name
        if avatar_color is not None:
            user["avatar_color"] = avatar_color
        return True

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        now = time.time()
        self._sessions[token_hash] = {
            "user_id": user_id,
            "expires_at": expires_at,
            "created_at": now,
            "last_seen": now,
        }

    def get_session(self, token_hash: str) -> Optional[dict]:
        sess = self._sessions.get(token_hash)
        if not sess:
            return None
        if sess["expires_at"] < time.time():
            del self._sessions[token_hash]
            return None
        return dict(sess)

    def touch_session(self, token_hash: str) -> None:
        if token_hash in self._sessions:
            self._sessions[token_hash]["last_seen"] = time.time()

    def delete_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id: str) -> int:
        doomed = [h for h, s in self._sessions.items() if s["user_id"] == user_id]
        for h in doomed:
            del self._sessions[h]
        return len(doomed)


_USERS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    avatar_color TEXT,
    created_at REAL NOT NULL
)
"""

_SESSIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
)
"""


class SQLiteAccountStore(AccountStore):
    """Accounts in a SQLite file -- by default the same file as room
    snapshots (different tables, one file to back up). Idempotent DDL on
    init, exactly like :class:`SQLiteStore` -- there is no separate
    migration step to run or forget."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_USERS_DDL_SQLITE)
            conn.execute(_SESSIONS_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    @contextmanager
    def _connect(self):
        # Same close-guaranteeing wrapper as SQLiteStore._connect -- see
        # the connection-leak note there (bites hard on Windows).
        conn = sqlite3.connect(self._path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row:
                return _user_row_to_dict(row)
            user_id = uuid.uuid4().hex
            created = time.time()
            name = display_name or email.split("@")[0]
            conn.execute(
                "INSERT INTO users (user_id, email, display_name, avatar_color, created_at) VALUES (?, ?, ?, NULL, ?)",
                (user_id, email, name, created),
            )
            return {
                "user_id": user_id,
                "email": email,
                "display_name": name,
                "avatar_color": None,
                "created_at": created,
            }

    def get_user(self, user_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return _user_row_to_dict(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
            return _user_row_to_dict(row) if row else None

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if avatar_color is not None:
            sets.append("avatar_color = ?")
            params.append(avatar_color)
        if not sets:
            return self.get_user(user_id) is not None
        params.append(user_id)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
            return cur.rowcount > 0

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, expires_at, now, now),
            )

    def get_session(self, token_hash: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, expires_at, created_at, last_seen FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            if row[1] < time.time():
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            return {"user_id": row[0], "expires_at": row[1], "created_at": row[2], "last_seen": row[3]}

    def touch_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (time.time(), token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return cur.rowcount


class PostgresAccountStore(AccountStore):
    """Accounts in Postgres, for multi-process deployments (k8s Mode B).
    Same asyncpg-behind-a-sync-interface bridge as
    :class:`crdt_cad.persistence.store.PostgresStore` -- a dedicated
    background thread runs its own event loop; every call blocks the
    calling thread until the query completes. See that class's docstring
    for why this trade-off is acceptable (and already made) here."""

    _SCHEMA_LOCK_KEY = 0x63AD_ACC7  # distinct from PostgresStore's lock key

    def __init__(self, dsn: str) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresAccountStore needs asyncpg -- install with `pip install crdt-cad[postgres]`, "
                "or unset CRDT_CAD_DATABASE_URL to keep accounts in SQLite."
            ) from exc
        self._asyncpg = asyncpg
        self._dsn = dsn
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="postgres-accounts-loop")
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
            # Same replicas-race-on-first-boot consideration as
            # PostgresStore._init_pool (verified there against a real
            # 3-replica kind deployment) -- serialize one-time DDL.
            await conn.execute("SELECT pg_advisory_lock($1)", self._SCHEMA_LOCK_KEY)
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        display_name TEXT,
                        avatar_color TEXT,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        last_seen DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", self._SCHEMA_LOCK_KEY)
        return pool

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()

        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE email = $1",
                    email,
                )
                if row:
                    return _user_row_to_dict(row)
                user_id = uuid.uuid4().hex
                created = time.time()
                name = display_name or email.split("@")[0]
                # ON CONFLICT: two processes racing to create the same
                # user must converge on one row, not error.
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (user_id, email, display_name, avatar_color, created_at)
                    VALUES ($1, $2, $3, NULL, $4)
                    ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                    RETURNING user_id, email, display_name, avatar_color, created_at
                    """,
                    user_id, email, name, created,
                )
                return _user_row_to_dict(row)

        return self._call(_go())

    def get_user(self, user_id: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE user_id = $1",
                    user_id,
                )
                return _user_row_to_dict(row) if row else None

        return self._call(_go())

    def get_user_by_email(self, email: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at FROM users WHERE email = $1",
                    email.strip().lower(),
                )
                return _user_row_to_dict(row) if row else None

        return self._call(_go())

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        async def _go():
            sets, params = [], []
            if display_name is not None:
                params.append(display_name)
                sets.append(f"display_name = ${len(params)}")
            if avatar_color is not None:
                params.append(avatar_color)
                sets.append(f"avatar_color = ${len(params)}")
            async with self._pool.acquire() as conn:
                if not sets:
                    row = await conn.fetchrow("SELECT 1 FROM users WHERE user_id = $1", user_id)
                    return row is not None
                params.append(user_id)
                result = await conn.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE user_id = ${len(params)}", *params
                )
                return result.endswith("1")

        return self._call(_go())

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        async def _go():
            now = time.time()
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sessions (token_hash, user_id, expires_at, created_at, last_seen) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    token_hash, user_id, expires_at, now, now,
                )

        self._call(_go())

    def get_session(self, token_hash: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, expires_at, created_at, last_seen FROM sessions WHERE token_hash = $1",
                    token_hash,
                )
                if not row:
                    return None
                if row["expires_at"] < time.time():
                    await conn.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)
                    return None
                return dict(row)

        return self._call(_go())

    def touch_session(self, token_hash: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET last_seen = $1 WHERE token_hash = $2", time.time(), token_hash
                )

        self._call(_go())

    def delete_session(self, token_hash: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)

        self._call(_go())

    def delete_user_sessions(self, user_id: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
                return int(result.rsplit(" ", 1)[1])

        return self._call(_go())
