"""Orchestrates prompt -> ``(generator, spec)`` -> mesh -> a batch of
:class:`MeshOp` ready for CRDT injection (Phase G1: dispatch across the
whole generator registry, not just the house archetype).

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

from pydantic import BaseModel

from crdt_cad.ai.interpreter import interpret_prompt
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.meshy_adapter import generate_mesh_via_meshy, meshy_api_key
from crdt_cad.ai.registry import get_generator
from crdt_cad.ai.validation import ValidationReport, validate_generated_mesh, validate_or_raise
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
    "metal": "#9099a3",
}

# The house generator predates pre-commit validation and its own
# docstring already documents why it doesn't (and isn't expected to)
# pass a strict watertight/consistent-winding bar -- see
# validation.py's docstring for the full explanation. Every generator
# introduced in Phase G1 *is* held to both checks. A hosted-API mesh
# (Meshy, when configured) is also exempted: its geometry isn't this
# project's own code, and that whole path is already documented as
# unverified against the live API (meshy_adapter.py's module docstring).
_RELAXED_VALIDATION_GENERATORS = {"house"}


@dataclass
class GenerationResult:
    ops: list[MeshOp]
    generator_name: str
    spec: BaseModel
    interpretation_source: str  # "llm" | "heuristic"
    mesh_source: str  # "meshy" | "procedural"
    vertex_count: int
    face_count: int
    triangle_count: int
    validation: ValidationReport


def generate_mesh_ops(prompt: str, *, actor_id: str = DEFAULT_ACTOR_ID) -> GenerationResult:
    """Synchronous end-to-end pipeline. Safe to call from a worker
    thread (``asyncio.to_thread``); does no networking of its own beyond
    whatever ``interpret_prompt``'s LLM path and (if ``MESHY_API_KEY`` is
    set) ``generate_mesh_via_meshy`` perform internally.

    The prompt is always interpreted into a ``(generator_name, spec)``
    pair (for the response's informational fields, and as the
    deterministic fallback), independent of which mesh actually gets
    used: if ``MESHY_API_KEY`` is set and Meshy's hosted text-to-3D API
    returns a real mesh, that mesh is injected instead of the
    dispatched generator's own procedural one -- see
    ``crdt_cad.ai.meshy_adapter``'s module docstring for why that whole
    path is unverified against the live API and always degrades safely
    to the procedural pipeline below on any failure.

    Raises :class:`crdt_cad.ai.validation.GenerationValidationError` if
    the resulting mesh fails pre-commit validation (rule 1: never
    silently inject a broken mesh) -- callers (the REST endpoint) turn
    this into a typed, visible error response.
    """
    generator_name, spec, source = interpret_prompt(prompt)

    mesh: GeneratedMesh | None = None
    mesh_source = "procedural"
    if meshy_api_key():
        mesh = generate_mesh_via_meshy(prompt)
        if mesh is not None:
            mesh_source = "meshy"
    if mesh is None:
        mesh = get_generator(generator_name).build(spec)

    relax = generator_name in _RELAXED_VALIDATION_GENERATORS or mesh_source == "meshy"
    if relax:
        validation = validate_generated_mesh(mesh, require_watertight=False, require_consistent_winding=False)
    else:
        validation = validate_or_raise(mesh)

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
        generator_name=generator_name,
        spec=spec,
        interpretation_source=source,
        mesh_source=mesh_source,
        vertex_count=len(mesh.vertices),
        face_count=len(mesh.faces),
        triangle_count=mesh.triangle_count(),
        validation=validation,
    )


def _color_for_material(material: str) -> str:
    return _MATERIAL_COLORS.get(material, "#b8b2a4")
