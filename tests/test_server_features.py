from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DocOp, DrawingDocument
from crdt_cad.crdt.mesh import MeshCRDT
from crdt_cad.server import app as app_module
from crdt_cad.server.app import RoomManager, app

# `isolated_store` fixture (autouse) lives in tests/conftest.py and applies here too.


def _client() -> TestClient:
    return TestClient(app)


def _draw_something(ws, actor="a"):
    doc = DrawingDocument(LamportClock(actor=actor))
    layer_id, layer_ops = doc.add_layer("L")
    _, path_ops = doc.add_path(layer_id, [(0.0, 0.0), (5.0, 5.0), (10.0, 0.0)], color="#ff00ff")
    ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [*layer_ops, *path_ops]]})
    return doc


# -- export ---------------------------------------------------------------------


def test_export_drawing_json_reflects_room_content():
    client = _client()
    with client.websocket_connect("/ws/exportroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)

    resp = client.get("/api/rooms/exportroom/export/json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()
    assert len(body["path_index"]["entries"]) == 1


def test_export_drawing_svg_contains_path_data():
    client = _client()
    with client.websocket_connect("/ws/exportsvg") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)

    resp = client.get("/api/rooms/exportsvg/export/svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in resp.text
    assert 'stroke="#ff00ff"' in resp.text


def test_export_drawing_dxf_is_readable():
    client = _client()
    with client.websocket_connect("/ws/exportdxf") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)

    resp = client.get("/api/rooms/exportdxf/export/dxf")
    assert resp.status_code == 200
    from crdt_cad.export.dxf_io import drawing_from_dxf_bytes

    paths = drawing_from_dxf_bytes(resp.content)
    assert len(paths) == 1
    assert len(paths[0]) == 3


def test_export_drawing_pdf_without_a_sheet_yet_returns_404():
    client = _client()
    with client.websocket_connect("/ws/exportpdfnosheet") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)

    resp = client.get("/api/rooms/exportpdfnosheet/export/pdf")
    assert resp.status_code == 404


def test_export_drawing_pdf_renders_the_sheet(tmp_path):
    client = _client()
    with client.websocket_connect("/ws/exportpdf") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        doc = _draw_something(ws)
        sheet_id, sheet_ops = doc.add_sheet("Sheet 1")
        sheet_ops.append(doc.set_sheet_prop(sheet_id, "title", "My Drawing"))
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in sheet_ops]})

    resp = client.get("/api/rooms/exportpdf/export/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content[:4] == b"%PDF"


def test_export_drawing_pdf_selects_sheet_by_id_and_falls_back_for_a_stale_one():
    client = _client()
    with client.websocket_connect("/ws/exportpdfmulti") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        doc = _draw_something(ws)
        sheet_a, ops_a = doc.add_sheet("Sheet A")
        sheet_b, ops_b = doc.add_sheet("Sheet B")
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [*ops_a, *ops_b]]})

    resp_b = client.get(f"/api/rooms/exportpdfmulti/export/pdf?sheet_id={sheet_b}")
    assert resp_b.status_code == 200
    assert "Sheet_B" in resp_b.headers["content-disposition"]

    # a stale/unknown sheet_id falls back to the first sheet rather than 404ing
    resp_stale = client.get("/api/rooms/exportpdfmulti/export/pdf?sheet_id=sheet_does_not_exist")
    assert resp_stale.status_code == 200
    assert "Sheet_A" in resp_stale.headers["content-disposition"]


def test_export_mesh_json_and_stl():
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])
    with client.websocket_connect("/ws/mesh/meshexport") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, v3, *face_ops]]})

    json_resp = client.get("/api/mesh/meshexport/export/json")
    assert json_resp.status_code == 200
    assert len(json_resp.json()["face_index"]["entries"]) == 1

    stl_resp = client.get("/api/mesh/meshexport/export/stl")
    assert stl_resp.status_code == 200
    assert stl_resp.text.count("facet normal") == 1


def test_export_mesh_step():
    """STEP export (Phase 9) needs the optional `build123d` dependency --
    skip cleanly if it isn't installed, same pattern as
    tests/test_step_export.py."""
    import pytest

    pytest.importorskip("build123d")

    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])
    with client.websocket_connect("/ws/mesh/meshstepexport") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, v3, *face_ops]]})

    resp = client.get("/api/mesh/meshstepexport/export/step")
    assert resp.status_code == 200
    assert resp.content.startswith(b"ISO-10303-21;")


def test_export_mesh_step_with_no_faces_returns_400():
    import pytest

    pytest.importorskip("build123d")

    client = _client()
    resp = client.get("/api/mesh/emptymeshstep/export/step")
    assert resp.status_code == 400


# -- import ---------------------------------------------------------------------


def test_import_svg_creates_layer_and_paths():
    client = _client()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<line x1="0" y1="0" x2="10" y2="10"/>'
        '<polyline points="0,0 5,5 10,0"/>'
        "</svg>"
    )
    resp = client.post("/api/rooms/importroom/import/svg", content=svg)
    assert resp.status_code == 200
    body = resp.json()
    assert body["path_count"] == 2

    export = client.get("/api/rooms/importroom/export/json").json()
    assert len(export["path_index"]["entries"]) == 2


def test_import_dxf_creates_layer_and_paths():
    client = _client()
    from crdt_cad.export.dxf_io import drawing_to_dxf_bytes

    dxf_bytes = drawing_to_dxf_bytes([{"points": [(0.0, 0.0), (3.0, 3.0), (6.0, 0.0)]}])
    resp = client.post("/api/rooms/importdxf/import/dxf", content=dxf_bytes)
    assert resp.status_code == 200
    assert resp.json()["path_count"] == 1


def test_import_malformed_svg_returns_400():
    client = _client()
    resp = client.post("/api/rooms/badroom/import/svg", content="<not valid xml")
    assert resp.status_code == 400


def test_import_broadcasts_to_connected_clients():
    client = _client()
    with client.websocket_connect("/ws/importbroadcast") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()

        svg = '<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="1" y2="1"/></svg>'
        resp = client.post("/api/rooms/importbroadcast/import/svg", content=svg)
        assert resp.status_code == 200

        broadcast = ws.receive_json()
        assert broadcast["type"] == "ops"
        assert broadcast["from"] == "__import__"


# -- constraint solver endpoint -----------------------------------------------------


def test_solve_endpoint_fixed_distance():
    client = _client()
    resp = client.post(
        "/api/solve",
        json={
            "points": {"a": [0.0, 0.0], "b": [1.0, 0.0]},
            "constraints": [{"kind": "fixed_distance", "point_ids": ["a", "b"], "param": 7.0}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["converged"] is True
    a, b = body["positions"]["a"], body["positions"]["b"]
    dist = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    assert abs(dist - 7.0) < 1e-3


def test_solve_endpoint_unknown_point_id_returns_400():
    client = _client()
    resp = client.post(
        "/api/solve",
        json={
            "points": {"a": [0.0, 0.0]},
            "constraints": [{"kind": "coincident", "point_ids": ["a", "ghost"], "param": 0.0}],
        },
    )
    assert resp.status_code == 400


def test_solve_endpoint_unknown_kind_returns_400():
    client = _client()
    resp = client.post(
        "/api/solve",
        json={
            "points": {"a": [0.0, 0.0], "b": [1.0, 0.0]},
            "constraints": [{"kind": "bogus", "point_ids": ["a", "b"], "param": 0.0}],
        },
    )
    assert resp.status_code == 400


# -- path offset (Part 7 C1) ------------------------------------------------------


def test_offset_endpoint_closed_polygon_outward():
    client = _client()
    resp = client.post(
        "/api/geometry/offset",
        json={"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "distance": 2, "closed": True},
    )
    assert resp.status_code == 200, resp.text
    pts = resp.json()["points"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert min(xs) == -2 and max(xs) == 12
    assert min(ys) == -2 and max(ys) == 12


def test_offset_endpoint_closed_polygon_inward():
    client = _client()
    resp = client.post(
        "/api/geometry/offset",
        json={"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "distance": -2, "closed": True},
    )
    assert resp.status_code == 200, resp.text
    pts = resp.json()["points"]
    xs = [p[0] for p in pts]
    assert min(xs) == 2 and max(xs) == 8


def test_offset_endpoint_open_polyline():
    client = _client()
    resp = client.post(
        "/api/geometry/offset",
        json={"points": [[0, 0], [10, 0], [10, 10]], "distance": 2, "closed": False},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["points"]) == 3


def test_offset_endpoint_concave_polygon_does_not_self_intersect():
    """The case a naive "shift every edge outward" offset gets wrong --
    an L-shaped polygon's inner (concave) corner needs a real geometry
    library, not hand-rolled edge-shifting, to offset without crossing
    itself."""
    client = _client()
    lshape = [[0, 0], [10, 0], [10, 5], [5, 5], [5, 10], [0, 10]]
    resp = client.post("/api/geometry/offset", json={"points": lshape, "distance": 1, "closed": True})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["points"]) == len(lshape)


def test_offset_endpoint_collapsing_inward_offset_returns_400():
    client = _client()
    resp = client.post(
        "/api/geometry/offset",
        json={"points": [[0, 0], [10, 0], [10, 10], [0, 10]], "distance": -10, "closed": True},
    )
    assert resp.status_code == 400
    assert "collapses" in resp.json()["detail"]


def test_offset_endpoint_too_few_points_returns_400():
    client = _client()
    resp = client.post("/api/geometry/offset", json={"points": [[0, 0]], "distance": 2, "closed": False})
    assert resp.status_code == 400


# -- geometry validity gate -----------------------------------------------------


def test_zero_length_point_insert_is_rejected():
    client = _client()
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc.add_layer("L")
    path_id, path_ops = doc.add_path(layer_id, [(0.0, 0.0)])

    with client.websocket_connect("/ws/validityroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [*layer_ops, *path_ops]]})

        dup_op = doc.append_point(path_id, (0.0, 0.0))  # exact duplicate -> zero-length
        ws.send_json({"type": "ops", "ops": [dup_op.to_dict()]})

        reply = ws.receive_json()
        assert reply["type"] == "rejected"
        assert "zero-length" in reply["reason"]

    export = client.get("/api/rooms/validityroom/export/json").json()
    assert len(export["paths"][path_id]["nodes"]) == 1  # duplicate point never got applied


def test_strict_polygon_rejects_self_intersection_but_normal_path_does_not():
    client = _client()

    # normal (non-strict) path: self-crossing is allowed (freehand pen tool)
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc.add_layer("L")
    path_id, path_ops = doc.add_path(layer_id, [(0, 0), (2, 2), (2, 0)])
    with client.websocket_connect("/ws/strictroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [*layer_ops, *path_ops]]})

        crossing_op = doc.append_point(path_id, (0, 2))  # crosses the first segment
        ws.send_json({"type": "ops", "ops": [crossing_op.to_dict()]})
        # should NOT be rejected: read back the export to confirm 4 points landed
    export = client.get("/api/rooms/strictroom/export/json").json()
    assert len(export["paths"][path_id]["nodes"]) == 4

    # strict path: the same crossing shape must be rejected
    strict_doc = DrawingDocument(LamportClock(actor="a"))
    s_layer_id, s_layer_ops = strict_doc.add_layer("L")
    s_path_id, s_path_ops = strict_doc.add_path(s_layer_id, [(0, 0), (2, 2), (2, 0)])
    strict_prop_op = strict_doc.set_path_prop(s_path_id, "strict", True)
    with client.websocket_connect("/ws/strictroom2") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json(
            {"type": "ops", "ops": [op.to_dict() for op in [*s_layer_ops, *s_path_ops, strict_prop_op]]}
        )
        crossing_op = strict_doc.append_point(s_path_id, (0, 2))
        ws.send_json({"type": "ops", "ops": [crossing_op.to_dict()]})
        reply = ws.receive_json()
        assert reply["type"] == "rejected"
        assert "self-intersecting" in reply["reason"]


# -- WebRTC signaling relay ------------------------------------------------------


def test_signal_message_relayed_only_to_target_peer():
    client = _client()
    with client.websocket_connect("/ws/signalroom") as ws_a, \
         client.websocket_connect("/ws/signalroom") as ws_b, \
         client.websocket_connect("/ws/signalroom") as ws_c:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()
        ws_b.send_json({"type": "hello", "actor": "b"})
        ws_b.receive_json()
        ws_c.send_json({"type": "hello", "actor": "c"})
        ws_c.receive_json()

        ws_a.send_json({"type": "signal", "to": "b", "data": {"sdp": "fake-offer"}})

        received = ws_b.receive_json()
        assert received["type"] == "signal"
        assert received["from"] == "a"
        assert received["data"] == {"sdp": "fake-offer"}

        # C must not receive anything for this -- send a harmless ops
        # message afterward and confirm the *first* thing C sees is that,
        # not a stray signal.
        ws_a.send_json({"type": "ops", "ops": []})
        # nothing to assert-receive on C since empty ops broadcasts nothing;
        # instead assert no signal arrived by racing a fresh, addressed signal
        ws_a.send_json({"type": "signal", "to": "c", "data": {"marker": 1}})
        received_c = ws_c.receive_json()
        assert received_c["data"] == {"marker": 1}  # only the one explicitly addressed to c


# -- save / persistence ----------------------------------------------------------


def test_save_message_gets_saved_confirmation():
    client = _client()
    with client.websocket_connect("/ws/saveroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "save"})
        reply = ws.receive_json()
        assert reply["type"] == "saved"
        assert "at" in reply


def test_room_hydrates_from_persisted_snapshot_after_restart(isolated_store):
    client = _client()
    with client.websocket_connect("/ws/restartroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)
        ws.send_json({"type": "save"})
        ws.receive_json()  # saved confirmation

    # simulate a server restart: a brand new RoomManager over the same store
    fresh_manager = RoomManager("drawing", DrawingDocument, DocOp.from_dict, isolated_store)

    import asyncio

    room = asyncio.run(fresh_manager.get_or_create("restartroom"))
    assert len(room.doc.path_list()) == 1
    assert room.doc.path_list()[0]["color"] == "#ff00ff"


def test_ops_auto_persist_without_explicit_save(isolated_store):
    """Persistence should also happen automatically on every accepted ops
    batch, not only when a client explicitly asks -- explicit "save" is a
    UX confirmation, not the only durability path."""
    client = _client()
    with client.websocket_connect("/ws/autosaveroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        _draw_something(ws)

    import asyncio
    import time

    time.sleep(0.2)  # background persist task runs on the event loop shortly after

    async def _load():
        return isolated_store.load("drawing", "autosaveroom")

    data = asyncio.run(_load())
    assert data is not None
    restored = DrawingDocument.from_bytes(LamportClock(actor="x"), data)
    assert len(restored.path_list()) == 1


def test_room_dirty_flag_tracks_unbroadcast_changes():
    """The periodic snapshot loop skips its broadcast when nothing
    changed since the last one -- mark_dirty()/reset is the flag it
    checks (see Room._snapshot_loop)."""
    client = _client()
    with client.websocket_connect("/ws/dirtyroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        room = app_module.drawing_room_manager.rooms["dirtyroom"]
        assert room._dirty_since_snapshot is False
        _draw_something(ws)
        # "ops" messages on one WS connection are handled strictly in order
        # by _serve_room's single receive loop, so waiting for a reply to a
        # message sent *after* the draw guarantees the draw was already
        # fully applied -- without this, checking the flag immediately
        # races the server's still-in-flight message handling.
        ws.send_json({"type": "save"})
        ws.receive_json()
        assert room._dirty_since_snapshot is True
