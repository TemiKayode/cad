"""3D-print preparation via ``pymeshlab``.

Deliberately **not** wired into the CRDT-injection path -- see the
package docstring for why. This is invoked only when a client asks for
a printable STL: it takes the crisp procedural (or, in the future, any
other) mesh, converts it into a triangle soup, removes duplicate
vertices/faces, and repairs non-manifold edges/vertices. Screened
Poisson surface reconstruction (full re-surfacing into a guaranteed
watertight shape) is implemented and available but **off by default**
-- it resamples the surface, which is the right trade for genuinely
messy/incomplete geometry (e.g. a hypothetical future scanned or
AI-hallucinated mesh) but would visibly round off this generator's
crisp architectural edges for no benefit, since the procedural mesh's
only real defect is the T-junctions noted in ``procedural_house.py``,
which plain non-manifold repair already resolves.

Falls back to a no-op passthrough (just triangulating each face by a
fan, same as the STL exporter) if ``pymeshlab`` isn't installed, so the
rest of the pipeline never hard-depends on it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("crdt_cad.ai.mesh_repair")

Position = tuple[float, float, float]
Triangle = tuple[int, int, int]


def _fan_triangulate_fallback(
    vertex_positions: dict[str, Position], face_loops: dict[str, list[str]]
) -> tuple[list[Position], list[Triangle]]:
    ids = list(vertex_positions.keys())
    index = {vid: i for i, vid in enumerate(ids)}
    vertices = [vertex_positions[vid] for vid in ids]
    triangles: list[Triangle] = []
    for loop in face_loops.values():
        idxs = [index[v] for v in loop if v in index]
        for i in range(1, len(idxs) - 1):
            triangles.append((idxs[0], idxs[i], idxs[i + 1]))
    return vertices, triangles


def repair_for_printing(
    vertex_positions: dict[str, Position],
    face_loops: dict[str, list[str]],
    *,
    poisson_reconstruct: bool = False,
    poisson_depth: int = 8,
) -> tuple[list[Position], list[Triangle]]:
    """Returns ``(vertices, triangles)`` -- a plain triangle soup with
    duplicate vertices/faces removed and non-manifold edges/vertices
    repaired, ready for a slicer. Falls back to an unrepaired fan
    triangulation (best-effort, not print-guaranteed) if ``pymeshlab``
    isn't installed or the repair pipeline raises for any reason.
    """
    try:
        import numpy as np
        import pymeshlab
    except ImportError:
        logger.warning("pymeshlab not installed -- returning an unrepaired triangulation")
        return _fan_triangulate_fallback(vertex_positions, face_loops)

    try:
        ids = list(vertex_positions.keys())
        index = {vid: i for i, vid in enumerate(ids)}
        vertex_matrix = np.array([vertex_positions[vid] for vid in ids], dtype=np.float64)
        face_list = [
            np.array([index[v] for v in loop if v in index], dtype=np.uint32)
            for loop in face_loops.values()
            if len([v for v in loop if v in index]) >= 3
        ]

        mesh = pymeshlab.Mesh(vertex_matrix=vertex_matrix, face_list_of_indices=face_list)
        meshset = pymeshlab.MeshSet()
        meshset.add_mesh(mesh)

        meshset.meshing_remove_duplicate_vertices()
        meshset.meshing_remove_duplicate_faces()
        meshset.meshing_repair_non_manifold_vertices()
        meshset.meshing_repair_non_manifold_edges()

        if poisson_reconstruct:
            meshset.compute_normal_per_vertex()
            meshset.generate_surface_reconstruction_screened_poisson(depth=poisson_depth)

        result = meshset.current_mesh()
        out_vertices = [tuple(row) for row in result.vertex_matrix().tolist()]
        out_triangles = [tuple(row) for row in result.face_matrix().tolist()]
        return out_vertices, out_triangles
    except Exception:
        logger.exception("pymeshlab repair pipeline failed -- returning an unrepaired triangulation")
        return _fan_triangulate_fallback(vertex_positions, face_loops)
