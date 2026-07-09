"""Registers the house generator (the original, pre-registry generator
-- see ``procedural_house.py``'s own module docstring for its geometry
design) into the Phase G1 registry alongside every new generator, so
dispatch (LLM tool-catalog and heuristic keyword matching) treats it as
just one more entry, not a hardcoded special case.
"""

from __future__ import annotations

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.procedural_house import build_house_mesh
from crdt_cad.ai.registry import GeneratorEntry, register

register(GeneratorEntry(
    name="house",
    description=(
        "A house/building: box-based rooms on a grid, bedrooms/floors/roof/garage, "
        "floor material and wall height, optional target floor area."
    ),
    spec_model=HouseSpec,
    build=build_house_mesh,
    keywords=("house", "home", "cottage", "cabin", "building", "bedroom", "apartment", "villa"),
))
