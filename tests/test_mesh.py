from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp, canonical_edge


def test_vertex_add_move_remove():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    assert mesh.vertex_positions()["v1"] == (0.0, 0.0, 0.0)
    mesh.move_vertex("v1", (1.0, 2.0, 3.0))
    assert mesh.vertex_positions()["v1"] == (1.0, 2.0, 3.0)
    mesh.remove_vertex("v1")
    assert "v1" not in mesh.vertex_positions()


def test_edge_is_order_independent():
    assert canonical_edge("v1", "v2") == canonical_edge("v2", "v1")

    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_edge("v2", "v1")
    assert ("v1", "v2") in mesh.edge_set()
    mesh.remove_edge("v1", "v2")
    assert mesh.edge_set() == set()


def test_face_add_and_read_loop():
    mesh = MeshCRDT(LamportClock(actor="a"))
    for vid in ("v1", "v2", "v3"):
        mesh.add_vertex(vid, (0.0, 0.0, 0.0))
    mesh.add_face("f1", ["v1", "v2", "v3"])
    assert mesh.face_loops() == {"f1": ["v1", "v2", "v3"]}

    mesh.remove_face("f1")
    assert mesh.face_loops() == {}


def test_concurrent_face_boundary_edits_converge():
    """Two collaborators, both offline, split different edges of the same
    face by inserting a new vertex into its boundary loop. After they come
    back online and merge, both must see an identical, valid boundary."""
    seed_clock = LamportClock(actor="seed")
    seed = MeshCRDT(seed_clock)
    for vid in ("v1", "v2", "v3"):
        seed.add_vertex(vid, (0.0, 0.0, 0.0))
    face_ops = seed.add_face("f1", ["v1", "v2", "v3"])

    mesh_a = MeshCRDT(LamportClock(actor="a"))
    mesh_b = MeshCRDT(LamportClock(actor="b"))
    for op in face_ops:
        mesh_a.apply(op)
        mesh_b.apply(op)

    # find the RGA op-id of v1 and v2 in the face boundary to anchor inserts
    boundary_ids = [op.payload["id"] for op in face_ops if op.target == "face_geom"]
    v1_id, v2_id = boundary_ids[0], boundary_ids[1]
    from crdt_cad.crdt.serialize import op_id_from_wire

    mesh_a.add_vertex("v1_5", (0.5, 0.0, 0.0))
    op_a = mesh_a.insert_face_vertex("f1", op_id_from_wire(v1_id), "v1_5")

    mesh_b.add_vertex("v2_5", (0.0, 0.5, 0.0))
    op_b = mesh_b.insert_face_vertex("f1", op_id_from_wire(v2_id), "v2_5")

    mesh_a.apply(op_b)
    mesh_b.apply(op_a)

    assert mesh_a.face_loops() == mesh_b.face_loops()
    assert mesh_a.face_loops()["f1"] == ["v1", "v1_5", "v2", "v2_5", "v3"]


def test_mesh_state_merge_converges_independent_of_order():
    mesh_a = MeshCRDT(LamportClock(actor="a"))
    mesh_a.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh_a.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh_a.add_edge("v1", "v2")
    mesh_a.add_face("f1", ["v1", "v2"])

    mesh_b = MeshCRDT(LamportClock(actor="b"))
    mesh_b.add_vertex("v3", (0.0, 1.0, 0.0))
    mesh_b.add_edge("v1", "v3")

    left = MeshCRDT(LamportClock(actor="m1"))
    left.merge(mesh_a)
    left.merge(mesh_b)

    right = MeshCRDT(LamportClock(actor="m2"))
    right.merge(mesh_b)
    right.merge(mesh_a)

    assert left.vertex_positions() == right.vertex_positions()
    assert left.edge_set() == right.edge_set()
    assert left.face_loops() == right.face_loops()


def test_mesh_op_wire_roundtrip_via_dict():
    mesh = MeshCRDT(LamportClock(actor="a"))
    op = mesh.add_vertex("v1", (1.0, 2.0, 3.0))
    wire = op.to_dict()
    restored_op = MeshOp.from_dict(wire)

    receiver = MeshCRDT(LamportClock(actor="b"))
    receiver.apply(restored_op)
    assert receiver.vertex_positions()["v1"] == [1.0, 2.0, 3.0] or receiver.vertex_positions()["v1"] == (1.0, 2.0, 3.0)


def test_mesh_serialization_roundtrip():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh.add_edge("v1", "v2")
    mesh.add_face("f1", ["v1", "v2"])

    restored = MeshCRDT.from_bytes(LamportClock(actor="b"), mesh.to_bytes())
    assert list(restored.vertex_positions().keys()) == list(mesh.vertex_positions().keys())
    assert restored.edge_set() == mesh.edge_set()
    assert restored.face_loops() == mesh.face_loops()


def test_mesh_ops_since_delta_sync():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    frontier = mesh.frontier()
    mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh.add_edge("v1", "v2")

    delta = mesh.ops_since(frontier)
    targets = sorted(op.target for op in delta)
    assert targets == ["edge", "vertex"]


def test_face_prop_set_and_read():
    mesh = MeshCRDT(LamportClock(actor="a"))
    for vid in ("v1", "v2", "v3"):
        mesh.add_vertex(vid, (0.0, 0.0, 0.0))
    mesh.add_face("f1", ["v1", "v2", "v3"])
    mesh.set_face_prop("f1", "material", "wood")
    mesh.set_face_prop("f1", "color", "#8b5a2b")
    assert mesh.face_props_dict("f1") == {"material": "wood", "color": "#8b5a2b"}


def test_face_prop_syncs_between_replicas():
    mesh_a = MeshCRDT(LamportClock(actor="a"))
    mesh_a.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh_a.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh_a.add_vertex("v3", (0.0, 1.0, 0.0))
    face_ops = mesh_a.add_face("f1", ["v1", "v2", "v3"])
    prop_op = mesh_a.set_face_prop("f1", "material", "wood")

    mesh_b = MeshCRDT(LamportClock(actor="b"))
    for op in [*face_ops, prop_op]:
        mesh_b.apply(op)
    assert mesh_b.face_props_dict("f1") == {"material": "wood"}


def test_face_prop_merge_and_serialization_roundtrip():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh.add_face("f1", ["v1", "v2"])
    mesh.set_face_prop("f1", "material", "wood")

    other = MeshCRDT(LamportClock(actor="b"))
    other.merge(mesh)
    assert other.face_props_dict("f1") == {"material": "wood"}

    restored = MeshCRDT.from_bytes(LamportClock(actor="c"), mesh.to_bytes())
    assert restored.face_props_dict("f1") == {"material": "wood"}
