"""Per-generator tests (Phase G1) -- every generator introduced this
phase gets the same invariant tests the house generator already had
(no duplicate vertices, no zero-length/degenerate edges, no orphan
vertex references) *plus* the stricter bar new generators are held to:
genuinely watertight, correctly-oriented solids (see validation.py's
docstring for why the house generator itself is exempted)."""

import pytest

from crdt_cad.ai import REGISTRY  # noqa: F401 -- triggers registration
from crdt_cad.ai.generators.architectural import ArchSpec, ColumnSpec, FenceSpec, StairsSpec, build_arch, build_column, build_fence, build_stairs
from crdt_cad.ai.generators.furniture import ChairSpec, ShelfSpec, TableSpec, build_chair, build_shelf, build_table
from crdt_cad.ai.generators.primitives import BoxSpec, ConeSpec, CylinderSpec, TorusSpec, build_box, build_cone, build_cylinder, build_torus
from crdt_cad.ai.generators.wall_opening import DoorSpec, WindowSpec, build_door, build_window
from crdt_cad.ai.validation import validate_generated_mesh


def _common_invariants(mesh):
    """The same checks test_procedural_house.py already runs on the
    house generator, applied uniformly to every new generator."""
    for face_id, loop in mesh.faces.items():
        assert len(loop) >= 3, f"{face_id} is degenerate"
        assert len(set(loop)) == len(loop), f"{face_id} repeats a vertex"
        for vid in loop:
            assert vid in mesh.vertices, f"{face_id} references missing vertex {vid}"
        pts = [mesh.vertices[v] for v in loop]
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            dist_sq = sum((a[k] - b[k]) ** 2 for k in range(3))
            assert dist_sq > 1e-12, f"{face_id} has a zero-length edge"


@pytest.mark.parametrize("name", sorted(REGISTRY.keys() - {"house"}))
def test_every_non_house_generator_default_spec_is_watertight_and_valid(name):
    entry = REGISTRY[name]
    mesh = entry.build(entry.spec_model())
    _common_invariants(mesh)
    report = validate_generated_mesh(mesh)
    assert report.ok, f"{name}: {report.errors}"


# -- primitives ---------------------------------------------------------------


def test_box_has_the_exact_requested_dimensions():
    mesh = build_box(BoxSpec(width_m=2.0, height_m=3.0, depth_m=4.0))
    xs = [p[0] for p in mesh.vertices.values()]
    ys = [p[1] for p in mesh.vertices.values()]
    zs = [p[2] for p in mesh.vertices.values()]
    assert max(xs) - min(xs) == pytest.approx(2.0)
    assert max(ys) - min(ys) == pytest.approx(3.0)
    assert max(zs) - min(zs) == pytest.approx(4.0)


def test_cylinder_min_and_max_segments_both_validate():
    for segments in (6, 64):
        mesh = build_cylinder(CylinderSpec(segments=segments))
        report = validate_generated_mesh(mesh)
        assert report.ok, f"segments={segments}: {report.errors}"


def test_cone_and_torus_validate():
    assert validate_generated_mesh(build_cone(ConeSpec())).ok
    assert validate_generated_mesh(build_torus(TorusSpec())).ok


# -- furniture ------------------------------------------------------------------


def test_table_has_four_legs_and_a_top():
    mesh = build_table(TableSpec())
    # 4 legs x 6 faces + 1 top x 6 faces = 30 faces
    assert len(mesh.faces) == 30


def test_chair_without_backrest_has_fewer_faces_than_with_one():
    with_back = build_chair(ChairSpec(back_height_m=0.4))
    without_back = build_chair(ChairSpec(back_height_m=0.0))  # a stool
    assert len(with_back.faces) > len(without_back.faces)
    assert validate_generated_mesh(without_back).ok


def test_shelf_count_controls_number_of_shelf_boxes():
    few = build_shelf(ShelfSpec(shelf_count=1))
    many = build_shelf(ShelfSpec(shelf_count=8))
    assert len(many.faces) > len(few.faces)


# -- architectural --------------------------------------------------------------


def test_stairs_step_count_scales_face_count():
    small = build_stairs(StairsSpec(step_count=2))
    big = build_stairs(StairsSpec(step_count=20))
    assert len(big.faces) > len(small.faces)
    assert len(big.faces) == 20 * 6
    assert validate_generated_mesh(big).ok


def test_column_with_zero_base_and_capital_is_still_valid():
    mesh = build_column(ColumnSpec(base_height_m=0.0, capital_height_m=0.0))
    assert validate_generated_mesh(mesh).ok


def test_arch_at_minimum_and_maximum_segments_both_validate():
    for segments in (6, 48):
        mesh = build_arch(ArchSpec(segments=segments))
        report = validate_generated_mesh(mesh)
        assert report.ok, f"segments={segments}: {report.errors}"


def test_arch_is_genuinely_non_convex_and_still_valid():
    """The exact regression case that originally surfaced the fan-
    triangulation bug -- an arch's annulus-segment profile is a textbook
    non-convex polygon."""
    mesh = build_arch(ArchSpec(span_m=3.0, thickness_m=1.0))
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    assert report.watertight
    assert report.manifold


def test_fence_post_count_matches_length_and_spacing():
    mesh = build_fence(FenceSpec(length_m=6.0, post_spacing_m=2.0, rail_count=1))
    # ceil(6/2)+1 = 4 posts x 6 faces + 1 rail x 6 faces = 30 faces
    assert len(mesh.faces) == 4 * 6 + 1 * 6


# -- door/window (CSG) -----------------------------------------------------------


def test_door_actually_removes_material_from_the_wall():
    """The whole point of using a real CSG cut rather than an assembly:
    the resulting solid must have strictly less volume than the
    uncut wall it started from."""
    from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, to_trimesh

    spec = DoorSpec()
    door_mesh = build_door(spec)
    door_volume = to_trimesh(door_mesh).volume

    uncut = MeshBuilder()
    add_box(uncut, (0.0, 0.0, 0.0), (spec.wall_width_m, spec.wall_height_m, spec.wall_thickness_m))
    uncut_volume = to_trimesh(uncut.mesh).volume

    assert door_volume < uncut_volume
    opening_volume = spec.door_width_m * spec.door_height_m * spec.wall_thickness_m
    assert door_volume == pytest.approx(uncut_volume - opening_volume, rel=0.05)


def test_window_sill_is_reflected_in_the_cut_geometry():
    mesh = build_window(WindowSpec(sill_height_m=1.5, window_height_m=0.5))
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors


def test_door_offset_past_wall_width_is_rejected_by_the_spec():
    with pytest.raises(ValueError):
        DoorSpec(wall_width_m=2.0, door_width_m=1.5, offset_m=1.0)  # 1.0 + 1.5 > 2.0


def test_window_taller_than_wall_is_rejected_by_the_spec():
    with pytest.raises(ValueError):
        WindowSpec(wall_height_m=2.0, sill_height_m=1.5, window_height_m=1.0)  # 1.5 + 1.0 > 2.0


def test_door_and_window_at_varied_offsets_stay_valid():
    for offset in (0.1, 1.0, 1.8):
        mesh = build_door(DoorSpec(wall_width_m=4.0, offset_m=offset))
        report = validate_generated_mesh(mesh)
        assert report.ok, f"offset={offset}: {report.errors}"
