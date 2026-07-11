"""glTF (binary `.glb`) and 3MF export for the 3D mesh demo, via
`trimesh`'s own built-in exporters (Part 7 C4) -- unlike STEP's optional
`build123d`/OpenCascade extra (a genuinely heavy, native-build
dependency), `trimesh` is already a core dependency of this project, so
these two formats only needed two ordinary, wheel-installable packages
added (`networkx`, `lxml`) rather than a new optional extra.

`.gltf` (the separate JSON + `.bin` variant) is not exposed here --
`trimesh`'s ``file_type="gltf"`` returns a dict of multiple files, which
doesn't fit this project's existing "one exported file, one HTTP
attachment" shape every other format (SVG/DXF/PDF/STL/STEP) already
has. `.glb` (the single-file binary container) is the standard choice
for exactly that shape and is what real glTF-consuming tools expect for
a one-click download anyway.

Faces are fan-triangulated from their first vertex before handing to
`trimesh`, the same technique `mesh_to_stl`/`mesh_to_step_bytes`
already use (see `stl_export.py`'s module docstring) -- correct for the
convex/simple polygons this project's own Face tool and Extrude
produce, not a general concave-safe triangulation. `process=False` on
the `trimesh.Trimesh` construction is deliberate: `trimesh`'s default
processing silently merges near-duplicate vertices and drops
degenerate faces, which would make the exported file not exactly match
`MeshCRDT`'s current state -- the same "export exactly what's there,
don't silently repair it" choice `mesh_to_stl`/`mesh_to_step_bytes`
already make.
"""

from __future__ import annotations

import numpy as np
import trimesh

Position = tuple[float, float, float]


def _triangulated_trimesh(
    vertex_positions: dict[str, Position], face_loops: dict[str, list[str]]
) -> trimesh.Trimesh | None:
    ids = list(vertex_positions.keys())
    if not ids:
        return None
    index_of = {vid: i for i, vid in enumerate(ids)}
    vertices = np.array([vertex_positions[vid] for vid in ids], dtype=float)
    triangles: list[tuple[int, int, int]] = []
    for loop in face_loops.values():
        pts = [v for v in loop if v in vertex_positions]
        if len(pts) < 3:
            continue
        for i in range(1, len(pts) - 1):
            triangles.append((index_of[pts[0]], index_of[pts[i]], index_of[pts[i + 1]]))
    if not triangles:
        return None
    return trimesh.Trimesh(vertices=vertices, faces=np.array(triangles, dtype=int), process=False)


def mesh_to_glb_bytes(vertex_positions: dict[str, Position], face_loops: dict[str, list[str]]) -> bytes:
    """Returns binary glTF (`.glb`) bytes, or `b""` if there's nothing
    exportable (no face has 3+ live vertices -- the same "skip it" rule
    every other mesh exporter in this project applies)."""
    mesh = _triangulated_trimesh(vertex_positions, face_loops)
    if mesh is None:
        return b""
    return trimesh.exchange.gltf.export_glb(mesh)


def mesh_to_3mf_bytes(vertex_positions: dict[str, Position], face_loops: dict[str, list[str]]) -> bytes:
    """Returns 3MF (a zip container) bytes, or `b""` if there's nothing
    exportable."""
    mesh = _triangulated_trimesh(vertex_positions, face_loops)
    if mesh is None:
        return b""
    data = mesh.export(file_type="3mf")
    return data if isinstance(data, bytes) else bytes(data)
