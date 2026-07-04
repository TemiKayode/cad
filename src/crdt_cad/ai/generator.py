"""Orchestrates prompt -> :class:`HouseSpec` -> mesh -> a batch of
:class:`MeshOp` ready for CRDT injection.

This module is pure CPU/network work with no ``asyncio`` in it on
purpose: :func:`generate_mesh_ops` is a plain synchronous function so
the FastAPI route can run the whole thing via ``asyncio.to_thread`` (as
the brief asks for) without a separate async code path to keep in sync.
Actually committing the resulting ops to a room -- applying them,
broadcasting them in batches so a large generated mesh doesn't arrive
as one giant WebSocket message, persisting -- is the server's job (see
``broadcast_ops_batched`` in ``crdt_cad.server.app``), since that's
inherently about the live room/event loop, not about generation.
"""

from __future__ import annotations

from dataclasses import dataclass

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.interpreter import interpret_prompt
from crdt_cad.ai.procedural_house import build_house_mesh
from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp

DEFAULT_ACTOR_ID = "ai_generator_bot"

_MATERIAL_COLORS = {
    "wood": "#8b5a2b",
    "concrete": "#9a9a92",
    "marble": "#e8e6e1",
    "tile": "#c9b79c",
    "carpet": "#7a4a3a",
    "stone": "#8a8378",
    "roof": "#5c4632",
    "exterior_wall": "#d8d2c4",
    "interior_wall": "#e8e4da",
}


def _color_for_material(material: str) -> str:
    return _MATERIAL_COLORS.get(material, "#b8b2a4")


@dataclass
class GenerationResult:
    ops: list[MeshOp]
    spec: HouseSpec
    interpretation_source: str  # "llm" | "heuristic"
    vertex_count: int
    face_count: int
    triangle_count: int


def generate_mesh_ops(prompt: str, *, actor_id: str = DEFAULT_ACTOR_ID) -> GenerationResult:
    """Synchronous end-to-end pipeline. Safe to call from a worker
    thread (``asyncio.to_thread``); does no networking of its own beyond
    whatever ``interpret_prompt``'s LLM path performs internally.
    """
    spec, source = interpret_prompt(prompt)
    mesh = build_house_mesh(spec)

    # Built against a throwaway MeshCRDT (not the live room's document) so
    # every op is minted with a fresh, correctly-ordered OpId from one
    # dedicated actor identity, and so the resulting op list can be handed
    # to the room to apply+broadcast in controlled batches rather than
    # mutating the live document eagerly and only trying to batch the
    # broadcast after the fact.
    clock = LamportClock(actor=actor_id)
    scratch = MeshCRDT(clock)
    ops: list[MeshOp] = []

    for vertex_id, position in mesh.vertices.items():
        ops.append(scratch.add_vertex(vertex_id, position))

    for face_id, loop in mesh.faces.items():
        ops.extend(scratch.add_face(face_id, loop))
        material = mesh.face_materials.get(face_id, "")
        if material:
            ops.append(scratch.set_face_prop(face_id, "material", material))
            ops.append(scratch.set_face_prop(face_id, "color", _color_for_material(material)))
        for i in range(len(loop)):
            a, b = loop[i], loop[(i + 1) % len(loop)]
            ops.append(scratch.add_edge(a, b))

    return GenerationResult(
        ops=ops,
        spec=spec,
        interpretation_source=source,
        vertex_count=len(mesh.vertices),
        face_count=len(mesh.faces),
        triangle_count=mesh.triangle_count(),
    )
