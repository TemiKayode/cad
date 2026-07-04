from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.mesh import MeshCRDT


def _box(mesh: MeshCRDT) -> tuple[str, str, str]:
    """A minimal 3-vertex triangle face, for tests that just need
    something extrudable / removable already in place."""
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh.add_vertex("v3", (0.0, 1.0, 0.0))
    mesh.add_face("f1", ["v1", "v2", "v3"])
    return "v1", "v2", "v3"


def test_undo_redo_vertex_create():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (1.0, 2.0, 3.0))
    assert "v1" in mesh.vertex_positions()

    mesh.undo()
    assert "v1" not in mesh.vertex_positions()

    mesh.redo()
    assert mesh.vertex_positions()["v1"] == (1.0, 2.0, 3.0)


def test_undo_redo_vertex_move_restores_previous_position_not_delete():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh.move_vertex("v1", (5.0, 5.0, 5.0))
    assert mesh.vertex_positions()["v1"] == (5.0, 5.0, 5.0)

    mesh.undo()  # undo the MOVE, not the creation
    assert mesh.vertex_positions()["v1"] == (0.0, 0.0, 0.0)  # still exists, at the old spot

    mesh.redo()
    assert mesh.vertex_positions()["v1"] == (5.0, 5.0, 5.0)


def test_undo_redo_vertex_remove():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (1.0, 2.0, 3.0))
    mesh.remove_vertex("v1")
    assert "v1" not in mesh.vertex_positions()

    mesh.undo()
    assert mesh.vertex_positions()["v1"] == (1.0, 2.0, 3.0)

    mesh.redo()
    assert "v1" not in mesh.vertex_positions()


def test_undo_redo_face_add():
    mesh = MeshCRDT(LamportClock(actor="a"))
    _box(mesh)
    assert mesh.face_loops() == {"f1": ["v1", "v2", "v3"]}

    mesh.undo()
    assert mesh.face_loops() == {}

    mesh.redo()
    # the SAME boundary comes back -- redo just re-flips face_index
    # membership, it never re-inserts RGA points (which would duplicate them)
    assert mesh.face_loops() == {"f1": ["v1", "v2", "v3"]}


def test_undo_redo_face_remove():
    mesh = MeshCRDT(LamportClock(actor="a"))
    _box(mesh)
    mesh.remove_face("f1")
    assert mesh.face_loops() == {}

    mesh.undo()
    assert mesh.face_loops() == {"f1": ["v1", "v2", "v3"]}

    mesh.redo()
    assert mesh.face_loops() == {}


def test_undo_redo_face_prop_set_with_and_without_previous_value():
    mesh = MeshCRDT(LamportClock(actor="a"))
    _box(mesh)

    mesh.set_face_prop("f1", "material", "wood")
    assert mesh.face_props_dict("f1")["material"] == "wood"
    mesh.undo()
    assert "material" not in mesh.face_props_dict("f1")  # no previous value -> undo removes it
    mesh.redo()
    assert mesh.face_props_dict("f1")["material"] == "wood"

    mesh.set_face_prop("f1", "material", "glass")
    mesh.undo()
    assert mesh.face_props_dict("f1")["material"] == "wood"  # restores the prior value
    mesh.redo()
    assert mesh.face_props_dict("f1")["material"] == "glass"


def test_undo_redo_extrude_is_one_bundled_step():
    mesh = MeshCRDT(LamportClock(actor="a"))
    _box(mesh)

    before_vertices = set(mesh.vertex_positions())
    before_faces = set(mesh.face_loops())

    mesh.extrude_face("f1", 2.0)
    after_vertices = set(mesh.vertex_positions())
    after_faces = set(mesh.face_loops())
    assert len(after_vertices) == len(before_vertices) + 3  # one new vertex per boundary vertex
    assert len(after_faces) == len(before_faces) + 4  # 3 side faces + 1 top face

    mesh.undo()  # a SINGLE undo() call removes everything the extrude created
    assert set(mesh.vertex_positions()) == before_vertices
    assert set(mesh.face_loops()) == before_faces

    mesh.redo()  # and a single redo() brings it all back
    assert set(mesh.vertex_positions()) == after_vertices
    assert set(mesh.face_loops()) == after_faces


def test_undo_extrude_does_not_clobber_concurrent_vertex_move():
    """The core promise, ported from DrawingDocument's identical test:
    undoing my own extrude must not roll back a collaborator's
    concurrent, unrelated vertex move."""
    mesh_a = MeshCRDT(LamportClock(actor="a"))
    v1, v2, v3 = _box(mesh_a)

    mesh_b = MeshCRDT(LamportClock(actor="b"))
    # replicate a's initial state onto b by replaying ops_since(empty vc)
    for op in mesh_a.ops_since(VectorClock()):
        mesh_b.apply(op)
    assert mesh_b.face_loops() == {"f1": ["v1", "v2", "v3"]}

    extrude_ops = mesh_a.extrude_face("f1", 3.0)
    for op in extrude_ops:
        mesh_b.apply(op)
    assert set(mesh_b.face_loops()) == set(mesh_a.face_loops())

    # b moves an unrelated vertex (v1, part of the original triangle, not
    # touched by the extrude's own vertex creation) while a is about to
    # undo the extrude
    move_op = mesh_b.move_vertex(v1, (9.0, 9.0, 9.0))

    # a undoes its extrude locally (removing every vertex/edge/face it
    # created) BEFORE receiving b's move
    undo_ops = mesh_a.undo()
    assert set(mesh_a.face_loops()) == {"f1"}
    for op in undo_ops:
        mesh_b.apply(op)

    # b's independent move must still have taken effect on b, and must not
    # have been reverted by a's undo once it arrives
    assert mesh_b.vertex_positions()[v1] == (9.0, 9.0, 9.0)

    # a receives b's move too -- the CRDT converges, and the concurrent
    # move survives on a as well, un-clobbered by the extrude undo
    mesh_a.apply(move_op)
    assert mesh_a.vertex_positions()[v1] == (9.0, 9.0, 9.0)
    assert set(mesh_a.face_loops()) == {"f1"}
    assert set(mesh_b.face_loops()) == {"f1"}


def test_undo_with_empty_stack_is_a_no_op():
    mesh = MeshCRDT(LamportClock(actor="a"))
    assert mesh.undo() == []
    assert mesh.redo() == []


def test_new_local_action_clears_the_redo_stack():
    mesh = MeshCRDT(LamportClock(actor="a"))
    mesh.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh.undo()
    mesh.add_vertex("v2", (1.0, 1.0, 1.0))  # a fresh action after an undo
    assert mesh.redo() == []  # the old redo (re-creating v1) is gone
