"""Causal metadata: actor identity, Lamport-ordered operation ids, vector clocks.

Two distinct clock concepts are used across the CRDT layer, on purpose:

``OpId`` (a Lamport timestamp paired with an actor id)
    Gives every CRDT operation a single, globally total order that every
    replica computes identically. This is what CRDTs use internally to
    break ties deterministically (e.g. "which concurrent insert wins the
    left-most slot", "which concurrent write to a field wins").

``VectorClock``
    Tracks, per actor, how many operations from that actor a replica has
    already applied. This is *not* used for tie-breaking -- it is used to
    answer "what have you not seen yet?" so two replicas (including an
    offline client reconnecting after edits) can exchange only the delta
    instead of the full document state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable, Mapping, NamedTuple

ActorId = str


def new_actor_id() -> ActorId:
    """Generate a fresh, effectively-unique actor id for a client/replica."""
    return uuid.uuid4().hex[:12]


class OpId(NamedTuple):
    """A Lamport timestamp scoped to the actor that produced it.

    Total order is (counter, actor) lexicographic. The actor tiebreak
    guarantees a strict total order even when two actors tick their
    Lamport counter to the same value concurrently -- every replica sorts
    such pairs identically because the comparison is pure data, not
    wall-clock or arrival order.
    """

    counter: int
    actor: ActorId

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.counter}@{self.actor}"

    @staticmethod
    def parse(s: str) -> "OpId":
        counter_str, actor = s.split("@", 1)
        return OpId(int(counter_str), actor)


class LamportClock:
    """Per-replica Lamport clock producing strictly increasing :class:`OpId`.

    Every local mutation calls :meth:`tick` to mint a fresh id. Every
    remote operation received calls :meth:`observe` so the local clock
    never falls behind causally-prior events -- this is the standard
    Lamport clock rule: ``local = max(local, remote) + 1`` on send/tick,
    ``local = max(local, remote)`` on receive.
    """

    __slots__ = ("actor", "counter")

    def __init__(self, actor: ActorId | None = None, counter: int = 0) -> None:
        self.actor: ActorId = actor or new_actor_id()
        self.counter = counter

    def tick(self) -> OpId:
        self.counter += 1
        return OpId(self.counter, self.actor)

    def observe(self, other_counter: int) -> None:
        if other_counter > self.counter:
            self.counter = other_counter

    def observe_id(self, op_id: OpId) -> None:
        self.observe(op_id.counter)

    def to_dict(self) -> dict:
        return {"actor": self.actor, "counter": self.counter}

    @staticmethod
    def from_dict(d: Mapping) -> "LamportClock":
        return LamportClock(actor=d["actor"], counter=int(d["counter"]))


@dataclass
class VectorClock:
    """Maps actor -> highest op counter from that actor seen so far.

    Used for causal delta-sync ("give me every op you have that I don't")
    and for detecting whether two replica states are concurrent (neither
    is an ancestor of the other) -- the situation that arises whenever a
    client edits while offline.
    """

    counters: dict[ActorId, int] = field(default_factory=dict)

    def get(self, actor: ActorId) -> int:
        return self.counters.get(actor, 0)

    def has_seen(self, op_id: OpId) -> bool:
        return self.get(op_id.actor) >= op_id.counter

    def record(self, op_id: OpId) -> None:
        if op_id.counter > self.get(op_id.actor):
            self.counters[op_id.actor] = op_id.counter

    def merge(self, other: "VectorClock") -> "VectorClock":
        """Pointwise max -- commutative, associative, idempotent."""
        merged = dict(self.counters)
        for actor, count in other.counters.items():
            if count > merged.get(actor, 0):
                merged[actor] = count
        return VectorClock(merged)

    def dominates(self, other: "VectorClock") -> bool:
        """True if self has seen everything other has seen (self >= other)."""
        return all(self.get(a) >= c for a, c in other.counters.items())

    def concurrent_with(self, other: "VectorClock") -> bool:
        return not self.dominates(other) and not other.dominates(self)

    def actors(self) -> Iterable[ActorId]:
        return self.counters.keys()

    def copy(self) -> "VectorClock":
        return VectorClock(dict(self.counters))

    def to_dict(self) -> dict:
        return dict(self.counters)

    @staticmethod
    def from_dict(d: Mapping) -> "VectorClock":
        return VectorClock({str(k): int(v) for k, v in d.items()})

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VectorClock):
            return NotImplemented
        a = {k: v for k, v in self.counters.items() if v}
        b = {k: v for k, v in other.counters.items() if v}
        return a == b
