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

import logging
import time
from dataclasses import dataclass

from pydantic import BaseModel

from crdt_cad.ai.dsl import DSLError, DSLProgramSpec, execute_dsl_program
from crdt_cad.ai.interpreter import interpret_edit, interpret_prompt, llm_repair_dsl_program
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.meshy_adapter import generate_mesh_via_meshy, meshy_api_key
from crdt_cad.ai.registry import REGISTRY, dispatch_by_keyword, get_generator
from crdt_cad.ai.scene import SceneSpec, expand_scene, merge_placed_objects
from crdt_cad.ai.scene_layout import solve_layout
from crdt_cad.ai.validation import (
    GenerationValidationError,
    ValidationReport,
    validate_generated_mesh,
    validate_or_raise,
)
from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp, new_id

logger = logging.getLogger("crdt_cad.ai.generator")

DEFAULT_ACTOR_ID = "ai_generator_bot"


class EditNotSupportedError(Exception):
    """Raised by :func:`generate_edit_ops` for a generation whose
    ``generator_name`` isn't a plain registry entry -- editing a scene
    (which sub-object would the edit even apply to?) or a DSL program
    (re-running its whole retry/repair loop for an edit is meaningfully
    more work than a single-object regenerate) isn't supported yet. A
    documented Phase G4 scope boundary, not a silent no-op: the endpoint
    turns this into a clear 422, the same as any other typed generation
    error."""

# The initial attempt plus this many repair attempts (each feeding the
# specific validation/budget error back to the model) before Phase G3
# gives up on DSL synthesis and falls back to the closest registry
# archetype rather than leaving the user with a bare error.
MAX_DSL_REPAIR_ATTEMPTS = 2

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
    # Populated only for ``generator_name == "scene"``: `ops` grouped by
    # scene object, in build order, so the server can commit each object
    # as its own batch -- a scene appearing object by object rather than
    # all at once. ``None`` for an ordinary single-object generation.
    object_ops: list[list[MeshOp]] | None = None
    # Populated only when the DSL path (Phase G3) was attempted: one
    # entry per attempt (``{"attempt": i, "outcome": "ok"|"failed",
    # "error": str | None}``) -- ahead of Phase G5/G6's own consumption
    # of this as a metric/report-card input. ``None`` for every other
    # generation path.
    dsl_attempts: list[dict] | None = None
    # Phase G4 provenance: the id every face this generation produced was
    # tagged with (``set_face_prop(face_id, "generation_id", ...)``), and
    # the key ``ops`` also writes a ``set_generation`` record under in
    # room state. Defaults to "" only so existing test doubles that
    # construct a `GenerationResult` directly (before this field existed)
    # don't need updating -- every real code path always sets it.
    generation_id: str = ""
    # Phase G5 report card field: total wall-clock time for this call.
    # The endpoint overwrites this with its own, more precise measurement
    # spanning both the early-interpretation phase (broadcast as "understood:
    # ..." chips) and the build phase, since those run as two separate
    # to_thread calls there -- this default covers direct (non-endpoint)
    # callers, e.g. tests, that invoke this module's functions standalone.
    elapsed_seconds: float = 0.0


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
    start = time.monotonic()
    generator_name, spec, source = interpret_prompt(prompt)
    result = generate_ops_from_interpretation(prompt, generator_name, spec, source, actor_id=actor_id)
    result.elapsed_seconds = time.monotonic() - start
    return result


def generate_ops_from_interpretation(
    prompt: str, generator_name: str, spec: BaseModel, source: str, *, actor_id: str = DEFAULT_ACTOR_ID,
    meshy_mesh: GeneratedMesh | None = None, meshy_attempted: bool = False,
) -> GenerationResult:
    """The rest of the pipeline once ``(generator_name, spec, source)``
    is already known -- split out from :func:`generate_mesh_ops` so the
    server (Phase G5) can call ``interpret_prompt`` itself, broadcast
    "understood: ..." chips to the room *before* geometry lands, and
    only then run this (slower) half as its own worker-thread call,
    without interpreting the prompt twice.

    ``meshy_mesh``/``meshy_attempted`` (Phase G7): a caller with its own
    room to stream progress to (the endpoint) uses the matured async
    Meshy path (``meshy_adapter.generate_mesh_via_meshy_async``)
    *before* calling this function, and passes whatever it got --
    including ``None`` on failure -- so this function's own (always
    synchronous) Meshy attempt below is skipped either way rather than
    redundantly retrying an already-failed call. Callers with no room
    (direct calls, tests) leave both at their defaults and get the
    original behavior: this function tries Meshy itself, synchronously,
    exactly as it always has.
    """
    if generator_name == "scene":
        return _generate_scene_ops(prompt, spec, source, actor_id=actor_id)
    if generator_name == "dsl":
        return _generate_dsl_ops(prompt, spec, source, actor_id=actor_id)

    mesh: GeneratedMesh | None = meshy_mesh
    mesh_source = "procedural"
    if mesh is not None:
        mesh_source = "meshy"
    elif not meshy_attempted and meshy_api_key():
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

    generation_id = new_id("gen")
    clock = LamportClock(actor=actor_id)
    scratch = MeshCRDT(clock)
    ops = _mint_ops_for_mesh(scratch, mesh, generation_id)
    ops.append(scratch.set_generation(generation_id, _generation_record(
        prompt, generator_name, spec, source, mesh_source,
    )))

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
        generation_id=generation_id,
    )


def interpretation_chips(generator_name: str, spec: BaseModel) -> list[str]:
    """Short "understood: ..." chips (Phase G5) summarizing what
    interpretation produced -- broadcast to the whole room immediately
    after ``interpret_prompt``/``interpret_edit`` returns, before any
    geometry has been built, so collaborators see what the model or
    heuristic understood even while the mesh is still being generated.
    The "after" counterpart to this is ``mesh3d.js``'s own
    ``describeGeneratedSpec``, which renders the *final* per-generation
    summary once geometry has actually landed -- deliberately not
    shared code, since a Python dict and a JS object need their own
    natural idioms, but the same information."""
    d = spec.model_dump()
    if generator_name == "house":
        chips = [f"{d['bedrooms']} bedroom(s)", f"{d['floors']} floor(s)", f"{d['floor_material']} floor", f"{d['style']} style"]
        if d.get("garage"):
            chips.append("garage")
        if d.get("roof_type") and d["roof_type"] != "flat":
            chips.append(f"{d['roof_type']} roof")
        return chips
    if generator_name == "scene":
        counts: dict[str, int] = {}
        for obj in d.get("objects", []):
            counts[obj["generator"]] = counts.get(obj["generator"], 0) + obj.get("count", 1)
        return [f"{n}x {name}" if n > 1 else name for name, n in counts.items()]
    if generator_name == "dsl":
        root_op = (d.get("root") or {}).get("op", "shape")
        chips = [f"custom {root_op} shape"]
        if d.get("material"):
            chips.append(d["material"])
        return chips
    chips = [f"{k.replace('_m', '')}: {v}m" for k, v in d.items() if isinstance(v, (int, float)) and k.endswith("_m")][:4]
    return chips or [generator_name]


def _generation_record(prompt: str, generator_name: str, spec: BaseModel, source: str, mesh_source: str) -> dict:
    """The Phase G4 spec-persistence record -- the *final* spec (not a
    history of every edit), per the brief's own "store each generation's
    final spec" language: an edit overwrites this record wholesale
    rather than appending to it."""
    return {
        "prompt": prompt,
        "generator_name": generator_name,
        "spec": spec.model_dump(),
        "interpretation_source": source,
        "mesh_source": mesh_source,
    }


def _fresh_ids(mesh: GeneratedMesh) -> tuple[dict[str, str], dict[str, str]]:
    """Every generator's own ``build()`` starts a fresh ``MeshBuilder``
    that numbers vertices/faces v1/f1/v2/f2/... from scratch -- perfectly
    fine for one generation in isolation, but a real collision the
    moment a *second, separate* generation lands in the same room: two
    sequential calls to ``get_generator(name).build(spec)`` produce the
    exact same id strings, so the second generation's ``add_vertex("v1",
    ...)`` doesn't create a new vertex, it silently overwrites the
    first's (an LWWMap ``set`` on an existing key is exactly that -- a
    move, not a create). Found and confirmed empirically while building
    Phase G4's edit path (which makes the collision immediately visible
    as corrupted geometry), fixed here so it can never happen on *any*
    generation path -- see the regression test in test_generator.py
    that generates a table then a chair into the same document and
    asserts both survive intact."""
    vertex_ids = {old: new_id("v") for old in mesh.vertices}
    face_ids = {old: new_id("f") for old in mesh.faces}
    return vertex_ids, face_ids


def _mint_ops_for_mesh(scratch: MeshCRDT, mesh: GeneratedMesh, generation_id: str) -> list[MeshOp]:
    """Shared vertex/face/material/edge op-minting loop -- built against
    a throwaway ``MeshCRDT`` (not the live room's document) so every op
    gets a fresh, correctly-ordered ``OpId`` from one dedicated actor
    identity, and the resulting op list can be handed to the room to
    apply+broadcast in controlled batches rather than mutating the live
    document eagerly. Shared by every generation path (single-object,
    DSL success, DSL fallback) so they mint ops identically.

    Every id is remapped to a globally-fresh one first (see
    :func:`_fresh_ids`), and every face is tagged with ``generation_id``
    (Phase G4 provenance) via the same ``face_props`` LWWMap-per-face
    mechanism already used for "material"/"color"/"scene_object" -- it
    merges cleanly across replicas (each face's prop bag resolves
    independently) and needs no new CRDT machinery, just one more key on
    a bag that already exists."""
    vertex_ids, face_ids = _fresh_ids(mesh)
    ops: list[MeshOp] = []
    for old_vertex_id, position in mesh.vertices.items():
        ops.append(scratch.add_vertex(vertex_ids[old_vertex_id], position))
    for old_face_id, loop in mesh.faces.items():
        new_face_id = face_ids[old_face_id]
        remapped_loop = [vertex_ids[v] for v in loop]
        ops.extend(scratch.add_face(new_face_id, remapped_loop))
        material = mesh.face_materials.get(old_face_id, "")
        if material:
            ops.append(scratch.set_face_prop(new_face_id, "material", material))
            ops.append(scratch.set_face_prop(new_face_id, "color", _color_for_material(material)))
        ops.append(scratch.set_face_prop(new_face_id, "generation_id", generation_id))
        for i in range(len(remapped_loop)):
            a, b = remapped_loop[i], remapped_loop[(i + 1) % len(remapped_loop)]
            ops.append(scratch.add_edge(a, b))
    return ops


def generation_geometry(
    face_loops: dict[str, list[str]], face_props_by_id: dict[str, dict], generation_id: str,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Pure lookup (no live doc access, easy to test in isolation): which
    faces/vertices/edges in a document currently belong to
    `generation_id`, for :func:`generate_edit_ops` to remove. A vertex
    is only included if *no other, differently-provenanced* face still
    references it -- an AI generation's own vertices are never shared
    with anything outside it in practice, but this is a real safety
    check, not an assumption, so a hypothetical future edit can never
    delete a vertex something else still depends on."""
    old_face_ids = [fid for fid in face_loops if face_props_by_id.get(fid, {}).get("generation_id") == generation_id]
    old_face_id_set = set(old_face_ids)

    used_elsewhere: set[str] = set()
    candidate_vertices: set[str] = set()
    for fid, loop in face_loops.items():
        if fid in old_face_id_set:
            candidate_vertices.update(loop)
        else:
            used_elsewhere.update(loop)
    old_vertex_ids = [v for v in candidate_vertices if v not in used_elsewhere]

    old_edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for fid in old_face_ids:
        loop = face_loops[fid]
        for i in range(len(loop)):
            a, b = loop[i], loop[(i + 1) % len(loop)]
            key = (a, b) if a <= b else (b, a)
            if key not in seen:
                seen.add(key)
                old_edges.append((a, b))

    return old_face_ids, old_vertex_ids, old_edges


def generate_edit_ops(
    edit_prompt: str,
    generation_id: str,
    prior_record: dict,
    old_face_ids: list[str],
    old_vertex_ids: list[str],
    old_edges: list[tuple[str, str]],
    start_counter: int,
    *,
    actor_id: str = DEFAULT_ACTOR_ID,
) -> GenerationResult:
    """Phase G4 follow-up edits: regenerates the *same* generation's
    mesh from an edited spec and applies the delta as ordinary CRDT ops
    -- remove the old generation's geometry, add the new geometry under
    the same generation id (an edit refines a generation, it doesn't
    start a new one). A pure function like :func:`generate_mesh_ops`
    (safe for a worker thread): the caller does the live-document
    reads (which faces/vertices/edges belong to `generation_id`, what
    Lamport counter this actor has already reached) on the event loop
    *before* calling this, and applies the returned ops back on the
    event loop afterward -- this function itself never touches the
    live room document.

    Scoped to single-object (registry) generations only: editing a
    scene or a DSL program raises :class:`EditNotSupportedError` rather
    than a half-implemented, likely-wrong result -- a documented,
    honest scope boundary for this phase.

    `start_counter` seeds this call's throwaway ``LamportClock`` so its
    *removal* ops out-rank the room's already-applied *creation* ops
    from this same actor identity. A brand-new ``LamportClock(actor=...)``
    always starts at counter 0, which is harmless for the fresh ids
    every other generation path mints (nothing to race against yet) but
    not for *removing* an id an earlier, separate call already wrote at
    a possibly much higher counter: ``LWWMap.apply`` rejects any op
    whose ``op_id <= existing.op_id``, so an unseeded clock's removal
    ops here would silently no-op, leaving the "removed" geometry still
    live. Pass ``room.doc.frontier().get(actor_id)``.
    """
    start = time.monotonic()
    check_edit_supported(prior_record)
    new_generator_name, new_spec, source = interpret_edit(edit_prompt, prior_record)
    result = generate_edit_ops_from_interpretation(
        edit_prompt, generation_id, new_generator_name, new_spec, source,
        old_face_ids, old_vertex_ids, old_edges, start_counter, actor_id=actor_id,
    )
    result.elapsed_seconds = time.monotonic() - start
    return result


def check_edit_supported(prior_record: dict) -> None:
    """Raises :class:`EditNotSupportedError` for a scene/DSL generation
    -- split out so the server (Phase G5) can check this *before*
    calling ``interpret_edit`` (and before broadcasting "understood:
    ..." chips for an edit that's about to be rejected anyway)."""
    generator_name = prior_record["generator_name"]
    if generator_name not in REGISTRY:
        raise EditNotSupportedError(
            f"editing a {generator_name!r} generation isn't supported yet -- "
            "only single-object registry generations (not scenes or custom DSL shapes) can be edited"
        )


def generate_edit_ops_from_interpretation(
    edit_prompt: str,
    generation_id: str,
    generator_name: str,
    new_spec: BaseModel,
    source: str,
    old_face_ids: list[str],
    old_vertex_ids: list[str],
    old_edges: list[tuple[str, str]],
    start_counter: int,
    *,
    actor_id: str = DEFAULT_ACTOR_ID,
) -> GenerationResult:
    """The rest of :func:`generate_edit_ops` once ``interpret_edit`` has
    already run -- split out for the same early-chip-broadcast reason as
    :func:`generate_ops_from_interpretation`."""
    mesh = get_generator(generator_name).build(new_spec)
    relax = generator_name in _RELAXED_VALIDATION_GENERATORS
    validation = (
        validate_generated_mesh(mesh, require_watertight=False, require_consistent_winding=False)
        if relax else validate_or_raise(mesh)
    )

    clock = LamportClock(actor=actor_id, counter=start_counter)
    scratch = MeshCRDT(clock)
    ops: list[MeshOp] = []
    for a, b in old_edges:
        ops.append(scratch.remove_edge(a, b))
    for face_id in old_face_ids:
        ops.append(scratch.remove_face(face_id))
    for vertex_id in old_vertex_ids:
        ops.append(scratch.remove_vertex(vertex_id))
    ops.extend(_mint_ops_for_mesh(scratch, mesh, generation_id))
    ops.append(scratch.set_generation(
        generation_id, _generation_record(edit_prompt, generator_name, new_spec, source, "procedural"),
    ))

    return GenerationResult(
        ops=ops,
        generator_name=generator_name,
        spec=new_spec,
        interpretation_source=source,
        mesh_source="procedural",
        vertex_count=len(mesh.vertices),
        face_count=len(mesh.faces),
        triangle_count=mesh.triangle_count(),
        validation=validation,
        generation_id=generation_id,
    )


def _generate_scene_ops(prompt: str, scene: SceneSpec, source: str, *, actor_id: str) -> GenerationResult:
    """Phase G2 scene path: build every object's own mesh, position them
    with the deterministic layout solver (never the LLM), merge into one
    globally-id-unique mesh, then mint ops *grouped by object* -- both
    ``ops`` (the flat list, for callers that don't care) and
    ``object_ops`` (one sub-list per object, for the server to commit as
    separate batches so the scene appears object by object). A whole
    scene is *one* Phase G4 generation (one id, one spec-persistence
    record for the composing prompt) -- undoing it removes every object
    at once, matching "the scene" being the unit the user actually asked
    for, not its individual objects."""
    expanded = expand_scene(scene)
    translations = solve_layout(expanded)
    mesh, per_object_ids = merge_placed_objects(expanded, translations)

    relax = any(obj.generator in _RELAXED_VALIDATION_GENERATORS for obj in expanded)
    if relax:
        validation = validate_generated_mesh(mesh, require_watertight=False, require_consistent_winding=False)
    else:
        validation = validate_or_raise(mesh)

    generation_id = new_id("gen")
    clock = LamportClock(actor=actor_id)
    scratch = MeshCRDT(clock)
    ops: list[MeshOp] = []
    object_ops: list[list[MeshOp]] = []

    # Same collision fix as `_mint_ops_for_mesh` (see its docstring):
    # `merge_placed_objects` already guarantees ids are unique *within*
    # this one scene, but its shared MeshBuilder still numbers from
    # v1/f1 every call, so a *second, separate* generation (scene or
    # not) in the same room would otherwise collide with this one.
    fresh_vertex_ids = {old: new_id("v") for old in mesh.vertices}
    fresh_face_ids = {old: new_id("f") for old in mesh.faces}

    for obj_index, (vertex_ids, face_ids) in enumerate(per_object_ids):
        this_object_ops: list[MeshOp] = []
        for vertex_id in vertex_ids:
            this_object_ops.append(scratch.add_vertex(fresh_vertex_ids[vertex_id], mesh.vertices[vertex_id]))
        for face_id in face_ids:
            loop = mesh.faces[face_id]
            new_face_id = fresh_face_ids[face_id]
            remapped_loop = [fresh_vertex_ids[v] for v in loop]
            this_object_ops.extend(scratch.add_face(new_face_id, remapped_loop))
            material = mesh.face_materials.get(face_id, "")
            if material:
                this_object_ops.append(scratch.set_face_prop(new_face_id, "material", material))
                this_object_ops.append(scratch.set_face_prop(new_face_id, "color", _color_for_material(material)))
            this_object_ops.append(scratch.set_face_prop(new_face_id, "scene_object", str(obj_index)))
            this_object_ops.append(scratch.set_face_prop(new_face_id, "generation_id", generation_id))
            for i in range(len(remapped_loop)):
                a, b = remapped_loop[i], remapped_loop[(i + 1) % len(remapped_loop)]
                this_object_ops.append(scratch.add_edge(a, b))
        object_ops.append(this_object_ops)
        ops.extend(this_object_ops)

    generation_op = scratch.set_generation(generation_id, _generation_record(prompt, "scene", scene, source, "procedural"))
    ops.append(generation_op)
    object_ops[-1].append(generation_op)

    return GenerationResult(
        ops=ops,
        object_ops=object_ops,
        generator_name="scene",
        spec=scene,
        interpretation_source=source,
        mesh_source="procedural",
        vertex_count=len(mesh.vertices),
        face_count=len(mesh.faces),
        triangle_count=mesh.triangle_count(),
        validation=validation,
        generation_id=generation_id,
    )


def _generate_dsl_ops(prompt: str, spec: DSLProgramSpec, source: str, *, actor_id: str) -> GenerationResult:
    """Phase G3: execute the DSL program -> validate -> on failure, feed
    the *specific* error back to the model for up to
    ``MAX_DSL_REPAIR_ATTEMPTS`` repair attempts -> on final failure, fall
    back to the closest registry archetype (a real, working object with
    reduced fidelity to the request) rather than a bare error, matching
    every other path's "never a broken/silent result" rule. Every
    attempt's outcome is recorded in ``dsl_attempts`` regardless of
    which branch this ends up returning through."""
    program = {"root": spec.root, "material": spec.material}
    attempts: list[dict] = []
    mesh: GeneratedMesh | None = None
    validation: ValidationReport | None = None

    for attempt in range(MAX_DSL_REPAIR_ATTEMPTS + 1):
        try:
            mesh = execute_dsl_program(program)
            validation = validate_or_raise(mesh)
            attempts.append({"attempt": attempt, "outcome": "ok", "error": None})
            break
        except (DSLError, GenerationValidationError) as exc:
            error_text = str(exc)
            attempts.append({"attempt": attempt, "outcome": "failed", "error": error_text})
            logger.info("DSL attempt %d failed for prompt %r: %s", attempt, prompt, error_text)
            mesh = None
            if attempt >= MAX_DSL_REPAIR_ATTEMPTS or source != "llm":
                break
            try:
                program = llm_repair_dsl_program(prompt, program, error_text)
            except Exception as repair_exc:
                logger.info("DSL repair call failed (%s); stopping retries", repair_exc)
                break

    if mesh is not None and validation is not None:
        generation_id = new_id("gen")
        clock = LamportClock(actor=actor_id)
        scratch = MeshCRDT(clock)
        ops = _mint_ops_for_mesh(scratch, mesh, generation_id)
        ops.append(scratch.set_generation(generation_id, _generation_record(prompt, "dsl", spec, source, "procedural")))
        return GenerationResult(
            ops=ops,
            generator_name="dsl",
            spec=spec,
            interpretation_source=source,
            mesh_source="procedural",
            vertex_count=len(mesh.vertices),
            face_count=len(mesh.faces),
            triangle_count=mesh.triangle_count(),
            validation=validation,
            dsl_attempts=attempts,
            generation_id=generation_id,
        )

    logger.warning("DSL synthesis exhausted all attempts for prompt %r; falling back to the registry", prompt)
    fallback_entry = dispatch_by_keyword(prompt) or get_generator("house")
    fallback_spec = fallback_entry.spec_model()
    fallback_mesh = fallback_entry.build(fallback_spec)
    relax = fallback_entry.name in _RELAXED_VALIDATION_GENERATORS
    fallback_validation = (
        validate_generated_mesh(fallback_mesh, require_watertight=False, require_consistent_winding=False)
        if relax else validate_or_raise(fallback_mesh)
    )
    generation_id = new_id("gen")
    clock = LamportClock(actor=actor_id)
    scratch = MeshCRDT(clock)
    ops = _mint_ops_for_mesh(scratch, fallback_mesh, generation_id)
    ops.append(scratch.set_generation(
        generation_id, _generation_record(prompt, fallback_entry.name, fallback_spec, source, "procedural"),
    ))
    return GenerationResult(
        ops=ops,
        generator_name=fallback_entry.name,
        spec=fallback_spec,
        interpretation_source=source,
        mesh_source="procedural",
        vertex_count=len(fallback_mesh.vertices),
        face_count=len(fallback_mesh.faces),
        triangle_count=fallback_mesh.triangle_count(),
        validation=fallback_validation,
        dsl_attempts=attempts,
        generation_id=generation_id,
    )


def _color_for_material(material: str) -> str:
    return _MATERIAL_COLORS.get(material, "#b8b2a4")
