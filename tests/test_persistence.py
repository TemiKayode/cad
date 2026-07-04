
import pytest

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.persistence.store import InMemoryStore, SQLiteStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStore()
    return SQLiteStore(tmp_path / "test.db")


def test_save_and_load_roundtrip(store):
    store.save("drawing", "room1", b"hello world")
    assert store.load("drawing", "room1") == b"hello world"


def test_load_missing_room_returns_none(store):
    assert store.load("drawing", "does-not-exist") is None


def test_save_overwrites_previous_value(store):
    store.save("drawing", "room1", b"v1")
    store.save("drawing", "room1", b"v2")
    assert store.load("drawing", "room1") == b"v2"


def test_list_rooms_filters_by_kind(store):
    store.save("drawing", "a", b"x")
    store.save("drawing", "b", b"y")
    store.save("mesh", "c", b"z")
    assert set(store.list_rooms("drawing")) == {"a", "b"}
    assert set(store.list_rooms("mesh")) == {"c"}


def test_delete_removes_room(store):
    store.save("drawing", "room1", b"x")
    store.delete("drawing", "room1")
    assert store.load("drawing", "room1") is None


def test_sqlite_store_persists_across_new_connections(tmp_path):
    path = tmp_path / "persist.db"
    store1 = SQLiteStore(path)
    store1.save("drawing", "room1", b"durable data")

    store2 = SQLiteStore(path)  # simulates a server restart re-opening the same db file
    assert store2.load("drawing", "room1") == b"durable data"


def test_real_document_snapshot_roundtrips_through_store(store):
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)], color="#ff0000")

    store.save("drawing", "room1", doc.to_bytes())
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), store.load("drawing", "room1"))
    assert restored.layer_list() == doc.layer_list()
