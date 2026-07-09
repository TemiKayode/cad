"""Parametric primitive generators (Phase G1): box, cylinder, cone,
torus, each a single watertight solid with real dimensions in metres.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, add_cone, add_cylinder, add_torus
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import GeneratorEntry, register


class BoxSpec(BaseModel):
    width_m: float = Field(gt=0, le=50.0, default=1.0)
    height_m: float = Field(gt=0, le=50.0, default=1.0)
    depth_m: float = Field(gt=0, le=50.0, default=1.0)
    material: str = "wood"


def build_box(spec: BoxSpec) -> GeneratedMesh:
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (spec.width_m, spec.height_m, spec.depth_m), spec.material)
    return b.mesh


class CylinderSpec(BaseModel):
    radius_m: float = Field(gt=0, le=25.0, default=0.5)
    height_m: float = Field(gt=0, le=50.0, default=1.0)
    material: str = "wood"
    segments: int = Field(ge=6, le=64, default=16)


def build_cylinder(spec: CylinderSpec) -> GeneratedMesh:
    b = MeshBuilder()
    add_cylinder(b, (0.0, 0.0, 0.0), spec.radius_m, spec.height_m, spec.material, spec.segments)
    return b.mesh


class ConeSpec(BaseModel):
    radius_m: float = Field(gt=0, le=25.0, default=0.5)
    height_m: float = Field(gt=0, le=50.0, default=1.0)
    material: str = "wood"
    segments: int = Field(ge=6, le=64, default=16)


def build_cone(spec: ConeSpec) -> GeneratedMesh:
    b = MeshBuilder()
    add_cone(b, (0.0, 0.0, 0.0), spec.radius_m, spec.height_m, spec.material, spec.segments)
    return b.mesh


class TorusSpec(BaseModel):
    major_radius_m: float = Field(gt=0, le=25.0, default=1.0)
    minor_radius_m: float = Field(gt=0, le=10.0, default=0.25)
    material: str = "metal"
    major_segments: int = Field(ge=6, le=64, default=16)
    minor_segments: int = Field(ge=4, le=32, default=8)


def build_torus(spec: TorusSpec) -> GeneratedMesh:
    b = MeshBuilder()
    add_torus(
        b, (0.0, spec.minor_radius_m, 0.0), spec.major_radius_m, spec.minor_radius_m,
        spec.material, spec.major_segments, spec.minor_segments,
    )
    return b.mesh


register(GeneratorEntry(
    name="box", description="A single rectangular box/cuboid with explicit width/height/depth in metres.",
    spec_model=BoxSpec, build=build_box, keywords=("box", "cube", "block", "cuboid"),
))
register(GeneratorEntry(
    name="cylinder", description="A single vertical cylinder with explicit radius and height in metres.",
    spec_model=CylinderSpec, build=build_cylinder, keywords=("cylinder", "tube", "pipe"),
))
register(GeneratorEntry(
    name="cone", description="A single cone with explicit base radius and height in metres.",
    spec_model=ConeSpec, build=build_cone, keywords=("cone",),
))
register(GeneratorEntry(
    name="torus", description="A single torus (ring/donut shape) with explicit major and minor radius in metres.",
    spec_model=TorusSpec, build=build_torus, keywords=("torus", "donut", "ring shape"),
))
