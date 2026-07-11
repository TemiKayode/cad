"""3D mesh boolean operations (Part 7 C6) -- union/subtract/intersect
via `trimesh`'s boolean API, backed by `manifold3d` (already a core
dependency of this project; the same library the AI generation
pipeline's door/window CSG-cut generators already use, see
``crdt_cad.ai.generators.wall_opening``). Operates on the mesh's
current flat vertex/face-loop representation -- see
``docs/brep_design.md`` for why a real topological B-Rep (which would
make a boolean's *inputs* structurally richer, e.g. exact edge
adjacency) wasn't built for this, and why this scope (compute a real,
correct boolean result off two triangle meshes, mint it as fresh
CRDT ops) is what's tractable without that larger rewrite.

Each operand is converted to a `trimesh.Trimesh` via
``mesh_interop.triangulated_trimesh`` (the same conversion glTF/3MF
export already uses), the boolean computed, and the result converted
back into a plain vertex/face dict via a fresh, sequential id scheme --
`trimesh`'s own boolean output is already a clean, welded mesh (unlike
OpenCascade's per-face-duplicated tessellation STEP import has to weld,
see ``step_export.mesh_from_step_bytes``), so no deduplication pass is
needed here.

**Orientation matters and isn't guaranteed by the caller** -- confirmed
live, not assumed: a real Box placed via the 3D demo's own primitive
tool triangulates to a *negative*-volume ("inside-out", inward-facing
normals) mesh, which `is_watertight` alone doesn't catch (that check is
about edge-manifoldness, not consistent outward orientation) but which
`manifold3d` flatly rejects with "Not all meshes are volumes!". Each
operand is normalized to positive volume (`Trimesh.invert()`, which
flips every face's winding and normal) before the boolean runs, rather
than assuming the caller's geometry is already correctly oriented.
"""

from __future__ import annotations

from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.export.mesh_interop import Position, triangulated_trimesh

BOOLEAN_OPS = ("union", "subtract", "intersect")


def _trimesh_to_generated_mesh(tri) -> GeneratedMesh:
    vertices = {f"v{i}": tuple(float(c) for c in v) for i, v in enumerate(tri.vertices)}
    faces = {f"f{i}": [f"v{a}", f"v{b}", f"v{c}"] for i, (a, b, c) in enumerate(tri.faces)}
    return GeneratedMesh(vertices=vertices, faces=faces)


def compute_mesh_boolean(
    op: str,
    a_positions: dict[str, Position],
    a_faces: dict[str, list[str]],
    b_positions: dict[str, Position],
    b_faces: dict[str, list[str]],
) -> GeneratedMesh:
    """Returns the boolean result as a `GeneratedMesh` with fresh,
    sequential vertex/face ids -- the caller (the REST endpoint) is
    responsible for remapping those to globally-unique ids before
    minting ops, the same "never reuse a caller/computation-local id"
    rule `mesh_from_step_bytes`'s own caller already follows. Raises
    `ValueError` (never a silent empty result) if either operand has no
    triangulable geometry, or if the computed result is empty -- an
    empty result is overwhelmingly a sign the operands don't overlap
    the way the user expected (e.g. `subtract` with no intersection is
    a no-op on `a`, not an empty mesh -- `trimesh`/`manifold3d` handle
    that correctly and return `a` unchanged; a *genuinely* empty result
    only happens for `intersect` on non-overlapping operands, which is
    worth surfacing as an error rather than silently creating a
    now-empty "object")."""
    if op not in BOOLEAN_OPS:
        raise ValueError(f"unknown boolean op {op!r} -- expected one of {BOOLEAN_OPS}")
    mesh_a = triangulated_trimesh(a_positions, a_faces)
    mesh_b = triangulated_trimesh(b_positions, b_faces)
    if mesh_a is None or mesh_b is None:
        raise ValueError("both operands need at least one face with 3 or more live vertices")
    # See the module docstring's orientation note -- a mesh whose faces
    # wind inward (negative volume) is otherwise a valid, watertight
    # solid; manifold3d just refuses to treat it as one.
    if mesh_a.volume < 0:
        mesh_a.invert()
    if mesh_b.volume < 0:
        mesh_b.invert()

    if op == "union":
        result = mesh_a.union(mesh_b)
    elif op == "subtract":
        result = mesh_a.difference(mesh_b)
    else:
        result = mesh_a.intersection(mesh_b)

    if result is None or len(result.faces) == 0:
        raise ValueError(f"{op} produced an empty mesh -- the operands may not overlap as expected")
    return _trimesh_to_generated_mesh(result)
