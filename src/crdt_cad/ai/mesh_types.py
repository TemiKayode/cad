"""The one shared data type every generator (old and new) produces --
split out from ``procedural_house.py`` (which used to define it) so
every other module in this package (``mesh_builder.py``, ``registry.py``,
``validation.py``, every ``generators/*.py``) can depend on it without
risking a circular import back through any *specific* generator's own
module. ``procedural_house.py`` re-exports these two names for backward
compatibility with existing import sites (``meshy_adapter.py``,
``generator.py``, and pre-Phase-5 tests already import them from there).
"""

from __future__ import annotations

from dataclasses import dataclass, field

Position = tuple[float, float, float]


@dataclass
class GeneratedMesh:
    vertices: dict[str, Position] = field(default_factory=dict)
    faces: dict[str, list[str]] = field(default_factory=dict)
    face_materials: dict[str, str] = field(default_factory=dict)

    def triangle_count(self) -> int:
        return sum(max(len(loop) - 2, 0) for loop in self.faces.values())
