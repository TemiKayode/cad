"""Pure-Python CRDT primitives used throughout crdt_cad.

Exposed building blocks:

- :mod:`crdt_cad.crdt.clock` -- actor identity, Lamport-ordered ``OpId``,
  and ``VectorClock`` for causal delta sync.
- :mod:`crdt_cad.crdt.lww` -- ``LWWRegister``, ``LWWMap`` and
  ``LWWElementSet`` (Last-Writer-Wins family).
- :mod:`crdt_cad.crdt.rga` -- ``RGA`` (Replicated Growable Array) for
  ordered sequences such as sketch paths and curves.
- :mod:`crdt_cad.crdt.mesh` -- ``MeshCRDT``, a composite CRDT for 3D
  vertices/edges/faces built out of the primitives above.
"""

from crdt_cad.crdt.clock import ActorId, LamportClock, OpId, VectorClock
from crdt_cad.crdt.lww import LWWElementSet, LWWMap, LWWRegister
from crdt_cad.crdt.mesh import MeshCRDT
from crdt_cad.crdt.rga import RGA

__all__ = [
    "ActorId",
    "LamportClock",
    "OpId",
    "VectorClock",
    "LWWRegister",
    "LWWMap",
    "LWWElementSet",
    "RGA",
    "MeshCRDT",
]
