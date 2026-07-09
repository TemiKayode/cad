"""Deterministic house-mesh construction from a :class:`HouseSpec`.

Bedrooms are laid out on a roughly-square grid of ``ROOM_SIZE_M``-metre
cells (scaled uniformly if ``floor_area_sq_m`` is set); each floor gets a
floor slab, a roof/ceiling slab, exterior perimeter walls, and interior
partition walls at every internal grid line, all sharing vertices at
grid points (not just visually adjacent, independently-positioned quads)
so the structure is a genuinely connected mesh. Multiple floors stack
directly on top of each other (or, with ``bedrooms_per_floor``, stack
with each floor's own distinct footprint, corner-aligned at the origin).

This is correct by construction, not by validation: every plain face is
a planar quad built from shared grid vertices, so there's no possibility
of the self-intersecting or degenerate geometry a generative model could
produce. The one topological wrinkle -- interior partition walls meet
the floor/roof slabs at "T-junctions" (a partition's edge runs along
part of one big slab's edge, not edge-for-edge with a matching slab
face), which is not strictly 2-manifold -- doesn't matter for
rendering/collaboration, and is exactly what ``mesh_repair.py`` exists
to clean up before anything gets 3D printed. See the package docstring
for why that repair happens at export time, not here.

Phase G1 enrichment: ``roof_type`` (gable/hip geometry on the top floor,
not just a flat slab), ``garage`` (an attached box), and real CSG-cut
door/window openings (``front_door``/``front_windows``) on the *ground
floor's front (south) wall only* -- not every wall on every floor. That
specific wall is built as one thick, cuttable solid (via the door/window
generator's boolean-difference machinery) instead of the usual flat,
zero-thickness quads, so it doesn't share vertices with the floor/ceiling
grid the way every other wall does; a real thick wall with a real hole
in it can't be paper-thin either. Extending real openings to every wall
on every floor is future work, not silently pretended here.
"""

from __future__ import annotations

import math

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.mesh_builder import MeshBuilder, add_box
from crdt_cad.ai.mesh_types import GeneratedMesh, Position

__all__ = ["GeneratedMesh", "Position", "ROOM_SIZE_M", "build_house_mesh"]

ROOM_SIZE_M = 4.0


def _grid_dims(bedrooms: int) -> tuple[int, int]:
    cols = math.ceil(math.sqrt(bedrooms))
    rows = math.ceil(bedrooms / cols)
    return cols, rows


def _room_size_for_floor(cols: int, rows: int, floor_area_sq_m: float | None) -> float:
    if floor_area_sq_m is None:
        return ROOM_SIZE_M
    base_area = cols * rows * ROOM_SIZE_M**2
    scale = math.sqrt(floor_area_sq_m / base_area)
    return ROOM_SIZE_M * scale


def _front_wall_openings(wall_width: float, wall_height: float, front_door: bool, front_windows: int) -> list[dict]:
    """Slots the wall into `n` equal segments (one per requested opening)
    so openings can never overlap by construction, then sizes each
    opening to a fraction of its own slot. The door (if any) always
    takes the middle slot."""
    n = front_windows + (1 if front_door else 0)
    if n == 0:
        return []
    margin = wall_width * 0.08
    usable = wall_width - 2 * margin
    slot_w = usable / n
    door_slot = n // 2 if front_door else None

    openings = []
    for i in range(n):
        slot_x0 = margin + i * slot_w
        if front_door and i == door_slot:
            width = min(0.9, slot_w * 0.7)
            height = min(2.1, wall_height * 0.9)
            sill = 0.0
        else:
            width = min(1.0, slot_w * 0.6)
            height = min(1.2, wall_height * 0.5)
            sill = min(0.9, wall_height * 0.35)
        offset = slot_x0 + (slot_w - width) / 2
        openings.append({"width": width, "height": height, "sill": sill, "offset": offset})
    return openings


def _add_front_wall_with_openings(
    b: MeshBuilder, width: float, height: float, y0: float, z: float,
    front_door: bool, front_windows: int, material: str,
) -> None:
    from crdt_cad.ai.generators.wall_opening import cut_wall_openings

    thickness = 0.15
    openings = _front_wall_openings(width, height, front_door, front_windows)
    wall_mesh = cut_wall_openings(width, height, thickness, openings, material)
    b.merge_generated(wall_mesh, remap=lambda p: (p[0], p[1] + y0, p[2] + z - thickness / 2))


def _add_gable_roof(b: MeshBuilder, ceil_grid: dict, cols: int, rows: int, room_size: float, y1: float, roof_height: float, material: str) -> None:
    ridge_z = rows * room_size / 2.0
    ridge_west = b.vertex((0.0, y1 + roof_height, ridge_z))
    ridge_east = b.vertex((cols * room_size, y1 + roof_height, ridge_z))
    nw, ne = ceil_grid[(0, 0)], ceil_grid[(0, cols)]
    sw, se = ceil_grid[(rows, 0)], ceil_grid[(rows, cols)]
    b.face([nw, ne, ridge_east, ridge_west], material)  # north slope
    b.face([se, sw, ridge_west, ridge_east], material)  # south slope
    b.face([sw, nw, ridge_west], material)  # west gable end
    b.face([ne, se, ridge_east], material)  # east gable end


def _add_hip_roof(b: MeshBuilder, ceil_grid: dict, cols: int, rows: int, room_size: float, y1: float, roof_height: float, material: str) -> None:
    apex = b.vertex((cols * room_size / 2.0, y1 + roof_height, rows * room_size / 2.0))
    nw, ne = ceil_grid[(0, 0)], ceil_grid[(0, cols)]
    sw, se = ceil_grid[(rows, 0)], ceil_grid[(rows, cols)]
    b.face([nw, ne, apex], material)
    b.face([ne, se, apex], material)
    b.face([se, sw, apex], material)
    b.face([sw, nw, apex], material)


def build_house_mesh(spec: HouseSpec) -> GeneratedMesh:
    b = MeshBuilder()

    per_floor_bedrooms = spec.bedrooms_per_floor or [spec.bedrooms] * spec.floors
    ground_cols, ground_rows = _grid_dims(per_floor_bedrooms[0])
    ground_room_size = _room_size_for_floor(ground_cols, ground_rows, spec.floor_area_sq_m)

    for floor_idx in range(spec.floors):
        y0 = floor_idx * spec.wall_height_m
        y1 = y0 + spec.wall_height_m
        is_top = floor_idx == spec.floors - 1

        cols, rows = _grid_dims(per_floor_bedrooms[floor_idx])
        room_size = ground_room_size if floor_idx == 0 else _room_size_for_floor(cols, rows, spec.floor_area_sq_m)

        floor_grid: dict[tuple[int, int], str] = {}
        ceil_grid: dict[tuple[int, int], str] = {}
        for r in range(rows + 1):
            for c in range(cols + 1):
                x, z = c * room_size, r * room_size
                floor_grid[(r, c)] = b.vertex((x, y0, z))
                ceil_grid[(r, c)] = b.vertex((x, y1, z))

        floor_loop = [floor_grid[(0, 0)], floor_grid[(0, cols)], floor_grid[(rows, cols)], floor_grid[(rows, 0)]]
        b.face(floor_loop, spec.floor_material)

        if is_top and spec.roof_type != "flat":
            roof_height = spec.wall_height_m * 0.6
            if spec.roof_type == "gable":
                _add_gable_roof(b, ceil_grid, cols, rows, room_size, y1, roof_height, spec.roof_material)
            elif spec.roof_type == "hip":
                _add_hip_roof(b, ceil_grid, cols, rows, room_size, y1, roof_height, spec.roof_material)
        else:
            roof_loop = [ceil_grid[(0, 0)], ceil_grid[(0, cols)], ceil_grid[(rows, cols)], ceil_grid[(rows, 0)]]
            b.face(list(reversed(roof_loop)), spec.roof_material if is_top else "concrete")

        perimeter: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for c in range(cols):
            perimeter.append(((0, c), (0, c + 1)))  # north
        for r in range(rows):
            perimeter.append(((r, cols), (r + 1, cols)))  # east
        for c in range(cols, 0, -1):
            perimeter.append(((rows, c), (rows, c - 1)))  # south
        for r in range(rows, 0, -1):
            perimeter.append(((r, 0), (r - 1, 0)))  # west

        front_wall_built = False
        if floor_idx == 0 and (spec.front_door or spec.front_windows > 0):
            _add_front_wall_with_openings(
                b, cols * room_size, spec.wall_height_m, y0, rows * room_size,
                spec.front_door, spec.front_windows, spec.wall_material,
            )
            front_wall_built = True

        for a, edge_b in perimeter:
            is_south_edge = a[0] == rows and edge_b[0] == rows
            if front_wall_built and is_south_edge:
                continue
            quad = [floor_grid[a], floor_grid[edge_b], ceil_grid[edge_b], ceil_grid[a]]
            b.face(quad, spec.wall_material)

        for r in range(rows):
            for c in range(1, cols):
                quad = [floor_grid[(r, c)], floor_grid[(r + 1, c)], ceil_grid[(r + 1, c)], ceil_grid[(r, c)]]
                b.face(quad, "interior_wall")
        for c in range(cols):
            for r in range(1, rows):
                quad = [floor_grid[(r, c)], floor_grid[(r, c + 1)], ceil_grid[(r, c + 1)], ceil_grid[(r, c)]]
                b.face(quad, "interior_wall")

    if spec.garage:
        garage_w, garage_d, garage_h = ground_room_size * 1.2, ground_room_size, spec.wall_height_m * 0.9
        add_box(b, (-garage_w - 0.5, 0.0, 0.0), (garage_w, garage_h, garage_d), spec.wall_material)

    return b.mesh
