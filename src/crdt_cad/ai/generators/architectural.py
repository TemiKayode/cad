"""Architectural element generators (Phase G1): stairs, column, arch,
fence/railing.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, add_cylinder, add_extruded_profile_xy
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import GeneratorEntry, register


class StairsSpec(BaseModel):
    step_count: int = Field(ge=1, le=40, default=12)
    step_width_m: float = Field(gt=0.2, le=5.0, default=1.0)
    step_run_m: float = Field(gt=0.1, le=0.6, default=0.28)
    step_rise_m: float = Field(gt=0.05, le=0.3, default=0.18)
    material: str = "concrete"


def build_stairs(spec: StairsSpec) -> GeneratedMesh:
    """Each step is a solid box reaching down to the ground -- a real
    staircase's steps are usually supported by stringers/risers, and
    building each as a full box down to y=0 both matches that support
    structure and keeps every step independently watertight."""
    b = MeshBuilder()
    for i in range(spec.step_count):
        y1 = (i + 1) * spec.step_rise_m
        z0 = i * spec.step_run_m
        add_box(b, (0.0, 0.0, z0), (spec.step_width_m, y1, spec.step_run_m), spec.material)
    return b.mesh


class ColumnSpec(BaseModel):
    shaft_radius_m: float = Field(gt=0.05, le=3.0, default=0.25)
    height_m: float = Field(gt=0.3, le=20.0, default=3.0)
    base_height_m: float = Field(ge=0.0, le=1.0, default=0.15)
    capital_height_m: float = Field(ge=0.0, le=1.0, default=0.15)
    material: str = "stone"
    segments: int = Field(ge=8, le=64, default=20)


def build_column(spec: ColumnSpec) -> GeneratedMesh:
    """A classical column silhouette: a wider base plinth, the main
    shaft, and a wider capital -- three stacked cylinders, each its own
    watertight solid (a real column's base/capital are usually distinct
    stones from the shaft anyway)."""
    b = MeshBuilder()
    wide_radius = spec.shaft_radius_m * 1.4
    y = 0.0
    if spec.base_height_m > 0:
        add_cylinder(b, (0.0, y, 0.0), wide_radius, spec.base_height_m, spec.material, spec.segments)
        y += spec.base_height_m
    shaft_height = spec.height_m - spec.base_height_m - spec.capital_height_m
    add_cylinder(b, (0.0, y, 0.0), spec.shaft_radius_m, max(shaft_height, 0.01), spec.material, spec.segments)
    y += max(shaft_height, 0.01)
    if spec.capital_height_m > 0:
        add_cylinder(b, (0.0, y, 0.0), wide_radius, spec.capital_height_m, spec.material, spec.segments)
    return b.mesh


class ArchSpec(BaseModel):
    span_m: float = Field(gt=0.3, le=15.0, default=2.0)  # inner opening width
    opening_height_m: float = Field(gt=0.3, le=10.0, default=2.2)  # inner opening straight-wall height
    thickness_m: float = Field(gt=0.05, le=2.0, default=0.3)
    depth_m: float = Field(gt=0.05, le=5.0, default=0.4)
    material: str = "stone"
    segments: int = Field(ge=6, le=48, default=16)


def build_arch(spec: ArchSpec) -> GeneratedMesh:
    """A semicircular archway: two straight jambs up to `opening_height_m`,
    then a half-annulus over the top -- the classic architectural arch
    profile, extruded along Z by `depth_m`. The profile is a single
    closed polygon (outer edge out, inner edge back), so the whole arch
    is one watertight solid, not two -- a real arch is not walkable
    through in this generator (G1 scope is the arch *shape*; wall
    openings you can pass through are the door/window generator)."""
    inner_r = spec.span_m / 2.0
    outer_r = inner_r + spec.thickness_m
    cx = inner_r  # profile origin at the left jamb's outer-bottom corner, so the whole shape sits at x >= 0

    # A single continuous boundary walk, no repeated points: outer
    # bottom-left -> up the outer-left jamb -> over the outer arc
    # (theta pi->0) -> outer bottom-right -> inner bottom-right -> back
    # over the inner arc (theta 0->pi) -> inner bottom-left -> implicit
    # close back to the start. The two arc loops (i=0..segments) already
    # include their own jamb-top endpoints, so no separate jamb-top point
    # is appended before/after them -- adding one too would duplicate the
    # arc's own first/last vertex and produce a zero-length edge.
    points: list[tuple[float, float]] = [(cx - outer_r, 0.0)]
    for i in range(spec.segments + 1):
        theta = math.pi - math.pi * i / spec.segments
        points.append((cx + outer_r * math.cos(theta), spec.opening_height_m + outer_r * math.sin(theta)))
    points.append((cx + outer_r, 0.0))
    points.append((cx + inner_r, 0.0))
    for i in range(spec.segments + 1):
        theta = math.pi * i / spec.segments
        points.append((cx + inner_r * math.cos(theta), spec.opening_height_m + inner_r * math.sin(theta)))
    points.append((cx - inner_r, 0.0))

    b = MeshBuilder()
    add_extruded_profile_xy(b, points, 0.0, spec.depth_m, spec.material)
    return b.mesh


class FenceSpec(BaseModel):
    length_m: float = Field(gt=0.3, le=100.0, default=6.0)
    height_m: float = Field(gt=0.2, le=3.0, default=1.0)
    post_spacing_m: float = Field(gt=0.3, le=5.0, default=1.5)
    post_size_m: float = Field(gt=0.02, le=0.3, default=0.08)
    rail_count: int = Field(ge=1, le=6, default=2)
    material: str = "wood"


def build_fence(spec: FenceSpec) -> GeneratedMesh:
    b = MeshBuilder()
    post_count = max(2, math.ceil(spec.length_m / spec.post_spacing_m) + 1)
    actual_spacing = spec.length_m / (post_count - 1)
    for i in range(post_count):
        x = i * actual_spacing
        add_box(b, (x - spec.post_size_m / 2, 0.0, -spec.post_size_m / 2), (spec.post_size_m, spec.height_m, spec.post_size_m), spec.material)

    rail_t = spec.post_size_m * 0.6
    for r in range(spec.rail_count):
        y = spec.height_m * (r + 1) / (spec.rail_count + 1)
        add_box(b, (0.0, y - rail_t / 2, -rail_t / 2), (spec.length_m, rail_t, rail_t), spec.material)
    return b.mesh


register(GeneratorEntry(
    name="stairs", description="A straight staircase, each step a solid box; count/width/run/rise all specified.",
    spec_model=StairsSpec, build=build_stairs, keywords=("stairs", "staircase", "steps"),
))
register(GeneratorEntry(
    name="column", description="A classical column: base plinth, shaft, and capital, with explicit height and radius.",
    spec_model=ColumnSpec, build=build_column, keywords=("column", "pillar"),
))
register(GeneratorEntry(
    name="arch", description="A semicircular archway shape (span/height/thickness/depth), a solid arc form.",
    spec_model=ArchSpec, build=build_arch, keywords=("arch", "archway"),
))
register(GeneratorEntry(
    name="fence", description="A fence/railing: evenly spaced posts with horizontal rails, given length and height.",
    spec_model=FenceSpec, build=build_fence, keywords=("fence", "railing", "rail "),
))
