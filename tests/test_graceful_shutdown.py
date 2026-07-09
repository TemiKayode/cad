"""Phase 19.4: verifies the `lifespan` shutdown hook in crdt_cad.server.app
-- what makes `kubectl rollout restart` / `docker stop` / a VM reboot
safe. Live-edit traffic persists on a debounced schedule (at most one
persist per PERSIST_MIN_INTERVAL_SECONDS per room -- see Phase 19.5's
load-test finding in app.py), so at any instant a room can legitimately
be holding accepted-but-not-yet-persisted ops; this shutdown hook is
what guarantees none of that is lost on SIGTERM. The test constructs
that state directly (mutating the in-memory doc without going through
the message handler) to prove the hook is a real backstop, not
something that happens to work only because some other code path
already covers it.
"""
import pytest

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.server import app as app_module

pytestmark = pytest.mark.asyncio


class _FakeWebSocket:
    def __init__(self):
        self.closed_with = None

    async def close(self, code=None):
        self.closed_with = code


async def test_shutdown_persists_a_dirty_unsaved_room_and_closes_clients(tmp_path, monkeypatch):
    from crdt_cad.persistence.store import SQLiteStore

    test_store = SQLiteStore(tmp_path / "shutdown-test.db")
    monkeypatch.setattr(app_module.drawing_room_manager, "store", test_store)

    room = await app_module.drawing_room_manager.get_or_create("shutdown-test-room")
    try:
        room.doc.add_layer("Unsaved Layer")  # mutates room.doc directly, not yet persisted
        room.mark_dirty()

        assert test_store.load("drawing", "shutdown-test-room") is None, "should not be persisted yet"

        fake_ws = _FakeWebSocket()
        room.clients["fake-actor"] = fake_ws

        await app_module._graceful_shutdown()

        persisted = test_store.load("drawing", "shutdown-test-room")
        assert persisted is not None, "shutdown must persist every dirty room"
        restored = DrawingDocument.from_bytes(LamportClock(actor="verify"), persisted)
        assert any(layer["name"] == "Unsaved Layer" for layer in restored.layer_list())

        assert fake_ws.closed_with == app_module.WS_CLOSE_GOING_AWAY
    finally:
        app_module.drawing_room_manager.rooms.pop("shutdown-test-room", None)


async def test_shutdown_tolerates_a_client_whose_close_raises(monkeypatch):
    """One misbehaving/already-gone connection shouldn't stop the shutdown
    hook from persisting and closing every other room/client."""
    from crdt_cad.persistence.store import InMemoryStore

    monkeypatch.setattr(app_module.mesh_room_manager, "store", InMemoryStore())

    room = await app_module.mesh_room_manager.get_or_create("shutdown-test-mesh-room")
    try:
        class _RaisingWebSocket:
            async def close(self, code=None):
                raise RuntimeError("socket already gone")

        good_ws = _FakeWebSocket()
        room.clients["bad-actor"] = _RaisingWebSocket()
        room.clients["good-actor"] = good_ws
        room.mark_dirty()

        await app_module._graceful_shutdown()  # must not raise

        assert good_ws.closed_with == app_module.WS_CLOSE_GOING_AWAY
    finally:
        app_module.mesh_room_manager.rooms.pop("shutdown-test-mesh-room", None)
