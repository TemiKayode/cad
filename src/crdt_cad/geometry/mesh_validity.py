"""3D analogue of ``validity.py``'s 2D self-intersection gate -- except a
CRDT merge can never be *rejected* the way an incoming point can (that
would break convergence: every replica must apply the same op regardless
of what else concurrently happened elsewhere). This is a **warning**,
not a gate: it runs *after* a delta touching face topology has already
been merged, and surfaces a problem for humans to look at and fix,
rather than blocking anything.

This exists specifically for the "Extrusion Nightmare" scenario the
architecture critique raised (see README, "Responses to the architecture
critique", claim 3): a face-boundary edit racing an extrude of that same
face can converge to geometry that's individually valid per sub-CRDT
(``MeshCRDT`` merges vertices/edges/face_index/face boundaries each
correctly on their own terms) but is *cross-component* inconsistent --
a face referencing a deleted vertex, two faces sharing an edge with
inconsistent winding, or an edge used by more than two faces.

Deliberately does **not** check global watertightness: an
in-progress collaborative mesh is essentially never a closed solid
(the AI generator's own house mid-batch, or a user who's placed three
vertices and one triangle, both look "not watertight" to `trimesh` and
always will until deliberately completed) -- surfacing that as a
"problem" on every edit would be constant, meaningless noise, not a
regression signal. The three checks below are all meaningful at *any*
point in editing, complete mesh or not.
"""

from __future__ import annotations

import numpy as np
import trimesh

VertexId = str
FaceId = str
Position = tuple[float, float, float]


def check_mesh_validity(
    vertex_positions: dict[VertexId, Position],
    face_loops: dict[FaceId, list[VertexId]],
) -> list[dict]:
    """Returns a list of ``{"faces": [face_id, ...], "problem": "..."}``
    dicts -- empty if nothing looks wrong. Pure read: never mutates
    anything, safe to run after every merge that touched face topology.
    """
    if not face_loops:
        return []

    vertex_ids = list(vertex_positions.keys())
    index = {vid: i for i, vid in enumerate(vertex_ids)}
    vertices = np.array([vertex_positions[vid] for vid in vertex_ids], dtype=np.float64)

    triangles: list[tuple[int, int, int]] = []
    triangle_face_id: list[FaceId] = []
    for face_id, loop in face_loops.items():
        idxs = [index[v] for v in loop if v in index]
        if len(idxs) < 3:
            # A face whose boundary no longer resolves to at least 3 live
            # vertices (e.g. one was concurrently deleted) can't even be
            # triangulated -- that is itself the problem, report it
            # directly rather than silently skipping it.
            problem_faces = [face_id]
            return [{"faces": problem_faces, "problem": "face boundary has fewer than 3 live vertices"}]
        for i in range(1, len(idxs) - 1):
            triangles.append((idxs[0], idxs[i], idxs[i + 1]))
            triangle_face_id.append(face_id)

    if not triangles:
        return []

    mesh = trimesh.Trimesh(vertices=vertices, faces=np.array(triangles), process=False)
    problems: list[dict] = []

    # 1. Degenerate triangles: zero (or near-zero) area, or a duplicate
    # vertex within one triangle -- meaningful regardless of how complete
    # the mesh is.
    ok = trimesh.triangles.nondegenerate(mesh.triangles)
    degenerate_faces = sorted({triangle_face_id[i] for i, is_ok in enumerate(ok) if not is_ok})
    if degenerate_faces:
        problems.append({"faces": degenerate_faces, "problem": "degenerate (zero-area or duplicate-vertex) face"})

    # 2. Non-manifold edges: an edge used by more than two triangles is a
    # real topological inconsistency at any point in editing (e.g. two
    # concurrently-created faces that ended up overlapping the same
    # boundary segment).
    edges_sorted = mesh.edges_sorted
    unique_edges, inverse, counts = np.unique(edges_sorted, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    nonmanifold_rows = np.nonzero(counts > 2)[0]
    if len(nonmanifold_rows):
        face_of_row = np.repeat(np.arange(len(mesh.faces)), 3)
        nonmanifold_faces: set[FaceId] = set()
        for row_idx in nonmanifold_rows:
            for tri_idx in face_of_row[np.nonzero(inverse == row_idx)[0]]:
                nonmanifold_faces.add(triangle_face_id[tri_idx])
        problems.append({"faces": sorted(nonmanifold_faces), "problem": "edge shared by more than two faces (non-manifold)"})

    # 3. Winding consistency between adjacent faces: two triangles sharing
    # an edge should traverse it in *opposite* directions (the standard
    # outward-consistent-normals convention). Checked per adjacent pair
    # (not just the whole-mesh mesh.is_winding_consistent boolean) so the
    # specific offending faces can be named, not just "somewhere in here".
    inconsistent_faces: set[FaceId] = set()
    for (f0, f1), (a, b) in zip(mesh.face_adjacency, mesh.face_adjacency_edges):
        dir0 = _edge_direction(mesh.faces[f0], a, b)
        dir1 = _edge_direction(mesh.faces[f1], a, b)
        if dir0 is not None and dir0 == dir1:
            inconsistent_faces.add(triangle_face_id[f0])
            inconsistent_faces.add(triangle_face_id[f1])
    if inconsistent_faces:
        problems.append({"faces": sorted(inconsistent_faces), "problem": "inconsistent winding between adjacent faces"})

    return problems


def _edge_direction(triangle, a: int, b: int) -> bool | None:
    """True if `triangle` traverses vertex-index edge (a, b) as a->b,
    False if b->a, None if the triangle doesn't contain this edge at all
    (shouldn't happen for a genuine face_adjacency_edges pair)."""
    for k in range(3):
        if triangle[k] == a and triangle[(k + 1) % 3] == b:
            return True
        if triangle[k] == b and triangle[(k + 1) % 3] == a:
            return False
    return None
