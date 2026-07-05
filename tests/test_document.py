from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DocOp, DrawingDocument, curve_prop_key, flatten_path_to_polyline
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
