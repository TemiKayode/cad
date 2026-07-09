"""Phase G2: scene composition (``scene.py``) and the deterministic
layout solver (``scene_layout.py``) that turns relations into concrete
world-space translations -- the "solver, not the LLM, owns final
coordinates" rule, tested independently of any prompt interpretation."""

import math

import pytest

from crdt_cad.ai import REGISTRY  # noqa: F401 -- triggers registration
from crdt_cad.ai.scene import SceneObjectSpec, SceneSpec, expand_scene, merge_placed_objects
from crdt_cad.ai.scene_layout import _local_aabb, _translate_aabb, _xz_overlap, solve_layout


def _table_and_chairs(count=4):
    return SceneSpec(objects=[
        SceneObjectSpec(generator="table"),
        SceneObjectSpec(generator="chair", relation="around", target_index=0, count=count),
    ])


# -- SceneSpec / SceneObjectSpec validation --------------------------------------


def test_unknown_generator_is_rejected():
    with pytest.raises(ValueError):
        SceneObjectSpec(generator="teapot")


def test_relation_without_target_index_is_rejected():
    with pytest.raises(ValueError):
        SceneSpec(objects=[
            SceneObjectSpec(generator="table"),
            SceneObjectSpec(generator="chair", relation="around"),  # missing target_index
        ])


def test_forward_reference_target_index_is_rejected():
    with pytest.raises(ValueError):
        SceneSpec(objects=[
            SceneObjectSpec(generator="chair", relation="around", target_index=1),
            SceneObjectSpec(generator="table"),
        ])


def test_target_index_referencing_self_or_later_is_rejected():
    with pytest.raises(ValueError):
        SceneSpec(objects=[
            SceneObjectSpec(generator="table"),
            SceneObjectSpec(generator="chair", relation="around", target_index=1),  # references itself
        ])


def test_row_relation_does_not_require_a_target_index():
    scene = SceneSpec(objects=[SceneObjectSpec(generator="chair", relation="row", count=3)])
    assert scene.objects[0].target_index is None


def test_object_count_is_bounded():
    with pytest.raises(ValueError):
        SceneObjectSpec(generator="chair", count=13)  # over the max of 12
    with pytest.raises(ValueError):
        SceneObjectSpec(generator="chair", count=0)  # under the min of 1


def test_scene_object_count_is_bounded():
    with pytest.raises(ValueError):
        SceneSpec(objects=[SceneObjectSpec(generator="box") for _ in range(11)])  # over max_length=10


# -- expand_scene -----------------------------------------------------------------


def test_expand_scene_produces_one_entry_per_count():
    objs = expand_scene(_table_and_chairs(count=4))
    assert len(objs) == 5  # 1 table + 4 chairs
    assert objs[0].generator == "table"
    assert all(o.generator == "chair" for o in objs[1:])


def test_expand_scene_remaps_target_index_to_first_copy_of_a_counted_target():
    scene = SceneSpec(objects=[
        SceneObjectSpec(generator="table", relation="row", count=2, spacing_m=2.0),
        SceneObjectSpec(generator="chair", relation="around", target_index=0, count=3),
    ])
    objs = expand_scene(scene)
    # 2 tables (indices 0, 1) then 3 chairs (indices 2, 3, 4), all anchored to index 0
    assert len(objs) == 5
    chairs = objs[2:]
    assert all(c.target_index == 0 for c in chairs)


def test_expand_scene_rejects_a_malformed_nested_spec():
    with pytest.raises(ValueError):
        expand_scene(SceneSpec(objects=[SceneObjectSpec(generator="table", spec={"width_m": -1.0})]))


# -- solve_layout: ground snapping, non-overlap, relation-specific placement ------


def test_ground_plane_snapping_for_every_non_stacked_relation():
    objs = expand_scene(_table_and_chairs(count=4))
    translations = solve_layout(objs)
    aabbs = [_translate_aabb(_local_aabb(o.mesh), *t) for o, t in zip(objs, translations)]
    for aabb in aabbs:
        assert aabb[0][1] == pytest.approx(0.0, abs=1e-9)


def test_on_top_of_stacks_on_the_targets_measured_top_not_a_guess():
    scene = SceneSpec(objects=[
        SceneObjectSpec(generator="table"),
        SceneObjectSpec(generator="box", relation="on_top_of", target_index=0),
    ])
    objs = expand_scene(scene)
    translations = solve_layout(objs)
    table_aabb = _translate_aabb(_local_aabb(objs[0].mesh), *translations[0])
    box_aabb = _translate_aabb(_local_aabb(objs[1].mesh), *translations[1])
    assert box_aabb[0][1] == pytest.approx(table_aabb[1][1])  # box bottom == table top


def test_around_distributes_copies_in_a_circle_at_equal_radius_from_target_center():
    objs = expand_scene(_table_and_chairs(count=4))
    translations = solve_layout(objs)
    table_aabb = _translate_aabb(_local_aabb(objs[0].mesh), *translations[0])
    tcx = (table_aabb[0][0] + table_aabb[1][0]) / 2
    tcz = (table_aabb[0][2] + table_aabb[1][2]) / 2

    radii = []
    for chair, t in zip(objs[1:], translations[1:]):
        chair_aabb = _translate_aabb(_local_aabb(chair.mesh), *t)
        ccx = (chair_aabb[0][0] + chair_aabb[1][0]) / 2
        ccz = (chair_aabb[0][2] + chair_aabb[1][2]) / 2
        radii.append(math.hypot(ccx - tcx, ccz - tcz))
    assert radii[0] == pytest.approx(radii[1], abs=1e-6)
    assert radii[0] == pytest.approx(radii[2], abs=1e-6)
    assert radii[0] == pytest.approx(radii[3], abs=1e-6)


def test_row_spaces_copies_evenly_along_x_by_spacing_m():
    scene = SceneSpec(objects=[SceneObjectSpec(generator="box", relation="row", count=3, spacing_m=1.5)])
    objs = expand_scene(scene)
    translations = solve_layout(objs)
    xs = [t[0] for t in translations]
    box_width = _local_aabb(objs[0].mesh)[1][0] - _local_aabb(objs[0].mesh)[0][0]
    assert xs[1] - xs[0] == pytest.approx(box_width + 1.5)
    assert xs[2] - xs[1] == pytest.approx(box_width + 1.5)


def test_beside_offsets_along_x_from_the_targets_edge():
    scene = SceneSpec(objects=[
        SceneObjectSpec(generator="table"),
        SceneObjectSpec(generator="fence", relation="beside", target_index=0),
    ])
    objs = expand_scene(scene)
    translations = solve_layout(objs)
    table_aabb = _translate_aabb(_local_aabb(objs[0].mesh), *translations[0])
    fence_aabb = _translate_aabb(_local_aabb(objs[1].mesh), *translations[1])
    assert fence_aabb[0][0] >= table_aabb[1][0]  # fence starts at/after the table's right edge


def test_no_two_independent_objects_overlap_in_xz():
    scene = SceneSpec(objects=[
        SceneObjectSpec(generator="table"),
        SceneObjectSpec(generator="box", relation="on_top_of", target_index=0),
        SceneObjectSpec(generator="chair", relation="row", count=3, spacing_m=0.5),
        SceneObjectSpec(generator="cone"),
        SceneObjectSpec(generator="fence", relation="beside", target_index=0),
    ])
    objs = expand_scene(scene)
    translations = solve_layout(objs)
    aabbs = [_translate_aabb(_local_aabb(o.mesh), *t) for o, t in zip(objs, translations)]
    for i in range(len(objs)):
        for j in range(i + 1, len(objs)):
            if objs[i].target_index == j or objs[j].target_index == i:
                continue  # an intentional relation (e.g. on_top_of) -- overlap is the point
            assert not _xz_overlap(aabbs[i], aabbs[j]), f"objects {i} and {j} overlap"


# -- merge_placed_objects -----------------------------------------------------------


def test_merge_placed_objects_produces_globally_unique_ids():
    objs = expand_scene(_table_and_chairs(count=4))
    translations = solve_layout(objs)
    mesh, per_object_ids = merge_placed_objects(objs, translations)

    all_vertex_ids = [vid for vids, _ in per_object_ids for vid in vids]
    all_face_ids = [fid for _, fids in per_object_ids for fid in fids]
    assert len(set(all_vertex_ids)) == len(all_vertex_ids)
    assert len(set(all_face_ids)) == len(all_face_ids)
    assert set(all_vertex_ids) == set(mesh.vertices.keys())
    assert set(all_face_ids) == set(mesh.faces.keys())


def test_merge_placed_objects_per_object_ids_line_up_with_input_order():
    objs = expand_scene(_table_and_chairs(count=2))
    translations = solve_layout(objs)
    _, per_object_ids = merge_placed_objects(objs, translations)
    assert len(per_object_ids) == len(objs)
    for (vertex_ids, face_ids), obj in zip(per_object_ids, objs):
        assert len(vertex_ids) == len(obj.mesh.vertices)
        assert len(face_ids) == len(obj.mesh.faces)


def test_merge_placed_objects_actually_translates_vertices():
    scene = SceneSpec(objects=[SceneObjectSpec(generator="box")])
    objs = expand_scene(scene)
    translations = [(5.0, 0.0, 5.0)]
    mesh, per_object_ids = merge_placed_objects(objs, translations)
    vertex_ids, _ = per_object_ids[0]
    original_xs = sorted(p[0] for p in objs[0].mesh.vertices.values())
    merged_xs = sorted(mesh.vertices[vid][0] for vid in vertex_ids)
    assert merged_xs == [pytest.approx(x + 5.0) for x in original_xs]
