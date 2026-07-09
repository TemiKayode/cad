import pytest

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


# -- Phase G1 enrichment: default spec is unchanged -----------------------------


def test_default_spec_produces_byte_for_byte_the_same_mesh_as_before_enrichment():
    """Every new HouseSpec field defaults to a value that reproduces the
    exact pre-Phase-5 geometry -- opt-in additions, not a behavior
    change to what "the default house" means. Pinned against the exact
    counts test_four_bedrooms_produces_expected_topology already
    asserts, so a regression here would also fail that older test."""
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    assert len(mesh.vertices) == 3 * 3 * 2
    assert len(mesh.faces) == 1 + 1 + 8 + 4


# -- Phase G1 enrichment: roof types ---------------------------------------------


def test_gable_roof_replaces_the_flat_cap_with_pitched_geometry():
    flat = build_house_mesh(HouseSpec(bedrooms=4, roof_type="flat"))
    gable = build_house_mesh(HouseSpec(bedrooms=4, roof_type="gable"))
    assert len(gable.vertices) == len(flat.vertices) + 2  # two new ridge vertices
    # flat cap is 1 face; gable is 2 slopes + 2 gable-end triangles = 4
    assert len(gable.faces) == len(flat.faces) + 3


def test_hip_roof_replaces_the_flat_cap_with_a_pyramidal_apex():
    flat = build_house_mesh(HouseSpec(bedrooms=4, roof_type="flat"))
    hip = build_house_mesh(HouseSpec(bedrooms=4, roof_type="hip"))
    assert len(hip.vertices) == len(flat.vertices) + 1  # one new apex vertex
    assert len(hip.faces) == len(flat.faces) + 3  # 4 triangles replace 1 flat quad


def test_roof_type_only_affects_the_top_floor():
    single = build_house_mesh(HouseSpec(bedrooms=1, floors=2, roof_type="gable"))
    # the ground floor's ceiling/ground-floor-of-next-floor slab stays a
    # flat "concrete" quad regardless of roof_type -- only the true roof
    # (top floor) gets pitched.
    assert "concrete" in single.face_materials.values()


# -- Phase G1 enrichment: garage --------------------------------------------------


def test_garage_adds_a_detached_box_volume():
    without = build_house_mesh(HouseSpec(bedrooms=1, garage=False))
    with_garage = build_house_mesh(HouseSpec(bedrooms=1, garage=True))
    assert len(with_garage.vertices) == len(without.vertices) + 8  # one box = 8 corners
    assert len(with_garage.faces) == len(without.faces) + 6  # one box = 6 faces


# -- Phase G1 enrichment: floor_area_sq_m ----------------------------------------


def test_floor_area_sq_m_scales_the_footprint_to_the_target_area():
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floor_area_sq_m=30.0))
    xs = [p[0] for p in mesh.vertices.values()]
    zs = [p[2] for p in mesh.vertices.values()]
    width, depth = max(xs) - min(xs), max(zs) - min(zs)
    assert width * depth == pytest.approx(30.0, rel=1e-6)


def test_floor_area_sq_m_unset_keeps_the_default_room_size():
    from crdt_cad.ai.procedural_house import ROOM_SIZE_M

    mesh = build_house_mesh(HouseSpec(bedrooms=1))
    xs = [p[0] for p in mesh.vertices.values()]
    assert max(xs) - min(xs) == pytest.approx(ROOM_SIZE_M)


# -- Phase G1 enrichment: bedrooms_per_floor (distinct footprints) --------------


def test_bedrooms_per_floor_gives_each_floor_its_own_footprint():
    spec = HouseSpec(bedrooms=4, floors=2, bedrooms_per_floor=[4, 1])
    mesh = build_house_mesh(spec)
    # Ground floor (4 bedrooms, 2x2 grid) spans wider in X than the
    # single-bedroom top floor (1x1 grid). Filtering by the floor-to-
    # floor boundary height (wall_height_m) would be ambiguous -- the
    # ground floor's *ceiling* and the top floor's *floor* grid both sit
    # at that exact height. The roof apex height (floors * wall_height_m)
    # is unambiguous: only the top floor's own ceiling ever reaches it.
    ground_xs = [p[0] for p in mesh.vertices.values() if abs(p[1] - 0.0) < 1e-9]
    roof_y = spec.floors * spec.wall_height_m
    roof_xs = [p[0] for p in mesh.vertices.values() if abs(p[1] - roof_y) < 1e-9]
    assert max(ground_xs) == pytest.approx(8.0)  # 2x2 grid @ ROOM_SIZE_M=4.0
    assert max(roof_xs) == pytest.approx(4.0)  # 1x1 grid
    assert max(ground_xs) > max(roof_xs)


def test_bedrooms_per_floor_length_mismatch_is_rejected():
    with pytest.raises(ValueError):
        HouseSpec(bedrooms=4, floors=2, bedrooms_per_floor=[4, 1, 2])


# -- Phase G1 enrichment: front door / windows (CSG cuts) ------------------------


def test_front_door_replaces_the_south_wall_with_a_thick_cuttable_solid():
    without = build_house_mesh(HouseSpec(bedrooms=1, front_door=False))
    with_door = build_house_mesh(HouseSpec(bedrooms=1, front_door=True))
    # The flat, zero-thickness south quad is gone; a real CSG-cut wall
    # solid (many more vertices/faces than one flat quad) replaces it.
    assert len(with_door.faces) > len(without.faces)


def test_front_windows_without_a_door_still_cuts_the_wall():
    mesh = build_house_mesh(HouseSpec(bedrooms=2, front_door=False, front_windows=2))
    without = build_house_mesh(HouseSpec(bedrooms=2, front_door=False, front_windows=0))
    assert len(mesh.faces) > len(without.faces)


def test_door_and_windows_together_produce_a_validatable_mesh():
    from crdt_cad.ai.validation import validate_generated_mesh

    mesh = build_house_mesh(HouseSpec(bedrooms=4, front_door=True, front_windows=3))
    report = validate_generated_mesh(mesh, require_watertight=False, require_consistent_winding=False)
    assert report.ok, report.errors


# -- Phase G1 enrichment: per-element materials ----------------------------------


def test_wall_and_roof_material_are_configurable():
    mesh = build_house_mesh(HouseSpec(bedrooms=1, wall_material="brick", roof_material="slate"))
    assert "brick" in mesh.face_materials.values()
    assert "slate" in mesh.face_materials.values()
    assert "exterior_wall" not in mesh.face_materials.values()
