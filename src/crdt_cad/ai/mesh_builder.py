"""Shared primitive builders for the generator registry (Phase G1).

Every new generator (table, chair, shelf, stairs, column, arch, fence,
box/cylinder/cone/torus) is an *assembly* of a small number of
independently-watertight solid primitives -- a table is a top box plus
four leg boxes, a chair is a seat plus a back plus four legs, a fence is
a row of posts plus rails. Building each primitive as its own closed,
manifold solid (rather than one shared quad soup) means the assembly as
a whole is watertight by construction: ``trimesh.Trimesh.is_watertight``
checks that every edge is shared by exactly two faces *across the whole
mesh*, which independently-closed, non-overlapping bodies satisfy
automatically -- no boolean union needed for a mesh that's allowed to be
several disjoint solids (a real table's top and legs are not one
continuous surface in reality either).

``MeshBuilder`` wraps the vertex/face id-counter bookkeeping
``procedural_house.py`` open-coded -- every generator needs the same
"mint a fresh id, record the vertex, mint a fresh id, record the face"
pattern, so it's centralized here once instead of copy-pasted fourteen
times.
"""

from __future__ import annotations

import math

from crdt_cad.ai.mesh_types import GeneratedMesh, Position


class MeshBuilder:
    """Accumulates vertices/faces into a :class:`GeneratedMesh` with
    auto-incrementing ids, shared across every primitive helper below."""

    def __init__(self) -> None:
        self.mesh = GeneratedMesh()
        self._v = 0
        self._f = 0

    def vertex(self, pos: Position) -> str:
        self._v += 1
        vid = f"v{self._v}"
        self.mesh.vertices[vid] = pos
        return vid

    def face(self, loop: list[str], material: str = "") -> str:
        self._f += 1
        fid = f"f{self._f}"
        self.mesh.faces[fid] = loop
        if material:
            self.mesh.face_materials[fid] = material
        return fid

    def merge_generated(self, other: GeneratedMesh, remap=lambda p: p) -> list[str]:
        """Adds every vertex/face of another already-built
        :class:`GeneratedMesh` (e.g. the output of the door/window CSG
        cut) as fresh vertices/faces on this builder, preserving each
        face's own material. `remap` transforms each vertex position
        before insertion (typically a translation, positioning the
        merged sub-mesh within the larger one)."""
        vertex_ids = {old: self.vertex(remap(pos)) for old, pos in other.vertices.items()}
        return [
            self.face([vertex_ids[v] for v in loop], other.face_materials.get(fid, ""))
            for fid, loop in other.faces.items()
        ]

    def merge_triangulated(self, tri_mesh, material: str = "", remap=lambda p: p, flip_winding: bool = False) -> list[str]:
        """Adds every vertex/face of an already-triangulated
        ``trimesh.Trimesh`` (e.g. the output of
        ``trimesh.creation.extrude_polygon``) as fresh vertices/faces.
        `remap` transforms each ``(x, y, z)`` vertex position before
        insertion -- callers use it to map the helper's own local axis
        convention onto whichever world axis "height"/"depth" is
        supposed to run along. Set `flip_winding` when `remap` swaps
        exactly two axes (e.g. local Z -> world Y, local Y -> world Z):
        a single-axis-pair swap is an orientation-reversing transform
        (determinant -1), so the correctly-oriented local mesh comes out
        with every normal pointing inward unless each triangle's winding
        is also reversed to compensate -- confirmed empirically, not
        just by this reasoning (see ``add_extruded_polygon``, the one
        caller that needs it: same axis swap as ``add_extruded_profile_xy``,
        which does *not* need flipping, since it only offsets Z rather
        than remapping which local axis maps to which world axis).
        Returns the new face ids."""
        vertex_ids = [self.vertex(remap(tuple(float(c) for c in v))) for v in tri_mesh.vertices]
        return [
            self.face([vertex_ids[i] for i in (reversed(tri) if flip_winding else tri)], material)
            for tri in tri_mesh.faces
        ]


def add_box(
    builder: MeshBuilder,
    origin: Position,
    size: tuple[float, float, float],
    material: str = "",
) -> list[str]:
    """Axis-aligned box, `origin` = the min corner, `size` = (width
    along X, height along Y, depth along Z). Returns the 6 face ids.
    Outward-facing winding (counter-clockwise viewed from outside), same
    convention `procedural_house.py` already uses for walls/floors."""
    x0, y0, z0 = origin
    w, h, d = size
    x1, y1, z1 = x0 + w, y0 + h, z0 + d

    v = {}
    for xi, x in enumerate((x0, x1)):
        for yi, y in enumerate((y0, y1)):
            for zi, z in enumerate((z0, z1)):
                v[(xi, yi, zi)] = builder.vertex((x, y, z))

    faces = []
    # -Y (bottom), +Y (top)
    faces.append(builder.face([v[(0, 0, 0)], v[(1, 0, 0)], v[(1, 0, 1)], v[(0, 0, 1)]], material))
    faces.append(builder.face([v[(0, 1, 0)], v[(0, 1, 1)], v[(1, 1, 1)], v[(1, 1, 0)]], material))
    # -Z (north), +Z (south)
    faces.append(builder.face([v[(0, 0, 0)], v[(0, 1, 0)], v[(1, 1, 0)], v[(1, 0, 0)]], material))
    faces.append(builder.face([v[(0, 0, 1)], v[(1, 0, 1)], v[(1, 1, 1)], v[(0, 1, 1)]], material))
    # -X (west), +X (east)
    faces.append(builder.face([v[(0, 0, 0)], v[(0, 0, 1)], v[(0, 1, 1)], v[(0, 1, 0)]], material))
    faces.append(builder.face([v[(1, 0, 0)], v[(1, 1, 0)], v[(1, 1, 1)], v[(1, 0, 1)]], material))
    return faces


def add_cylinder(
    builder: MeshBuilder,
    center_base: Position,
    radius: float,
    height: float,
    material: str = "",
    segments: int = 16,
) -> list[str]:
    """Closed vertical cylinder (Y-axis), `center_base` = the center of
    the bottom disc. `segments` is bounded by callers (see the per-spec
    field constraints below) -- unbounded here would let a hostile spec
    request an arbitrarily large mesh."""
    cx, cy, cz = center_base
    bottom_ring = []
    top_ring = []
    for i in range(segments):
        theta = 2 * math.pi * i / segments
        x, z = cx + radius * math.cos(theta), cz + radius * math.sin(theta)
        bottom_ring.append(builder.vertex((x, cy, z)))
        top_ring.append(builder.vertex((x, cy + height, z)))

    bottom_center = builder.vertex((cx, cy, cz))
    top_center = builder.vertex((cx, cy + height, cz))

    faces = []
    for i in range(segments):
        j = (i + 1) % segments
        faces.append(builder.face([bottom_center, bottom_ring[i], bottom_ring[j]], material))
        faces.append(builder.face([top_center, top_ring[j], top_ring[i]], material))
        faces.append(builder.face([bottom_ring[i], top_ring[i], top_ring[j], bottom_ring[j]], material))
    return faces


def add_cone(
    builder: MeshBuilder,
    center_base: Position,
    radius: float,
    height: float,
    material: str = "",
    segments: int = 16,
) -> list[str]:
    cx, cy, cz = center_base
    bottom_ring = []
    for i in range(segments):
        theta = 2 * math.pi * i / segments
        x, z = cx + radius * math.cos(theta), cz + radius * math.sin(theta)
        bottom_ring.append(builder.vertex((x, cy, z)))

    bottom_center = builder.vertex((cx, cy, cz))
    apex = builder.vertex((cx, cy + height, cz))

    faces = []
    for i in range(segments):
        j = (i + 1) % segments
        faces.append(builder.face([bottom_center, bottom_ring[i], bottom_ring[j]], material))
        faces.append(builder.face([bottom_ring[j], bottom_ring[i], apex], material))
    return faces


def add_torus(
    builder: MeshBuilder,
    center: Position,
    major_radius: float,
    minor_radius: float,
    material: str = "",
    major_segments: int = 16,
    minor_segments: int = 8,
) -> list[str]:
    """Torus in the X/Z plane (the "donut" lies flat, hole axis is Y),
    `center` = the torus's own center point."""
    cx, cy, cz = center
    ring: dict[tuple[int, int], str] = {}
    for i in range(major_segments):
        phi = 2 * math.pi * i / major_segments
        for j in range(minor_segments):
            theta = 2 * math.pi * j / minor_segments
            r = major_radius + minor_radius * math.cos(theta)
            x = cx + r * math.cos(phi)
            y = cy + minor_radius * math.sin(theta)
            z = cz + r * math.sin(phi)
            ring[(i, j)] = builder.vertex((x, y, z))

    faces = []
    for i in range(major_segments):
        i2 = (i + 1) % major_segments
        for j in range(minor_segments):
            j2 = (j + 1) % minor_segments
            faces.append(builder.face([ring[(i, j)], ring[(i, j2)], ring[(i2, j2)], ring[(i2, j)]], material))
    return faces


def add_extruded_polygon(
    builder: MeshBuilder,
    points_xz: list[tuple[float, float]],
    y0: float,
    height: float,
    material: str = "",
) -> list[str]:
    """Extrudes a closed polygon in the X/Z plane upward along Y by
    `height`. Used for any custom building footprint. Delegates the
    actual triangulation to ``trimesh.creation.extrude_polygon`` (via
    ``shapely``) rather than a hand-rolled fan triangulation -- a fan
    from one vertex is only exact for a *convex* polygon; an L-shaped or
    otherwise non-convex footprint would silently produce degenerate or
    inverted triangles with the naive approach (this is exactly the bug
    the arch generator's non-convex annulus-segment profile surfaced,
    see :func:`add_extruded_profile_xy`'s docstring)."""
    tri = _extrude_polygon_2d(points_xz, height)
    # trimesh's extrude_polygon extrudes its local (x, y) along local z;
    # this function's contract is footprint in world X/Z, height along
    # world Y, so local (x, y, z) -> world (x, y0 + z, y) -- swapping
    # which local axis (y vs z) maps to which world axis, an
    # orientation-reversing transform, hence flip_winding=True (see
    # MeshBuilder.merge_triangulated's docstring; verified empirically,
    # not just derived -- confirmed both a convex square and a
    # non-convex L-shape recover the correct positive volume with this
    # flag set, and fail without it).
    return builder.merge_triangulated(
        tri, material, remap=lambda p: (p[0], y0 + p[2], p[1]), flip_winding=True,
    )


def add_extruded_profile_xy(
    builder: MeshBuilder,
    points_xy: list[tuple[float, float]],
    z0: float,
    depth: float,
    material: str = "",
) -> list[str]:
    """Extrudes a closed polygon in the X/Y (vertical) plane along +Z by
    `depth` -- the same idea as :func:`add_extruded_polygon` in the
    other plane, used for a silhouette-shaped solid like an arch, where
    the *profile* is a vertical cross-section, not a footprint. Also
    delegates triangulation to ``trimesh``/``shapely`` -- see
    :func:`add_extruded_polygon`'s docstring for why a hand-rolled fan
    triangulation isn't safe for a non-convex profile (an arch's
    annulus-segment silhouette is a textbook non-convex shape)."""
    tri = _extrude_polygon_2d(points_xy, depth)
    # local (x, y, z) -> world (x, y, z0 + z): this function's contract
    # already matches trimesh's own local axes (profile in X/Y, extrude
    # along Z), just offset by z0.
    return builder.merge_triangulated(tri, material, remap=lambda p: (p[0], p[1], z0 + p[2]))


def _extrude_polygon_2d(points_2d: list[tuple[float, float]], height: float):
    """Shared helper: builds a real ``trimesh.Trimesh`` for a closed 2D
    polygon extruded along its local Z by `height`, via
    ``trimesh.creation.extrude_polygon`` -- correct for convex *and*
    non-convex simple polygons, unlike a hand-rolled vertex-0 fan."""
    import trimesh
    from shapely.geometry import Polygon

    return trimesh.creation.extrude_polygon(Polygon(points_2d), height=height)


def to_trimesh(mesh: GeneratedMesh):
    """Converts to a real ``trimesh.Trimesh`` (triangulated by a simple
    fan -- every face this module produces is already convex/planar, so
    a fan triangulation is exact, not an approximation) -- the format
    the CSG boolean engine (door/window openings) and pre-commit
    validation both need."""
    import numpy as np
    import trimesh

    vertex_ids = list(mesh.vertices.keys())
    index = {vid: i for i, vid in enumerate(vertex_ids)}
    vertices = np.array([mesh.vertices[vid] for vid in vertex_ids], dtype=np.float64)

    triangles = []
    for loop in mesh.faces.values():
        idxs = [index[v] for v in loop if v in index]
        for i in range(1, len(idxs) - 1):
            triangles.append((idxs[0], idxs[i], idxs[i + 1]))

    return trimesh.Trimesh(vertices=vertices, faces=np.array(triangles, dtype=np.int64), process=False)


def from_trimesh(tri_mesh, material: str = "") -> GeneratedMesh:
    """Inverse of :func:`to_trimesh` -- rebuilds a plain
    :class:`GeneratedMesh` (triangular faces, one material for the whole
    result) from a ``trimesh.Trimesh``, e.g. the output of a CSG boolean
    operation. Vertex/face ids are freshly minted; there's no meaningful
    correspondence to preserve across a boolean op (it can add, remove,
    and re-triangulate vertices the inputs never had)."""
    b = MeshBuilder()
    vertex_ids = [b.vertex(tuple(float(c) for c in v)) for v in tri_mesh.vertices]
    for tri in tri_mesh.faces:
        b.face([vertex_ids[i] for i in tri], material)
    return b.mesh
