"""Tests for Phase 5: the periodic self-heal broadcast is a lightweight
`{"type": "frontier", ...}` ping (O(actor count)), not a full document
`snapshot` (O(document size)) -- clients request a real catch-up only on
an actual mismatch, via the new "resync" -> "delta"/"snapshot" path.
"""

import time

from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.server import app as app_module
from crdt_cad.server.app import app

# `isolated_store` fixture (autouse) lives in tests/conftest.py and applies here too.


def _client() -> TestClient:
    return TestClient(app)


def _draw_something(ws, actor="a") -> DrawingDocument:
    """Returns the local DrawingDocument used to mint the ops, so a test
    can read its exact `.frontier()` instead of hand-counting op ticks."""
    doc = DrawingDocument(LamportClock(actor=actor))
    layer_id, layer_ops = doc.add_layer("L")
    _, path_ops = doc.add_path(layer_id, [(0.0, 0.0), (5.0, 5.0), (10.0, 0.0)])
    ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [*layer_ops, *path_ops]]})
    return doc


def test_quiescent_room_sends_no_periodic_broadcast(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_INTERVAL_SECONDS", 0.05)
    client = _client()
    with client.websocket_connect("/ws/quiescentroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()  # initial snapshot
        time.sleep(0.2)  # several periodic-loop ticks pass with nothing happening

        # if a spurious broadcast had been sent, it would already be sitting
        # in the socket's receive buffer ahead of this "saved" reply
        ws.send_json({"type": "save"})
        reply = ws.receive_json()
        assert reply["type"] == "saved"


def test_dirty_room_periodic_broadcast_is_a_frontier_ping_not_a_snapshot(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_INTERVAL_SECONDS", 0.05)
    client = _client()
    with client.websocket_connect("/ws/frontierroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        doc = _draw_something(ws)  # marks the room dirty
        expected_counter = doc.frontier().to_dict()["a"]

        msg = ws.receive_json()  # the next periodic tick broadcasts
        assert msg["type"] == "frontier"
        assert "doc" not in msg
        assert "frontier" in msg
        assert msg["frontier"].get("a") == expected_counter


def test_client_with_stale_frontier_self_heals_via_resync(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_INTERVAL_SECONDS", 0.05)
    client = _client()
    with client.websocket_connect("/ws/healroom") as ws_a, client.websocket_connect("/ws/healroom") as ws_b:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()
        ws_b.send_json({"type": "hello", "actor": "b"})
        ws_b.receive_json()

        # A non-empty (if nominal) known_frontier: an *empty* dict is
        # treated the same as "no known frontier at all" (the existing
        # "hello" handshake already makes that same choice, see
        # test_resync_with_no_known_frontier_gets_a_full_snapshot), which
        # would hit the full-snapshot fallback instead of the delta path
        # this test means to exercise. Counter 0 means "seen nothing from
        # a yet" -- a real client would have this after its own very first
        # local edit, or after any snapshot/delta that mentions actor "a".
        known_frontier_a = {"a": 0}

        room = app_module.drawing_room_manager.rooms["healroom"]

        # `b`'s live "ops" broadcast already reaches every *other* connected
        # client immediately (that's the normal, already-tested path) -- so
        # to actually exercise the periodic-ping backstop, simulate `a`
        # missing that broadcast the way a transient network blip would:
        # briefly drop it from the room's client bookkeeping while `b`
        # draws, then "reconnect" it before the next periodic tick.
        a_ws = room.clients.pop("a")
        doc_b = _draw_something(ws_b, actor="b")
        expected_b_counter = doc_b.frontier().to_dict()["b"]
        # b's "ops" message is handled asynchronously on the server -- wait
        # for a reply to a *later* message on the same connection before
        # restoring `a`, so the broadcast attempt (which found `a` absent)
        # has definitely already happened (same synchronization technique
        # used elsewhere in this suite for the identical race).
        ws_b.send_json({"type": "save"})
        ws_b.receive_json()
        room.clients["a"] = a_ws

        # the next periodic tick broadcasts the lightweight frontier ping,
        # which now reaches `a` -- the first thing it hears about `b`'s change
        frontier_msg = ws_a.receive_json()
        assert frontier_msg["type"] == "frontier"
        assert frontier_msg["frontier"].get("b") == expected_b_counter

        # what a real client's RelayConnection.isBehind()-triggered request
        # looks like on the wire
        ws_a.send_json({"type": "resync", "known_frontier": known_frontier_a})
        delta_msg = ws_a.receive_json()
        assert delta_msg["type"] == "delta"
        assert len(delta_msg["ops"]) == expected_b_counter  # every op b minted, all from one actor
        assert delta_msg["frontier"].get("b") == expected_b_counter


def test_resync_with_no_known_frontier_gets_a_full_snapshot(monkeypatch):
    """The response of last resort: a client that (for whatever reason)
    never has a recorded frontier asks for a full snapshot outright."""
    client = _client()
    with client.websocket_connect("/ws/lastresortroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)
        ws.send_json({"type": "save"})
        ws.receive_json()

        ws.send_json({"type": "resync", "known_frontier": None})
        reply = ws.receive_json()
        assert reply["type"] == "snapshot"
        assert len(reply["doc"]["path_index"]["entries"]) == 1
