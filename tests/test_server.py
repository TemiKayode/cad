from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DocOp, DrawingDocument
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp
from crdt_cad.server.app import app, mesh_room_manager, room_manager


def _fresh_client() -> TestClient:
    room_manager.rooms.clear()
    mesh_room_manager.rooms.clear()
    return TestClient(app)


def test_health_endpoint():
    client = _fresh_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_metrics_endpoint_exposes_prometheus_text():
    client = _fresh_client()
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "crdt_cad_rooms" in resp.text


def test_new_client_receives_full_snapshot_on_connect():
    client = _fresh_client()
    with client.websocket_connect("/ws/room1") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert msg["doc"]["layers"]["entries"] == []


def test_ops_from_one_client_broadcast_to_another():
    client = _fresh_client()
    doc_a = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc_a.add_layer("Shared Layer")

    with client.websocket_connect("/ws/room2") as ws_a, client.websocket_connect("/ws/room2") as ws_b:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()  # snapshot
        ws_b.send_json({"type": "hello", "actor": "b"})
        ws_b.receive_json()  # snapshot

        ws_a.send_json({"type": "ops", "ops": [op.to_dict() for op in layer_ops]})

        broadcast = ws_b.receive_json()
        assert broadcast["type"] == "ops"
        assert broadcast["from"] == "a"

        doc_b = DrawingDocument(LamportClock(actor="b"))
        for op_dict in broadcast["ops"]:
            doc_b.apply(DocOp.from_dict(op_dict))
        assert doc_b.layer_list()[0]["name"] == "Shared Layer"


def test_reconnect_with_known_frontier_receives_only_the_delta():
    client = _fresh_client()
    room = "room3"

    doc_a = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc_a.add_layer("L")

    # A connects, publishes the layer, then disconnects ("goes offline").
    with client.websocket_connect(f"/ws/{room}") as ws_a:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()  # initial empty snapshot
        ws_a.send_json({"type": "ops", "ops": [op.to_dict() for op in layer_ops]})
        frontier_before_disconnect = doc_a.frontier().to_dict()

    # B connects while A is offline and adds a path to that layer.
    doc_b = DrawingDocument(LamportClock(actor="b"))
    with client.websocket_connect(f"/ws/{room}") as ws_b:
        ws_b.send_json({"type": "hello", "actor": "b"})
        snapshot = ws_b.receive_json()
        assert snapshot["type"] == "snapshot"
        for entry in snapshot["doc"]["layers"]["entries"]:
            pass  # sanity: layer already present server-side
        assert layer_id in [e["k"] for e in snapshot["doc"]["layers"]["entries"]]

        path_id, path_ops = doc_b.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)], color="#00ff00")
        ws_b.send_json({"type": "ops", "ops": [op.to_dict() for op in path_ops]})

    # A reconnects with its last-known frontier and must get exactly the delta
    # (B's path), not a full resend of its own layer_add.
    with client.websocket_connect(f"/ws/{room}") as ws_a:
        ws_a.send_json({"type": "hello", "actor": "a", "known_frontier": frontier_before_disconnect})
        reply = ws_a.receive_json()
        assert reply["type"] == "delta"

        delta_ops = [DocOp.from_dict(o) for o in reply["ops"]]
        for op in delta_ops:
            doc_a.apply(op)

        assert [list(p) for p in doc_a.path_list()[0]["points"]] == [[0.0, 0.0], [1.0, 1.0]]
        assert doc_a.path_list()[0]["color"] == "#00ff00"

        # the delta must not have needed to resend the layer op A already had
        assert not any(op.target == "layer" for op in delta_ops)


def test_mesh_room_broadcasts_vertex_and_face_ops_between_clients():
    client = _fresh_client()
    mesh_a = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh_a.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh_a.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh_a.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh_a.add_face("f1", ["v1", "v2", "v3"])
    ops = [v1, v2, v3, *face_ops]

    with client.websocket_connect("/ws/mesh/room1") as ws_a, client.websocket_connect("/ws/mesh/room1") as ws_b:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()
        ws_b.send_json({"type": "hello", "actor": "b"})
        ws_b.receive_json()

        ws_a.send_json({"type": "ops", "ops": [op.to_dict() for op in ops]})
        broadcast = ws_b.receive_json()
        assert broadcast["type"] == "ops"

        mesh_b = MeshCRDT(LamportClock(actor="b"))
        for op_dict in broadcast["ops"]:
            mesh_b.apply(MeshOp.from_dict(op_dict))

        assert mesh_b.face_loops() == {"f1": ["v1", "v2", "v3"]}
        assert mesh_b.vertex_positions()["v2"] == [1.0, 0.0, 0.0]


def test_mesh_room_reconnect_delta_after_offline_edit():
    client = _fresh_client()
    room = "mesh-room2"

    mesh_a = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh_a.add_vertex("v1", (0.0, 0.0, 0.0))

    with client.websocket_connect(f"/ws/mesh/{room}") as ws_a:
        ws_a.send_json({"type": "hello", "actor": "a"})
        ws_a.receive_json()
        ws_a.send_json({"type": "ops", "ops": [v1.to_dict()]})
        frontier_before_disconnect = mesh_a.frontier().to_dict()

    mesh_b = MeshCRDT(LamportClock(actor="b"))
    with client.websocket_connect(f"/ws/mesh/{room}") as ws_b:
        ws_b.send_json({"type": "hello", "actor": "b"})
        ws_b.receive_json()
        v2 = mesh_b.add_vertex("v2", (2.0, 0.0, 0.0))
        edge_op = mesh_b.add_edge("v1", "v2")
        ws_b.send_json({"type": "ops", "ops": [v2.to_dict(), edge_op.to_dict()]})

    with client.websocket_connect(f"/ws/mesh/{room}") as ws_a:
        ws_a.send_json({"type": "hello", "actor": "a", "known_frontier": frontier_before_disconnect})
        reply = ws_a.receive_json()
        assert reply["type"] == "delta"

        for op_dict in reply["ops"]:
            mesh_a.apply(MeshOp.from_dict(op_dict))

        assert mesh_a.vertex_positions()["v2"] == [2.0, 0.0, 0.0]
        assert ("v1", "v2") in mesh_a.edge_set()
        assert not any(op.target == "vertex" and op.payload.get("k") == "v1" for op in [MeshOp.from_dict(o) for o in reply["ops"]])
