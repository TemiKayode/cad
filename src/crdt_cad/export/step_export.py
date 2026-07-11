"""STEP export for the 3D mesh demo, via ``build123d`` (OpenCascade
bindings) -- Phase 9's re-evaluation of the README's long-standing
"pythonOCC is conda-only in practice" blocker.

That blocker is now out of date for this specific combination: `pip
install build123d` pulls in `cadquery-ocp-novtk` (a real wheel, not a
conda-forge-only package) and works here (Windows, Python 3.14) --
confirmed by actually building and exporting a real STEP file during
development, not assumed. Scoped exactly to what the brief allows for
this stretch item: "a faceted-B-Rep STEP writer fed from MeshCRDT
triangles."

Each face loop becomes one planar ``Face`` in the common case. Nothing
in this project enforces face planarity (see
``crdt_cad.geometry.mesh_validity``'s module docstring for why
cross-component checks are warnings, not gates), so a face whose
vertices have drifted non-planar -- confirmed to raise
``ValueError: Cannot build face(s): wires not planar`` when tried
directly -- falls back to a fan triangulation of that one face instead
(the same technique ``mesh_to_stl`` already uses), since any 3 points
are trivially coplanar.

If every face joins into one closed, positive-volume solid, that's what
gets written (a genuine ``MANIFOLD_SOLID_BREP``). Building a ``Solid``
from an *open* shell doesn't raise -- it silently produces a
zero-volume degenerate solid instead (confirmed directly, not assumed)
-- so that case is detected explicitly and the raw faces are written as
an (open) ``Compound`` instead of falsely claiming a closed solid. An
incomplete/WIP mesh is the overwhelmingly common case while actively
editing, per this project's "warning not gate" design elsewhere, so this
isn't a rare edge case worth ignoring.

Deliberately not attempted: curved/NURBS surfaces, non-manifold repair,
or any topology healing -- this is a straightforward faceted export of
whatever ``MeshCRDT`` already has, not a CAD-quality solid-modeling step.

``mesh_from_step_bytes`` (Part 7 C4) is the other direction: it reads a
STEP file back in and *tessellates* it (``Shape.tessellate``, real
OpenCascade faceting, not a re-derivation of the exact B-Rep) into a
plain triangle mesh -- any curved/NURBS surface a real STEP file
contains comes back as flat facets at the given tolerance, the same
"faceted, not a CAD-quality solid-modeling step" honesty this module's
export side already commits to.
"""

from __future__ import annotations

import io
import os
import tempfile

from crdt_cad.ai.mesh_types import GeneratedMesh

Position = tuple[float, float, float]


def _face_or_fan(pts: list[Position]) -> list:
    """One planar `Face` if the loop is planar, else a fan of triangular
    `Face`s (always planar) covering the same loop -- see module
    docstring."""
    import build123d as bd

    try:
        return [bd.Face(bd.Wire.make_polygon(pts, close=True))]
    except ValueError:
        return [
            bd.Face(bd.Wire.make_polygon([pts[0], pts[i], pts[i + 1]], close=True))
            for i in range(1, len(pts) - 1)
        ]


def mesh_to_step_bytes(vertex_positions: dict[str, Position], face_loops: dict[str, list[str]]) -> bytes:
    """Returns STEP (AP214) file bytes for the given mesh, or `b""` if
    there's nothing exportable (no face has 3+ live vertices -- the same
    "skip it" rule `mesh_to_stl` already applies for a face referencing a
    deleted vertex)."""
    import build123d as bd

    faces: list = []
    for loop in face_loops.values():
        pts = [vertex_positions[v] for v in loop if v in vertex_positions]
        if len(pts) < 3:
            continue
        faces.extend(_face_or_fan(pts))
    if not faces:
        return b""

    shape = bd.Solid(bd.Shell(faces))
    if abs(shape.volume) < 1e-9:
        shape = bd.Compound(faces)

    buf = io.BytesIO()
    bd.export_step(shape, buf)
    return buf.getvalue()


def mesh_from_step_bytes(data: bytes, tolerance: float = 0.1) -> GeneratedMesh:
    """Part 7 C4 (STEP-import interop): reads a STEP file back in via
    `build123d.import_step` and tessellates every solid/shell it
    contains into a triangle mesh (`Shape.tessellate`, the same
    OpenCascade-backed faceting `mesh_to_step_bytes`'s own round-trip
    was verified against). `import_step` needs a real filesystem path
    (it wraps `STEPCAFControl_Reader.ReadFile`, which reads a path, not
    a stream) -- `data` is written to a throwaway temp file first and
    always cleaned up, even on failure.

    A malformed/unparseable STEP file doesn't raise from `import_step`
    itself (OpenCascade logs a parser error to stderr and hands back an
    empty `Compound` instead) -- `tessellate` raising `ValueError:
    Cannot tessellate an empty shape` on that empty result is what
    actually surfaces the failure to the caller, so no separate
    emptiness check is needed here.

    OpenCascade's tessellator emits one independent vertex triple per
    triangle (no sharing across faces), so a naive pass-through would
    inflate a modest STEP file into thousands of near-duplicate CRDT
    vertices; positions are welded by rounding to 6 decimal places
    first, the same precision `mesh_to_stl`'s own vertex formatting
    already uses."""
    import build123d as bd

    fd, path = tempfile.mkstemp(suffix=".step")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shape = bd.import_step(path)
        raw_vertices, raw_triangles = shape.tessellate(tolerance)
    finally:
        os.unlink(path)

    welded_index: dict[tuple[float, float, float], str] = {}
    vertices: dict[str, Position] = {}
    remap: list[str] = []
    for v in raw_vertices:
        key = (round(v.X, 6), round(v.Y, 6), round(v.Z, 6))
        vid = welded_index.get(key)
        if vid is None:
            vid = f"v{len(welded_index)}"
            welded_index[key] = vid
            vertices[vid] = key
        remap.append(vid)

    faces: dict[str, list[str]] = {}
    for i, (a, b, c) in enumerate(raw_triangles):
        loop = [remap[a], remap[b], remap[c]]
        if len({*loop}) < 3:
            continue  # degenerate triangle collapsed by welding
        faces[f"f{i}"] = loop

    return GeneratedMesh(vertices=vertices, faces=faces)
