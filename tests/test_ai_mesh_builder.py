"""Tests for the shared primitive builders (Phase G1) -- every generator
assembles from these, so a winding/watertightness bug here would
silently corrupt every generator built on top of it (exactly what
happened during development: box/cylinder/cone initially had inverted
winding, caught by the ``is_volume`` check these tests pin down)."""

from crdt_cad.ai.mesh_builder import (
    MeshBuilder,
    add_box,
    add_cone,
    add_cylinder,
    add_extruded_polygon,
    add_extruded_profile_xy,
    add_torus,
    from_trimesh,
    to_trimesh,
)


def _assert_proper_solid(mesh, expected_volume=None, tol=1e-6):
    tri = to_trimesh(mesh)
    assert tri.is_watertight, "not watertight"
    assert tri.is_volume, "not a properly oriented volume (inverted/inconsistent winding)"
    assert tri.volume > 0
    if expected_volume is not None:
        assert abs(tri.volume - expected_volume) < tol


def test_box_is_a_watertight_correctly_oriented_solid_with_the_right_volume():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (2.0, 3.0, 4.0), "wood")
    _assert_proper_solid(b.mesh, expected_volume=24.0)


def test_box_at_a_nonzero_origin_has_the_same_volume():
    b = MeshBuilder()
    add_box(b, (5.0, -2.0, 1.0), (1.0, 1.0, 1.0), "wood")
    _assert_proper_solid(b.mesh, expected_volume=1.0)


def test_cylinder_is_a_watertight_correctly_oriented_solid():
    b = MeshBuilder()
    add_cylinder(b, (0.0, 0.0, 0.0), radius=1.0, height=2.0, segments=32)
    import math
    _assert_proper_solid(b.mesh, expected_volume=math.pi * 1.0**2 * 2.0, tol=0.05)


def test_cone_is_a_watertight_correctly_oriented_solid():
    b = MeshBuilder()
    add_cone(b, (0.0, 0.0, 0.0), radius=1.0, height=3.0, segments=32)
    import math
    _assert_proper_solid(b.mesh, expected_volume=(1.0 / 3.0) * math.pi * 1.0**2 * 3.0, tol=0.05)


def test_torus_is_a_watertight_correctly_oriented_solid():
    b = MeshBuilder()
    add_torus(b, (0.0, 0.0, 0.0), major_radius=2.0, minor_radius=0.5, major_segments=32, minor_segments=16)
    import math
    expected = 2 * math.pi**2 * 2.0 * 0.5**2
    _assert_proper_solid(b.mesh, expected_volume=expected, tol=0.5)


def test_extruded_polygon_convex_footprint_is_a_proper_solid():
    b = MeshBuilder()
    add_extruded_polygon(b, [(0, 0), (2, 0), (2, 3), (0, 3)], y0=0.0, height=1.5)
    _assert_proper_solid(b.mesh, expected_volume=2 * 3 * 1.5)


def test_extruded_polygon_nonconvex_l_shape_is_a_proper_solid():
    """The bug this specifically regression-tests: a naive fan
    triangulation from vertex 0 produces degenerate/incorrect triangles
    for a non-convex polygon. An L-shape (area 3) is the simplest
    non-convex case."""
    l_shape = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    b = MeshBuilder()
    add_extruded_polygon(b, l_shape, y0=0.0, height=1.0)
    _assert_proper_solid(b.mesh, expected_volume=3.0)


def test_extruded_profile_xy_nonconvex_is_a_proper_solid():
    l_shape = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    b = MeshBuilder()
    add_extruded_profile_xy(b, l_shape, z0=0.0, depth=1.0)
    _assert_proper_solid(b.mesh, expected_volume=3.0)


def test_multiple_disjoint_boxes_still_validate_as_one_watertight_mesh():
    """The property every assembly generator (table, chair, shelf,
    fence, stairs) relies on: several independently-closed solids in one
    GeneratedMesh still satisfy is_watertight (a global edge-count
    property), with no boolean union needed."""
    b = MeshBuilder()
    add_box(b, (0, 0, 0), (1, 1, 1))
    add_box(b, (5, 0, 0), (1, 1, 1))
    add_box(b, (10, 0, 0), (1, 1, 1))
    tri = to_trimesh(b.mesh)
    assert tri.is_watertight
    assert abs(tri.volume - 3.0) < 1e-9


def test_to_trimesh_and_from_trimesh_round_trip_preserves_volume():
    b = MeshBuilder()
    add_box(b, (0, 0, 0), (2, 2, 2), "wood")
    tri = to_trimesh(b.mesh)
    roundtripped = from_trimesh(tri, "wood")
    tri2 = to_trimesh(roundtripped)
    assert abs(tri.volume - tri2.volume) < 1e-9
    assert all(m == "wood" for m in roundtripped.face_materials.values())


def test_merge_generated_preserves_materials_and_applies_remap():
    from crdt_cad.ai.mesh_types import GeneratedMesh

    source = GeneratedMesh(
        vertices={"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)},
        faces={"f1": ["a", "b", "c"]},
        face_materials={"f1": "stone"},
    )
    b = MeshBuilder()
    b.merge_generated(source, remap=lambda p: (p[0] + 10, p[1], p[2]))
    assert set(b.mesh.vertices.values()) == {(10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (10.0, 1.0, 0.0)}
    assert list(b.mesh.face_materials.values()) == ["stone"]
