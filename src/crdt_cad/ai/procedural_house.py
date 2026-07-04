"""Deterministic house-mesh construction from a :class:`HouseSpec`.

Bedrooms are laid out on a roughly-square grid of ``ROOM_SIZE_M``-metre
cells; each floor gets a floor slab, a roof/ceiling slab, exterior
perimeter walls, and interior partition walls at every internal grid
line, all sharing vertices at grid points (not just visually adjacent,
independently-positioned quads) so the structure is a genuinely
connected mesh. Multiple floors stack directly on top of each other.

This is correct by construction, not by validation: every face is a
planar quad built from shared grid vertices, so there's no possibility
of the self-intersecting or degenerate geometry a generative model
could produce. The one topological wrinkle -- interior partition walls
meet the floor/roof slabs at "T-junctions" (a partition's edge runs
along part of one big slab's edge, not edge-for-edge with a matching
slab face), which is not strictly 2-manifold -- doesn't matter for
rendering/collaboration, and is exactly what ``mesh_repair.py`` exists
to clean up before anything gets 3D printed. See the package docstring
for why that repair happens at export time, not here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from crdt_cad.ai.house_spec import HouseSpec

Position = tuple[float, float, float]

ROOM_SIZE_M = 4.0


@dataclass
class GeneratedMesh:
    vertices: dict[str, Position] = field(default_factory=dict)
    faces: dict[str, list[str]] = field(default_factory=dict)
    face_materials: dict[str, str] = field(default_factory=dict)

    def triangle_count(self) -> int:
        return sum(max(len(loop) - 2, 0) for loop in self.faces.values())


def build_house_mesh(spec: HouseSpec) -> GeneratedMesh:
    mesh = GeneratedMesh()
    counters = {"v": 0, "f": 0}

    def new_vertex(pos: Position) -> str:
        counters["v"] += 1
        vid = f"v{counters['v']}"
        mesh.vertices[vid] = pos
        return vid

    def new_face(loop: list[str], material: str) -> str:
        counters["f"] += 1
        fid = f"f{counters['f']}"
        mesh.faces[fid] = loop
        mesh.face_materials[fid] = material
        return fid

    cols = math.ceil(math.sqrt(spec.bedrooms))
    rows = math.ceil(spec.bedrooms / cols)

    for floor_idx in range(spec.floors):
        y0 = floor_idx * spec.wall_height_m
        y1 = y0 + spec.wall_height_m

        floor_grid: dict[tuple[int, int], str] = {}
        ceil_grid: dict[tuple[int, int], str] = {}
        for r in range(rows + 1):
            for c in range(cols + 1):
                x, z = c * ROOM_SIZE_M, r * ROOM_SIZE_M
                floor_grid[(r, c)] = new_vertex((x, y0, z))
                ceil_grid[(r, c)] = new_vertex((x, y1, z))

        floor_loop = [floor_grid[(0, 0)], floor_grid[(0, cols)], floor_grid[(rows, cols)], floor_grid[(rows, 0)]]
        new_face(floor_loop, spec.floor_material)

        roof_loop = [ceil_grid[(0, 0)], ceil_grid[(0, cols)], ceil_grid[(rows, cols)], ceil_grid[(rows, 0)]]
        new_face(list(reversed(roof_loop)), "roof" if floor_idx == spec.floors - 1 else "concrete")

        perimeter: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for c in range(cols):
            perimeter.append(((0, c), (0, c + 1)))  # north
        for r in range(rows):
            perimeter.append(((r, cols), (r + 1, cols)))  # east
        for c in range(cols, 0, -1):
            perimeter.append(((rows, c), (rows, c - 1)))  # south
        for r in range(rows, 0, -1):
            perimeter.append(((r, 0), (r - 1, 0)))  # west
        for a, b in perimeter:
            quad = [floor_grid[a], floor_grid[b], ceil_grid[b], ceil_grid[a]]
            new_face(quad, "exterior_wall")

        for r in range(rows):
            for c in range(1, cols):
                quad = [floor_grid[(r, c)], floor_grid[(r + 1, c)], ceil_grid[(r + 1, c)], ceil_grid[(r, c)]]
                new_face(quad, "interior_wall")
        for c in range(cols):
            for r in range(1, rows):
                quad = [floor_grid[(r, c)], floor_grid[(r, c + 1)], ceil_grid[(r, c + 1)], ceil_grid[(r, c)]]
                new_face(quad, "interior_wall")

    return mesh
