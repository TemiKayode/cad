"""Deterministic scene layout solver (Phase G2). Takes each object's own
already-built, un-positioned mesh (see ``scene.expand_scene``) and a
relation ("around"/"on_top_of"/"row"/"beside"/"none") to an earlier
object, and computes a world-space translation for each -- ground-plane
snapping, non-overlapping placement, and "on" relationships stacking
correctly at the target's actual measured height, not a guess.

This is the one piece of scene composition explicitly *not* delegated
to the LLM: "the solver, not the LLM, owns final coordinates" -- every
function here is a pure geometric computation over already-known
bounding boxes, with no model call anywhere in this module.
"""

from __future__ import annotations

import math

from crdt_cad.ai.mesh_types import GeneratedMesh, Position

AABB = tuple[Position, Position]  # (min corner, max corner)

_MARGIN_M = 0.3


def _local_aabb(mesh: GeneratedMesh) -> AABB:
    xs = [p[0] for p in mesh.vertices.values()]
    ys = [p[1] for p in mesh.vertices.values()]
    zs = [p[2] for p in mesh.vertices.values()]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _translate_aabb(aabb: AABB, dx: float, dy: float, dz: float) -> AABB:
    (x0, y0, z0), (x1, y1, z1) = aabb
    return (x0 + dx, y0 + dy, z0 + dz), (x1 + dx, y1 + dy, z1 + dz)


def _center_xz(aabb: AABB) -> tuple[float, float]:
    (x0, _, z0), (x1, _, z1) = aabb
    return (x0 + x1) / 2, (z0 + z1) / 2


def _xz_overlap(a: AABB, b: AABB) -> bool:
    (ax0, _, az0), (ax1, _, az1) = a
    (bx0, _, bz0), (bx1, _, bz1) = b
    return ax0 < bx1 and bx0 < ax1 and az0 < bz1 and bz0 < az1


def solve_layout(objects) -> list[Position]:  # objects: list[scene.ExpandedObject]
    """Returns one ``(dx, dy, dz)`` translation per object in `objects`,
    same order and length. Objects are processed once, in order (every
    `target_index` is guaranteed to reference an already-processed
    object -- see `scene.SceneSpec`'s own validation), grouping
    "around"/"row" relations (which distribute `count` copies together)
    at the first copy of each group.
    """
    translations: list[Position] = []
    placed_aabbs: list[AABB] = []
    next_free_x = 0.0

    i = 0
    n = len(objects)
    while i < n:
        obj = objects[i]

        if obj.relation == "around" and obj.copy_index == 0:
            group = objects[i:i + obj.copy_count]
            target_aabb = placed_aabbs[obj.target_index]
            cx, cz = _center_xz(target_aabb)
            (tx0, _, tz0), (tx1, _, tz1) = target_aabb
            target_radius = max(tx1 - tx0, tz1 - tz0) / 2
            for k, member in enumerate(group):
                local = _local_aabb(member.mesh)
                (lx0, ly0, lz0), (lx1, _, lz1) = local
                obj_radius = max(lx1 - lx0, lz1 - lz0) / 2
                radius = target_radius + obj_radius + _MARGIN_M
                theta = 2 * math.pi * k / obj.copy_count
                world_x = cx + radius * math.cos(theta)
                world_z = cz + radius * math.sin(theta)
                dx = world_x - (lx0 + lx1) / 2
                dz = world_z - (lz0 + lz1) / 2
                dy = -ly0  # ground-plane snap
                translations.append((dx, dy, dz))
                placed_aabbs.append(_translate_aabb(local, dx, dy, dz))
            next_free_x = max(next_free_x, cx + target_radius + 2 * _MARGIN_M)
            i += obj.copy_count
            continue

        if obj.relation == "row" and obj.copy_index == 0:
            group = objects[i:i + obj.copy_count]
            cursor_x = next_free_x
            for member in group:
                local = _local_aabb(member.mesh)
                (lx0, ly0, lz0), (lx1, _, lz1) = local
                dx = cursor_x - lx0
                dy = -ly0
                dz = -(lz0 + lz1) / 2  # centered on Z=0
                translations.append((dx, dy, dz))
                placed_aabbs.append(_translate_aabb(local, dx, dy, dz))
                cursor_x += (lx1 - lx0) + member.spacing_m
            next_free_x = cursor_x + _MARGIN_M
            i += obj.copy_count
            continue

        if obj.relation == "on_top_of":
            target_aabb = placed_aabbs[obj.target_index]
            local = _local_aabb(obj.mesh)
            (lx0, ly0, lz0), (lx1, _, lz1) = local
            tcx, tcz = _center_xz(target_aabb)
            dx = tcx - (lx0 + lx1) / 2
            dz = tcz - (lz0 + lz1) / 2
            dy = target_aabb[1][1] - ly0  # sits on the target's measured top, not a guess
            translations.append((dx, dy, dz))
            placed_aabbs.append(_translate_aabb(local, dx, dy, dz))
            i += 1
            continue

        if obj.relation == "beside":
            target_aabb = placed_aabbs[obj.target_index]
            local = _local_aabb(obj.mesh)
            (lx0, ly0, lz0), (lx1, _, lz1) = local
            dx = target_aabb[1][0] + _MARGIN_M - lx0
            dz = _center_xz(target_aabb)[1] - (lz0 + lz1) / 2
            dy = -ly0
            translations.append((dx, dy, dz))
            placed_aabbs.append(_translate_aabb(local, dx, dy, dz))
            i += 1
            continue

        # relation == "none": ground-snapped, placed at the shared
        # left-to-right cursor so independent objects never overlap.
        local = _local_aabb(obj.mesh)
        (lx0, ly0, lz0), (lx1, _, lz1) = local
        dx = next_free_x - lx0
        dy = -ly0
        dz = -(lz0 + lz1) / 2
        translations.append((dx, dy, dz))
        placed_aabbs.append(_translate_aabb(local, dx, dy, dz))
        next_free_x += (lx1 - lx0) + _MARGIN_M
        i += 1

    _resolve_cross_group_overlaps(objects, translations, placed_aabbs)
    return translations


def _resolve_cross_group_overlaps(objects, translations: list[Position], placed_aabbs: list[AABB]) -> None:
    """The per-relation placement rules above already avoid overlap
    *within* one group (a row's spacing, a circle's radius, a shared
    left-to-right cursor) -- this is a final safety net for objects
    from *different* groups that could still end up overlapping in the
    XZ plane (e.g. an "around" circle swinging into a later "none"
    object's path). Bounded iteration count: this is a cheap heuristic
    nudge, not a physics solver -- if it hasn't converged in a handful
    of passes, leave it be rather than loop indefinitely."""
    n = len(objects)
    for _ in range(6):
        moved = False
        for a in range(n):
            for b in range(a + 1, n):
                if objects[a].target_index == b or objects[b].target_index == a:
                    continue  # an intentional relation (e.g. "on_top_of") -- not a bug to fix
                if _xz_overlap(placed_aabbs[a], placed_aabbs[b]):
                    push = (placed_aabbs[a][1][0] - placed_aabbs[b][0][0]) + _MARGIN_M
                    dx, dy, dz = translations[b]
                    translations[b] = (dx + push, dy, dz)
                    placed_aabbs[b] = _translate_aabb(placed_aabbs[b], push, 0.0, 0.0)
                    moved = True
        if not moved:
            break
