"""ASCII STL export for MeshCRDT.

Faces are fan-triangulated from their first vertex, the same technique
the 3D demo's Three.js renderer uses (see ``buildFaceGeometry`` in
``demo/static/mesh3d.js``) -- correct for the convex/simple polygons the
demo's Face tool and Extrude produce, not a general (concave-safe) ear-
clipping triangulation.
"""

from __future__ import annotations

import numpy as np

Position = tuple[float, float, float]


def _triangle_normal(a: Position, b: Position, c: Position) -> Position:
    av, bv, cv = np.array(a), np.array(b), np.array(c)
    n = np.cross(bv - av, cv - av)
    norm = np.linalg.norm(n)
    if norm < 1e-12:
        return (0.0, 0.0, 0.0)
    n = n / norm
    return (float(n[0]), float(n[1]), float(n[2]))


def mesh_to_stl(
    vertex_positions: dict[str, Position],
    face_loops: dict[str, list[str]],
    name: str = "crdt_cad_mesh",
) -> str:
    lines = [f"solid {name}"]
    for loop in face_loops.values():
        pts = [vertex_positions[v] for v in loop if v in vertex_positions]
        if len(pts) < 3:
            continue
        for i in range(1, len(pts) - 1):
            a, b, c = pts[0], pts[i], pts[i + 1]
            nx, ny, nz = _triangle_normal(a, b, c)
            lines.append(f"  facet normal {nx:.6f} {ny:.6f} {nz:.6f}")
            lines.append("    outer loop")
            for v in (a, b, c):
                lines.append(f"      vertex {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
            lines.append("    endloop")
            lines.append("  endfacet")
    lines.append(f"endsolid {name}")
    return "\n".join(lines)
