"""Furniture generators (Phase G1): table, chair, shelf/bookcase. Each
is an assembly of watertight box primitives -- a real table's top and
legs are physically separate solids too, so this is correct by
construction, not a simplification (see mesh_builder.py's module
docstring for why disjoint watertight solids validate cleanly).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import GeneratorEntry, register

_LEG_THICKNESS_M = 0.06


class TableSpec(BaseModel):
    width_m: float = Field(gt=0.2, le=10.0, default=1.4)
    depth_m: float = Field(gt=0.2, le=10.0, default=0.8)
    height_m: float = Field(gt=0.2, le=2.0, default=0.75)
    top_thickness_m: float = Field(gt=0.01, le=0.3, default=0.04)
    material: str = "wood"


def build_table(spec: TableSpec) -> GeneratedMesh:
    b = MeshBuilder()
    leg_h = spec.height_m - spec.top_thickness_m
    inset = _LEG_THICKNESS_M  # legs sit inset from each edge by their own thickness
    for x in (inset, spec.width_m - inset - _LEG_THICKNESS_M):
        for z in (inset, spec.depth_m - inset - _LEG_THICKNESS_M):
            add_box(b, (x, 0.0, z), (_LEG_THICKNESS_M, leg_h, _LEG_THICKNESS_M), spec.material)
    add_box(b, (0.0, leg_h, 0.0), (spec.width_m, spec.top_thickness_m, spec.depth_m), spec.material)
    return b.mesh


class ChairSpec(BaseModel):
    seat_width_m: float = Field(gt=0.2, le=2.0, default=0.45)
    seat_depth_m: float = Field(gt=0.2, le=2.0, default=0.45)
    seat_height_m: float = Field(gt=0.2, le=1.2, default=0.45)
    # 0 is a legitimate value, not just a lower bound to clear -- it's
    # what makes this generator also cover a backless stool (see its
    # "stool" dispatch keyword below); build_chair's own `> 0` check
    # already treats 0 as "no backrest".
    back_height_m: float = Field(ge=0.0, le=1.2, default=0.45)
    material: str = "wood"


def build_chair(spec: ChairSpec) -> GeneratedMesh:
    b = MeshBuilder()
    leg_t = 0.04
    seat_t = 0.04
    inset = leg_t
    for x in (inset, spec.seat_width_m - inset - leg_t):
        for z in (inset, spec.seat_depth_m - inset - leg_t):
            add_box(b, (x, 0.0, z), (leg_t, spec.seat_height_m, leg_t), spec.material)
    add_box(b, (0.0, spec.seat_height_m, 0.0), (spec.seat_width_m, seat_t, spec.seat_depth_m), spec.material)
    if spec.back_height_m > 0:
        add_box(
            b,
            (0.0, spec.seat_height_m + seat_t, spec.seat_depth_m - leg_t),
            (spec.seat_width_m, spec.back_height_m, leg_t),
            spec.material,
        )
    return b.mesh


class ShelfSpec(BaseModel):
    width_m: float = Field(gt=0.2, le=6.0, default=0.9)
    height_m: float = Field(gt=0.2, le=4.0, default=1.8)
    depth_m: float = Field(gt=0.1, le=1.0, default=0.3)
    shelf_count: int = Field(ge=1, le=12, default=4)
    panel_thickness_m: float = Field(gt=0.005, le=0.1, default=0.02)
    material: str = "wood"


def build_shelf(spec: ShelfSpec) -> GeneratedMesh:
    b = MeshBuilder()
    t = spec.panel_thickness_m
    # two side panels
    add_box(b, (0.0, 0.0, 0.0), (t, spec.height_m, spec.depth_m), spec.material)
    add_box(b, (spec.width_m - t, 0.0, 0.0), (t, spec.height_m, spec.depth_m), spec.material)
    # back panel
    add_box(b, (0.0, 0.0, spec.depth_m - t), (spec.width_m, spec.height_m, t), spec.material)
    # shelves, evenly spaced including top and bottom
    inner_width = spec.width_m - 2 * t
    for i in range(spec.shelf_count):
        y = i * (spec.height_m - t) / max(spec.shelf_count - 1, 1) if spec.shelf_count > 1 else 0.0
        add_box(b, (t, y, 0.0), (inner_width, t, spec.depth_m), spec.material)
    return b.mesh


register(GeneratorEntry(
    name="table", description="A four-legged table with a flat top; width/depth/height in metres.",
    spec_model=TableSpec, build=build_table, keywords=("table", "desk"),
))
register(GeneratorEntry(
    name="chair", description="A four-legged chair with a seat and optional backrest.",
    spec_model=ChairSpec, build=build_chair, keywords=("chair", "stool"),
))
register(GeneratorEntry(
    name="shelf", description="A bookcase/shelving unit: two side panels, a back panel, and evenly spaced shelves.",
    spec_model=ShelfSpec, build=build_shelf, keywords=("shelf", "shelves", "bookcase", "bookshelf"),
))
