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
"""

from __future__ import annotations

import io

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
