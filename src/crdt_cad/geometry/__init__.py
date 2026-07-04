"""Geometry kernel: primitives, validity checks, and a constraint solver.

This package is intentionally independent of the CRDT layer: it validates
and solves geometry given plain numbers, and the server (or a future
richer client) is responsible for calling it *before* turning an edit
into CRDT ops -- validity is a pre-commit gate, not something the CRDTs
themselves know about (see the design note in ``crdt.mesh``).
"""

from crdt_cad.geometry.constraints import Constraint, Sketch, SolveResult, solve
from crdt_cad.geometry.validity import GeometryError, path_is_self_intersecting, validate_new_point

__all__ = [
    "Constraint",
    "Sketch",
    "SolveResult",
    "solve",
    "GeometryError",
    "path_is_self_intersecting",
    "validate_new_point",
]
