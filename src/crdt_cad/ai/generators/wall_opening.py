"""Door and window generators (Phase G1): the one pair in the "at
minimum" list that needs a real boolean cut, not just an assembly of
disjoint solids -- a door/window is a *hole through* a wall, which only
CSG difference produces correctly.

Uses ``trimesh``'s boolean engine (the ``manifold3d`` backend --
pip-installable, no external CAD program) to subtract an opening box
from a wall box. The opening box is extended slightly beyond both wall
faces (``_EPS``) so the cut boundary is never exactly coplanar with the
wall's own faces -- coplanar boolean inputs are a classic source of
degenerate/missing triangles in every CSG engine, manifold3d included.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, from_trimesh, to_trimesh
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import GeneratorEntry, register

_EPS = 0.02


def cut_wall_openings(
    wall_width: float, wall_height: float, wall_thickness: float,
    openings: list[dict], material: str,
) -> GeneratedMesh:
    """Cuts every opening in `openings` (each a dict with `width`,
    `height`, `sill`, `offset`) out of one solid wall via successive CSG
    differences -- the multi-opening generalization of the single-cut
    ``_cut_wall`` the `door`/`window` generators use, reused by the
    house generator to punch a door and several windows into the same
    wall segment in one pass. Caller is responsible for ensuring
    `openings` don't overlap each other (see
    ``procedural_house._front_wall_openings``, which slots them so they
    can't)."""
    wall_builder = MeshBuilder()
    add_box(wall_builder, (0.0, 0.0, 0.0), (wall_width, wall_height, wall_thickness), material)
    wall_tri = to_trimesh(wall_builder.mesh)

    for opening in openings:
        opening_builder = MeshBuilder()
        add_box(
            opening_builder,
            (opening["offset"], opening["sill"], -_EPS),
            (opening["width"], opening["height"], wall_thickness + 2 * _EPS),
            material,
        )
        wall_tri = wall_tri.difference(to_trimesh(opening_builder.mesh))

    return from_trimesh(wall_tri, material)


def _cut_wall(
    wall_width: float, wall_height: float, wall_thickness: float,
    opening_width: float, opening_height: float, sill_height: float, offset: float,
    material: str,
) -> GeneratedMesh:
    return cut_wall_openings(
        wall_width, wall_height, wall_thickness,
        [{"width": opening_width, "height": opening_height, "sill": sill_height, "offset": offset}],
        material,
    )


class DoorSpec(BaseModel):
    wall_width_m: float = Field(gt=0.5, le=30.0, default=3.0)
    wall_height_m: float = Field(gt=0.5, le=10.0, default=2.7)
    wall_thickness_m: float = Field(gt=0.02, le=1.0, default=0.2)
    door_width_m: float = Field(gt=0.4, le=3.0, default=0.9)
    door_height_m: float = Field(gt=0.8, le=4.0, default=2.1)
    offset_m: float = Field(ge=0.0, default=1.0)  # distance from wall's left edge to door's left edge
    material: str = "exterior_wall"

    @model_validator(mode="after")
    def _door_fits_in_wall(self) -> "DoorSpec":
        if self.offset_m + self.door_width_m > self.wall_width_m:
            raise ValueError("door opening extends past the wall's right edge")
        if self.door_height_m > self.wall_height_m:
            raise ValueError("door is taller than the wall")
        return self


def build_door(spec: DoorSpec) -> GeneratedMesh:
    return _cut_wall(
        spec.wall_width_m, spec.wall_height_m, spec.wall_thickness_m,
        spec.door_width_m, spec.door_height_m, 0.0, spec.offset_m, spec.material,
    )


class WindowSpec(BaseModel):
    wall_width_m: float = Field(gt=0.5, le=30.0, default=3.0)
    wall_height_m: float = Field(gt=0.5, le=10.0, default=2.7)
    wall_thickness_m: float = Field(gt=0.02, le=1.0, default=0.2)
    window_width_m: float = Field(gt=0.2, le=6.0, default=1.2)
    window_height_m: float = Field(gt=0.2, le=4.0, default=1.2)
    sill_height_m: float = Field(ge=0.0, le=3.0, default=0.9)
    offset_m: float = Field(ge=0.0, default=0.9)
    material: str = "exterior_wall"

    @model_validator(mode="after")
    def _window_fits_in_wall(self) -> "WindowSpec":
        if self.offset_m + self.window_width_m > self.wall_width_m:
            raise ValueError("window opening extends past the wall's right edge")
        if self.sill_height_m + self.window_height_m > self.wall_height_m:
            raise ValueError("window opening extends above the wall's top edge")
        return self


def build_window(spec: WindowSpec) -> GeneratedMesh:
    return _cut_wall(
        spec.wall_width_m, spec.wall_height_m, spec.wall_thickness_m,
        spec.window_width_m, spec.window_height_m, spec.sill_height_m, spec.offset_m, spec.material,
    )


register(GeneratorEntry(
    name="door", description="A wall segment with a real rectangular door opening cut through it (CSG boolean).",
    spec_model=DoorSpec, build=build_door, keywords=("door", "doorway"),
))
register(GeneratorEntry(
    name="window", description="A wall segment with a real rectangular window opening cut through it (CSG boolean).",
    spec_model=WindowSpec, build=build_window, keywords=("window",),
))
