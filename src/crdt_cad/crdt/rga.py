"""RGA -- Replicated Growable Array, an ordered-sequence CRDT.

Used for anything with meaningful order: polyline/spline control points,
the vertex loop of a mesh face boundary, etc.

Algorithm
---------
Every element gets a globally unique id (a Lamport :class:`OpId`) and
remembers the id of the element it was inserted immediately to the right
of (``origin``; ``None`` = head of the list). Deleting never removes an
element, it only flags it as a tombstone -- so it can keep serving as a
stable anchor for whatever was inserted next to it, even if that insert
arrives from a replica that hasn't heard about the delete yet.

Two elements inserted concurrently at the *same* origin are ordered by
descending ``OpId`` (higher id wins the left-most slot). Elements
inserted at a *different* (deeper) origin are treated as a subtree and
skipped over as a block. This is the classic RGA integration rule (Roh
et al., "Replicated Abstract Data Types", 2011); see ``_integrate`` for
the exact walk. Because every element's ``origin`` is causally *before*
it (Lamport clocks guarantee ``origin.counter < id.counter`` whenever an
op causally depends on another), replaying *any* set of known elements in
ascending-id order always integrates origins before the children that
reference them -- which is what makes the state-based :meth:`merge`
below simple: dedupe by id, then replay in id order. That replay is
deterministic, so any two replicas that end up with the same set of
elements converge to the identical sequence, regardless of merge order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Iterable, Iterator, Optional, TypeVar

from crdt_cad.crdt.clock import LamportClock, OpId, VectorClock
from crdt_cad.crdt.serialize import (
    dumps_msgpack,
    loads_msgpack,
    op_id_from_wire,
    op_id_to_wire,
)

V = TypeVar("V")


@dataclass
class _Node(Generic[V]):
    id: OpId
    origin: Optional[OpId]
    value: V
    deleted_by: Optional[OpId] = None

    @property
    def tombstone(self) -> bool:
        return self.deleted_by is not None


@dataclass(frozen=True)
class RGAInsertOp(Generic[V]):
    id: OpId
    origin: Optional[OpId]
    value: V

    def to_dict(self) -> dict:
        return {
            "t": "ins",
            "id": op_id_to_wire(self.id),
            "o": op_id_to_wire(self.origin) if self.origin else None,
            "v": self.value,
        }

    @staticmethod
    def from_dict(d: dict) -> "RGAInsertOp":
        return RGAInsertOp(
            id=op_id_from_wire(d["id"]),
            origin=op_id_from_wire(d["o"]) if d.get("o") else None,
            value=d["v"],
        )


@dataclass(frozen=True)
class RGADeleteOp:
    target: OpId
    op_id: OpId

    def to_dict(self) -> dict:
        return {"t": "del", "target": op_id_to_wire(self.target), "id": op_id_to_wire(self.op_id)}

    @staticmethod
    def from_dict(d: dict) -> "RGADeleteOp":
        return RGADeleteOp(target=op_id_from_wire(d["target"]), op_id=op_id_from_wire(d["id"]))


RGAOp = RGAInsertOp | RGADeleteOp


def op_from_dict(d: dict) -> RGAOp:
    return RGAInsertOp.from_dict(d) if d["t"] == "ins" else RGADeleteOp.from_dict(d)


class RGA(Generic[V]):
    """A replicated, ordered, insert/delete sequence of values of type V."""

    def __init__(self, clock: LamportClock) -> None:
        self._clock = clock
        self._seq: list[_Node[V]] = []
        self._by_id: dict[OpId, _Node[V]] = {}
        self._pending_deletes: dict[OpId, OpId] = {}  # target -> delete op_id

    # -- position helpers -----------------------------------------------------
    def _index_of(self, op_id: Optional[OpId]) -> int:
        if op_id is None:
            return -1
        node = self._by_id[op_id]
        return self._seq.index(node)

    def _integrate(self, node: _Node[V]) -> None:
        self._by_id[node.id] = node
        origin_idx = self._index_of(node.origin)
        i = origin_idx + 1
        while i < len(self._seq):
            other = self._seq[i]
            other_origin_idx = self._index_of(other.origin)
            if other_origin_idx < origin_idx:
                break
            if other_origin_idx == origin_idx:
                if other.id > node.id:
                    i += 1
                    continue
                break
            i += 1  # other is part of a deeper subtree; skip past it
        self._seq.insert(i, node)
        pending = self._pending_deletes.pop(node.id, None)
        if pending is not None:
            node.deleted_by = pending

    # -- local mutation ---------------------------------------------------------
    def insert_after(self, after: Optional[OpId], value: V) -> RGAInsertOp:
        op_id = self._clock.tick()
        node = _Node(op_id, after, value)
        self._integrate(node)
        return RGAInsertOp(op_id, after, value)

    def append(self, value: V) -> RGAInsertOp:
        last = self._seq[-1].id if self._seq else None
        return self.insert_after(last, value)

    def delete(self, target: OpId) -> RGADeleteOp:
        op_id = self._clock.tick()
        node = self._by_id.get(target)
        if node is not None:
            if node.deleted_by is None or op_id < node.deleted_by:
                node.deleted_by = op_id
        else:
            self._pending_deletes[target] = op_id
        return RGADeleteOp(target=target, op_id=op_id)

    # -- remote application -------------------------------------------------
    def apply(self, op: RGAOp) -> bool:
        if isinstance(op, RGAInsertOp):
            return self.apply_insert(op)
        return self.apply_delete(op)

    def apply_insert(self, op: RGAInsertOp) -> bool:
        self._clock.observe_id(op.id)
        if op.id in self._by_id:
            return False
        self._integrate(_Node(op.id, op.origin, op.value))
        return True

    def apply_delete(self, op: RGADeleteOp) -> bool:
        self._clock.observe_id(op.op_id)
        node = self._by_id.get(op.target)
        if node is None:
            existing = self._pending_deletes.get(op.target)
            if existing is None or op.op_id < existing:
                self._pending_deletes[op.target] = op.op_id
            return existing is None
        if node.deleted_by is not None and node.deleted_by <= op.op_id:
            return False
        node.deleted_by = op.op_id
        return True

    # -- reads ------------------------------------------------------------------
    def values(self) -> list[V]:
        return [n.value for n in self._seq if not n.tombstone]

    def entries(self) -> list[tuple[OpId, V]]:
        return [(n.id, n.value) for n in self._seq if not n.tombstone]

    def value_at(self, op_id: OpId) -> Optional[V]:
        """Live value at a specific, stable node id, or None if that id
        doesn't exist or has since been deleted -- for features that
        reference one particular point by id rather than its current
        position in the list (Phase 13 dimensions), so the reference
        keeps resolving correctly across concurrent inserts/deletes
        elsewhere in the same path. O(1) via `_by_id`, not a scan."""
        node = self._by_id.get(op_id)
        if node is None or node.tombstone:
            return None
        return node.value

    def __iter__(self) -> Iterator[V]:
        return iter(self.values())

    def __len__(self) -> int:
        return sum(1 for n in self._seq if not n.tombstone)

    def last_id(self) -> Optional[OpId]:
        return self._seq[-1].id if self._seq else None

    # -- tombstone value compaction ---------------------------------------------
    def compact(self, safe_vc: VectorClock) -> int:
        """Drops the stored *value* (not the node) of tombstones whose
        delete is causally stable -- i.e. every actor's counter in
        ``safe_vc`` already covers that delete's ``OpId``. Returns how
        many values were dropped.

        Why only the value, and not the whole node: an RGA node's
        ``id``/``origin`` are load-bearing forever, even after deletion --
        ``_integrate`` uses a tombstone as an anchor for anything inserted
        next to it later (this is intentional and tested; see
        ``test_delete_hides_value_but_keeps_tombstone_as_anchor``). Fully
        removing a node would break that anchor for any not-yet-merged
        insert that still references it, and -- critically -- *different*
        replicas would discover that breakage at different times (whoever
        happens to have compacted first), which is exactly the kind of
        replica-dependent behavior a CRDT must never have. True safe full
        removal needs a distributed stability protocol (every replica
        provably done referencing the id, not just "the delete looks
        old") that this module does not implement; see the README's
        Roadmap. Compacting only the value is unconditionally safe: it
        never touches the ordering metadata, so it can't affect
        convergence, while still reclaiming the dominant cost for large
        payloads (e.g. a deleted path's hundreds of point tuples).

        One narrow known gap: ``ops_since`` for a client that has
        already recorded a node's *delete* but not yet its *insert* (an
        unusual out-of-order-delivery case; the normal relay path always
        offers both together) would replay the insert with a dropped
        value and no follow-up delete to shadow it. Treat `compact` as
        an occasional maintenance operation on rooms known to be caught
        up, not a hot-path optimization.
        """
        count = 0
        for node in self._seq:
            if node.deleted_by is None or node.value is None:
                continue
            if safe_vc.has_seen(node.deleted_by):
                node.value = None
                count += 1
        return count

    # -- state-based merge --------------------------------------------------
    def _load_records(
        self, records: Iterable[tuple[OpId, Optional[OpId], V, Optional[OpId]]]
    ) -> None:
        self._seq = []
        self._by_id = {}
        for op_id, origin, value, deleted_by in sorted(records, key=lambda r: r[0]):
            node = _Node(op_id, origin, value)
            self._integrate(node)
            if deleted_by is not None and (
                node.deleted_by is None or deleted_by < node.deleted_by
            ):
                node.deleted_by = deleted_by
            self._clock.observe(op_id.counter)

    def _signature(self) -> frozenset:
        return frozenset((n.id, n.deleted_by) for n in self._by_id.values())

    def merge(self, other: "RGA[V]") -> bool:
        before = self._signature()
        ids = set(self._by_id) | set(other._by_id)
        records = []
        for op_id in ids:
            a = self._by_id.get(op_id)
            b = other._by_id.get(op_id)
            src = a or b
            deleted_by = None
            for n in (a, b):
                if n is not None and n.deleted_by is not None:
                    deleted_by = n.deleted_by if deleted_by is None else min(deleted_by, n.deleted_by)
            records.append((op_id, src.origin, src.value, deleted_by))
        pending = dict(self._pending_deletes)
        for target, del_id in other._pending_deletes.items():
            if target not in pending or del_id < pending[target]:
                pending[target] = del_id
        self._load_records(records)
        self._pending_deletes = {
            t: d for t, d in pending.items() if t not in self._by_id
        }
        for target, del_id in pending.items():
            node = self._by_id.get(target)
            if node is not None and (node.deleted_by is None or del_id < node.deleted_by):
                node.deleted_by = del_id
        return self._signature() != before

    # -- delta sync -----------------------------------------------------------
    def ops_since(self, vc: VectorClock) -> list[RGAOp]:
        out: list[RGAOp] = []
        for node in self._seq:
            if not vc.has_seen(node.id):
                out.append(RGAInsertOp(node.id, node.origin, node.value))
            if node.deleted_by is not None and not vc.has_seen(node.deleted_by):
                out.append(RGADeleteOp(node.id, node.deleted_by))
        return out

    def frontier(self) -> VectorClock:
        vc = VectorClock()
        for node in self._seq:
            vc.record(node.id)
            if node.deleted_by is not None:
                vc.record(node.deleted_by)
        return vc

    # -- serialization --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "nodes": [
                {
                    "id": op_id_to_wire(n.id),
                    "o": op_id_to_wire(n.origin) if n.origin else None,
                    "v": n.value,
                    "db": op_id_to_wire(n.deleted_by) if n.deleted_by else None,
                }
                for n in self._seq
            ],
            "pending_deletes": [
                [op_id_to_wire(t), op_id_to_wire(d)] for t, d in self._pending_deletes.items()
            ],
        }

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "RGA":
        rga: RGA = RGA(clock)
        records = [
            (
                op_id_from_wire(n["id"]),
                op_id_from_wire(n["o"]) if n.get("o") else None,
                n["v"],
                op_id_from_wire(n["db"]) if n.get("db") else None,
            )
            for n in d["nodes"]
        ]
        rga._load_records(records)
        for t, dl in d.get("pending_deletes", []):
            rga._pending_deletes[op_id_from_wire(t)] = op_id_from_wire(dl)
        return rga

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "RGA":
        return RGA.from_dict(clock, loads_msgpack(data))
