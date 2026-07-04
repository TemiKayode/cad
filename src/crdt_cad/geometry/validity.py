"""Geometry validity checks: reject degenerate/self-intersecting input
*before* it is turned into CRDT ops.

Deliberately pure, allocation-light, dependency-free geometry -- these
run on every point the server accepts from a client, so they need to be
cheap and have no surprising failure modes.
"""

from __future__ import annotations

Point = tuple[float, float]

ZERO_LENGTH_EPS = 1e-9


class GeometryError(ValueError):
    """Raised when an edit would produce invalid geometry."""


def _orientation(p: Point, q: Point, r: Point) -> int:
    val = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
    if abs(val) < 1e-12:
        return 0
    return 1 if val > 0 else 2


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    """True if q lies on segment p-r, given p, q, r are already collinear."""
    return (
        min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
        and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
    )


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Standard orientation-based segment intersection test (handles the
    collinear-overlap edge cases via the on-segment checks)."""
    o1 = _orientation(p1, p2, p3)
    o2 = _orientation(p1, p2, p4)
    o3 = _orientation(p3, p4, p1)
    o4 = _orientation(p3, p4, p2)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p1, p3, p2):
        return True
    if o2 == 0 and _on_segment(p1, p4, p2):
        return True
    if o3 == 0 and _on_segment(p3, p1, p4):
        return True
    if o4 == 0 and _on_segment(p3, p2, p4):
        return True
    return False


def path_is_self_intersecting(points: list[Point], *, is_closed: bool = False) -> bool:
    """Checks every pair of non-adjacent segments in a polyline.

    Adjacent segments sharing an endpoint are never flagged (that's just
    the path continuing normally). When ``is_closed``, segments wrap
    around (the last vertex connects back to the first), and that
    wrap-around pair of segments is *also* treated as adjacent -- they
    legitimately share the start vertex, same as any other consecutive
    pair, not a real self-intersection.
    """
    n = len(points)
    if is_closed:
        if n < 3:
            return False
        segments = [(points[i], points[(i + 1) % n]) for i in range(n)]
    else:
        if n < 4:
            return False
        segments = [(points[i], points[i + 1]) for i in range(n - 1)]

    num_segments = len(segments)
    for i in range(num_segments):
        for j in range(i + 1, num_segments):
            if j == i + 1:
                continue  # numerically-adjacent segments share an endpoint
            if is_closed and i == 0 and j == num_segments - 1:
                continue  # cyclically-adjacent wrap-around pair
            a1, a2 = segments[i]
            b1, b2 = segments[j]
            if segments_intersect(a1, a2, b1, b2):
                return True
    return False


def validate_new_point(
    existing_points: list[Point],
    candidate: Point,
    *,
    check_self_intersection: bool = False,
) -> None:
    """Raises :class:`GeometryError` if appending ``candidate`` to
    ``existing_points`` would produce degenerate or (optionally) a
    self-intersecting path.

    ``check_self_intersection`` defaults to off because it would be
    overly aggressive for freehand pen strokes (crossing your own
    doodle is normal); precision profile/polygon tools opt in.
    """
    if existing_points:
        last = existing_points[-1]
        dx, dy = candidate[0] - last[0], candidate[1] - last[1]
        if dx * dx + dy * dy < ZERO_LENGTH_EPS:
            raise GeometryError("zero-length segment")

    if check_self_intersection:
        candidate_path = [*existing_points, candidate]
        if path_is_self_intersecting(candidate_path):
            raise GeometryError("self-intersecting path")


def validate_closed_polygon(points: list[Point]) -> None:
    """Validates a polygon loop (as used by the strict Polygon tool):
    no zero-length edges, not self-intersecting, at least 3 vertices."""
    if len(points) < 3:
        raise GeometryError("a polygon needs at least 3 vertices")
    for i in range(len(points)):
        a, b = points[i], points[(i + 1) % len(points)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if dx * dx + dy * dy < ZERO_LENGTH_EPS:
            raise GeometryError("zero-length edge")
    if path_is_self_intersecting(points, is_closed=True):
        raise GeometryError("self-intersecting polygon")
