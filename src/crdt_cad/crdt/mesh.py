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


def canonical_edge(v1: VertexId, v2: VertexId) -> str:
    a, b = (v1, v2) if v1 <= v2 else (v2, v1)
    return f"{a}{_EDGE_KEY_SEP}{b}"


def decode_edge(key: str) -> Edge:
    a, b = key.split(_EDGE_KEY_SEP)
    return (a, b)


@dataclass(frozen=True)
class MeshOp:
    """A routable envelope around one op from one of the mesh's sub-CRDTs."""

    target: str  # "vertex" | "edge" | "face_index" | "face_geom"
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
        op = self.vertices.set(vertex_id, position)
        return MeshOp("vertex", op.to_dict())

    def move_vertex(self, vertex_id: VertexId, position: Position) -> MeshOp:
        return self.add_vertex(vertex_id, position)

    def remove_vertex(self, vertex_id: VertexId) -> MeshOp:
        op = self.vertices.delete(vertex_id)
        return MeshOp("vertex", op.to_dict())

    # -- local mutation: edges -----------------------------------------------
    def add_edge(self, v1: VertexId, v2: VertexId) -> MeshOp:
        op = self.edges.add(canonical_edge(v1, v2))
        return MeshOp("edge", op.to_dict())

    def remove_edge(self, v1: VertexId, v2: VertexId) -> MeshOp:
        op = self.edges.remove(canonical_edge(v1, v2))
        return MeshOp("edge", op.to_dict())

    # -- local mutation: faces -----------------------------------------------
    def add_face(self, face_id: FaceId, vertex_loop: list[VertexId]) -> list[MeshOp]:
        """Create a face whose boundary is the given ordered vertex loop."""
        ops: list[MeshOp] = [MeshOp("face_index", self.face_index.add(face_id).to_dict())]
        rga = self._face_rga(face_id)
        prev = rga.last_id()
        for vertex_id in vertex_loop:
            insert_op = rga.insert_after(prev, vertex_id)
            prev = insert_op.id
            ops.append(MeshOp("face_geom", insert_op.to_dict(), face_id=face_id))
        return ops

    def remove_face(self, face_id: FaceId) -> MeshOp:
        op = self.face_index.remove(face_id)
        return MeshOp("face_index", op.to_dict())

    def set_face_prop(self, face_id: FaceId, key: str, value) -> MeshOp:
        op = self._face_props(face_id).set(key, value)
        return MeshOp("face_prop", op.to_dict(), face_id=face_id)

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
        return mesh

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "MeshCRDT":
        return MeshCRDT.from_dict(clock, loads_msgpack(data))
