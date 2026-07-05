"""Tests for Phase 17: rooms as projects. Covers the parts of app.py
that are genuinely new server-side surface -- the workspace room
listing, rename, thumbnail, version-history checkpoint/list/restore,
and read-only share-link role enforcement (both at the WS ops boundary
and the REST editor-only endpoints). Route-level (home page moving to
`/`, the 2D demo moving to `/2d`) is covered at the bottom.

`isolated_store` (autouse, tests/conftest.py) gives every test a fresh
in-memory store and empty rooms.
"""

import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.server import app as app_module
from crdt_cad.server import security
from crdt_cad.server.app import app


def _client() -> TestClient:
    return TestClient(app)


def _hello(ws, actor="a", token=None):
    msg = {"type": "hello", "actor": actor}
    if token is not None:
        msg["token"] = token
    ws.send_json(msg)


def _one_layer_op() -> dict:
    doc = DrawingDocument(LamportClock(actor="workspace-actor"))
    _, ops = doc.add_layer("L")
    return ops[0].to_dict()


# -- workspace room listing --------------------------------------------------


def test_workspace_rooms_lists_both_kinds_sorted_newest_first(isolated_store):
    isolated_store.save("drawing", "sketch1", b"x")
    time.sleep(0.01)
    isolated_store.save("mesh", "mesh1", b"y")

    resp = _client().get("/api/workspace/rooms")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["room_id"] for r in rows] == ["mesh1", "sketch1"]
    assert rows[0]["kind"] == "mesh"
    assert rows[1]["kind"] == "drawing"
    assert rows[0]["display_name"] is None


def test_workspace_rooms_empty_when_no_rooms_exist():
    resp = _client().get("/api/workspace/rooms")
    assert resp.status_code == 200
    assert resp.json() == []


# -- rename -------------------------------------------------------------------


def test_rename_drawing_room_roundtrips_into_workspace_listing(isolated_store):
    isolated_store.save("drawing", "room1", b"x")
    resp = _client().post("/api/rooms/room1/rename", json={"display_name": "My Floor Plan"})
    assert resp.status_code == 200

    rows = _client().get("/api/workspace/rooms").json()
    assert rows[0]["display_name"] == "My Floor Plan"


def test_rename_mesh_room_roundtrips(isolated_store):
    isolated_store.save("mesh", "meshroom1", b"x")
    resp = _client().post("/api/mesh/meshroom1/rename", json={"display_name": "House v2"})
    assert resp.status_code == 200
    rows = _client().get("/api/workspace/rooms").json()
    assert rows[0]["display_name"] == "House v2"


def test_rename_nonexistent_room_is_404():
    resp = _client().post("/api/rooms/does-not-exist/rename", json={"display_name": "X"})
    assert resp.status_code == 404


# -- thumbnail ------------------------------------------------------------------


def test_drawing_thumbnail_renders_real_svg_from_snapshot():
    client = _client()
    with client.websocket_connect("/ws/thumbroom") as ws:
        _hello(ws)
        ws.receive_json()  # snapshot
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        ws.send_json({"type": "save"})
        ws.receive_json()  # saved

    resp = client.get("/api/rooms/thumbroom/thumbnail.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in resp.content


# -- version history ------------------------------------------------------------


def test_explicit_save_creates_a_version_checkpoint():
    client = _client()
    with client.websocket_connect("/ws/versroom1") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        ws.send_json({"type": "save"})
        ws.receive_json()

    versions = client.get("/api/rooms/versroom1/versions").json()
    assert len(versions) == 1
    assert "created_at" in versions[0]


def test_quiescent_room_gets_no_periodic_checkpoint(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "VERSION_CHECKPOINT_INTERVAL_SECONDS", 0.05)
    client = _client()
    with client.websocket_connect("/ws/versroom2") as ws:
        _hello(ws)
        ws.receive_json()
        time.sleep(0.2)  # several ticks pass with nothing edited
        ws.send_json({"type": "save"})
        ws.receive_json()  # "saved" -- an explicit save always checkpoints once

    versions = client.get("/api/rooms/versroom2/versions").json()
    assert len(versions) == 1  # only the explicit save's checkpoint, not one per idle tick


def test_periodic_checkpoint_fires_when_dirty(monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(app_module, "VERSION_CHECKPOINT_INTERVAL_SECONDS", 0.05)
    client = _client()
    with client.websocket_connect("/ws/versroom3") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        # the next periodic tick broadcasts a frontier ping once dirty --
        # wait for it as a synchronization point, then give the checkpoint
        # (which fires in that same loop iteration) a moment to complete.
        msg = ws.receive_json()
        assert msg["type"] == "frontier"
        time.sleep(0.1)

    versions = client.get("/api/rooms/versroom3/versions").json()
    assert len(versions) == 1


def test_version_history_prunes_to_configured_max(monkeypatch):
    monkeypatch.setattr(security, "max_versions_per_room", lambda: 2)
    client = _client()
    with client.websocket_connect("/ws/versroom4") as ws:
        _hello(ws)
        ws.receive_json()
        for _ in range(4):
            ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
            ws.send_json({"type": "save"})
            ws.receive_json()

    versions = client.get("/api/rooms/versroom4/versions").json()
    assert len(versions) == 2


def test_restore_forks_into_a_new_room_without_touching_the_original():
    client = _client()
    with client.websocket_connect("/ws/origroom") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        ws.send_json({"type": "save"})
        ws.receive_json()

    versions = client.get("/api/rooms/origroom/versions").json()
    version_id = versions[0]["version_id"]

    resp = client.post(f"/api/rooms/origroom/versions/{version_id}/restore")
    assert resp.status_code == 200
    new_room_id = resp.json()["new_room_id"]
    assert new_room_id != "origroom"

    # the fork has the restored content...
    forked = client.get(f"/api/rooms/{new_room_id}/export/json").json()
    assert len(forked["layers"]["entries"]) == 1
    # ...and the original room is completely untouched (still there, same id).
    original = client.get("/api/rooms/origroom/export/json").json()
    assert len(original["layers"]["entries"]) == 1


def test_restore_unknown_version_is_404():
    resp = _client().post("/api/rooms/anyroom/versions/999999/restore")
    assert resp.status_code == 404


# -- read-only share links: minting -------------------------------------------


def test_share_link_requires_auth_enabled():
    resp = _client().post("/api/rooms/anyroom/share-link", json={"role": "viewer"})
    assert resp.status_code == 400


def test_share_link_mints_a_working_viewer_token(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    editor_token = security.mint_room_token("drawing", "shareroom1")
    resp = _client().post(
        f"/api/rooms/shareroom1/share-link?token={editor_token}",
        json={"role": "viewer"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "viewer"
    assert security.token_role(body["token"], "drawing", "shareroom1") == "viewer"


def test_viewer_token_cannot_mint_a_share_link_of_either_role(monkeypatch):
    """Privilege-escalation guard: a viewer holding a read-only link must
    not be able to mint themselves (or anyone else) a fresh link at all --
    not even another viewer-role one -- via this endpoint."""
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    viewer_token = security.mint_room_token("drawing", "shareroom2", role="viewer")
    resp = _client().post(
        f"/api/rooms/shareroom2/share-link?token={viewer_token}",
        json={"role": "viewer"},
    )
    assert resp.status_code == 403


def test_viewer_token_rejected_from_editor_only_rest_endpoints(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    viewer_token = security.mint_room_token("drawing", "shareroom3", role="viewer")
    resp = _client().post(
        f"/api/rooms/shareroom3/rename?token={viewer_token}",
        json={"display_name": "Nope"},
    )
    assert resp.status_code == 403


def test_editor_token_still_allowed_on_editor_only_endpoints(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    editor_token = security.mint_room_token("drawing", "shareroom4")
    client = _client()
    with client.websocket_connect("/ws/shareroom4") as ws:
        _hello(ws, token=editor_token)
        ws.receive_json()
        # the room needs to actually exist in the store (rename 404s
        # otherwise) -- an explicit save is the simplest way to get there.
        ws.send_json({"type": "save"})
        ws.receive_json()
    resp = client.post(
        f"/api/rooms/shareroom4/rename?token={editor_token}",
        json={"display_name": "Fine"},
    )
    assert resp.status_code == 200


# -- read-only share links: WS enforcement -------------------------------------


def test_viewer_ws_receives_role_in_snapshot(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    viewer_token = security.mint_room_token("drawing", "wsviewroom1", role="viewer")
    client = _client()
    with client.websocket_connect("/ws/wsviewroom1") as ws:
        _hello(ws, token=viewer_token)
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["role"] == "viewer"


def test_editor_ws_receives_editor_role_by_default():
    client = _client()
    with client.websocket_connect("/ws/wseditroom1") as ws:
        _hello(ws)
        snapshot = ws.receive_json()
        assert snapshot["role"] == "editor"


def test_viewer_ws_ops_message_is_rejected_not_applied(monkeypatch):
    """The actual enforcement boundary: a hand-crafted viewer WS sending
    ops must be refused server-side -- not just have its UI hidden."""
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    viewer_token = security.mint_room_token("drawing", "wsviewroom2", role="viewer")
    client = _client()
    with client.websocket_connect("/ws/wsviewroom2") as ws:
        _hello(ws, token=viewer_token)
        ws.receive_json()  # snapshot
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        reply = ws.receive_json()
        assert reply["type"] == "rejected"

    # confirm the op was never actually applied/persisted -- a fresh
    # editor connection to the same room sees an empty document.
    editor_token = security.mint_room_token("drawing", "wsviewroom2")
    with client.websocket_connect("/ws/wsviewroom2") as ws2:
        _hello(ws2, token=editor_token)
        snapshot = ws2.receive_json()
        assert snapshot["doc"]["layers"]["entries"] == []


def test_editor_ws_ops_still_accepted_normally(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    editor_token = security.mint_room_token("drawing", "wseditroom2")
    client = _client()
    with client.websocket_connect("/ws/wseditroom2") as ws:
        _hello(ws, token=editor_token)
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        # no "rejected" reply -- the next thing on the wire (if anything)
        # would be another client's broadcast, not this actor's own op
        # (broadcast excludes the sender), so just confirm persistence.
        ws.send_json({"type": "save"})
        reply = ws.receive_json()
        assert reply["type"] == "saved"

    resp = client.get(f"/api/rooms/wseditroom2/export/json?token={editor_token}")
    assert len(resp.json()["layers"]["entries"]) == 1


def test_viewer_ws_token_scoped_to_kind_and_room_still_enforced(monkeypatch):
    """Role is additive on top of the existing kind/room scoping -- a
    viewer token minted for the wrong room still gets rejected outright,
    it doesn't just silently downgrade access."""
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    viewer_token = security.mint_room_token("drawing", "roomA", role="viewer")
    client = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/roomB") as ws:
            _hello(ws, token=viewer_token)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


# -- route changes: home page moves to "/", 2D demo moves to "/2d" ------------


def test_home_route_serves_the_workspace_page():
    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"crdt-cad" in resp.content


def test_2d_route_serves_the_sketch_demo():
    resp = _client().get("/2d")
    assert resp.status_code == 200
    assert b"toolPen" in resp.content


def test_3d_route_unchanged():
    resp = _client().get("/3d")
    assert resp.status_code == 200
    assert b"toolVertex" in resp.content
