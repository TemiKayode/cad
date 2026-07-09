"""MeshCRDT -- a composite CRDT for 3D vertices / edges / face boundaries.

Design note: rather than inventing a bespoke, from-first-principles mesh
merge algorithm (risky -- mesh CRDTs are an open research area and a
hand-rolled version would be very easy to get subtly wrong), this composes
the two proven primitives already in this package:

- ``vertices``: an :class:`~crdt_cad.crdt.lww.LWWMap` from vertex id to
  ``(x, y, z)`` position. Concurrent moves of the same vertex resolve by
  last-writer-wins; concurrent moves of *different* vertices never
  conflict.
- ``edges``: an :class:`~crdt_cad.crdt.lww.LWWElementSet` of canonical
  ``(vertex_id, vertex_id)`` pairs, for wireframe/topology existence.
- ``face_index``: an :class:`LWWElementSet` of face ids -- whether a face
  currently exists at all.
- ``faces``: one :class:`~crdt_cad.crdt.rga.RGA` per face id, holding the
  ordered loop of vertex ids that bounds that face. Using an RGA here
  (rather than a plain list) is what lets two users concurrently insert
  vertices into the *same* face boundary (e.g. both splitting an edge of
  the same face while offline) without clobbering each other.

Merging a mesh is therefore just "merge each component independently",
which inherits convergence from each component's own proof -- composing
CRDTs this way is itself a standard, sound technique.

What this layer deliberately does **not** do: enforce manifoldness, face
winding, planarity, or reject self-intersecting topology. That validation
belongs to the geometry kernel, which must approve an edit *before* it is
turned into ops and applied here (see the "reject invalid geometry before
committing to the CRDT" requirement) -- the CRDT's job is only to make
already-valid edits merge without conflict, not to define validity.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.lww import LWWElementSet, LWWMap, LWWOp
from crdt_cad.crdt.rga import RGA, op_from_dict
from crdt_cad.crdt.serialize import dumps_msgpack, loads_msgpack

VertexId = str
FaceId = str
Position = tuple[float, float, float]
Edge = tuple[VertexId, VertexId]

_EDGE_KEY_SEP = "\x1f"  # unit separator; edges are stored as string keys (not
# tuples) because tuple dict-keys don't survive a JSON/MessagePack round trip
# (they decode back as lists, which are unhashable and would crash on lookup).


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def canonical_edge(v1: VertexId, v2: VertexId) -> str:
    a, b = (v1, v2) if v1 <= v2 else (v2, v1)
    return f"{a}{_EDGE_KEY_SEP}{b}"


def decode_edge(key: str) -> Edge:
    a, b = key.split(_EDGE_KEY_SEP)
    return (a, b)


@dataclass(frozen=True)
class MeshOp:
    """A routable envelope around one op from one of the mesh's sub-CRDTs."""

    target: str  # "vertex" | "edge" | "face_index" | "face_geom" | "face_prop" | "presence" | "generation"
    payload: dict
    face_id: Optional[FaceId] = None

    def to_dict(self) -> dict:
        return {"target": self.target, "face_id": self.face_id, "payload": self.payload}

    @staticmethod
    def from_dict(d: dict) -> "MeshOp":
        return MeshOp(target=d["target"], face_id=d.get("face_id"), payload=d["payload"])


class MeshCRDT:
    def __init__(self, clock: LamportClock) -> None:
        self._clock = clock
        self.vertices: LWWMap[VertexId, Position] = LWWMap(clock)
        self.edges: LWWElementSet[str] = LWWElementSet(clock)
        self.face_index: LWWElementSet[FaceId] = LWWElementSet(clock)
        self.faces: dict[FaceId, RGA[VertexId]] = {}
        self.face_props: dict[FaceId, LWWMap] = {}  # face_id -> {color, material, ...}
        self.presence: LWWMap[str, dict] = LWWMap(clock)  # actor id -> ephemeral cursor/focus payload
        # Phase G4 (Part 5): AI-generation provenance/spec-persistence record,
        # keyed by generation id -> {"prompt", "generator_name", "spec",
        # "interpretation_source", "mesh_source"}. An LWWMap (not a plain
        # dict) for the same reason every other piece of shared room state
        # is one: it needs to merge across replicas without a hand-rolled
        # conflict rule, and a whole-record last-writer-wins is the right
        # granularity here (an edit always replaces the *entire* record,
        # never patches one field of it, so there's no reason to split it
        # into per-field entries the way face_props does).
        self.generations: LWWMap[str, dict] = LWWMap(clock)
        self._undo: list[dict] = []
        self._redo: list[dict] = []

    def _face_rga(self, face_id: FaceId) -> RGA[VertexId]:
        if face_id not in self.faces:
            self.faces[face_id] = RGA(self._clock)
        return self.faces[face_id]

    def _face_props(self, face_id: FaceId) -> LWWMap:
        if face_id not in self.face_props:
            self.face_props[face_id] = LWWMap(self._clock)
        return self.face_props[face_id]

    # -- local mutation: vertices --------------------------------------------
    def add_vertex(self, vertex_id: VertexId, position: Position) -> MeshOp:
        """Creates a new vertex, or moves an existing one -- whichever this
        turns out to be is recorded distinctly for undo: undoing a move
        restores the previous position, undoing a create removes the vertex
        entirely. See ``move_vertex`` for the same call under the name that
        makes the "existing vertex" case read more naturally at call sites."""
        if vertex_id in self.vertices:
            self._undo.append(
                {"kind": "vertex_move", "vertex_id": vertex_id, "previous": self.vertices.get(vertex_id), "forward": position}
            )
        else:
            self._undo.append({"kind": "vertex_create", "vertex_id": vertex_id, "position": position})
        self._redo.clear()
        op = self.vertices.set(vertex_id, position)
        return MeshOp("vertex", op.to_dict())

    def move_vertex(self, vertex_id: VertexId, position: Position) -> MeshOp:
        return self.add_vertex(vertex_id, position)

    def remove_vertex(self, vertex_id: VertexId) -> MeshOp:
        self._undo.append({"kind": "vertex_remove", "vertex_id": vertex_id, "previous": self.vertices.get(vertex_id)})
        self._redo.clear()
        op = self.vertices.delete(vertex_id)
        return MeshOp("vertex", op.to_dict())

    # -- local mutation: edges -----------------------------------------------
    def add_edge(self, v1: VertexId, v2: VertexId) -> MeshOp:
        self._undo.append({"kind": "edge_add", "v1": v1, "v2": v2})
        self._redo.clear()
        op = self.edges.add(canonical_edge(v1, v2))
        return MeshOp("edge", op.to_dict())

    def remove_edge(self, v1: VertexId, v2: VertexId) -> MeshOp:
        self._undo.append({"kind": "edge_remove", "v1": v1, "v2": v2})
        self._redo.clear()
        op = self.edges.remove(canonical_edge(v1, v2))
        return MeshOp("edge", op.to_dict())

    # -- local mutation: faces -----------------------------------------------
    def add_face(self, face_id: FaceId, vertex_loop: list[VertexId]) -> list[MeshOp]:
        """Create a face whose boundary is the given ordered vertex loop."""
        self._undo.append({"kind": "face_add", "face_id": face_id})
        self._redo.clear()
        ops: list[MeshOp] = [MeshOp("face_index", self.face_index.add(face_id).to_dict())]
        rga = self._face_rga(face_id)
        prev = rga.last_id()
        for vertex_id in vertex_loop:
            insert_op = rga.insert_after(prev, vertex_id)
            prev = insert_op.id
            ops.append(MeshOp("face_geom", insert_op.to_dict(), face_id=face_id))
        return ops

    def remove_face(self, face_id: FaceId) -> MeshOp:
        self._undo.append({"kind": "face_remove", "face_id": face_id})
        self._redo.clear()
        op = self.face_index.remove(face_id)
        return MeshOp("face_index", op.to_dict())

    def set_face_prop(self, face_id: FaceId, key: str, value) -> MeshOp:
        props = self._face_props(face_id)
        had_previous = key in props
        self._undo.append(
            {
                "kind": "face_prop_set",
                "face_id": face_id,
                "key": key,
                "previous": props.get(key) if had_previous else None,
                "had_previous": had_previous,
                "forward_value": value,
            }
        )
        self._redo.clear()
        op = props.set(key, value)
        return MeshOp("face_prop", op.to_dict(), face_id=face_id)

    # -- local mutation: AI-generation provenance/spec records (Phase G4) ----
    def set_generation(self, generation_id: str, record: dict) -> MeshOp:
        had_previous = generation_id in self.generations
        self._undo.append(
            {
                "kind": "generation_set",
                "generation_id": generation_id,
                "previous": self.generations.get(generation_id) if had_previous else None,
                "had_previous": had_previous,
                "forward_value": record,
            }
        )
        self._redo.clear()
        op = self.generations.set(generation_id, record)
        return MeshOp("generation", op.to_dict())

    def generation(self, generation_id: str) -> Optional[dict]:
        return self.generations.get(generation_id)

    def generations_dict(self) -> dict[str, dict]:
        return dict(self.generations.items())

    def extrude_face(self, face_id: FaceId, height: float) -> list[MeshOp]:
        """Extrudes a face along +Y by ``height``: duplicates its boundary
        loop at the new height, connects old-to-new with one side face per
        edge, and caps the top with a face over the new loop -- exactly
        mirroring the client-side ``extrudeFace()`` in ``mesh3d.js``.

        Bundled as a **single** undo entry covering every vertex, edge, and
        face this creates: undoing an extrude removes all of it in one
        step, regardless of what else may have concurrently changed
        elsewhere in the mesh (a concurrent, unrelated vertex move is a
        separate undo entry on a possibly different replica entirely, and
        is never touched by this one -- see
        ``test_undo_extrude_does_not_clobber_concurrent_vertex_move``).
        """
        loop = self.face_loops().get(face_id)
        if not loop or len(loop) < 3:
            raise ValueError(f"cannot extrude face {face_id!r}: not found or degenerate")

        undo_mark = len(self._undo)
        ops: list[MeshOp] = []
        positions = self.vertex_positions()

        new_loop = []
        for vid in loop:
            x, y, z = positions[vid]
            new_vid = new_id("v")
            ops.append(self.add_vertex(new_vid, (x, y + height, z)))
            new_loop.append(new_vid)

        def _ring(ring: list[VertexId]) -> None:
            for i in range(len(ring)):
                ops.append(self.add_edge(ring[i], ring[(i + 1) % len(ring)]))

        for i in range(len(loop)):
            j = (i + 1) % len(loop)
            side_loop = [loop[i], loop[j], new_loop[j], new_loop[i]]
            ops.extend(self.add_face(new_id("face"), side_loop))
            _ring(side_loop)

        ops.extend(self.add_face(new_id("face"), new_loop))
        _ring(new_loop)

        sub_entries = self._undo[undo_mark:]
        del self._undo[undo_mark:]
        self._undo.append({"kind": "composite", "entries": sub_entries})
        self._redo.clear()
        return ops

    def insert_face_vertex(
        self, face_id: FaceId, after: Optional[object], vertex_id: VertexId
    ) -> MeshOp:
        rga = self._face_rga(face_id)
        op = rga.insert_after(after, vertex_id)
        return MeshOp("face_geom", op.to_dict(), face_id=face_id)

    def remove_face_vertex(self, face_id: FaceId, target: object) -> MeshOp:
        rga = self._face_rga(face_id)
        op = rga.delete(target)
        return MeshOp("face_geom", op.to_dict(), face_id=face_id)

    # -- undo / redo: inverted ops, not snapshots --------------------------------
    #
    # Same rule as DrawingDocument (crdt/document.py): undo/redo never touch
    # history directly, they synthesize the *opposite* edit and run it
    # through a fresh OpId, so the result is just another op that merges
    # like any other -- it undoes *this actor's* change without disturbing
    # a concurrent change made by anyone else. _apply_inverse/_apply_forward
    # operate directly on the raw sub-CRDTs (self.vertices, self.edges,
    # self.face_index, ...), not through add_vertex/add_face/etc., so
    # replaying an undo never itself records a *new* undo entry.
    def undo(self) -> list[MeshOp]:
        if not self._undo:
            return []
        entry = self._undo.pop()
        ops = self._apply_inverse(entry)
        self._redo.append(entry)
        return ops

    def redo(self) -> list[MeshOp]:
        if not self._redo:
            return []
        entry = self._redo.pop()
        ops = self._apply_forward(entry)
        self._undo.append(entry)
        return ops

    def _apply_inverse(self, entry: dict) -> list[MeshOp]:
        kind = entry["kind"]
        if kind == "composite":
            ops: list[MeshOp] = []
            for sub in reversed(entry["entries"]):
                ops.extend(self._apply_inverse(sub))
            return ops
        if kind == "vertex_create":
            return [MeshOp("vertex", self.vertices.delete(entry["vertex_id"]).to_dict())]
        if kind in ("vertex_move", "vertex_remove"):
            return [MeshOp("vertex", self.vertices.set(entry["vertex_id"], entry["previous"]).to_dict())]
        if kind == "edge_add":
            return [MeshOp("edge", self.edges.remove(canonical_edge(entry["v1"], entry["v2"])).to_dict())]
        if kind == "edge_remove":
            return [MeshOp("edge", self.edges.add(canonical_edge(entry["v1"], entry["v2"])).to_dict())]
        if kind == "face_add":
            return [MeshOp("face_index", self.face_index.remove(entry["face_id"]).to_dict())]
        if kind == "face_remove":
            return [MeshOp("face_index", self.face_index.add(entry["face_id"]).to_dict())]
        if kind == "face_prop_set":
            props = self._face_props(entry["face_id"])
            op = props.set(entry["key"], entry["previous"]) if entry["had_previous"] else props.delete(entry["key"])
            return [MeshOp("face_prop", op.to_dict(), face_id=entry["face_id"])]
        if kind == "generation_set":
            op = (
                self.generations.set(entry["generation_id"], entry["previous"])
                if entry["had_previous"] else self.generations.delete(entry["generation_id"])
            )
            return [MeshOp("generation", op.to_dict())]
        raise ValueError(f"unknown undo entry kind: {kind}")

    def _apply_forward(self, entry: dict) -> list[MeshOp]:
        kind = entry["kind"]
        if kind == "composite":
            ops: list[MeshOp] = []
            for sub in entry["entries"]:
                ops.extend(self._apply_forward(sub))
            return ops
        if kind == "vertex_create":
            return [MeshOp("vertex", self.vertices.set(entry["vertex_id"], entry["position"]).to_dict())]
        if kind == "vertex_move":
            return [MeshOp("vertex", self.vertices.set(entry["vertex_id"], entry["forward"]).to_dict())]
        if kind == "vertex_remove":
            return [MeshOp("vertex", self.vertices.delete(entry["vertex_id"]).to_dict())]
        if kind == "edge_add":
            return [MeshOp("edge", self.edges.add(canonical_edge(entry["v1"], entry["v2"])).to_dict())]
        if kind == "edge_remove":
            return [MeshOp("edge", self.edges.remove(canonical_edge(entry["v1"], entry["v2"])).to_dict())]
        if kind == "face_add":
            return [MeshOp("face_index", self.face_index.add(entry["face_id"]).to_dict())]
        if kind == "face_remove":
            return [MeshOp("face_index", self.face_index.remove(entry["face_id"]).to_dict())]
        if kind == "face_prop_set":
            op = self._face_props(entry["face_id"]).set(entry["key"], entry["forward_value"])
            return [MeshOp("face_prop", op.to_dict(), face_id=entry["face_id"])]
        if kind == "generation_set":
            op = self.generations.set(entry["generation_id"], entry["forward_value"])
            return [MeshOp("generation", op.to_dict())]
        raise ValueError(f"unknown redo entry kind: {kind}")

    # -- local mutation: presence (ephemeral, per-actor) ----------------------
    def set_presence(self, actor: str, payload: dict) -> MeshOp:
        op = self.presence.set(actor, payload)
        return MeshOp("presence", op.to_dict())

    # -- remote application ---------------------------------------------------
    def apply(self, op: MeshOp) -> bool:
        if op.target == "vertex":
            return self.vertices.apply(LWWOp.from_dict(op.payload))
        if op.target == "edge":
            return self.edges.apply(LWWOp.from_dict(op.payload))
        if op.target == "face_index":
            return self.face_index.apply(LWWOp.from_dict(op.payload))
        if op.target == "face_geom":
            assert op.face_id is not None
            return self._face_rga(op.face_id).apply(op_from_dict(op.payload))
        if op.target == "face_prop":
            assert op.face_id is not None
            return self._face_props(op.face_id).apply(LWWOp.from_dict(op.payload))
        if op.target == "presence":
            return self.presence.apply(LWWOp.from_dict(op.payload))
        if op.target == "generation":
            return self.generations.apply(LWWOp.from_dict(op.payload))
        raise ValueError(f"unknown mesh op target: {op.target}")

    # -- state-based merge ------------------------------------------------------
    def merge(self, other: "MeshCRDT") -> bool:
        changed = False
        changed |= self.vertices.merge(other.vertices)
        changed |= self.edges.merge(other.edges)
        changed |= self.face_index.merge(other.face_index)
        for face_id in set(self.faces) | set(other.faces):
            if face_id in other.faces:
                changed |= self._face_rga(face_id).merge(other.faces[face_id])
        for face_id in set(self.face_props) | set(other.face_props):
            if face_id in other.face_props:
                changed |= self._face_props(face_id).merge(other.face_props[face_id])
        changed |= self.presence.merge(other.presence)
        changed |= self.generations.merge(other.generations)
        return changed

    # -- reads ------------------------------------------------------------------
    def vertex_positions(self) -> dict[VertexId, Position]:
        return dict(self.vertices.items())

    def edge_set(self) -> set[Edge]:
        return {decode_edge(e) for e in self.edges.to_set()}

    def face_loops(self) -> dict[FaceId, list[VertexId]]:
        live_faces = self.face_index.to_set()
        return {
            fid: self.faces[fid].values()
            for fid in live_faces
            if fid in self.faces
        }

    def face_props_dict(self, face_id: FaceId) -> dict:
        return dict(self._face_props(face_id).items())

    # -- delta sync ---------------------------------------------------------
    def frontier(self) -> VectorClock:
        vc = self.vertices.frontier()
        vc = vc.merge(self.edges.frontier())
        vc = vc.merge(self.face_index.frontier())
        for rga in self.faces.values():
            vc = vc.merge(rga.frontier())
        for m in self.face_props.values():
            vc = vc.merge(m.frontier())
        vc = vc.merge(self.presence.frontier())
        vc = vc.merge(self.generations.frontier())
        return vc

    def ops_since(self, vc: VectorClock) -> list[MeshOp]:
        out: list[MeshOp] = [MeshOp("vertex", op.to_dict()) for op in self.vertices.ops_since(vc)]
        out += [MeshOp("edge", op.to_dict()) for op in self.edges.ops_since(vc)]
        out += [MeshOp("face_index", op.to_dict()) for op in self.face_index.ops_since(vc)]
        for face_id, rga in self.faces.items():
            out += [
                MeshOp("face_geom", op.to_dict(), face_id=face_id)
                for op in rga.ops_since(vc)
            ]
        for face_id, m in self.face_props.items():
            out += [MeshOp("face_prop", op.to_dict(), face_id=face_id) for op in m.ops_since(vc)]
        out += [MeshOp("presence", op.to_dict()) for op in self.presence.ops_since(vc)]
        out += [MeshOp("generation", op.to_dict()) for op in self.generations.ops_since(vc)]
        return out

    # -- reads: presence ------------------------------------------------------
    def presence_list(self) -> list[dict]:
        return [{"actor": actor, **payload} for actor, payload in self.presence.items()]

    # -- serialization ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "vertices": self.vertices.to_dict(),
            "edges": self.edges.to_dict(),
            "face_index": self.face_index.to_dict(),
            "faces": {fid: rga.to_dict() for fid, rga in self.faces.items()},
            "face_props": {fid: m.to_dict() for fid, m in self.face_props.items()},
            "presence": self.presence.to_dict(),
            "generations": self.generations.to_dict(),
        }

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "MeshCRDT":
        mesh = MeshCRDT(clock)
        mesh.vertices = LWWMap.from_dict(clock, d["vertices"])
        mesh.edges = LWWElementSet.from_dict(clock, d["edges"])
        mesh.face_index = LWWElementSet.from_dict(clock, d["face_index"])
        mesh.faces = {
            fid: RGA.from_dict(clock, rga_dict) for fid, rga_dict in d["faces"].items()
        }
        if "face_props" in d:
            mesh.face_props = {
                fid: LWWMap.from_dict(clock, m) for fid, m in d["face_props"].items()
            }
        if "presence" in d:
            mesh.presence = LWWMap.from_dict(clock, d["presence"])
        if "generations" in d:
            mesh.generations = LWWMap.from_dict(clock, d["generations"])
        return mesh

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "MeshCRDT":
        return MeshCRDT.from_dict(clock, loads_msgpack(data))
