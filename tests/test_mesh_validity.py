from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.mesh import MeshCRDT
from crdt_cad.geometry.mesh_validity import check_mesh_validity


def test_empty_mesh_has_no_problems():
    assert check_mesh_validity({}, {}) == []


def test_single_valid_triangle_has_no_problems():
    verts = {"v1": (0.0, 0.0, 0.0), "v2": (1.0, 0.0, 0.0), "v3": (0.0, 1.0, 0.0)}
    faces = {"f1": ["v1", "v2", "v3"]}
    assert check_mesh_validity(verts, faces) == []


def test_valid_box_has_no_problems():
    """A real, complete, watertight box -- the case that must stay clean
    even though it's the most "complex" valid input this test file has."""
    verts = {
        "v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (1.0, 1.0, 0.0), "v3": (0.0, 1.0, 0.0),
        "v4": (0.0, 0.0, 1.0), "v5": (1.0, 0.0, 1.0), "v6": (1.0, 1.0, 1.0), "v7": (0.0, 1.0, 1.0),
    }
    faces = {
        "bottom": ["v0", "v3", "v2", "v1"],
        "top": ["v4", "v5", "v6", "v7"],
        "front": ["v0", "v1", "v5", "v4"],
        "back": ["v3", "v7", "v6", "v2"],
        "left": ["v0", "v4", "v7", "v3"],
        "right": ["v1", "v2", "v6", "v5"],
    }
    assert check_mesh_validity(verts, faces) == []


def test_incomplete_wip_mesh_is_not_flagged_just_for_being_unfinished():
    """A lone triangle floating in space is not watertight and never will
    be until deliberately completed -- this must NOT be treated as a
    problem on its own (see the module docstring's rationale)."""
    verts = {"v1": (0.0, 0.0, 0.0), "v2": (5.0, 0.0, 0.0), "v3": (0.0, 5.0, 0.0)}
    faces = {"floor": ["v1", "v2", "v3"]}
    assert check_mesh_validity(verts, faces) == []


def test_degenerate_face_zero_area_is_flagged():
    verts = {"v1": (0.0, 0.0, 0.0), "v2": (0.0, 0.0, 0.0), "v3": (1.0, 0.0, 0.0)}  # v1 == v2
    faces = {"f1": ["v1", "v2", "v3"]}
    problems = check_mesh_validity(verts, faces)
    assert len(problems) == 1
    assert problems[0]["faces"] == ["f1"]
    assert "degenerate" in problems[0]["problem"]


def test_degenerate_face_collinear_points_is_flagged():
    verts = {"v1": (0.0, 0.0, 0.0), "v2": (1.0, 0.0, 0.0), "v3": (2.0, 0.0, 0.0)}  # all on one line
    faces = {"f1": ["v1", "v2", "v3"]}
    problems = check_mesh_validity(verts, faces)
    assert len(problems) == 1
    assert "degenerate" in problems[0]["problem"]


def test_face_referencing_a_deleted_vertex_is_flagged_directly():
    """The "Extrusion Nightmare" shape: a face's boundary references a
    vertex that a concurrent edit deleted -- the merge itself never
    breaks (each sub-CRDT converges fine on its own terms), but the
    resulting cross-component state is nonsense and should be surfaced."""
    verts = {"v1": (0.0, 0.0, 0.0), "v2": (1.0, 0.0, 0.0)}  # v3 doesn't exist
    faces = {"f1": ["v1", "v2", "v3"]}
    problems = check_mesh_validity(verts, faces)
    assert len(problems) == 1
    assert problems[0]["faces"] == ["f1"]
    assert "fewer than 3 live vertices" in problems[0]["problem"]


def test_nonmanifold_edge_shared_by_three_faces_is_flagged():
    verts = {
        "v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (0.0, 1.0, 0.0),
        "v3": (1.0, 1.0, 0.0), "v4": (0.0, 0.0, 1.0),
    }
    # all three faces share the edge (v1, v2)
    faces = {
        "f1": ["v0", "v1", "v2"],
        "f2": ["v1", "v3", "v2"],
        "f3": ["v1", "v4", "v2"],
    }
    problems = check_mesh_validity(verts, faces)
    non_manifold = [p for p in problems if "non-manifold" in p["problem"]]
    assert len(non_manifold) == 1
    assert set(non_manifold[0]["faces"]) == {"f1", "f2", "f3"}


def test_inconsistent_winding_between_adjacent_faces_is_flagged():
    verts = {"v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (0.0, 1.0, 0.0), "v3": (1.0, 1.0, 0.0)}
    # f1 traverses shared edge (v1,v2) as v1->v2; f2 ALSO traverses it as
    # v1->v2 (should be v2->v1 for consistent outward winding) -- this is
    # exactly the shape the "extrusion nightmare" produces: one face's
    # boundary got edited independently of its neighbor's.
    faces = {
        "f1": ["v0", "v1", "v2"],
        "f2": ["v1", "v2", "v3"],
    }
    problems = check_mesh_validity(verts, faces)
    winding = [p for p in problems if "winding" in p["problem"]]
    assert len(winding) == 1
    assert set(winding[0]["faces"]) == {"f1", "f2"}


def test_consistent_winding_between_adjacent_faces_is_not_flagged():
    verts = {"v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (0.0, 1.0, 0.0), "v3": (1.0, 1.0, 0.0)}
    faces = {
        "f1": ["v0", "v1", "v2"],  # traverses shared edge (v1,v2) as v1->v2
        "f2": ["v1", "v3", "v2"],  # traverses shared edge as v2->v1 (opposite) -- correct
    }
    assert check_mesh_validity(verts, faces) == []


def test_the_extrusion_nightmare_end_to_end_via_real_concurrent_merge():
    """The actual scenario the architecture critique raised, exercised
    through real MeshCRDT replicas and a real merge -- not just hand-built
    dicts. Replica A extrudes a face; concurrently, replica B deletes one
    of that same face's boundary vertices (e.g. reshaping it). Each
    sub-CRDT merges perfectly correctly on its own terms (that's the
    point: this is NOT a bug in the CRDT merge itself), but the combined
    result is cross-component nonsense -- exactly what this checker
    exists to surface, since the merge can't be rejected without breaking
    convergence."""
    clock_a = LamportClock(actor="a")
    mesh_a = MeshCRDT(clock_a)
    mesh_a.add_vertex("v1", (0.0, 0.0, 0.0))
    mesh_a.add_vertex("v2", (1.0, 0.0, 0.0))
    mesh_a.add_vertex("v3", (0.0, 1.0, 0.0))
    mesh_a.add_face("f1", ["v1", "v2", "v3"])

    clock_b = LamportClock(actor="b")
    mesh_b = MeshCRDT(clock_b)
    for op in mesh_a.ops_since(VectorClock()):  # replicate a's initial state onto b
        mesh_b.apply(op)
    assert mesh_b.face_loops() == {"f1": ["v1", "v2", "v3"]}
    assert check_mesh_validity(mesh_b.vertex_positions(), mesh_b.face_loops()) == []

    # a extrudes f1 (new vertices/edges/side-faces/top-face, all valid on
    # their own)
    extrude_ops = mesh_a.extrude_face("f1", 2.0)

    # concurrently, b deletes v3 -- a boundary vertex of the ORIGINAL
    # (still-existing) face f1, unaware of a's extrude
    remove_op = mesh_b.remove_vertex("v3")

    # both replicas receive the other's ops -- the merge itself never
    # fails or throws for either side
    for op in extrude_ops:
        mesh_b.apply(op)
    mesh_a.apply(remove_op)

    assert mesh_a.vertex_positions() == mesh_b.vertex_positions()
    assert mesh_a.face_loops() == mesh_b.face_loops()

    # f1's boundary RGA still lists v3 (RGA elements are never deleted,
    # only tombstoned in the *separate* vertices LWWMap -- see
    # crdt/rga.py) -- so f1 now references a vertex with no live position.
    # Each sub-CRDT converged perfectly; the cross-component result is
    # exactly the "Extrusion Nightmare" this checker exists to catch.
    problems = check_mesh_validity(mesh_a.vertex_positions(), mesh_a.face_loops())
    assert any(p["faces"] == ["f1"] and "fewer than 3 live vertices" in p["problem"] for p in problems)
