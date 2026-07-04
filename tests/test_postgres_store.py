"""Tests for PostgresStore -- the real-horizontal-scaling persistence
backend (Phase 7). Mirrors test_persistence.py's SQLite/InMemory
coverage, plus the one property that's actually the point of this
backend: two separate PostgresStore instances (standing in for two
server *processes*) against the same DSN see each other's writes.

Per the brief ("unit-test it with a mocked/skipped-if-unavailable
pattern so CI doesn't need Postgres"): this whole module skips cleanly
if `asyncpg` isn't installed, and the `store` fixture skips any
individual test if no Postgres is actually reachable at the configured
DSN -- CI (and any environment without the optional `postgres` extra
and a live database) never needs one. Point `CRDT_CAD_TEST_DATABASE_URL`
at a real Postgres (e.g. `docker run -e POSTGRES_PASSWORD=postgres -p
55432:5432 postgres:16-alpine`) to actually exercise this file.
"""

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument

pytest.importorskip("asyncpg")

from crdt_cad.persistence.store import PostgresStore  # noqa: E402

TEST_DSN = os.environ.get(
    "CRDT_CAD_TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/crdt_cad_test"
)


def _quick_reachability_check(dsn: str) -> str | None:
    """A raw, sub-second TCP probe done once at collection time, before
    any test runs -- asyncpg's own connection failure can take several
    seconds per attempt (retries/DNS/handshake timeouts), which would
    otherwise turn "Postgres isn't running" into ~30s of accumulated
    per-test skip overhead on every plain `pytest tests/` run. Returns
    None if reachable, else a skip reason."""
    parsed = urlparse(dsn)
    host, port = parsed.hostname or "localhost", parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return None
    except OSError as exc:
        return f"no Postgres reachable at {host}:{port} ({exc})"


_skip_reason = _quick_reachability_check(TEST_DSN)
if _skip_reason:
    pytest.skip(_skip_reason, allow_module_level=True)


def _room_id() -> str:
    return f"room-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def store():
    try:
        s = PostgresStore(TEST_DSN)
    except Exception as exc:
        pytest.skip(f"PostgresStore failed to initialize against {TEST_DSN}: {exc}")
        return
    yield s
    s.close()


def test_save_and_load_roundtrip(store):
    room = _room_id()
    store.save("drawing", room, b"hello world")
    assert store.load("drawing", room) == b"hello world"


def test_load_missing_room_returns_none(store):
    assert store.load("drawing", _room_id()) is None


def test_save_overwrites_previous_value(store):
    room = _room_id()
    store.save("drawing", room, b"v1")
    store.save("drawing", room, b"v2")
    assert store.load("drawing", room) == b"v2"


def test_list_rooms_filters_by_kind(store):
    room_a, room_b, room_c = _room_id(), _room_id(), _room_id()
    store.save("drawing", room_a, b"x")
    store.save("drawing", room_b, b"y")
    store.save("mesh", room_c, b"z")
    drawing_rooms = store.list_rooms("drawing")
    mesh_rooms = store.list_rooms("mesh")
    assert room_a in drawing_rooms and room_b in drawing_rooms
    assert room_c not in drawing_rooms
    assert room_c in mesh_rooms


def test_delete_removes_room(store):
    room = _room_id()
    store.save("drawing", room, b"x")
    store.delete("drawing", room)
    assert store.load("drawing", room) is None


def test_two_store_instances_share_state_against_the_same_dsn():
    """The actual point of PostgresStore: unlike a per-process SQLite
    file, two separate instances (standing in for two server replicas)
    against the same database see each other's writes immediately --
    this is what makes horizontal scaling of room state legitimate."""
    try:
        store1 = PostgresStore(TEST_DSN)
    except Exception as exc:
        pytest.skip(f"no local Postgres reachable at {TEST_DSN} ({exc})")
        return
    try:
        store2 = PostgresStore(TEST_DSN)
        try:
            room = _room_id()
            store1.save("drawing", room, b"written by replica 1")
            assert store2.load("drawing", room) == b"written by replica 1"
        finally:
            store2.close()
    finally:
        store1.close()


def test_real_document_snapshot_roundtrips_through_store(store):
    room = _room_id()
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)], color="#ff0000")

    store.save("drawing", room, doc.to_bytes())
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), store.load("drawing", room))
    assert restored.layer_list() == doc.layer_list()
