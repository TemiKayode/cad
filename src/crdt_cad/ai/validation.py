"""Pre-commit generation validation (Phase G1, rule 1 in
``AI_GENERATION_PROMPT.md``): "Anything the LLM influences passes
validation (watertight/manifold/bounds via the existing trimesh
machinery) before touching a room, and a validation failure is a
visible, typed error -- never a silently-injected broken mesh."

This is deliberately distinct from ``crdt_cad.geometry.mesh_validity``,
which is a **post-merge warning** over a live, possibly-mid-edit
collaborative document (an in-progress mesh is never watertight and
that's fine -- see that module's own docstring). This module runs
**before** a freshly generated mesh is turned into ops at all, so it can
hold every generator to a stricter bar: a generator's output is either a
real, complete, well-formed solid (or assembly of solids) or the
generation is rejected outright with a specific reason, not merged as a
"warning" someone might miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import trimesh

from crdt_cad.ai.mesh_types import GeneratedMesh

# Generous but real ceilings -- catch a runaway/malicious spec (e.g. a DSL
# loop bound gone wrong in G3) before it ever reaches vertex/face counts
# that would blow up the room's ops budget or the browser's renderer.
MAX_VERTICES = 50_000
MAX_FACES = 50_000
MAX_BOUNDING_BOX_M = 200.0  # a generated object/scene larger than this in
# any axis is almost certainly a spec error (wrong units, a runaway loop),
# not a legitimate design -- reject rather than silently injecting it.


@dataclass
class ValidationReport:
    ok: bool
    watertight: bool
    manifold: bool
    within_bounds: bool
    vertex_count: int
    face_count: int
    triangle_count: int
    bounding_box: tuple[float, float, float]
    errors: list[str] = field(default_factory=list)
    # Phase G5 report card field. Checked against each face's *original*
    # polygon loop (a triangle is trivially always planar; this only
    # means something for a quad+ face). Informational, not a hard gate
    # on `ok` -- no generator in this registry has ever produced a
    # non-planar face (every one is simple parametric geometry), so
    # promoting this to a failure condition alongside watertight/manifold
    # would be an untested new failure mode; report it honestly, don't
    # gate on it yet.
    planar: bool = True
    non_planar_face_count: int = 0


class GenerationValidationError(Exception):
    """Raised when a generated mesh fails validation -- the typed,
    visible error the brief asks for. Carries the full report so callers
    (the REST endpoint) can surface exactly what failed, not just
    "generation failed."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__("; ".join(report.errors) or "generated mesh failed validation")


def _check_planarity(mesh: GeneratedMesh, tolerance: float = 1e-4) -> tuple[bool, int]:
    """Checks each face's *original* polygon loop (not the post-
    triangulation triangles, which are trivially planar always) lies
    within `tolerance` of a single plane fit through its own vertices.
    Returns ``(all_planar, non_planar_face_count)``."""
    non_planar = 0
    for loop in mesh.faces.values():
        pts = [mesh.vertices[v] for v in loop if v in mesh.vertices]
        if len(pts) < 4:
            continue  # a triangle (or degenerate loop) is always planar
        arr = np.array(pts, dtype=np.float64)
        origin = arr[0]
        edge1 = arr[1] - origin
        normal = None
        for i in range(2, len(arr)):
            candidate = np.cross(edge1, arr[i] - origin)
            length = np.linalg.norm(candidate)
            if length > 1e-9:
                normal = candidate / length
                break
        if normal is None:
            continue  # collinear/degenerate loop -- not this check's concern
        deviation = np.abs((arr - origin) @ normal).max()
        if deviation > tolerance:
            non_planar += 1
    return non_planar == 0, non_planar


def _triangulate(mesh: GeneratedMesh) -> tuple[np.ndarray, np.ndarray, list[str]]:
    vertex_ids = list(mesh.vertices.keys())
    index = {vid: i for i, vid in enumerate(vertex_ids)}
    vertices = np.array([mesh.vertices[vid] for vid in vertex_ids], dtype=np.float64)

    triangles: list[tuple[int, int, int]] = []
    triangle_face_id: list[str] = []
    for face_id, loop in mesh.faces.items():
        idxs = [index[v] for v in loop if v in index]
        for i in range(1, len(idxs) - 1):
            triangles.append((idxs[0], idxs[i], idxs[i + 1]))
            triangle_face_id.append(face_id)
    return vertices, np.array(triangles, dtype=np.int64) if triangles else np.zeros((0, 3), dtype=np.int64), triangle_face_id


def validate_generated_mesh(
    mesh: GeneratedMesh,
    *,
    require_watertight: bool = True,
    require_consistent_winding: bool = True,
    max_vertices: int = MAX_VERTICES,
    max_faces: int = MAX_FACES,
    max_bounding_box_m: float = MAX_BOUNDING_BOX_M,
) -> ValidationReport:
    """Runs the bounds/watertight/manifold checks and returns a full
    report either way -- callers that want the "raise on failure"
    behavior should use :func:`validate_or_raise`. Kept as a separate,
    non-raising function so the report itself (not just pass/fail) can
    be surfaced in the G5 report card even for a mesh that's allowed to
    have some issues.

    The house generator predates this module and was never designed to
    pass either check -- its docstring already documents interior-wall
    "T-junctions" as a known, accepted, non-manifold gap that "doesn't
    matter for rendering/collaboration"; its floor/ceiling caps and
    walls also don't share one globally consistent outward-normal
    convention (confirmed pre-existing, not a Phase 5 regression: the
    exact same inconsistency reproduces against the pre-Phase-5 code at
    commit bb483fe). ``generator.py`` passes
    ``require_watertight=False, require_consistent_winding=False``
    specifically for the house generator to reflect that pre-existing,
    documented reality -- every generator introduced in Phase G1 *is*
    held to both checks at their default (`True`)."""
    errors: list[str] = []
    vertex_count = len(mesh.vertices)
    face_count = len(mesh.faces)

    if vertex_count > max_vertices:
        errors.append(f"{vertex_count} vertices exceeds the {max_vertices} limit")
    if face_count > max_faces:
        errors.append(f"{face_count} faces exceeds the {max_faces} limit")

    if not mesh.vertices:
        errors = errors or ["empty mesh"]
        return ValidationReport(
            ok=False, watertight=True, manifold=True, within_bounds=not errors,
            vertex_count=0, face_count=0, triangle_count=0, bounding_box=(0.0, 0.0, 0.0), errors=errors,
        )

    positions = np.array(list(mesh.vertices.values()), dtype=np.float64)
    mins, maxs = positions.min(axis=0), positions.max(axis=0)
    bbox = tuple(float(v) for v in (maxs - mins))
    within_bounds = all(dim <= max_bounding_box_m for dim in bbox)
    if not within_bounds:
        errors.append(f"bounding box {bbox} exceeds the {max_bounding_box_m}m-per-axis limit")

    vertices, triangles, triangle_face_id = _triangulate(mesh)
    if len(triangles) == 0:
        errors = errors or ["mesh has no triangulable faces"]
        return ValidationReport(
            ok=False, watertight=True, manifold=True, within_bounds=within_bounds,
            vertex_count=vertex_count, face_count=face_count, triangle_count=0, bounding_box=bbox,
            errors=errors,
        )

    tri_mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, process=False)

    watertight = bool(tri_mesh.is_watertight)
    if require_watertight and not watertight:
        errors.append("mesh is not watertight (has boundary edges -- an open hole in the solid)")

    manifold = bool(tri_mesh.is_winding_consistent)
    if require_consistent_winding and not manifold:
        errors.append("mesh has inconsistent face winding (non-manifold topology)")

    degenerate_ok = trimesh.triangles.nondegenerate(tri_mesh.triangles)
    if not bool(degenerate_ok.all()):
        n_bad = int((~degenerate_ok).sum())
        errors.append(f"{n_bad} degenerate (zero-area) triangle(s)")

    planar, non_planar_face_count = _check_planarity(mesh)

    ok = not errors
    return ValidationReport(
        ok=ok,
        watertight=watertight,
        manifold=manifold,
        within_bounds=within_bounds,
        vertex_count=vertex_count,
        face_count=face_count,
        triangle_count=len(triangles),
        bounding_box=bbox,
        errors=errors,
        planar=planar,
        non_planar_face_count=non_planar_face_count,
    )


def validate_or_raise(mesh: GeneratedMesh, **kwargs) -> ValidationReport:
    report = validate_generated_mesh(mesh, **kwargs)
    if not report.ok:
        raise GenerationValidationError(report)
    return report
