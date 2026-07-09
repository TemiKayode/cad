"""Phase 19.5: the debounced persist (`Room.persist_debounced`) found and
fixed by the load test -- persisting a full-document snapshot per accepted
ops message made per-message cost grow with document size until the event
loop drowned (12s mean op latency at 50 clients x 5 ops/s). These tests pin
the debounce contract: bounded persist rate, a trailing flush so the newest
state always lands within one interval, `0` restoring the old
persist-per-message behavior, and graceful shutdown superseding (not
duplicating) a pending flush.
"""
import asyncio

import pytest

from crdt_cad.crdt.document import DocOp, DrawingDocument
from crdt_cad.persistence.store import InMemoryStore
from crdt_cad.server import app as app_module

pytestmark = pytest.mark.asyncio

INTERVAL = 0.3


class _CountingStore(InMemoryStore):
    def __init__(self):
        super().__init__()
        self.saves = 0

    def save(self, kind, room_id, data):
        self.saves += 1
        super().save(kind, room_id, data)


def _make_room(store) -> app_module.Room:
    return app_module.Room("debounce-room", "drawing", DrawingDocument, DocOp.from_dict, store)


async def test_rapid_edits_persist_boundedly_with_trailing_flush(monkeypatch):
    monkeypatch.setattr(app_module, "PERSIST_MIN_INTERVAL_SECONDS", INTERVAL)
    store = _CountingStore()
    room = _make_room(store)

    for i in range(20):
        room.doc.add_layer(f"layer-{i}")
        room.mark_dirty()
        await room.persist_debounced()

    # First call persists immediately (nothing persisted yet); the other 19
    # collapse into one scheduled trailing flush.
    assert store.saves == 1

    await asyncio.sleep(INTERVAL * 2)
    assert store.saves == 2, "the trailing flush must fire exactly once"

    # And that flush captured the *final* state, not the state at schedule
    # time -- persist serializes the live doc, so every edit that happened
    # while the flush was pending is included.
    from crdt_cad.crdt.clock import LamportClock

    restored = DrawingDocument.from_bytes(LamportClock(actor="verify"), store.load("drawing", "debounce-room"))
    names = {layer["name"] for layer in restored.layer_list()}
    assert "layer-19" in names


async def test_zero_interval_restores_persist_per_message(monkeypatch):
    monkeypatch.setattr(app_module, "PERSIST_MIN_INTERVAL_SECONDS", 0.0)
    store = _CountingStore()
    room = _make_room(store)

    for i in range(5):
        room.doc.add_layer(f"layer-{i}")
        await room.persist_debounced()

    assert store.saves == 5


async def test_shutdown_supersedes_a_pending_flush(monkeypatch):
    """SIGTERM while a trailing flush is still pending: the shutdown persist
    must (a) capture the newest state and (b) cancel the pending flush
    rather than leaving a stray task to fire after the loop is gone."""
    monkeypatch.setattr(app_module, "PERSIST_MIN_INTERVAL_SECONDS", INTERVAL)
    store = _CountingStore()
    room = _make_room(store)
    app_module.drawing_room_manager.rooms["debounce-room"] = room
    try:
        room.doc.add_layer("first")
        room.mark_dirty()
        await room.persist_debounced()  # immediate persist
        room.doc.add_layer("second")
        room.mark_dirty()
        await room.persist_debounced()  # within interval -> schedules trailing flush
        assert room._deferred_persist is not None and not room._deferred_persist.done()

        await app_module._graceful_shutdown()

        assert store.saves == 2, "shutdown persist replaces the flush, not stacks on it"
        assert room._deferred_persist.done(), "no pending flush may outlive shutdown"
        from crdt_cad.crdt.clock import LamportClock

        restored = DrawingDocument.from_bytes(LamportClock(actor="verify"), store.load("drawing", "debounce-room"))
        assert {"first", "second"} <= {layer["name"] for layer in restored.layer_list()}
    finally:
        app_module.drawing_room_manager.rooms.pop("debounce-room", None)
