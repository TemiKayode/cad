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

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from crdt_cad.crdt.clock import ActorId, LamportClock, VectorClock
from crdt_cad.crdt.lww import LWWElementSet, LWWMap, LWWOp
from crdt_cad.crdt.rga import RGA, op_from_dict
from crdt_cad.crdt.serialize import (
    dumps_msgpack,
    loads_msgpack,
)

PathId = str
LayerId = str
CommentId = str
Point = tuple[float, float]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass(frozen=True)
class DocOp:
    """Routable envelope around one op from one of the document's sub-CRDTs."""

    target: str  # "layer" | "layer_prop" | "path_index" | "path_prop" | "path_geom" | "comment" | "presence"
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

    # -- paths ------------------------------------------------------------------
    def add_path(
        self, layer_id: LayerId, points: list[Point], color: str = "#111111",
        stroke_width: float = 2.0, path_id: PathId | None = None,
    ) -> tuple[PathId, list[DocOp]]:
        path_id = path_id or new_id("path")
        ops = [DocOp("path_index", self.path_index.add(path_id).to_dict())]
        props = self._path_props(path_id)
        ops.append(DocOp("path_prop", props.set("layer_id", layer_id).to_dict(), scope=path_id))
        ops.append(DocOp("path_prop", props.set("color", color).to_dict(), scope=path_id))
        ops.append(DocOp("path_prop", props.set("stroke_width", stroke_width).to_dict(), scope=path_id))
        geom = self._path_geom(path_id)
        prev = None
        for point in points:
            insert_op = geom.insert_after(prev, point)
            prev = insert_op.id
            ops.append(DocOp("path_geom", insert_op.to_dict(), scope=path_id))
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
        return changed

    # -- reads ------------------------------------------------------------------
    def layer_list(self) -> list[dict]:
        return [
            {"id": lid, **dict(self._layer_props(lid).items())}
            for lid in self.layers.to_set()
        ]

    def path_list(self) -> list[dict]:
        out = []
        for pid in self.path_index.to_set():
            props = dict(self._path_props(pid).items())
            points = self._path_geom(pid).values()
            out.append({"id": pid, "points": points, **props})
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
        return doc

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "DrawingDocument":
        return DrawingDocument.from_dict(clock, loads_msgpack(data))
