"""2D modify-tool geometry (Part 7, Phase C1) that genuinely needs a
robust numerical library rather than being hand-rolled in the browser.

Most of C1 (mirror, linear/circular array) is plain affine math the
client already does entirely itself, via the same per-path
``{tx,ty,rotation,scale}`` transform duplicate/move/rotate use -- see
``demo/static/sketch.js``. Offsetting a path is different: correct
polygon offsetting has to handle self-intersection at concave corners,
which a hand-rolled "shift every edge outward" approach gets wrong in
exactly the cases that matter. ``shapely`` (already a core dependency
for the door/window/arch generators) solves this properly, so this one
piece of C1 is computed server-side and shipped back to the client as
plain points -- mirroring how the constraint solver (``constraints.py``)
is the one piece of the sketch tool that's server-side for the same
"needs a real numerical library" reason.
"""

from __future__ import annotations

from shapely.geometry import LineString, Polygon


class OffsetError(ValueError):
    pass


def offset_path(points: list[tuple[float, float]], distance: float, closed: bool) -> list[tuple[float, float]]:
    """Returns the offset path's points. A closed path (``closed=True``,
    a filled shape or polygon) offsets as a polygon boundary -- positive
    ``distance`` grows it outward, negative shrinks it inward. An open
    path offsets as a parallel curve -- positive is a left-hand offset,
    negative right-hand, relative to the path's own point order.

    Raises :class:`OffsetError` for degenerate input (fewer than 2
    points, or an inward offset that collapses the polygon entirely --
    e.g. offsetting a 10-unit-wide shape inward by 20 units has no
    sensible result, not just a very small one)."""
    if len(points) < 2:
        raise OffsetError("a path needs at least 2 points to offset")
    if closed:
        if len(points) < 3:
            raise OffsetError("a closed path needs at least 3 points to offset")
        poly = Polygon(points)
        if not poly.is_valid or poly.is_empty:
            raise OffsetError("this path's points don't form a simple (non-self-intersecting) polygon")
        result = poly.buffer(distance, join_style="mitre")
        if result.is_empty:
            raise OffsetError(f"offsetting by {distance} collapses this shape entirely -- try a smaller distance")
        # A buffer() on a simple polygon returns a simple Polygon; treat
        # anything else (a MultiPolygon from a self-intersecting result,
        # or a polygon that split into disjoint pieces) as the same
        # "collapsed" case above -- there's no single path to hand back.
        if result.geom_type != "Polygon" or list(result.interiors):
            raise OffsetError(f"offsetting by {distance} splits this shape into multiple pieces -- try a smaller distance")
        return list(result.exterior.coords)[:-1]  # shapely closes the ring; the CRDT path shouldn't repeat the first point
    line = LineString(points)
    result = line.offset_curve(distance)
    if result.is_empty:
        raise OffsetError(f"offsetting by {distance} collapses this path entirely -- try a smaller distance")
    return list(result.coords)
