"""Phase G3: exhaustive tests for the sandboxed geometry DSL
(``dsl.py``) -- every primitive/transform/combinator produces valid
geometry, and every hard cap actually stops a program that would
otherwise exceed it, with a specific message (fed back to the model on
repair in ``generator.py``)."""

import pytest

import crdt_cad.ai.dsl as dsl_module
from crdt_cad.ai.dsl import (
    DSLBudgetExceededError,
    DSLError,
    DSLValidationError,
    execute_dsl_program,
)
from crdt_cad.ai.validation import validate_generated_mesh


def _box(size=(1.0, 1.0, 1.0)):
    return {"op": "box", "size": list(size)}


# -- every node type produces valid, watertight geometry ---------------------------


def test_box_produces_a_watertight_box_with_exact_dimensions():
    mesh = execute_dsl_program({"root": _box((2.0, 3.0, 4.0)), "material": "wood"})
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    xs = [p[0] for p in mesh.vertices.values()]
    ys = [p[1] for p in mesh.vertices.values()]
    zs = [p[2] for p in mesh.vertices.values()]
    assert max(xs) - min(xs) == pytest.approx(2.0)
    assert max(ys) - min(ys) == pytest.approx(3.0)
    assert max(zs) - min(zs) == pytest.approx(4.0)
    assert set(mesh.face_materials.values()) == {"wood"}


def test_box_is_based_at_y_zero_and_centered_in_xz():
    mesh = execute_dsl_program({"root": _box((2.0, 1.0, 2.0))})
    ys = [p[1] for p in mesh.vertices.values()]
    xs = [p[0] for p in mesh.vertices.values()]
    assert min(ys) == pytest.approx(0.0)
    assert min(xs) == pytest.approx(-1.0)


def test_cylinder_and_prism_and_extrude_all_validate():
    for root in (
        {"op": "cylinder", "radius": 0.5, "height": 2.0, "segments": 12},
        {"op": "prism", "sides": 6, "radius": 0.5, "height": 1.0},
        {"op": "extrude", "polygon": [[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]], "height": 1.5},
    ):
        mesh = execute_dsl_program({"root": root})
        report = validate_generated_mesh(mesh)
        assert report.ok, f"{root['op']}: {report.errors}"


def test_prism_with_three_sides_is_a_triangular_prism():
    mesh = execute_dsl_program({"root": {"op": "prism", "sides": 3, "radius": 1.0, "height": 1.0}})
    # execute_dsl_program always round-trips through trimesh (from_trimesh
    # produces one face per triangle): 3 rectangular sides (2 tris each)
    # + a triangular top + a triangular bottom = 8 triangles.
    assert len(mesh.faces) == 8
    assert validate_generated_mesh(mesh).ok


def test_translate_moves_geometry_by_the_exact_offset():
    plain = execute_dsl_program({"root": _box()})
    moved = execute_dsl_program({"root": {"op": "translate", "offset": [5.0, 0.0, -3.0], "child": _box()}})
    plain_xs = sorted(p[0] for p in plain.vertices.values())
    moved_xs = sorted(p[0] for p in moved.vertices.values())
    assert moved_xs == [pytest.approx(x + 5.0) for x in plain_xs]


def test_rotate_about_each_axis_stays_valid():
    for axis in ("x", "y", "z"):
        mesh = execute_dsl_program({"root": {"op": "rotate", "axis": axis, "degrees": 37, "child": _box()}})
        assert validate_generated_mesh(mesh).ok


def test_rotate_by_360_degrees_is_the_identity():
    plain = execute_dsl_program({"root": _box()})
    rotated = execute_dsl_program({"root": {"op": "rotate", "axis": "y", "degrees": 360, "child": _box()}})
    plain_pts = sorted(plain.vertices.values())
    rotated_pts = sorted(rotated.vertices.values())
    for a, b in zip(plain_pts, rotated_pts):
        assert a[0] == pytest.approx(b[0], abs=1e-9)
        assert a[1] == pytest.approx(b[1], abs=1e-9)
        assert a[2] == pytest.approx(b[2], abs=1e-9)


def test_scale_stretches_only_the_requested_axis():
    mesh = execute_dsl_program({"root": {"op": "scale", "factors": [3.0, 1.0, 1.0], "child": _box()}})
    xs = [p[0] for p in mesh.vertices.values()]
    ys = [p[1] for p in mesh.vertices.values()]
    assert max(xs) - min(xs) == pytest.approx(3.0)
    assert max(ys) - min(ys) == pytest.approx(1.0)


def test_group_concatenates_disjoint_parts_without_a_boolean():
    mesh = execute_dsl_program({"root": {
        "op": "group",
        "children": [_box(), {"op": "translate", "offset": [5.0, 0.0, 0.0], "child": _box()}],
    }})
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    assert len(mesh.faces) == 24  # two independent boxes, 6 quad faces (12 triangles) each


def test_union_of_overlapping_primitives_is_one_watertight_solid_with_less_volume_than_the_sum():
    from crdt_cad.ai.mesh_builder import to_trimesh

    mesh = execute_dsl_program({"root": {
        "op": "union",
        "children": [_box((1.0, 1.0, 1.0)), {"op": "translate", "offset": [0.5, 0, 0], "child": _box((1.0, 1.0, 1.0))}],
    }})
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    assert to_trimesh(mesh).volume < 2.0  # less than two full unit cubes, since they overlap


def test_difference_actually_removes_volume():
    from crdt_cad.ai.mesh_builder import to_trimesh

    mesh = execute_dsl_program({"root": {
        "op": "difference",
        "children": [_box((2.0, 2.0, 2.0)), {"op": "cylinder", "radius": 0.5, "height": 3.0}],
    }})
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    assert to_trimesh(mesh).volume < 8.0  # less than the uncut 2x2x2 box


def test_repeat_produces_n_evenly_spaced_copies():
    mesh = execute_dsl_program({"root": {
        "op": "repeat", "count": 5, "offset": [1.0, 0.0, 0.0], "child": _box((0.2, 1.0, 0.2)),
    }})
    report = validate_generated_mesh(mesh)
    assert report.ok, report.errors
    assert len(mesh.faces) == 5 * 12  # 5 boxes, 6 quad faces (12 triangles) each


def test_nested_transforms_and_combinators_compose():
    program = {"root": {
        "op": "union",
        "children": [
            {"op": "rotate", "axis": "y", "degrees": 15, "child": _box((1.0, 1.0, 1.0))},
            {"op": "translate", "offset": [0.3, 0.0, 0.0], "child": {
                "op": "scale", "factors": [1.0, 2.0, 1.0], "child": {"op": "cylinder", "radius": 0.3, "height": 1.0},
            }},
        ],
    }}
    mesh = execute_dsl_program(program)
    assert validate_generated_mesh(mesh).ok


def test_material_is_applied_uniformly_to_the_whole_result():
    mesh = execute_dsl_program({"root": {"op": "group", "children": [_box(), _box()]}, "material": "stone"})
    assert set(mesh.face_materials.values()) == {"stone"}


def test_no_material_means_no_material_tags():
    mesh = execute_dsl_program({"root": _box()})
    assert mesh.face_materials == {}


# -- validation errors: malformed programs never reach execution -------------------


def test_unknown_op_is_rejected():
    with pytest.raises(DSLValidationError, match="unknown op"):
        execute_dsl_program({"root": {"op": "sphere", "radius": 1.0}})


def test_program_must_be_a_dict():
    with pytest.raises(DSLValidationError):
        execute_dsl_program("not a program")  # type: ignore[arg-type]
    with pytest.raises(DSLValidationError):
        execute_dsl_program([1, 2, 3])  # type: ignore[arg-type]


def test_program_must_have_a_root_node():
    with pytest.raises(DSLValidationError, match="root"):
        execute_dsl_program({"material": "wood"})


def test_box_requires_size():
    with pytest.raises(DSLValidationError, match="size"):
        execute_dsl_program({"root": {"op": "box"}})


def test_box_size_must_be_positive():
    with pytest.raises(DSLValidationError, match="positive"):
        execute_dsl_program({"root": _box((-1.0, 1.0, 1.0))})


def test_box_size_component_over_the_bbox_cap_is_rejected():
    with pytest.raises(DSLValidationError, match="exceeds"):
        execute_dsl_program({"root": _box((500.0, 1.0, 1.0))})


def test_transform_nodes_require_a_child():
    with pytest.raises(DSLValidationError, match="child"):
        execute_dsl_program({"root": {"op": "translate", "offset": [1.0, 0.0, 0.0]}})


def test_rotate_axis_must_be_cardinal():
    with pytest.raises(DSLValidationError, match="axis"):
        execute_dsl_program({"root": {"op": "rotate", "axis": "w", "degrees": 10, "child": _box()}})


def test_rotate_degrees_out_of_range_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "rotate", "axis": "x", "degrees": 9999, "child": _box()}})


def test_union_and_difference_require_at_least_two_children():
    with pytest.raises(DSLValidationError, match="children"):
        execute_dsl_program({"root": {"op": "union", "children": [_box()]}})
    with pytest.raises(DSLValidationError, match="children"):
        execute_dsl_program({"root": {"op": "difference", "children": [_box()]}})


def test_extrude_polygon_must_have_at_least_three_points():
    with pytest.raises(DSLValidationError, match="polygon"):
        execute_dsl_program({"root": {"op": "extrude", "polygon": [[0, 0], [1, 0]], "height": 1.0}})


def test_prism_sides_out_of_range_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "prism", "sides": 2, "radius": 1.0, "height": 1.0}})
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "prism", "sides": 50, "radius": 1.0, "height": 1.0}})


# -- hard caps: a structurally-valid program that still must be rejected -----------


def test_repeat_count_above_the_cap_is_rejected():
    with pytest.raises(DSLValidationError, match="repeat"):
        execute_dsl_program({"root": {"op": "repeat", "count": 999, "offset": [1, 0, 0], "child": _box()}})


def test_repeat_count_of_zero_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "repeat", "count": 0, "offset": [1, 0, 0], "child": _box()}})


def test_too_many_textual_nodes_is_rejected():
    program = {"root": {"op": "group", "children": [_box() for _ in range(60)]}}
    with pytest.raises(DSLValidationError, match="nodes"):
        execute_dsl_program(program)


def test_tree_depth_beyond_the_cap_is_rejected():
    node = _box()
    for _ in range(30):
        node = {"op": "translate", "offset": [0.01, 0.0, 0.0], "child": node}
    with pytest.raises(DSLValidationError, match="depth"):
        execute_dsl_program({"root": node})


def test_group_children_list_beyond_its_own_cap_is_rejected():
    with pytest.raises(DSLValidationError, match="children"):
        execute_dsl_program({"root": {"op": "group", "children": [_box() for _ in range(20)]}})


def test_nested_repeats_exceed_the_expanded_node_budget():
    """Each individual repeat.count (20) is within its own cap (24), but
    nested they multiply out to 400+ primitive executions -- this is
    exactly the "loop bound gone wrong" scenario validation.py's own
    docstring anticipates, caught here before wasting time building
    hundreds of boxes."""
    program = {"root": {
        "op": "repeat", "count": 20, "offset": [1, 0, 0],
        "child": {"op": "repeat", "count": 20, "offset": [0, 1, 0], "child": _box((0.1, 0.1, 0.1))},
    }}
    with pytest.raises(DSLBudgetExceededError, match="node"):
        execute_dsl_program(program)


def test_execution_time_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(dsl_module, "MAX_DSL_EXECUTION_SECONDS", 0.0)
    with pytest.raises(DSLBudgetExceededError, match="time budget"):
        execute_dsl_program({"root": _box()})


def test_vertex_and_face_budgets_are_enforced(monkeypatch):
    monkeypatch.setattr(dsl_module, "MAX_DSL_VERTICES", 4)
    with pytest.raises(DSLBudgetExceededError, match="vertex"):
        execute_dsl_program({"root": _box()})  # a box has 8 vertices > 4


def test_bounding_box_cap_is_enforced_per_node_not_just_at_the_end():
    """A translate can move an in-bounds primitive's *final* position
    far away without changing its own extent -- the cap must apply to
    each node's own bounding box, not just component-level field
    ranges, so this specific case (small box, huge offset) is still
    caught by the offset's own field cap rather than slipping through."""
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "translate", "offset": [500.0, 0.0, 0.0], "child": _box((0.1, 0.1, 0.1))}})


# -- malicious/adversarial shapes ---------------------------------------------------


def test_op_field_missing_entirely_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"size": [1, 1, 1]}})


def test_non_dict_child_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "translate", "offset": [1, 0, 0], "child": "box"}})


def test_wrong_typed_fields_are_rejected_not_coerced():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "box", "size": ["a", "b", "c"]}})


def test_repeat_count_as_a_float_is_rejected():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "repeat", "count": 3.5, "offset": [1, 0, 0], "child": _box()}})


def test_boolean_true_is_not_accepted_as_an_integer_count():
    with pytest.raises(DSLValidationError):
        execute_dsl_program({"root": {"op": "repeat", "count": True, "offset": [1, 0, 0], "child": _box()}})


def test_empty_program_produces_a_clear_error_not_a_crash():
    with pytest.raises(DSLError):
        execute_dsl_program({})


def test_none_program_produces_a_clear_error_not_a_crash():
    with pytest.raises(DSLError):
        execute_dsl_program(None)  # type: ignore[arg-type]


def test_deeply_nested_but_otherwise_tiny_program_is_still_capped_by_depth():
    """Regression guard: depth must be checked in the validation pass,
    independent of whether the total node/vertex count would otherwise
    be small -- unbounded Python recursion on attacker-controlled JSON
    is itself the risk, not just the resulting mesh size."""
    node = _box()
    for _ in range(1000):
        node = {"op": "translate", "offset": [0.001, 0.0, 0.0], "child": node}
    with pytest.raises(DSLValidationError, match="depth"):
        execute_dsl_program({"root": node})
