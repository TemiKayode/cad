from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument


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
