"""Server-level (WS) integration tests for Phase 6's validity_warning
broadcast -- crdt_cad.geometry.mesh_validity itself is unit-tested in
tests/test_mesh_validity.py; this file confirms app.py actually wires it
up: runs after face-topology-touching ops, broadcasts a
`validity_warning` to the room, and is skipped entirely for ops that
can't create this class of problem.
"""

from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT
from crdt_cad.server.app import app

# `isolated_store` fixture (autouse) lives in tests/conftest.py and applies here too.


def _client() -> TestClient:
    return TestClient(app)


def test_valid_face_creation_triggers_no_validity_warning():
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])
    with client.websocket_connect("/ws/mesh/validroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, v3, *face_ops]]})

        # synchronize: if a spurious validity_warning had been (wrongly)
        # queued, it would arrive before this "saved" reply
        ws.send_json({"type": "save"})
        reply = ws.receive_json()
        assert reply["type"] == "saved"


def test_creating_a_face_referencing_a_missing_vertex_triggers_a_validity_warning():
    """Simulates the merged result of a concurrent edit elsewhere having
    already removed a vertex this face's boundary references -- the
    op message itself is exactly a "face_geom" touch, so the check
    is guaranteed to run against the (already-inconsistent) merged state."""
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    # "v3" is never created here -- as if a concurrent edit elsewhere had
    # already deleted it before this face's boundary op arrived.
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])

    with client.websocket_connect("/ws/mesh/brokenroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, *face_ops]]})

        warning = ws.receive_json()
        assert warning["type"] == "validity_warning"
        assert warning["faces"] == ["f1"]
        assert any("fewer than 3 live vertices" in p["problem"] for p in warning["problems"])


def test_deleting_a_boundary_vertex_alone_triggers_a_validity_warning():
    """The other half of the "Extrusion Nightmare": deleting a face's
    boundary vertex is a `target == "vertex"` op with no accompanying
    face_index/face_geom op in the same message -- it must still trigger
    the check (see `_touches_mesh_topology` in app.py), otherwise this
    exact class of problem would silently go unflagged whenever the
    deletion doesn't happen to land in the same batch as some unrelated
    face-topology edit."""
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])
    with client.websocket_connect("/ws/mesh/deleteboundaryroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, v3, *face_ops]]})
        ws.send_json({"type": "save"})
        ws.receive_json()  # confirms the face-creation batch is fully settled first

        remove_op = mesh.remove_vertex("v3")
        ws.send_json({"type": "ops", "ops": [remove_op.to_dict()]})

        warning = ws.receive_json()
        assert warning["type"] == "validity_warning"
        assert warning["faces"] == ["f1"]
        assert any("fewer than 3 live vertices" in p["problem"] for p in warning["problems"])


def test_pure_vertex_move_never_triggers_a_validity_check():
    """A plain vertex reposition can't create a face-topology problem on
    its own -- confirm the check is skipped entirely (not just
    "ran and found nothing"), via the same follow-up-message
    synchronization trick used elsewhere in this suite."""
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    v1 = mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    v2 = mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    v3 = mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh.add_face("f1", ["v1", "v2", "v3"])
    with client.websocket_connect("/ws/mesh/moveonlyroom") as ws:
        ws.send_json({"type": "hello", "actor": "a"})
        ws.receive_json()
        ws.send_json({"type": "ops", "ops": [op.to_dict() for op in [v1, v2, v3, *face_ops]]})
        ws.send_json({"type": "save"})
        ws.receive_json()

        move_op = mesh.move_vertex("v1", (5.0, 5.0, 5.0))
        ws.send_json({"type": "ops", "ops": [move_op.to_dict()]})
        ws.send_json({"type": "save"})
        reply = ws.receive_json()
        assert reply["type"] == "saved"  # nothing else was queued ahead of it
