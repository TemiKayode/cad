"""DrawingDocument -- the 2D CRDT document model used by the collaborative
sketch demo. Composes the primitives exactly the way a real CAD object
graph would:

- ``layers``: :class:`LWWElementSet` of layer ids (which layers exist).
- ``layer_props``: one :class:`LWWMap` per layer (name, visibility, colour).
- ``path_index``: :class:`LWWElementSet` of path ids (which paths exist).
- ``paths``: one :class:`RGA` per path id -- the ordered polyline/curve
  point sequence, so two people can draw into/extend the same stroke
  concurrently, or split it, without clobbering each other.
- ``path_props``: one :class:`LWWMap` per path (colour, stroke width,
  which layer it belongs to).
- ``comments``: :class:`LWWMap` of comment id -> annotation payload,
  each pinned to a path id + point index, satisfying the "comments and
  annotations attached to geometry, CRDT-based" requirement.
- ``presence``: :class:`LWWMap` of actor id -> ephemeral cursor/selection
  payload. Each actor only ever writes their own key, so this behaves
  exactly like one independent LWW-Register per actor.

Undo/redo
---------
Per the spec, undo/redo must be **inverted CRDT operations, not state
snapshots** -- undoing an edit while collaborators are mid-edit elsewhere
must not roll back their work too. So ``undo()``/``redo()`` never touch
history directly; they look up what changed, synthesize the *opposite*
edit, and run it through the same local-mutation path as any other edit
(ticking a brand new ``OpId``). The result is just another op that merges
like any other -- it undoes *this actor's* change without disturbing
concurrent changes made by anyone else.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from crdt_cad.crdt.clock import ActorId, LamportClock, OpId, VectorClock
from crdt_cad.crdt.lww import LWWElementSet, LWWMap, LWWOp
from crdt_cad.crdt.rga import RGA, op_from_dict
from crdt_cad.crdt.serialize import (
    dumps_msgpack,
    loads_msgpack,
)

PathId = str
LayerId = str
CommentId = str
DimId = str
ConstraintId = str
GroupId = str
SheetId = str
Point = tuple[float, float]

# Document units (Phase 11): stored/CRDT geometry is always in raw
# px-equivalent world units, regardless of this setting -- "units" is a
# *display*-layer conversion (cursor readout, numeric shape input,
# SVG/DXF export scale), never a migration of existing coordinates. The
# 96 px/inch convention matches CSS's own px definition, so "px" is
# exactly 1:1 and needs no special-casing anywhere that already assumes
# today's raw-pixel behavior.
UNITS_PX_PER_UNIT = {"px": 1.0, "mm": 96.0 / 25.4, "in": 96.0}


def px_per_unit(units: str) -> float:
    return UNITS_PX_PER_UNIT.get(units, 1.0)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# -- curve segments (Phase 8) -------------------------------------------------
#
# A path's points remain a plain RGA[Point] -- no new CRDT primitive, and
# every existing persisted path is still valid data. Curve info for the
# segment *arriving* at a given anchor point is stored as an ordinary
# `path_prop` entry keyed by that anchor's own (stable) RGA node id, e.g.
# `curve:5@actor123`. Two concurrent edits to *different* segments'
# curves are therefore independent LWWMap writes (no clobbering), the
# same guarantee every other path_prop (color, stroke_width, ...)
# already has -- bundling all segments into one JSON blob under a single
# key would have re-introduced exactly the "concurrent edits to
# unrelated data clobber each other" problem LWWMap-per-key already
# avoids for color/width/layer_id.
#
# A segment with no `curve:` entry (the default, and every point in a
# path created before this existed) is a straight line -- backward
# compatible by construction, not by special-casing old data.
#
# Value shape: {"kind": "quad", "c": [cx, cy]} (one control point, for
# `quadraticCurveTo`/SVG Q/T) or {"kind": "cubic", "c1": [..], "c2": [..]}
# (two control points, for `bezierCurveTo`/SVG C/S).
CURVE_PROP_PREFIX = "curve:"


def curve_prop_key(node_id) -> str:
    """`node_id` is normally the `OpId` of the anchor point this curve
    segment arrives at (str()'s to `"{counter}@{actor}"`), but a
    caller that already has that string (e.g. from `path_list()`'s
    `point_ids`) can pass it directly -- f-string formatting a string
    is the identity, so this works either way."""
    return f"{CURVE_PROP_PREFIX}{node_id}"


def _cubic_point(p0: Point, c1: Point, c2: Point, p1: Point, t: float) -> Point:
    mt = 1.0 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * p1[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * p1[1]
    return (x, y)


def _quad_point(p0: Point, c: Point, p1: Point, t: float) -> Point:
    mt = 1.0 - t
    x = mt**2 * p0[0] + 2 * mt * t * c[0] + t**2 * p1[0]
    y = mt**2 * p0[1] + 2 * mt * t * c[1] + t**2 * p1[1]
    return (x, y)


def flatten_path_to_polyline(
    points: list[Point], point_ids: list, props: dict, samples_per_curve: int = 12
) -> list[Point]:
    """Expands any curve segments into `samples_per_curve` evenly-spaced
    points along the Bezier, for a consumer (DXF's `LWPOLYLINE`) that can
    only represent straight polylines -- see the module's Phase 8
    docstring above and the README's Import/export section for why DXF
    export flattens curves rather than approximating them with arcs or
    splines. A segment with no curve prop passes through as a single
    straight point, same as today. `point_ids` may be shorter than
    `points` or contain `None`s (a caller with no id information at all,
    e.g. a hand-built dict in a test) -- that segment is just treated as
    a straight line, never an error.
    """
    if not points:
        return []
    out = [points[0]]
    for i in range(1, len(points)):
        node_id = point_ids[i] if point_ids and i < len(point_ids) else None
        seg = props.get(curve_prop_key(node_id)) if node_id is not None else None
        p0 = points[i - 1]
        p1 = points[i]
        if seg is None:
            out.append(p1)
        elif seg["kind"] == "cubic":
            c1, c2 = tuple(seg["c1"]), tuple(seg["c2"])
            for s in range(1, samples_per_curve + 1):
                out.append(_cubic_point(p0, c1, c2, p1, s / samples_per_curve))
        elif seg["kind"] == "quad":
            c = tuple(seg["c"])
            for s in range(1, samples_per_curve + 1):
                out.append(_quad_point(p0, c, p1, s / samples_per_curve))
        else:
            out.append(p1)
    return out


# -- whole-path transform (Phase 12) -------------------------------------------
#
# A path's move/rotate/scale lives entirely in its `transform` path_prop
# ({tx, ty, rotation (degrees), scale}), never rewriting the underlying
# RGA points or a shape's own parametric fields -- see sketch.js's
# matching comment for the full CRDT-merge rationale. Exporters (SVG,
# DXF) have no transform concept of their own, so the transform is baked
# into plain coordinates only at export time, via this function --
# mirrors sketch.js's getTransform/pathBaseCenter/applyPathTransform/
# transformedShapeProps exactly, so an exported file matches what the
# browser already renders.
_IDENTITY_TRANSFORM = {"tx": 0.0, "ty": 0.0, "rotation": 0.0, "scale": 1.0}


def _path_base_center(points: list[Point], props: dict) -> Point:
    shape = props.get("shape")
    if shape == "line":
        return ((props["x1"] + props["x2"]) / 2, (props["y1"] + props["y2"]) / 2)
    if shape == "rect":
        return (props["x"] + props["w"] / 2, props["y"] + props["h"] / 2)
    if shape in ("circle", "ellipse", "arc"):
        return (props["cx"], props["cy"])
    if not points:
        return (0.0, 0.0)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _apply_point_transform(pt: Point, pivot: Point, t: dict) -> Point:
    dx = (pt[0] - pivot[0]) * t["scale"]
    dy = (pt[1] - pivot[1]) * t["scale"]
    rad = math.radians(t["rotation"])
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    return (
        pivot[0] + dx * cos_r - dy * sin_r + t["tx"],
        pivot[1] + dx * sin_r + dy * cos_r + t["ty"],
    )


def _rect_corners(x: float, y: float, w: float, h: float) -> list[Point]:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]  # closed: repeats the first corner


def _ellipse_boundary(cx: float, cy: float, rx: float, ry: float, samples: int = 64) -> list[Point]:
    return [
        (cx + rx * math.cos(2 * math.pi * i / samples), cy + ry * math.sin(2 * math.pi * i / samples))
        for i in range(samples + 1)  # +1 closes the loop (repeats the first point)
    ]


_SHAPE_FIELD_KEYS = ("shape", "x", "y", "w", "h", "cx", "cy", "r", "rx", "ry", "start_angle", "end_angle")


def bake_path_transform(path_dict: dict) -> dict:
    """Returns a copy of `path_dict` (as produced by `path_list()`) with
    its `transform` field (if any, and non-identity) applied directly to
    `points` / shape fields / any curve control points -- for exporters
    that have no transform concept of their own. Absent or identity
    transform returns `path_dict` unchanged (the overwhelmingly common
    case, and every path that predates this feature).

    A rotated rect/ellipse can't be expressed as axis-aligned
    `x/y/w/h`/`cx/cy/rx/ry` any more (a rotated box or off-axis ellipse
    just isn't that shape), so under non-zero rotation those two kinds
    are converted to a plain point boundary instead -- sampled exactly
    (rect: its 4 corners; ellipse: densely around its rim) and forward-
    transformed like any other point, then exported as a closed
    polyline/polygon by the same fallback path a freehand path already
    uses. Line, circle, and arc never need this: a line is just 2
    points (any rotation is exact), a circle is rotation-invariant, and
    an arc's rotation is exactly "add rotation to both angles" -- all
    three stay native shape elements at any transform."""
    raw_t = path_dict.get("transform") or _IDENTITY_TRANSFORM
    t = {
        "tx": raw_t.get("tx", 0.0),
        "ty": raw_t.get("ty", 0.0),
        "rotation": raw_t.get("rotation", 0.0),
        "scale": raw_t.get("scale", 1.0),
    }
    if t["tx"] == 0 and t["ty"] == 0 and t["rotation"] == 0 and t["scale"] == 1:
        return path_dict

    out = dict(path_dict)
    points = out.get("points") or []
    pivot = _path_base_center(points, out)
    if points:
        out["points"] = [_apply_point_transform(p, pivot, t) for p in points]

    shape = out.get("shape")
    flatten_to_boundary = shape in ("rect", "ellipse") and t["rotation"] % 360 != 0
    if flatten_to_boundary:
        if shape == "rect":
            base_pts = _rect_corners(out["x"], out["y"], out["w"], out["h"])
        else:
            base_pts = _ellipse_boundary(out["cx"], out["cy"], out["rx"], out["ry"])
        out["points"] = [_apply_point_transform(p, pivot, t) for p in base_pts]
        for key in _SHAPE_FIELD_KEYS:
            out.pop(key, None)
        out.pop("point_ids", None)
    elif shape == "line":
        out["x1"], out["y1"] = _apply_point_transform((out["x1"], out["y1"]), pivot, t)
        out["x2"], out["y2"] = _apply_point_transform((out["x2"], out["y2"]), pivot, t)
    elif shape == "rect":
        out["x"], out["y"] = _apply_point_transform((out["x"], out["y"]), pivot, t)
        out["w"] = out["w"] * t["scale"]
        out["h"] = out["h"] * t["scale"]
    elif shape in ("circle", "ellipse", "arc"):
        out["cx"], out["cy"] = _apply_point_transform((out["cx"], out["cy"]), pivot, t)
        if "r" in out:
            out["r"] = out["r"] * t["scale"]
        if "rx" in out:
            out["rx"] = out["rx"] * t["scale"]
        if "ry" in out:
            out["ry"] = out["ry"] * t["scale"]
        if "start_angle" in out:
            out["start_angle"] = out["start_angle"] + t["rotation"]
        if "end_angle" in out:
            out["end_angle"] = out["end_angle"] + t["rotation"]

    # Curve control points (Phase 8) are stored raw/untransformed and keyed
    # by anchor node id -- without this, a rotated/scaled freehand path's
    # anchors would move but its Bezier handles wouldn't, visibly warping
    # the curve on export. A flattened rect/ellipse boundary (above) never
    # has curve entries of its own, so this is a no-op for it.
    point_ids = out.get("point_ids")
    if point_ids:
        for node_id in point_ids:
            key = curve_prop_key(node_id)
            seg = out.get(key)
            if seg is None:
                continue
            new_seg = dict(seg)
            if seg["kind"] == "cubic":
                new_seg["c1"] = list(_apply_point_transform(tuple(seg["c1"]), pivot, t))
                new_seg["c2"] = list(_apply_point_transform(tuple(seg["c2"]), pivot, t))
            elif seg["kind"] == "quad":
                new_seg["c"] = list(_apply_point_transform(tuple(seg["c"]), pivot, t))
            out[key] = new_seg
    return out


@dataclass(frozen=True)
class DocOp:
    """Routable envelope around one op from one of the document's sub-CRDTs."""

    target: str  # "layer" | "layer_prop" | "path_index" | "path_prop" | "path_geom" | "comment" | "presence" | "setting" | "dimension" | "constraint" | "group" | "sheet" | "sheet_prop"
    payload: dict
    scope: Optional[str] = None  # layer_id / path_id, when the target needs one

    def to_dict(self) -> dict:
        return {"target": self.target, "scope": self.scope, "payload": self.payload}

    @staticmethod
    def from_dict(d: dict) -> "DocOp":
        return DocOp(target=d["target"], scope=d.get("scope"), payload=d["payload"])


class DrawingDocument:
    def __init__(self, clock: LamportClock) -> None:
        self.clock = clock
        self.layers: LWWElementSet[LayerId] = LWWElementSet(clock)
        self.layer_props: dict[LayerId, LWWMap] = {}
        self.path_index: LWWElementSet[PathId] = LWWElementSet(clock)
        self.paths: dict[PathId, RGA[Point]] = {}
        self.path_props: dict[PathId, LWWMap] = {}
        self.comments: LWWMap[CommentId, dict] = LWWMap(clock)
        self.presence: LWWMap[ActorId, dict] = LWWMap(clock)
        # Document-level settings (Phase 11: "units": "px"|"mm"|"in", plus
        # "grid_spacing"/"snap_step" the brief asks for) -- one LWWMap so
        # concurrent edits to different settings merge field-wise for
        # free, same as every other prop bag in this file. Stored
        # geometry is always in raw px-equivalent world units regardless
        # of this setting -- "units" is a *display*-layer conversion
        # (cursor readout, numeric shape input, SVG/DXF export scale),
        # never a migration of existing coordinates.
        self.settings: LWWMap[str, object] = LWWMap(clock)
        # Dimension annotations (Phase 13): a linear dimension references
        # its two anchor points by (path_id, node_id) -- the same stable
        # RGA node id `curve_prop_key` anchors on, not a `point_index`
        # the way `comments` does -- so it keeps resolving to the
        # *correct* point (and "no longer resolvable" is detectable) even
        # after a concurrent insert/delete elsewhere in the same path.
        # Payload: {"a_path", "a_node", "b_path", "b_node", "offset"}.
        self.dimensions: LWWMap[DimId, dict] = LWWMap(clock)
        # Persistent sketch constraints (Phase 14): the solver
        # (geometry.constraints) and /api/solve are built and tested but
        # were, before this, never durable -- applying one was a one-off
        # action, not a fact the document remembered. Payload:
        # {"kind", "anchors": {name: anchor}, "param"}, where each anchor
        # is either {"type": "point", "path_id", "node_id"} (an RGA node
        # id, same anchoring rationale as `dimensions` above) or
        # {"type": "shape_center", "path_id"} (a circle's cx/cy, for the
        # `tangent` kind -- a circle has no RGA points to anchor to at
        # all). Anchor *names* per kind: coincident/fixed_distance use
        # p1/p2; parallel/perpendicular use p1a/p1b/p2a/p2b (a line's two
        # defining points); tangent uses circle/line_a/line_b -- mirrors
        # sketch.js's ANCHOR_NAMES_BY_KIND exactly.
        self.constraints: LWWMap[ConstraintId, dict] = LWWMap(clock)
        # Groups (Phase 15): a `group_id` field on path_props (an
        # ordinary LWW field, so it merges the same way color/width
        # already do) names which group a path belongs to; `groups`
        # itself just tracks which group ids currently exist, mirroring
        # `layers` -- existence only, no per-group properties needed.
        self.groups: LWWElementSet[GroupId] = LWWElementSet(clock)
        # Print sheets (Part 7 C3): existence + per-sheet field map,
        # mirroring `layers`/`layer_props` exactly -- a title block's
        # fields (title/drawn_by/date/drawing_number/revision/notes) and
        # a sheet's page setup (page_size/orientation/margin_mm/
        # scale_mode/scale_ratio) are independently editable by different
        # collaborators, so each field is its own LWWMap entry rather
        # than one atomic dict write (which would let a concurrent edit
        # to "date" silently clobber a concurrent edit to "title").
        # Defaults for any field not yet set are applied by the reader
        # (`sheet_list`), not baked in at creation time, same as
        # `layer_props` never writes a default `visible`/`color`.
        self.sheets: LWWElementSet[SheetId] = LWWElementSet(clock)
        self.sheet_props: dict[SheetId, LWWMap] = {}
        self._undo: list[dict] = []
        self._redo: list[dict] = []

    def _layer_props(self, layer_id: LayerId) -> LWWMap:
        if layer_id not in self.layer_props:
            self.layer_props[layer_id] = LWWMap(self.clock)
        return self.layer_props[layer_id]

    def _path_props(self, path_id: PathId) -> LWWMap:
        if path_id not in self.path_props:
            self.path_props[path_id] = LWWMap(self.clock)
        return self.path_props[path_id]

    def _path_geom(self, path_id: PathId) -> RGA[Point]:
        if path_id not in self.paths:
            self.paths[path_id] = RGA(self.clock)
        return self.paths[path_id]

    def _sheet_props(self, sheet_id: SheetId) -> LWWMap:
        if sheet_id not in self.sheet_props:
            self.sheet_props[sheet_id] = LWWMap(self.clock)
        return self.sheet_props[sheet_id]

    # -- layers -----------------------------------------------------------------
    def add_layer(self, name: str, layer_id: LayerId | None = None) -> tuple[LayerId, list[DocOp]]:
        layer_id = layer_id or new_id("layer")
        ops = [DocOp("layer", self.layers.add(layer_id).to_dict())]
        ops.append(DocOp("layer_prop", self._layer_props(layer_id).set("name", name).to_dict(), scope=layer_id))
        self._undo.append({"kind": "layer_add", "layer_id": layer_id})
        self._redo.clear()
        return layer_id, ops

    def remove_layer(self, layer_id: LayerId) -> DocOp:
        op = DocOp("layer", self.layers.remove(layer_id).to_dict())
        self._undo.append({"kind": "layer_remove", "layer_id": layer_id})
        self._redo.clear()
        return op

    # -- groups (Phase 15) --------------------------------------------------------
    def add_group(self, group_id: GroupId | None = None) -> tuple[GroupId, DocOp]:
        group_id = group_id or new_id("group")
        return group_id, DocOp("group", self.groups.add(group_id).to_dict())

    def remove_group(self, group_id: GroupId) -> DocOp:
        return DocOp("group", self.groups.remove(group_id).to_dict())

    # -- print sheets (Part 7 C3) --------------------------------------------------
    def add_sheet(self, name: str, sheet_id: SheetId | None = None) -> tuple[SheetId, list[DocOp]]:
        """Only `name` is required up front -- every other field (page
        setup, title block text) is set afterward via `set_sheet_prop`
        and defaulted at read time by `sheet_list` (see `SHEET_DEFAULTS`
        in `crdt_cad.export.pdf_io`), the same "don't write defaults the
        reader already knows how to fill in" choice `add_layer` makes
        for `visible`/`color`."""
        sheet_id = sheet_id or new_id("sheet")
        ops = [DocOp("sheet", self.sheets.add(sheet_id).to_dict())]
        ops.append(DocOp("sheet_prop", self._sheet_props(sheet_id).set("name", name).to_dict(), scope=sheet_id))
        return sheet_id, ops

    def remove_sheet(self, sheet_id: SheetId) -> DocOp:
        return DocOp("sheet", self.sheets.remove(sheet_id).to_dict())

    def set_sheet_prop(self, sheet_id: SheetId, key: str, value: Any) -> DocOp:
        op = self._sheet_props(sheet_id).set(key, value)
        return DocOp("sheet_prop", op.to_dict(), scope=sheet_id)

    def sheet_list(self) -> list[dict]:
        return [{"id": sid, **dict(self._sheet_props(sid).items())} for sid in self.sheets]

    # -- paths ------------------------------------------------------------------
    def add_path(
        self, layer_id: LayerId, points: list[Point], color: str = "#111111",
        stroke_width: float = 2.0, path_id: PathId | None = None,
        curves: dict[int, dict] | None = None,
    ) -> tuple[PathId, list[DocOp]]:
        """`curves`, if given, maps a 0-based index into `points` (the
        *arrival* end of a segment -- index i describes how points[i-1]
        connects to points[i]; index 0 is meaningless and ignored, same
        as it would be for an SVG path's initial `M`) to a curve payload
        -- see `curve_prop_key`'s module docstring for the exact shape.
        Indices are only a convenience for building a whole path in one
        call: each one is immediately rekeyed to that anchor's own stable
        RGA node id before becoming a `path_prop` op, so the resulting
        ops (and every concurrent edit built on top of them) are
        index-independent, same as every other op this method emits."""
        path_id = path_id or new_id("path")
        ops = [DocOp("path_index", self.path_index.add(path_id).to_dict())]
        props = self._path_props(path_id)
        ops.append(DocOp("path_prop", props.set("layer_id", layer_id).to_dict(), scope=path_id))
        ops.append(DocOp("path_prop", props.set("color", color).to_dict(), scope=path_id))
        ops.append(DocOp("path_prop", props.set("stroke_width", stroke_width).to_dict(), scope=path_id))
        geom = self._path_geom(path_id)
        prev = None
        for i, point in enumerate(points):
            insert_op = geom.insert_after(prev, point)
            prev = insert_op.id
            ops.append(DocOp("path_geom", insert_op.to_dict(), scope=path_id))
            if curves and i in curves:
                key = curve_prop_key(insert_op.id)
                ops.append(DocOp("path_prop", props.set(key, curves[i]).to_dict(), scope=path_id))
        self._undo.append({"kind": "path_add", "path_id": path_id})
        self._redo.clear()
        return path_id, ops

    def append_point(self, path_id: PathId, point: Point) -> DocOp:
        geom = self._path_geom(path_id)
        op = geom.append(point)
        return DocOp("path_geom", op.to_dict(), scope=path_id)

    def remove_path(self, path_id: PathId) -> DocOp:
        op = DocOp("path_index", self.path_index.remove(path_id).to_dict())
        self._undo.append({"kind": "path_remove", "path_id": path_id})
        self._redo.clear()
        return op

    def set_path_prop(self, path_id: PathId, key: str, value: Any) -> DocOp:
        props = self._path_props(path_id)
        previous = props.get(key)
        had_previous = key in props
        op = props.set(key, value)
        self._undo.append(
            {
                "kind": "prop_set",
                "path_id": path_id,
                "key": key,
                "previous": previous,
                "had_previous": had_previous,
                "forward_value": value,
            }
        )
        self._redo.clear()
        return DocOp("path_prop", op.to_dict(), scope=path_id)

    # -- comments ------------------------------------------------------------
    def add_comment(self, path_id: PathId, point_index: int, text: str, author: ActorId) -> tuple[CommentId, DocOp]:
        comment_id = new_id("comment")
        op = self.comments.set(
            comment_id,
            {"path_id": path_id, "point_index": point_index, "text": text, "author": author},
        )
        return comment_id, DocOp("comment", op.to_dict())

    def remove_comment(self, comment_id: CommentId) -> DocOp:
        return DocOp("comment", self.comments.delete(comment_id).to_dict())

    # -- presence (ephemeral, per-actor) --------------------------------------
    def set_presence(self, actor: ActorId, cursor: dict) -> DocOp:
        op = self.presence.set(actor, cursor)
        return DocOp("presence", op.to_dict())

    # -- document settings (Phase 11: units, grid/snap) -----------------------
    def set_setting(self, key: str, value: object) -> DocOp:
        op = self.settings.set(key, value)
        return DocOp("setting", op.to_dict())

    def settings_dict(self) -> dict:
        return dict(self.settings.items())

    # -- dimension annotations (Phase 13) --------------------------------------
    def add_dimension(
        self, a_path: PathId, a_node, b_path: PathId, b_node, offset: float = 30.0
    ) -> tuple[DimId, DocOp]:
        """`a_node`/`b_node` are `(counter, actor)` pairs (an `OpId`, or
        any 2-tuple/list of the same shape -- e.g. straight off the wire,
        which has no tuple type) identifying each anchor's specific RGA
        node, not a `point_index`; see the `dimensions` field's docstring
        in `__init__` for why. `offset` is the perpendicular distance
        (world px) from the measured line to the rendered dimension
        line, matching standard drafting convention."""
        dim_id = new_id("dim")
        op = self.dimensions.set(
            dim_id,
            {
                "a_path": a_path, "a_node": list(a_node),
                "b_path": b_path, "b_node": list(b_node),
                "offset": offset,
            },
        )
        return dim_id, DocOp("dimension", op.to_dict())

    def remove_dimension(self, dim_id: DimId) -> DocOp:
        return DocOp("dimension", self.dimensions.delete(dim_id).to_dict())

    def _resolve_anchor(self, path_id: PathId, node_id) -> Optional[Point]:
        rga = self.paths.get(path_id)
        if rga is None:
            return None
        return rga.value_at(OpId(node_id[0], node_id[1]))

    def resolve_dimension_points(self, payload: dict) -> Optional[tuple[Point, Point]]:
        """Live positions of a dimension's two anchor points, or None if
        either anchor's path or point no longer exists (e.g. a
        concurrent delete) -- callers (export, `dimension_list`) must
        treat that as "can't currently render this dimension right now,"
        never an error."""
        a = self._resolve_anchor(payload["a_path"], payload["a_node"])
        b = self._resolve_anchor(payload["b_path"], payload["b_node"])
        if a is None or b is None:
            return None
        return a, b

    # -- sketch constraints (Phase 14) -----------------------------------------
    def add_constraint(self, kind: str, anchors: dict, param: float = 0.0) -> tuple[ConstraintId, DocOp]:
        """`anchors` is a name -> anchor dict (see the `constraints`
        field's docstring in `__init__` for the two anchor shapes and
        the per-kind anchor names) -- this is a thin CRDT-storage
        wrapper, not a copy of the solver's own `Constraint` dataclass;
        `crdt_cad.geometry.constraints.Constraint` stays the solver's
        own abstraction with its own transient point ids, unrelated to
        how a constraint durably references real document geometry."""
        constraint_id = new_id("constraint")
        op = self.constraints.set(constraint_id, {"kind": kind, "anchors": anchors, "param": param})
        return constraint_id, DocOp("constraint", op.to_dict())

    def remove_constraint(self, constraint_id: ConstraintId) -> DocOp:
        return DocOp("constraint", self.constraints.delete(constraint_id).to_dict())

    # -- undo / redo: inverted ops, not snapshots --------------------------------
    def undo(self) -> list[DocOp]:
        if not self._undo:
            return []
        entry = self._undo.pop()
        ops = self._apply_inverse(entry)
        self._redo.append(entry)
        return ops

    def redo(self) -> list[DocOp]:
        if not self._redo:
            return []
        entry = self._redo.pop()
        ops = self._apply_forward(entry)
        self._undo.append(entry)
        return ops

    def _apply_inverse(self, entry: dict) -> list[DocOp]:
        kind = entry["kind"]
        if kind == "layer_add":
            return [DocOp("layer", self.layers.remove(entry["layer_id"]).to_dict())]
        if kind == "layer_remove":
            return [DocOp("layer", self.layers.add(entry["layer_id"]).to_dict())]
        if kind == "path_add":
            return [DocOp("path_index", self.path_index.remove(entry["path_id"]).to_dict())]
        if kind == "path_remove":
            return [DocOp("path_index", self.path_index.add(entry["path_id"]).to_dict())]
        if kind == "prop_set":
            props = self._path_props(entry["path_id"])
            if entry["had_previous"]:
                op = props.set(entry["key"], entry["previous"])
            else:
                op = props.delete(entry["key"])
            return [DocOp("path_prop", op.to_dict(), scope=entry["path_id"])]
        raise ValueError(f"unknown undo entry kind: {kind}")

    def _apply_forward(self, entry: dict) -> list[DocOp]:
        kind = entry["kind"]
        if kind == "layer_add":
            return [DocOp("layer", self.layers.add(entry["layer_id"]).to_dict())]
        if kind == "layer_remove":
            return [DocOp("layer", self.layers.remove(entry["layer_id"]).to_dict())]
        if kind == "path_add":
            return [DocOp("path_index", self.path_index.add(entry["path_id"]).to_dict())]
        if kind == "path_remove":
            return [DocOp("path_index", self.path_index.remove(entry["path_id"]).to_dict())]
        if kind == "prop_set":
            props = self._path_props(entry["path_id"])
            op = props.set(entry["key"], entry["forward_value"])
            return [DocOp("path_prop", op.to_dict(), scope=entry["path_id"])]
        raise ValueError(f"unknown redo entry kind: {kind}")

    # -- remote application -------------------------------------------------------
    def apply(self, op: DocOp) -> bool:
        if op.target == "layer":
            return self.layers.apply(LWWOp.from_dict(op.payload))
        if op.target == "layer_prop":
            assert op.scope is not None
            return self._layer_props(op.scope).apply(LWWOp.from_dict(op.payload))
        if op.target == "path_index":
            return self.path_index.apply(LWWOp.from_dict(op.payload))
        if op.target == "path_prop":
            assert op.scope is not None
            return self._path_props(op.scope).apply(LWWOp.from_dict(op.payload))
        if op.target == "path_geom":
            assert op.scope is not None
            return self._path_geom(op.scope).apply(op_from_dict(op.payload))
        if op.target == "comment":
            return self.comments.apply(LWWOp.from_dict(op.payload))
        if op.target == "presence":
            return self.presence.apply(LWWOp.from_dict(op.payload))
        if op.target == "setting":
            return self.settings.apply(LWWOp.from_dict(op.payload))
        if op.target == "dimension":
            return self.dimensions.apply(LWWOp.from_dict(op.payload))
        if op.target == "constraint":
            return self.constraints.apply(LWWOp.from_dict(op.payload))
        if op.target == "group":
            return self.groups.apply(LWWOp.from_dict(op.payload))
        if op.target == "sheet":
            return self.sheets.apply(LWWOp.from_dict(op.payload))
        if op.target == "sheet_prop":
            assert op.scope is not None
            return self._sheet_props(op.scope).apply(LWWOp.from_dict(op.payload))
        raise ValueError(f"unknown doc op target: {op.target}")

    # -- state-based merge ------------------------------------------------------
    def merge(self, other: "DrawingDocument") -> bool:
        changed = False
        changed |= self.layers.merge(other.layers)
        for layer_id in set(self.layer_props) | set(other.layer_props):
            if layer_id in other.layer_props:
                changed |= self._layer_props(layer_id).merge(other.layer_props[layer_id])
        changed |= self.path_index.merge(other.path_index)
        for path_id in set(self.path_props) | set(other.path_props):
            if path_id in other.path_props:
                changed |= self._path_props(path_id).merge(other.path_props[path_id])
        for path_id in set(self.paths) | set(other.paths):
            if path_id in other.paths:
                changed |= self._path_geom(path_id).merge(other.paths[path_id])
        changed |= self.comments.merge(other.comments)
        changed |= self.presence.merge(other.presence)
        changed |= self.settings.merge(other.settings)
        changed |= self.dimensions.merge(other.dimensions)
        changed |= self.constraints.merge(other.constraints)
        changed |= self.groups.merge(other.groups)
        changed |= self.sheets.merge(other.sheets)
        for sheet_id in set(self.sheet_props) | set(other.sheet_props):
            if sheet_id in other.sheet_props:
                changed |= self._sheet_props(sheet_id).merge(other.sheet_props[sheet_id])
        return changed

    # -- reads ------------------------------------------------------------------
    def layer_list(self) -> list[dict]:
        # Iterates `self.layers` directly (not `.to_set()`, which
        # discards order by converting to a real `set`) -- LWWElementSet
        # is backed by an LWWMap whose dict preserves each element's
        # *first-added* position, so this is genuine creation order, not
        # an accident of hashing. Phase 15 (fills) needs "layer order,
        # then creation order" for correct z-order; this is what
        # supplies the "layer order" half.
        return [
            {"id": lid, **dict(self._layer_props(lid).items())}
            for lid in self.layers
        ]

    def path_list(self) -> list[dict]:
        out = []
        for pid in self.path_index:
            props = dict(self._path_props(pid).items())
            entries = self._path_geom(pid).entries()
            out.append(
                {
                    "id": pid,
                    "points": [pt for _node_id, pt in entries],
                    # Stringified node ids, aligned index-for-index with
                    # "points" -- lets a consumer (svg_io's exporter,
                    # flatten_path_to_polyline) look up curve_prop_key(id)
                    # in the spread-in props above to know how each
                    # segment should be drawn.
                    "point_ids": [str(node_id) for node_id, _pt in entries],
                    **props,
                }
            )
        return out

    def path_points(self, path_id: PathId) -> list[Point]:
        """Public read accessor: current live points of one path, in
        order. Used by the server's pre-commit validity gate to check a
        candidate new point against what a path currently looks like
        without needing to touch the (otherwise internal) sub-CRDTs."""
        return self._path_geom(path_id).values()

    def path_props_dict(self, path_id: PathId) -> dict:
        return dict(self._path_props(path_id).items())

    def comment_list(self) -> list[dict]:
        return [{"id": cid, **payload} for cid, payload in self.comments.items()]

    def presence_list(self) -> list[dict]:
        return [{"actor": actor, **payload} for actor, payload in self.presence.items()]

    def dimension_list(self) -> list[dict]:
        """One dict per stored dimension, `a_pos`/`b_pos` filled in with
        the anchors' *live* resolved positions when both still exist
        (absent otherwise, e.g. a concurrent delete of one anchor point)
        -- callers (SVG/DXF export) don't need to re-derive resolution
        themselves."""
        out = []
        for dim_id, payload in self.dimensions.items():
            entry = {"id": dim_id, **payload}
            resolved = self.resolve_dimension_points(payload)
            if resolved is not None:
                entry["a_pos"], entry["b_pos"] = resolved
            out.append(entry)
        return out

    def constraint_list(self) -> list[dict]:
        return [{"id": cid, **payload} for cid, payload in self.constraints.items()]

    def group_list(self) -> list[GroupId]:
        return list(self.groups)

    # -- delta sync ---------------------------------------------------------------
    def frontier(self) -> VectorClock:
        vc = self.layers.frontier()
        for m in self.layer_props.values():
            vc = vc.merge(m.frontier())
        vc = vc.merge(self.path_index.frontier())
        for m in self.path_props.values():
            vc = vc.merge(m.frontier())
        for rga in self.paths.values():
            vc = vc.merge(rga.frontier())
        vc = vc.merge(self.comments.frontier())
        vc = vc.merge(self.presence.frontier())
        vc = vc.merge(self.settings.frontier())
        vc = vc.merge(self.dimensions.frontier())
        vc = vc.merge(self.constraints.frontier())
        vc = vc.merge(self.groups.frontier())
        vc = vc.merge(self.sheets.frontier())
        for m in self.sheet_props.values():
            vc = vc.merge(m.frontier())
        return vc

    def ops_since(self, vc: VectorClock) -> list[DocOp]:
        out: list[DocOp] = [DocOp("layer", op.to_dict()) for op in self.layers.ops_since(vc)]
        for layer_id, m in self.layer_props.items():
            out += [DocOp("layer_prop", op.to_dict(), scope=layer_id) for op in m.ops_since(vc)]
        out += [DocOp("path_index", op.to_dict()) for op in self.path_index.ops_since(vc)]
        for path_id, m in self.path_props.items():
            out += [DocOp("path_prop", op.to_dict(), scope=path_id) for op in m.ops_since(vc)]
        for path_id, rga in self.paths.items():
            out += [DocOp("path_geom", op.to_dict(), scope=path_id) for op in rga.ops_since(vc)]
        out += [DocOp("comment", op.to_dict()) for op in self.comments.ops_since(vc)]
        out += [DocOp("presence", op.to_dict()) for op in self.presence.ops_since(vc)]
        out += [DocOp("setting", op.to_dict()) for op in self.settings.ops_since(vc)]
        out += [DocOp("dimension", op.to_dict()) for op in self.dimensions.ops_since(vc)]
        out += [DocOp("constraint", op.to_dict()) for op in self.constraints.ops_since(vc)]
        out += [DocOp("group", op.to_dict()) for op in self.groups.ops_since(vc)]
        out += [DocOp("sheet", op.to_dict()) for op in self.sheets.ops_since(vc)]
        for sheet_id, m in self.sheet_props.items():
            out += [DocOp("sheet_prop", op.to_dict(), scope=sheet_id) for op in m.ops_since(vc)]
        return out

    # -- serialization --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "layers": self.layers.to_dict(),
            "layer_props": {lid: m.to_dict() for lid, m in self.layer_props.items()},
            "path_index": self.path_index.to_dict(),
            "path_props": {pid: m.to_dict() for pid, m in self.path_props.items()},
            "paths": {pid: rga.to_dict() for pid, rga in self.paths.items()},
            "comments": self.comments.to_dict(),
            "presence": self.presence.to_dict(),
            "settings": self.settings.to_dict(),
            "dimensions": self.dimensions.to_dict(),
            "constraints": self.constraints.to_dict(),
            "groups": self.groups.to_dict(),
            "sheets": self.sheets.to_dict(),
            "sheet_props": {sid: m.to_dict() for sid, m in self.sheet_props.items()},
        }

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "DrawingDocument":
        doc = DrawingDocument(clock)
        doc.layers = LWWElementSet.from_dict(clock, d["layers"])
        doc.layer_props = {lid: LWWMap.from_dict(clock, m) for lid, m in d["layer_props"].items()}
        doc.path_index = LWWElementSet.from_dict(clock, d["path_index"])
        doc.path_props = {pid: LWWMap.from_dict(clock, m) for pid, m in d["path_props"].items()}
        doc.paths = {pid: RGA.from_dict(clock, r) for pid, r in d["paths"].items()}
        doc.comments = LWWMap.from_dict(clock, d["comments"])
        doc.presence = LWWMap.from_dict(clock, d["presence"])
        # "settings" (Phase 11) is absent from any snapshot persisted
        # before this existed -- default to an empty LWWMap rather than
        # KeyError, so old rooms still load cleanly.
        doc.settings = LWWMap.from_dict(clock, d["settings"]) if "settings" in d else LWWMap(clock)
        # Same backward-compat default for "dimensions" (Phase 13).
        doc.dimensions = LWWMap.from_dict(clock, d["dimensions"]) if "dimensions" in d else LWWMap(clock)
        # Same backward-compat default for "constraints" (Phase 14).
        doc.constraints = LWWMap.from_dict(clock, d["constraints"]) if "constraints" in d else LWWMap(clock)
        # Same backward-compat default for "groups" (Phase 15).
        doc.groups = LWWElementSet.from_dict(clock, d["groups"]) if "groups" in d else LWWElementSet(clock)
        # Same backward-compat default for "sheets" (Part 7 C3).
        doc.sheets = LWWElementSet.from_dict(clock, d["sheets"]) if "sheets" in d else LWWElementSet(clock)
        doc.sheet_props = (
            {sid: LWWMap.from_dict(clock, m) for sid, m in d["sheet_props"].items()} if "sheet_props" in d else {}
        )
        return doc

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "DrawingDocument":
        return DrawingDocument.from_dict(clock, loads_msgpack(data))
