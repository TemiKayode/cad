"""Importing this package runs every generator module's registration
side effect (each module calls ``crdt_cad.ai.registry.register(...)`` at
import time), populating ``crdt_cad.ai.registry.REGISTRY``. Any code
that needs the full registry populated -- the interpreter's tool
catalog, the heuristic dispatcher, the generation endpoint -- imports
this package (not an individual generator module) to guarantee that.
"""

from __future__ import annotations

from crdt_cad.ai.generators import architectural, furniture, house, primitives, wall_opening  # noqa: F401
from crdt_cad.ai.registry import REGISTRY

__all__ = ["REGISTRY"]
