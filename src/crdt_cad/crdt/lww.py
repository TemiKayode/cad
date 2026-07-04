"""Last-Writer-Wins CRDTs: ``LWWRegister``, ``LWWMap``, ``LWWElementSet``.

All three share one conflict rule: every write (or delete) is stamped with
an :class:`~crdt_cad.crdt.clock.OpId`, and whichever stamped value has the
greater ``OpId`` wins -- deterministically, on every replica, because
``OpId`` is a strict total order (Lamport counter, actor-id tiebreak).
Deletes are writes too (a tombstone value), so "set then delete" and
"delete then set" both converge correctly regardless of delivery order.

``LWWMap`` is the general "bag of independently-mutable fields" CRDT used
for object/layer properties. ``LWWElementSet`` is a thin specialisation of
it (membership only) used for "is this layer/object id currently part of
the document" style sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Hashable, Iterator, TypeVar

from crdt_cad.crdt.clock import LamportClock, OpId, VectorClock
from crdt_cad.crdt.serialize import (
    dumps_msgpack,
    loads_msgpack,
    op_id_from_wire,
    op_id_to_wire,
)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_TOMBSTONE = object()  # sentinel distinguishing "deleted" from "value is None"


@dataclass(frozen=True)
class LWWOp(Generic[K]):
    """A single stamped write, applicable to a register/map/set entry.

    ``key`` is ``None`` for a bare :class:`LWWRegister`. ``deleted=True``
    means this op is a tombstone (a "remove") rather than a value write.
    """

    op_id: OpId
    key: K | None
    value: Any
    deleted: bool = False

    def to_dict(self) -> dict:
        return {
            "id": op_id_to_wire(self.op_id),
            "k": self.key,
            "v": None if self.deleted else self.value,
            "d": self.deleted,
        }

    @staticmethod
    def from_dict(d: dict) -> "LWWOp":
        return LWWOp(
            op_id=op_id_from_wire(d["id"]),
            key=d.get("k"),
            value=d.get("v"),
            deleted=bool(d.get("d", False)),
        )


@dataclass
class _Entry:
    op_id: OpId
    value: Any


class LWWRegister(Generic[V]):
    """A single mutable value with last-writer-wins conflict resolution.

    Used for ephemeral, high-churn state such as cursor position / current
    selection ("presence"), where only the latest write ever matters.
    """

    def __init__(self, clock: LamportClock, initial: V | None = None) -> None:
        self._clock = clock
        self._entry: _Entry | None = (
            _Entry(OpId(0, clock.actor), initial) if initial is not None else None
        )

    @property
    def value(self) -> V | None:
        return self._entry.value if self._entry is not None else None

    @property
    def op_id(self) -> OpId | None:
        return self._entry.op_id if self._entry is not None else None

    def set(self, value: V) -> LWWOp:
        op_id = self._clock.tick()
        self._entry = _Entry(op_id, value)
        return LWWOp(op_id=op_id, key=None, value=value, deleted=False)

    def apply(self, op: LWWOp) -> bool:
        """Apply a (possibly remote) op. Returns True if it changed state."""
        self._clock.observe_id(op.op_id)
        if self._entry is not None and op.op_id <= self._entry.op_id:
            return False
        self._entry = _Entry(op.op_id, op.value)
        return True

    def merge(self, other: "LWWRegister[V]") -> bool:
        if other._entry is None:
            return False
        return self.apply(LWWOp(other._entry.op_id, None, other._entry.value))

    def to_dict(self) -> dict:
        if self._entry is None:
            return {"id": None, "v": None}
        return {"id": op_id_to_wire(self._entry.op_id), "v": self._entry.value}

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "LWWRegister":
        reg = LWWRegister(clock)
        if d.get("id") is not None:
            reg._entry = _Entry(op_id_from_wire(d["id"]), d["v"])
        return reg


class LWWMap(Generic[K, V]):
    """A map of independently-LWW-resolved fields.

    Used for object/layer property bags: each key (e.g. ``"color"``,
    ``"layer_id"``, ``"stroke_width"``) is resolved independently, so
    concurrent edits to *different* properties of the same object always
    merge cleanly -- only concurrent edits to the *same* property require
    a winner, decided by ``OpId``.
    """

    def __init__(self, clock: LamportClock) -> None:
        self._clock = clock
        self._entries: dict[K, _Entry] = {}

    # -- local mutation -------------------------------------------------
    def set(self, key: K, value: V) -> LWWOp:
        op_id = self._clock.tick()
        self._entries[key] = _Entry(op_id, value)
        return LWWOp(op_id=op_id, key=key, value=value, deleted=False)

    def delete(self, key: K) -> LWWOp:
        op_id = self._clock.tick()
        self._entries[key] = _Entry(op_id, _TOMBSTONE)
        return LWWOp(op_id=op_id, key=key, value=None, deleted=True)

    # -- remote application ----------------------------------------------
    def apply(self, op: LWWOp) -> bool:
        self._clock.observe_id(op.op_id)
        existing = self._entries.get(op.key)
        if existing is not None and op.op_id <= existing.op_id:
            return False
        new_value = _TOMBSTONE if op.deleted else op.value
        self._entries[op.key] = _Entry(op.op_id, new_value)
        return True

    def merge(self, other: "LWWMap[K, V]") -> bool:
        changed = False
        for key, entry in other._entries.items():
            changed |= self.apply(
                LWWOp(
                    entry.op_id,
                    key,
                    None if entry.value is _TOMBSTONE else entry.value,
                    deleted=entry.value is _TOMBSTONE,
                )
            )
        return changed

    # -- reads ------------------------------------------------------------
    def get(self, key: K, default: V | None = None) -> V | None:
        entry = self._entries.get(key)
        if entry is None or entry.value is _TOMBSTONE:
            return default
        return entry.value

    def __contains__(self, key: K) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry.value is not _TOMBSTONE

    def keys(self) -> Iterator[K]:
        return (k for k, e in self._entries.items() if e.value is not _TOMBSTONE)

    def items(self) -> Iterator[tuple[K, V]]:
        return (
            (k, e.value) for k, e in self._entries.items() if e.value is not _TOMBSTONE
        )

    def __len__(self) -> int:
        return sum(1 for e in self._entries.values() if e.value is not _TOMBSTONE)

    # -- delta sync ---------------------------------------------------------
    def ops_since(self, vc: VectorClock) -> list[LWWOp]:
        """Ops this replica has that the given vector clock hasn't seen.

        Lets a reconnecting (possibly-offline) client receive only what
        changed since the last state it acknowledged, instead of a full
        snapshot, once both sides know the delta's starting vector clock.
        """
        out = []
        for key, entry in self._entries.items():
            if not vc.has_seen(entry.op_id):
                out.append(
                    LWWOp(
                        entry.op_id,
                        key,
                        None if entry.value is _TOMBSTONE else entry.value,
                        deleted=entry.value is _TOMBSTONE,
                    )
                )
        return out

    def frontier(self) -> VectorClock:
        vc = VectorClock()
        for entry in self._entries.values():
            vc.record(entry.op_id)
        return vc

    # -- serialization ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "entries": [
                {
                    "k": key,
                    "id": op_id_to_wire(e.op_id),
                    "v": None if e.value is _TOMBSTONE else e.value,
                    "d": e.value is _TOMBSTONE,
                }
                for key, e in self._entries.items()
            ]
        }

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "LWWMap":
        m: LWWMap = LWWMap(clock)
        for item in d["entries"]:
            op_id = op_id_from_wire(item["id"])
            value = _TOMBSTONE if item.get("d") else item["v"]
            m._entries[item["k"]] = _Entry(op_id, value)
        return m

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "LWWMap":
        return LWWMap.from_dict(clock, loads_msgpack(data))


class LWWElementSet(Generic[V]):
    """Add/remove set where membership is decided by last-writer-wins.

    ``element in set`` iff the highest-``OpId`` op touching that element
    was an add. Built directly on the same entry/merge machinery as
    :class:`LWWMap`, keyed by the element itself.
    """

    def __init__(self, clock: LamportClock) -> None:
        self._map: LWWMap[V, bool] = LWWMap(clock)

    def add(self, element: V) -> LWWOp:
        return self._map.set(element, True)

    def remove(self, element: V) -> LWWOp:
        return self._map.delete(element)

    def apply(self, op: LWWOp) -> bool:
        return self._map.apply(op)

    def merge(self, other: "LWWElementSet[V]") -> bool:
        return self._map.merge(other._map)

    def __contains__(self, element: V) -> bool:
        return element in self._map

    def __iter__(self) -> Iterator[V]:
        return self._map.keys()

    def __len__(self) -> int:
        return len(self._map)

    def to_set(self) -> set[V]:
        return set(self._map.keys())

    def ops_since(self, vc: VectorClock) -> list[LWWOp]:
        return self._map.ops_since(vc)

    def frontier(self) -> VectorClock:
        return self._map.frontier()

    def to_dict(self) -> dict:
        return self._map.to_dict()

    @staticmethod
    def from_dict(clock: LamportClock, d: dict) -> "LWWElementSet":
        s: LWWElementSet = LWWElementSet(clock)
        s._map = LWWMap.from_dict(clock, d)
        return s

    def to_bytes(self) -> bytes:
        return dumps_msgpack(self.to_dict())

    @staticmethod
    def from_bytes(clock: LamportClock, data: bytes) -> "LWWElementSet":
        return LWWElementSet.from_dict(clock, loads_msgpack(data))
