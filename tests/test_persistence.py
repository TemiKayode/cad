
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


# -- Phase 17: workspace metadata (display names, detailed listing) ---------


def test_list_rooms_detailed_includes_metadata_and_is_newest_first(store):
    store.save("drawing", "a", b"x")
    store.save("drawing", "b", b"y")
    rows = store.list_rooms_detailed("drawing")
    assert [r["room_id"] for r in rows] == ["b", "a"]
    assert all(r["display_name"] is None for r in rows)
    assert rows[0]["updated_at"] >= rows[1]["updated_at"]


def test_set_display_name_roundtrips_and_reports_missing_room(store):
    store.save("drawing", "room1", b"x")
    assert store.set_display_name("drawing", "room1", "My Floor Plan") is True
    rows = store.list_rooms_detailed("drawing")
    assert rows[0]["display_name"] == "My Floor Plan"

    assert store.set_display_name("drawing", "does-not-exist", "X") is False


def test_set_display_name_empty_clears_it(store):
    store.save("drawing", "room1", b"x")
    store.set_display_name("drawing", "room1", "Name")
    store.set_display_name("drawing", "room1", "")
    assert store.list_rooms_detailed("drawing")[0]["display_name"] is None


def test_room_updated_at_reflects_latest_save(store):
    store.save("drawing", "room1", b"x")
    t1 = store.room_updated_at("drawing", "room1")
    assert t1 is not None
    assert store.room_updated_at("drawing", "does-not-exist") is None


# -- Phase 17: version history -----------------------------------------------


def test_save_version_then_list_and_load_roundtrips(store):
    v1 = store.save_version("drawing", "room1", b"snap-1")
    v2 = store.save_version("drawing", "room1", b"snap-2")
    assert v1 != v2

    versions = store.list_versions("drawing", "room1")
    assert [v["version_id"] for v in versions] == [v2, v1]  # newest first

    assert store.load_version("drawing", "room1", v1) == b"snap-1"
    assert store.load_version("drawing", "room1", v2) == b"snap-2"
    assert store.load_version("drawing", "room1", 999999) is None


def test_save_version_prunes_beyond_keep_limit(store):
    ids = [store.save_version("drawing", "room1", f"snap-{i}".encode(), keep=3) for i in range(5)]
    versions = store.list_versions("drawing", "room1")
    assert len(versions) == 3
    kept_ids = {v["version_id"] for v in versions}
    assert kept_ids == set(ids[-3:])  # only the 3 most recent survive


def test_versions_are_scoped_per_room_and_kind(store):
    store.save_version("drawing", "room1", b"a")
    store.save_version("drawing", "room2", b"b")
    store.save_version("mesh", "room1", b"c")
    assert len(store.list_versions("drawing", "room1")) == 1
    assert len(store.list_versions("drawing", "room2")) == 1
    assert len(store.list_versions("mesh", "room1")) == 1


def test_delete_room_also_clears_its_versions(store):
    store.save("drawing", "room1", b"x")
    store.save_version("drawing", "room1", b"snap")
    store.delete("drawing", "room1")
    assert store.list_versions("drawing", "room1") == []
