import math

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import (
    DocOp,
    DrawingDocument,
    bake_path_transform,
    curve_prop_key,
    flatten_path_to_polyline,
)
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string


def test_add_layer_and_path_basic_flow():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("Sketch 1")
    path_id, _ = doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)], color="#ff0000")

    layers = doc.layer_list()
    assert len(layers) == 1
    assert layers[0]["name"] == "Sketch 1"

    paths = doc.path_list()
    assert len(paths) == 1
    assert paths[0]["points"] == [(0.0, 0.0), (1.0, 1.0)]
    assert paths[0]["color"] == "#ff0000"
    assert paths[0]["layer_id"] == layer_id


# -- curve segments (Phase 8) -------------------------------------------------


def test_add_path_with_curves_stores_prop_keyed_by_stable_node_id():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(
        layer_id,
        [(0.0, 0.0), (3.0, 0.0)],
        curves={1: {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)}},
    )
    path = doc.path_list()[0]
    assert path["points"] == [(0.0, 0.0), (3.0, 0.0)]
    node_id_for_point_1 = path["point_ids"][1]
    assert path[curve_prop_key(node_id_for_point_1)] == {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)}
    # index 0 is the path's initial moveto -- there's no "previous point"
    # for it to have an arriving curve from, so nothing is stored there.
    assert curve_prop_key(path["point_ids"][0]) not in path


def test_add_path_curves_survive_a_real_svg_export_and_reimport_round_trip():
    """The actual end-to-end path a real import goes through: construction-
    time index-keyed curves -> stable-node-id-keyed path_prop storage
    (add_path) -> flat props svg_io reads from (path_list) -> SVG text ->
    reparsed back to index-keyed curves (drawing_from_svg_string)."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(
        layer_id,
        [(0.0, 0.0), (3.0, 0.0)],
        curves={1: {"kind": "quad", "c": (1.5, 3.0)}},
    )
    svg = drawing_to_svg_string(doc.path_list())
    assert "Q 1.50,3.00 3.00,0.00" in svg
    reimported = drawing_from_svg_string(svg)
    assert reimported[0]["points"] == [(0.0, 0.0), (3.0, 0.0)]
    assert reimported[0]["curves"] == {1: {"kind": "quad", "c": (1.5, 3.0)}}


def test_concurrent_curve_edits_to_different_segments_dont_clobber_each_other():
    """Each segment's curve lives at its own LWWMap key (curve_prop_key),
    exactly like color/stroke_width already do -- verifies the design
    claim in curve_prop_key's docstring with a real two-replica merge,
    not just an assertion in a comment: two actors concurrently curving
    *different* segments of the same path must both survive the merge,
    unlike a design that bundled every segment into one JSON blob under
    a single key (where the second write would have silently discarded
    the first)."""
    clock_a = LamportClock(actor="a")
    doc_a = DrawingDocument(clock_a)
    layer_id, layer_ops = doc_a.add_layer("L")
    path_id, path_ops = doc_a.add_path(layer_id, [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])

    clock_b = LamportClock(actor="b")
    doc_b = DrawingDocument(clock_b)
    for op in [*layer_ops, *path_ops]:
        doc_b.apply(op)

    point_ids = doc_a.path_list()[0]["point_ids"]
    seg1_id, seg2_id = point_ids[1], point_ids[2]

    # a curves segment 0->1; concurrently, b (unaware of a's edit) curves
    # segment 1->2.
    props_a = doc_a._path_props(path_id)
    op_a = props_a.set(curve_prop_key(seg1_id), {"kind": "quad", "c": (0.5, 1.0)})
    props_b = doc_b._path_props(path_id)
    op_b = props_b.set(curve_prop_key(seg2_id), {"kind": "quad", "c": (1.5, 1.0)})

    doc_a.apply(DocOp("path_prop", op_b.to_dict(), scope=path_id))
    doc_b.apply(DocOp("path_prop", op_a.to_dict(), scope=path_id))

    for doc in (doc_a, doc_b):
        path = doc.path_list()[0]
        assert path[curve_prop_key(seg1_id)] == {"kind": "quad", "c": (0.5, 1.0)}
        assert path[curve_prop_key(seg2_id)] == {"kind": "quad", "c": (1.5, 1.0)}


def test_flatten_path_to_polyline_samples_curve_segments():
    points = [(0.0, 0.0), (10.0, 0.0)]
    point_ids = ["n0", "n1"]
    props = {curve_prop_key("n1"): {"kind": "quad", "c": (5.0, 10.0)}}
    flattened = flatten_path_to_polyline(points, point_ids, props, samples_per_curve=4)
    assert len(flattened) == 5  # 1 start anchor + 4 samples
    assert flattened[0] == (0.0, 0.0)
    assert flattened[-1] == (10.0, 0.0)
    assert flattened[2][1] > 3.0  # the curve bulges well above a straight line


def test_flatten_path_to_polyline_passes_through_straight_segments_unchanged():
    points = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]
    flattened = flatten_path_to_polyline(points, ["n0", "n1", "n2"], {})
    assert flattened == points


def test_append_point_extends_existing_path():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, _ = doc.add_path(layer_id, [(0.0, 0.0)])
    doc.append_point(path_id, (2.0, 2.0))
    assert doc.path_list()[0]["points"] == [(0.0, 0.0), (2.0, 2.0)]


def test_undo_redo_path_add():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(layer_id, [(0.0, 0.0)])
    assert len(doc.path_list()) == 1

    doc.undo()
    assert len(doc.path_list()) == 0

    doc.redo()
    assert len(doc.path_list()) == 1


def test_undo_redo_prop_set_restores_previous_value():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, _ = doc.add_path(layer_id, [(0.0, 0.0)], color="#111111")

    doc.set_path_prop(path_id, "color", "#ff0000")
    assert doc.path_list()[0]["color"] == "#ff0000"

    doc.undo()
    assert doc.path_list()[0]["color"] == "#111111"

    doc.redo()
    assert doc.path_list()[0]["color"] == "#ff0000"


def test_undo_does_not_clobber_concurrent_remote_edit():
    """The core promise of inverted-op undo: undoing my own change must not
    roll back a collaborator's concurrent, unrelated change."""
    doc_a = DrawingDocument(LamportClock(actor="a"))
    layer_id, ops = doc_a.add_layer("L")

    doc_b = DrawingDocument(LamportClock(actor="b"))
    for op in ops:
        doc_b.apply(op)

    path_id, path_ops = doc_a.add_path(layer_id, [(0.0, 0.0)], color="#111111")
    for op in path_ops:
        doc_b.apply(op)

    # b changes the color while a is about to undo an unrelated later action
    color_ops = [doc_b.set_path_prop(path_id, "color", "#00ff00")]

    # a undoes its path_add locally (removing the path) then receives b's color change
    undo_ops = doc_a.undo()
    assert doc_a.path_list() == []
    for op in color_ops:
        doc_a.apply(op)

    # the color change still applied to the (now-hidden) path's properties;
    # it must not have resurrected the path or thrown.
    assert doc_a.path_list() == []
    assert doc_a._path_props(path_id).get("color") == "#00ff00"

    for op in undo_ops:
        doc_b.apply(op)
    assert doc_b.path_list() == []
    assert doc_b._path_props(path_id).get("color") == "#00ff00"


def test_offline_edit_then_merge_converges_and_preserves_both_edits():
    """Simulates the headline scenario: two users start from the same
    document, one goes offline and keeps drawing, the other keeps drawing
    online; when the offline user reconnects and merges, both sets of
    edits must be present on both sides with no conflicts or data loss."""
    seed = DrawingDocument(LamportClock(actor="seed"))
    layer_id, layer_ops = seed.add_layer("Shared Layer")

    online = DrawingDocument(LamportClock(actor="online"))
    offline = DrawingDocument(LamportClock(actor="offline"))
    for op in layer_ops:
        online.apply(op)
        offline.apply(op)

    # offline user disconnects here and keeps drawing locally
    offline.add_path(layer_id, [(0.0, 0.0), (1.0, 0.0)], color="#ff0000")

    # meanwhule the online user draws something else and it syncs to no one
    # else (single-server-of-record simulation is out of scope for this unit
    # test; we only assert the eventual merge is correct)
    online.add_path(layer_id, [(5.0, 5.0), (6.0, 6.0)], color="#0000ff")

    # offline user reconnects: bidirectional state merge
    offline.merge(online)
    online.merge(offline)

    offline_paths = {p["color"]: p["points"] for p in offline.path_list()}
    online_paths = {p["color"]: p["points"] for p in online.path_list()}
    assert offline_paths == online_paths
    assert offline_paths == {
        "#ff0000": [(0.0, 0.0), (1.0, 0.0)],
        "#0000ff": [(5.0, 5.0), (6.0, 6.0)],
    }


def test_presence_updates_are_independent_per_actor():
    doc = DrawingDocument(LamportClock(actor="a"))
    op1 = doc.set_presence("alice", {"x": 1, "y": 2})
    doc.apply(op1)  # idempotent local apply is harmless
    doc2 = DrawingDocument(LamportClock(actor="b"))
    doc2.apply(op1)
    op2 = doc2.set_presence("bob", {"x": 3, "y": 4})
    doc.apply(op2)

    assert {p["actor"] for p in doc.presence_list()} == {"alice", "bob"}


# -- document settings (Phase 11: units, grid/snap) ---------------------------


def test_settings_default_to_empty():
    doc = DrawingDocument(LamportClock(actor="a"))
    assert doc.settings_dict() == {}


def test_set_setting_roundtrips():
    doc = DrawingDocument(LamportClock(actor="a"))
    doc.apply(doc.set_setting("units", "mm"))
    assert doc.settings_dict()["units"] == "mm"


def test_settings_merge_field_wise_like_every_other_prop_bag():
    """Two actors concurrently setting *different* settings must both
    survive the merge -- the same LWWMap-per-key guarantee color/width
    already have on path_props, not a bundled blob that would let one
    concurrent write silently clobber the other."""
    clock_a = LamportClock(actor="a")
    doc_a = DrawingDocument(clock_a)
    clock_b = LamportClock(actor="b")
    doc_b = DrawingDocument(clock_b)

    op_units = doc_a.set_setting("units", "in")
    op_grid = doc_b.set_setting("grid_spacing", 5.0)
    doc_a.apply(op_grid)
    doc_b.apply(op_units)

    assert doc_a.settings_dict() == {"units": "in", "grid_spacing": 5.0}
    assert doc_b.settings_dict() == {"units": "in", "grid_spacing": 5.0}


def test_settings_survive_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    doc.apply(doc.set_setting("units", "mm"))
    doc.apply(doc.set_setting("snap_step", 2.5))
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    assert restored.settings_dict() == {"units": "mm", "snap_step": 2.5}


def test_settings_default_to_empty_when_absent_from_an_old_snapshot():
    """A snapshot persisted before this component existed has no
    "settings" key at all -- from_dict must default to an empty LWWMap
    rather than KeyError, so old rooms still load cleanly."""
    doc = DrawingDocument(LamportClock(actor="a"))
    d = doc.to_dict()
    del d["settings"]
    restored = DrawingDocument.from_dict(LamportClock(actor="b"), d)
    assert restored.settings_dict() == {}
    # and it's still a real, usable LWWMap afterward, not a stub
    restored.apply(restored.set_setting("units", "px"))
    assert restored.settings_dict() == {"units": "px"}


def test_comment_add_and_remove():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, _ = doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)])
    comment_id, _ = doc.add_comment(path_id, 1, "check this corner", author="alice")
    assert len(doc.comment_list()) == 1
    doc.remove_comment(comment_id)
    assert doc.comment_list() == []


def test_document_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)], color="#123456")
    doc.add_comment("whatever", 0, "note", author="alice")

    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    assert restored.layer_list() == doc.layer_list()
    assert restored.comment_list() == doc.comment_list()

    # points round-trip as lists rather than tuples (MessagePack/JSON have no
    # tuple type) -- normalize before comparing, since that's the documented
    # wire-format contract, not a data-loss bug.
    def _normalize(paths):
        return [
            {**p, "points": [list(pt) for pt in p["points"]]} for p in paths
        ]

    assert _normalize(restored.path_list()) == _normalize(doc.path_list())


def test_ops_since_delta_sync_for_late_joiner_style_catchup():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc.add_layer("L")
    frontier = doc.frontier()

    path_id, path_ops = doc.add_path(layer_id, [(0.0, 0.0)])
    delta = doc.ops_since(frontier)

    catchup = DrawingDocument(LamportClock(actor="b"))
    for op in layer_ops:
        catchup.apply(op)
    for op in delta:
        catchup.apply(op)

    assert catchup.path_list() == doc.path_list()
    assert catchup.layer_list() == doc.layer_list()


# -- whole-path transform / export baking (Phase 12) ----------------------------


def test_bake_path_transform_is_a_no_op_when_transform_is_absent():
    path = {"points": [(0.0, 0.0), (1.0, 1.0)], "point_ids": [None, None]}
    assert bake_path_transform(path) is path


def test_bake_path_transform_is_a_no_op_for_an_explicit_identity_transform():
    path = {
        "points": [(0.0, 0.0), (1.0, 1.0)],
        "transform": {"tx": 0, "ty": 0, "rotation": 0, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert baked["points"] == path["points"]


def test_bake_path_transform_translates_freehand_points():
    path = {
        "points": [(0.0, 0.0), (10.0, 0.0)],
        "transform": {"tx": 5, "ty": 7, "rotation": 0, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert baked["points"] == [(5.0, 7.0), (15.0, 7.0)]


def test_bake_path_transform_rotates_a_freehand_path_around_its_bounding_box_center():
    # A horizontal segment from (0,0) to (10,0) has bbox center (5,0);
    # rotating 180 degrees around that pivot flips it end-for-end.
    path = {
        "points": [(0.0, 0.0), (10.0, 0.0)],
        "transform": {"tx": 0, "ty": 0, "rotation": 180, "scale": 1},
    }
    baked = bake_path_transform(path)
    (x0, y0), (x1, y1) = baked["points"]
    assert math.isclose(x0, 10.0, abs_tol=1e-9) and math.isclose(y0, 0.0, abs_tol=1e-9)
    assert math.isclose(x1, 0.0, abs_tol=1e-9) and math.isclose(y1, 0.0, abs_tol=1e-9)


def test_bake_path_transform_scales_a_circle_shape_around_its_own_center():
    path = {
        "points": [],
        "shape": "circle",
        "cx": 100.0,
        "cy": 50.0,
        "r": 10.0,
        "transform": {"tx": 0, "ty": 0, "rotation": 0, "scale": 2},
    }
    baked = bake_path_transform(path)
    assert baked["cx"] == 100.0 and baked["cy"] == 50.0  # center is the pivot, so it doesn't move
    assert baked["r"] == 20.0


def test_bake_path_transform_flattens_a_rotated_rect_into_its_actual_rotated_corners():
    """A rotated rect can't be represented as axis-aligned x/y/w/h any
    more -- that would silently export the wrong shape -- so a non-zero
    rotation converts it to a plain closed point boundary instead."""
    path = {
        "points": [],
        "shape": "rect",
        "x": 100.0,
        "y": 100.0,
        "w": 100.0,
        "h": 60.0,
        "transform": {"tx": 0, "ty": 0, "rotation": 45, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert "shape" not in baked
    pts = baked["points"]
    assert pts[0] == pts[-1]  # closed
    assert len(pts) == 5
    # Consecutive edge lengths should still be 100 and 60 (a rotation is a
    # rigid transform -- it must not distort the rectangle's own size).
    def dist(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])
    assert math.isclose(dist(pts[0], pts[1]), 100.0, abs_tol=1e-6)
    assert math.isclose(dist(pts[1], pts[2]), 60.0, abs_tol=1e-6)


def test_bake_path_transform_keeps_an_unrotated_rect_as_a_native_shape():
    """Scale/translate alone (rotation == 0, the common case) never needs
    to fall back to a point boundary -- confirms the flattening above is
    specifically rotation-triggered, not a blanket behavior change."""
    path = {
        "points": [],
        "shape": "rect",
        "x": 0.0, "y": 0.0, "w": 10.0, "h": 20.0,
        "transform": {"tx": 1, "ty": 1, "rotation": 0, "scale": 2},
    }
    baked = bake_path_transform(path)
    assert baked["shape"] == "rect"


def test_bake_path_transform_flattens_a_rotated_ellipse_into_a_sampled_boundary():
    path = {
        "points": [],
        "shape": "ellipse",
        "cx": 0.0,
        "cy": 0.0,
        "rx": 10.0,
        "ry": 5.0,
        "transform": {"tx": 0, "ty": 0, "rotation": 90, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert "shape" not in baked
    xs = [p[0] for p in baked["points"]]
    ys = [p[1] for p in baked["points"]]
    # A 90-degree rotation swaps the ellipse's axes: it should now be
    # ~5 wide (was rx=10) and ~10 tall (was ry=5).
    assert math.isclose(max(xs) - min(xs), 10.0, abs_tol=1e-2)
    assert math.isclose(max(ys) - min(ys), 20.0, abs_tol=1e-2)


def test_bake_path_transform_rotates_an_arc_exactly_via_its_angles_not_flattening():
    """Unlike rect/ellipse, an arc's rotation is exactly representable
    as +rotation on both angles -- no point-flattening needed or done."""
    path = {
        "points": [],
        "shape": "arc",
        "cx": 0.0, "cy": 0.0, "r": 5.0,
        "start_angle": 0.0, "end_angle": 90.0,
        "transform": {"tx": 0, "ty": 0, "rotation": 30, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert baked["shape"] == "arc"
    assert baked["start_angle"] == 30.0
    assert baked["end_angle"] == 120.0


def test_bake_path_transform_moves_and_scales_a_rect_shape():
    path = {
        "points": [],
        "shape": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 10.0,
        "h": 20.0,
        "transform": {"tx": 3, "ty": 4, "rotation": 0, "scale": 2},
    }
    baked = bake_path_transform(path)
    # pivot is the rect's own center (5, 10); scaling by 2 around it moves
    # the top-left corner from (0,0) to (5,10)-(10,20) = (-5, -10) + tx/ty
    assert baked["w"] == 20.0 and baked["h"] == 40.0
    assert math.isclose(baked["x"], -5.0 + 3, abs_tol=1e-9)
    assert math.isclose(baked["y"], -10.0 + 4, abs_tol=1e-9)


def test_bake_path_transform_adds_rotation_to_an_arcs_start_and_end_angle():
    path = {
        "points": [],
        "shape": "arc",
        "cx": 0.0,
        "cy": 0.0,
        "r": 5.0,
        "start_angle": 0.0,
        "end_angle": 90.0,
        "transform": {"tx": 0, "ty": 0, "rotation": 30, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert baked["start_angle"] == 30.0
    assert baked["end_angle"] == 120.0


def test_bake_path_transform_also_transforms_curve_control_points():
    """A rotated/scaled freehand path's Bezier handles (Phase 8) must move
    along with its anchor points -- otherwise the exported curve would
    visibly warp relative to its (correctly moved) endpoints."""
    path = {
        "points": [(0.0, 0.0), (10.0, 0.0)],
        "point_ids": ["a", "b"],
        curve_prop_key("b"): {"kind": "quad", "c": [5.0, 5.0]},
        "transform": {"tx": 100, "ty": 0, "rotation": 0, "scale": 1},
    }
    baked = bake_path_transform(path)
    assert baked["points"] == [(100.0, 0.0), (110.0, 0.0)]
    assert baked[curve_prop_key("b")]["c"] == [105.0, 5.0]


# -- Dimension annotations (Phase 13) ---------------------------------------------


def _two_point_path(doc, layer_id):
    path_id, _ = doc.add_path(layer_id, [(0.0, 0.0), (100.0, 0.0)])
    entries = doc._path_geom(path_id).entries()
    return path_id, entries[0][0], entries[1][0]


def test_add_dimension_resolves_to_the_live_positions_of_its_anchors():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node, offset=20.0)
    doc.apply(op)
    resolved = doc.resolve_dimension_points(doc.dimensions.get(dim_id))
    assert resolved == ((0.0, 0.0), (100.0, 0.0))


def test_dimension_list_includes_resolved_positions_alongside_the_raw_payload():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node)
    doc.apply(op)
    entry = doc.dimension_list()[0]
    assert entry["id"] == dim_id
    assert entry["a_pos"] == (0.0, 0.0)
    assert entry["b_pos"] == (100.0, 0.0)


def test_dimension_resolution_fails_gracefully_when_an_anchor_point_is_deleted():
    """A dimension's anchor references a specific RGA node id (not a
    point_index the way `comments` does), so it must survive a
    concurrent insert/delete elsewhere in the same path -- and correctly
    report "unresolvable" (not raise) once its own anchor is actually
    gone."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node)
    doc.apply(op)

    doc._path_geom(path_id).delete(b_node)
    assert doc.resolve_dimension_points(doc.dimensions.get(dim_id)) is None
    entry = doc.dimension_list()[0]
    assert "a_pos" not in entry and "b_pos" not in entry


def test_dimension_resolution_unaffected_by_an_unrelated_insert_elsewhere_in_the_path():
    """The whole point of anchoring by node id instead of point_index:
    inserting a brand-new point earlier in the path must not shift which
    point the dimension resolves to."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node)
    doc.apply(op)

    doc._path_geom(path_id).insert_after(None, (-50.0, -50.0))  # inserted before a_node
    resolved = doc.resolve_dimension_points(doc.dimensions.get(dim_id))
    assert resolved == ((0.0, 0.0), (100.0, 0.0))


def test_dimensions_merge_field_wise_like_every_other_lww_component():
    doc_a = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc_a.add_layer("L")
    path_id, path_ops = doc_a.add_path(layer_id, [(0.0, 0.0), (100.0, 0.0)])
    entries = doc_a._path_geom(path_id).entries()
    a_node, b_node = entries[0][0], entries[1][0]

    doc_b = DrawingDocument(LamportClock(actor="b"))
    for op in layer_ops + path_ops:
        doc_b.apply(op)

    dim_id, op = doc_a.add_dimension(path_id, a_node, path_id, b_node)
    doc_a.apply(op)
    doc_b.merge(doc_a)
    assert dim_id in dict(doc_b.dimensions.items())


def test_dimensions_survive_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node, offset=15.0)
    doc.apply(op)

    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    assert dict(restored.dimensions.items())[dim_id]["offset"] == 15.0
    # points round-trip as lists rather than tuples (MessagePack/JSON have
    # no tuple type) -- see test_document_serialization_roundtrip's own
    # normalization note above for the same documented behavior.
    a, b = restored.resolve_dimension_points(restored.dimensions.get(dim_id))
    assert list(a) == [0.0, 0.0] and list(b) == [100.0, 0.0]


def test_dimensions_default_to_empty_when_absent_from_an_old_snapshot():
    doc = DrawingDocument(LamportClock(actor="a"))
    d = doc.to_dict()
    del d["dimensions"]
    restored = DrawingDocument.from_dict(LamportClock(actor="b"), d)
    assert dict(restored.dimensions.items()) == {}
    # and it's still a real, usable LWWMap afterward, not a stub
    layer_id, _ = restored.add_layer("L")
    path_id, a_node, b_node = _two_point_path(restored, layer_id)
    dim_id, op = restored.add_dimension(path_id, a_node, path_id, b_node)
    restored.apply(op)
    assert dim_id in dict(restored.dimensions.items())


def test_remove_dimension_deletes_it():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    dim_id, op = doc.add_dimension(path_id, a_node, path_id, b_node)
    doc.apply(op)
    doc.apply(doc.remove_dimension(dim_id))
    assert dim_id not in dict(doc.dimensions.items())


# -- sketch constraints (Phase 14) -------------------------------------------------


def _point_anchor(path_id, node_id):
    return {"type": "point", "path_id": path_id, "node_id": list(node_id)}


def test_add_constraint_roundtrips_kind_anchors_and_param():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    cid, op = doc.add_constraint(
        "fixed_distance",
        {"p1": _point_anchor(path_id, a_node), "p2": _point_anchor(path_id, b_node)},
        param=42.0,
    )
    doc.apply(op)
    entry = doc.constraint_list()[0]
    assert entry["id"] == cid
    assert entry["kind"] == "fixed_distance"
    assert entry["param"] == 42.0
    assert entry["anchors"]["p1"]["path_id"] == path_id


def test_constraint_supports_a_shape_center_anchor_for_tangent():
    """A circle has no RGA points at all -- tangent's `circle` anchor
    must be representable without one (see the `constraints` field's
    docstring for the two anchor shapes)."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    anchors = {
        "circle": {"type": "shape_center", "path_id": "circle_path"},
        "line_a": _point_anchor(path_id, a_node),
        "line_b": _point_anchor(path_id, b_node),
    }
    cid, op = doc.add_constraint("tangent", anchors, param=25.0)
    doc.apply(op)
    entry = doc.constraint_list()[0]
    assert entry["anchors"]["circle"]["type"] == "shape_center"
    assert entry["id"] == cid


def test_constraints_merge_field_wise_like_every_other_lww_component():
    doc_a = DrawingDocument(LamportClock(actor="a"))
    layer_id, layer_ops = doc_a.add_layer("L")
    path_id, path_ops = doc_a.add_path(layer_id, [(0.0, 0.0), (100.0, 0.0)])
    entries = doc_a._path_geom(path_id).entries()
    a_node, b_node = entries[0][0], entries[1][0]

    doc_b = DrawingDocument(LamportClock(actor="b"))
    for op in layer_ops + path_ops:
        doc_b.apply(op)

    cid, op = doc_a.add_constraint("coincident", {"p1": _point_anchor(path_id, a_node), "p2": _point_anchor(path_id, b_node)})
    doc_a.apply(op)
    doc_b.merge(doc_a)
    assert cid in dict(doc_b.constraints.items())


def test_constraints_survive_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    cid, op = doc.add_constraint(
        "parallel",
        {
            "p1a": _point_anchor(path_id, a_node), "p1b": _point_anchor(path_id, b_node),
            "p2a": _point_anchor(path_id, a_node), "p2b": _point_anchor(path_id, b_node),
        },
    )
    doc.apply(op)
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    assert dict(restored.constraints.items())[cid]["kind"] == "parallel"


def test_constraints_default_to_empty_when_absent_from_an_old_snapshot():
    doc = DrawingDocument(LamportClock(actor="a"))
    d = doc.to_dict()
    del d["constraints"]
    restored = DrawingDocument.from_dict(LamportClock(actor="b"), d)
    assert dict(restored.constraints.items()) == {}
    # and it's still a real, usable LWWMap afterward, not a stub
    layer_id, _ = restored.add_layer("L")
    path_id, a_node, b_node = _two_point_path(restored, layer_id)
    cid, op = restored.add_constraint("coincident", {"p1": _point_anchor(path_id, a_node), "p2": _point_anchor(path_id, b_node)})
    restored.apply(op)
    assert cid in dict(restored.constraints.items())


def test_remove_constraint_deletes_it():
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_id, a_node, b_node = _two_point_path(doc, layer_id)
    cid, op = doc.add_constraint("coincident", {"p1": _point_anchor(path_id, a_node), "p2": _point_anchor(path_id, b_node)})
    doc.apply(op)
    doc.apply(doc.remove_constraint(cid))
    assert cid not in dict(doc.constraints.items())


# -- groups (Phase 15) -------------------------------------------------------------


def test_add_group_then_tagging_paths_with_its_group_id():
    """Grouping itself is just an ordinary group_id path_prop field (an
    LWW field, merges like color/width already do) -- `groups` only
    tracks which group ids currently exist, mirroring `layers`."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_id, _ = doc.add_layer("L")
    path_a, _ = doc.add_path(layer_id, [(0.0, 0.0), (1.0, 1.0)])
    path_b, _ = doc.add_path(layer_id, [(2.0, 2.0), (3.0, 3.0)])
    gid, op = doc.add_group()
    doc.apply(op)
    doc.apply(doc.set_path_prop(path_a, "group_id", gid))
    doc.apply(doc.set_path_prop(path_b, "group_id", gid))
    assert doc.group_list() == [gid]
    assert doc.path_props_dict(path_a)["group_id"] == gid
    assert doc.path_props_dict(path_b)["group_id"] == gid


def test_groups_merge_like_every_other_lww_element_set():
    doc_a = DrawingDocument(LamportClock(actor="a"))
    doc_b = DrawingDocument(LamportClock(actor="b"))
    gid, op = doc_a.add_group()
    doc_a.apply(op)
    doc_b.merge(doc_a)
    assert doc_b.group_list() == [gid]


def test_groups_survive_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    gid, op = doc.add_group()
    doc.apply(op)
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    assert restored.group_list() == [gid]


def test_groups_default_to_empty_when_absent_from_an_old_snapshot():
    doc = DrawingDocument(LamportClock(actor="a"))
    d = doc.to_dict()
    del d["groups"]
    restored = DrawingDocument.from_dict(LamportClock(actor="b"), d)
    assert restored.group_list() == []
    # and it's still a real, usable LWWElementSet afterward, not a stub
    gid, op = restored.add_group()
    restored.apply(op)
    assert restored.group_list() == [gid]


def test_remove_group_deletes_it():
    doc = DrawingDocument(LamportClock(actor="a"))
    gid, op = doc.add_group()
    doc.apply(op)
    doc.apply(doc.remove_group(gid))
    assert doc.group_list() == []


# -- print sheets (Part 7 C3) -------------------------------------------------


def test_add_sheet_only_writes_name_up_front():
    doc = DrawingDocument(LamportClock(actor="a"))
    sid, ops = doc.add_sheet("Sheet 1")
    for op in ops:
        doc.apply(op)
    assert doc.sheet_list() == [{"id": sid, "name": "Sheet 1"}]


def test_set_sheet_prop_adds_fields_independently():
    doc = DrawingDocument(LamportClock(actor="a"))
    sid, ops = doc.add_sheet("Sheet 1")
    for op in ops:
        doc.apply(op)
    doc.apply(doc.set_sheet_prop(sid, "page_size", "a3"))
    doc.apply(doc.set_sheet_prop(sid, "title", "My Drawing"))
    entry = doc.sheet_list()[0]
    assert entry["page_size"] == "a3"
    assert entry["title"] == "My Drawing"
    assert entry["name"] == "Sheet 1"


def test_sheets_merge_field_wise_so_concurrent_edits_to_different_fields_both_survive():
    """A title block's fields (drawn_by, revision, ...) are independently
    editable by different collaborators -- each is its own LWWMap entry,
    the same `layer_props` shape, not one atomic dict write that would
    let a concurrent edit to one field clobber a concurrent edit to
    another."""
    doc_a = DrawingDocument(LamportClock(actor="a"))
    sid, ops = doc_a.add_sheet("Sheet 1")
    for op in ops:
        doc_a.apply(op)
    doc_b = DrawingDocument(LamportClock(actor="b"))
    doc_b.merge(doc_a)

    doc_a.apply(doc_a.set_sheet_prop(sid, "revision", "A"))
    doc_b.apply(doc_b.set_sheet_prop(sid, "drawn_by", "Alice"))

    changed = doc_a.merge(doc_b)
    assert changed
    entry = doc_a.sheet_list()[0]
    assert entry["revision"] == "A"
    assert entry["drawn_by"] == "Alice"


def test_sheets_survive_serialization_roundtrip():
    doc = DrawingDocument(LamportClock(actor="a"))
    sid, ops = doc.add_sheet("Sheet 1")
    for op in ops:
        doc.apply(op)
    doc.apply(doc.set_sheet_prop(sid, "orientation", "portrait"))
    restored = DrawingDocument.from_bytes(LamportClock(actor="b"), doc.to_bytes())
    entry = restored.sheet_list()[0]
    assert entry["id"] == sid
    assert entry["orientation"] == "portrait"


def test_sheets_default_to_empty_when_absent_from_an_old_snapshot():
    doc = DrawingDocument(LamportClock(actor="a"))
    d = doc.to_dict()
    del d["sheets"]
    del d["sheet_props"]
    restored = DrawingDocument.from_dict(LamportClock(actor="b"), d)
    assert restored.sheet_list() == []
    # still a real, usable component afterward, not a stub
    sid, ops = restored.add_sheet("Sheet 1")
    for op in ops:
        restored.apply(op)
    assert restored.sheet_list() == [{"id": sid, "name": "Sheet 1"}]


def test_remove_sheet_deletes_it():
    doc = DrawingDocument(LamportClock(actor="a"))
    sid, ops = doc.add_sheet("Sheet 1")
    for op in ops:
        doc.apply(op)
    doc.apply(doc.remove_sheet(sid))
    assert doc.sheet_list() == []


def test_layer_list_and_path_list_preserve_creation_order():
    """Regression test: layer_list/path_list used to iterate
    LWWElementSet.to_set() (a real Python `set`, which does not
    preserve insertion order), so their output order was an accident of
    hashing, not genuine creation order. Phase 15's fills need "layer
    order, then creation order" for correct z-order -- this is only
    true because LWWElementSet is backed by an LWWMap whose dict
    preserves each element's first-added position, and layer_list/
    path_list now iterate it directly instead of going through
    to_set()."""
    doc = DrawingDocument(LamportClock(actor="a"))
    layer_ids = [doc.add_layer(f"L{i}")[0] for i in range(8)]
    assert [layer["id"] for layer in doc.layer_list()] == layer_ids

    layer_id = layer_ids[0]
    path_ids = [doc.add_path(layer_id, [(0.0, 0.0), (float(i), float(i))])[0] for i in range(8)]
    assert [path["id"] for path in doc.path_list()] == path_ids
