from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.procedural_house import build_house_mesh


def test_single_bedroom_single_floor_is_a_closed_box():
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    assert len(mesh.vertices) == 8  # 4 floor corners + 4 ceiling corners
    assert len(mesh.faces) == 6  # floor, roof, 4 exterior walls -- a box
    assert all(len(loop) == 4 for loop in mesh.faces.values())


def test_four_bedrooms_produces_expected_topology():
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    # 2x2 room grid -> 3x3 grid points, floor + ceiling
    assert len(mesh.vertices) == 3 * 3 * 2
    # 1 floor + 1 roof + 8 exterior walls (perimeter of a 2x2 grid) + 4 interior partitions
    assert len(mesh.faces) == 1 + 1 + 8 + 4


def test_every_face_references_only_real_vertices():
    mesh = build_house_mesh(HouseSpec(bedrooms=6, floors=2))
    for face_id, loop in mesh.faces.items():
        assert len(loop) >= 3, f"{face_id} is degenerate"
        assert len(set(loop)) == len(loop), f"{face_id} repeats a vertex"
        for vid in loop:
            assert vid in mesh.vertices


def test_no_zero_length_edges_within_any_face():
    mesh = build_house_mesh(HouseSpec(bedrooms=9, floors=1))
    for face_id, loop in mesh.faces.items():
        pts = [mesh.vertices[v] for v in loop]
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            dist_sq = sum((a[k] - b[k]) ** 2 for k in range(3))
            assert dist_sq > 1e-9, f"{face_id} has a zero-length edge"


def test_every_face_is_planar_axis_aligned():
    """All faces here are horizontal (constant Y) or vertical (constant
    X or constant Z) quads by construction -- a real geometric sanity
    check, not just a structural one."""
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    for face_id, loop in mesh.faces.items():
        pts = [mesh.vertices[v] for v in loop]
        xs = {round(p[0], 9) for p in pts}
        ys = {round(p[1], 9) for p in pts}
        zs = {round(p[2], 9) for p in pts}
        assert len(xs) == 1 or len(ys) == 1 or len(zs) == 1, f"{face_id} is not axis-planar"


def test_multi_floor_stacks_vertically_without_overlap():
    spec = HouseSpec(bedrooms=1, floors=3, wall_height_m=3.0)
    mesh = build_house_mesh(spec)
    distinct_ys = sorted({round(p[1], 6) for p in mesh.vertices.values()})
    assert distinct_ys == [0.0, 3.0, 6.0, 9.0]  # floor 0/1/2 boundaries + roof


def test_floor_material_is_applied_to_every_floor_slab():
    spec = HouseSpec(bedrooms=1, floors=2, floor_material="oak")
    mesh = build_house_mesh(spec)
    floor_materials = [m for m in mesh.face_materials.values() if m == "oak"]
    assert len(floor_materials) == 2  # one floor slab per level


def test_interior_walls_present_only_when_multiple_rooms():
    single = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    multi = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    assert "interior_wall" not in single.face_materials.values()
    assert "interior_wall" in multi.face_materials.values()


def test_triangle_count_matches_fan_triangulation_of_quads():
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    assert mesh.triangle_count() == len(mesh.faces) * 2  # every face here is a quad -> 2 triangles
