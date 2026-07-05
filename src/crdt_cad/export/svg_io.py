"""SVG export/import for 2D sketch paths.

Import handles the common "reference geometry" case: straight-segment
entities (``<line>``, ``<polyline>``, ``<polygon>``) and ``<path>``
using ``M``/``L`` (absolute or relative) plus, as of Phase 8, cubic and
quadratic Beziers (``C``/``S``/``Q``/``T``, absolute or relative,
including the ``S``/``T`` "smooth" variants' reflected control point).

Elliptical arcs (``A``/``a``) are still not parsed. Unlike an unhandled
straight command, an arc's flag arguments (large-arc-flag, sweep-flag)
are single 0/1 digits that can appear with no separating whitespace or
comma in real-world SVGs (e.g. ``A25,25 0 01 50,50``) -- a tokenizer
that didn't know to stop there would silently misinterpret those flags
as coordinates and corrupt every point after them, which is worse than
today's honest partial import. So encountering ``A``/``a`` stops parsing
that ``<path>`` immediately and returns whatever points/curves were
accumulated before it, the same "truncate rather than guess" choice the
original M/L-only importer already made for anything else unhandled.
"""

from __future__ import annotations

import math
import re
from xml.etree import ElementTree as ET

from crdt_cad.crdt.document import curve_prop_key, px_per_unit

Point = tuple[float, float]

# DXF-style unit suffixes SVG understands on <svg width="..."/height="...">
# (viewBox itself stays unitless, per SVG convention -- these just tell a
# viewer/consumer what the numbers physically mean).
_SVG_UNIT_SUFFIX = {"px": "", "mm": "mm", "in": "in"}


def _shape_bounds(shape: dict) -> tuple[float, float, float, float] | None:
    """Returns (min_x, min_y, max_x, max_y) for a shape primitive
    (Phase 11) or a text object (Phase 15), or None if `shape` isn't a
    recognized kind."""
    kind = shape.get("shape")
    if kind == "line":
        xs = [shape["x1"], shape["x2"]]
        ys = [shape["y1"], shape["y2"]]
    elif kind == "rect":
        xs = [shape["x"], shape["x"] + shape["w"]]
        ys = [shape["y"], shape["y"] + shape["h"]]
    elif kind == "circle":
        xs = [shape["cx"] - shape["r"], shape["cx"] + shape["r"]]
        ys = [shape["cy"] - shape["r"], shape["cy"] + shape["r"]]
    elif kind == "ellipse":
        xs = [shape["cx"] - shape["rx"], shape["cx"] + shape["rx"]]
        ys = [shape["cy"] - shape["ry"], shape["cy"] + shape["ry"]]
    elif kind == "arc":
        # Conservative bound: the full circle the arc is cut from --
        # tighter bounding-box-of-the-actual-sweep isn't worth the extra
        # trig for a viewBox padding calculation.
        xs = [shape["cx"] - shape["r"], shape["cx"] + shape["r"]]
        ys = [shape["cy"] - shape["r"], shape["cy"] + shape["r"]]
    elif kind == "text":
        # (x, y) is the top-left corner (canvas textBaseline="top" /
        # SVG dominant-baseline="hanging", kept consistent between the
        # two renderers -- see _shape_svg_element). No real font metrics
        # available here, so width is a rough per-character estimate --
        # good enough for a viewBox padding calculation, not meant to be
        # exact.
        font_size = shape.get("font_size", 16)
        content = shape.get("content", "")
        xs = [shape["x"], shape["x"] + len(content) * font_size * 0.6]
        ys = [shape["y"], shape["y"] + font_size * 1.2]
    else:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _fill_attrs(props: dict) -> str:
    """Shared by shapes and freehand/polygon paths (Phase 15): `fill` +
    `fill_opacity` are ordinary path_props fields, so both kinds of
    geometry support them identically. Absent/`"none"` renders exactly
    as every pre-Phase-15 export already did -- an unfilled outline."""
    fill = props.get("fill")
    if not fill or fill == "none":
        return 'fill="none"'
    opacity = props.get("fill_opacity", 1.0)
    return f'fill="{fill}" fill-opacity="{opacity}"'


def _dash_attr(props: dict, scale: float) -> str:
    """`dash` (Phase 15: "solid" | "dashed" | "dotted") as an SVG
    `stroke-dasharray`, sized off the path's own stroke width so it
    looks proportionate at any thickness. "dotted" relies on a round
    linecap to actually render as circular dots (a dash length near
    zero with a round cap) rather than nothing -- both call sites below
    already set `stroke-linecap="round"` unconditionally, so this
    doesn't need to repeat it."""
    dash = props.get("dash", "solid")
    sw = props.get("stroke_width", 2.5) * scale
    if dash == "dashed":
        return f'stroke-dasharray="{sw * 4:.3f},{sw * 3:.3f}"'
    if dash == "dotted":
        return f'stroke-dasharray="0.01,{sw * 2.5:.3f}"'
    return ""


def _shape_svg_element(shape: dict, scale: float) -> str | None:
    """One native SVG element per shape kind (Phase 11) -- `<line>`,
    `<rect>`, `<circle>`, `<ellipse>`, or an elliptical-arc `<path>` --
    rather than always flattening to a polyline, so the exported file is
    a faithful, editable shape in any real vector tool. Also handles a
    text object (Phase 15) as a native `<text>` element. Returns None if
    `shape` isn't a recognized kind (the caller falls back to the
    point-list `<path>` every freehand/polygon path already uses)."""
    kind = shape.get("shape")
    color = shape.get("color", "#111111")
    stroke_width = shape.get("stroke_width", 2.5) * scale
    fill = _fill_attrs(shape)
    dash = _dash_attr(shape, scale)
    common = f'stroke="{color}" stroke-width="{stroke_width:.3f}" {fill} {dash} stroke-linecap="round" stroke-linejoin="round"'
    if kind == "line":
        x1, y1, x2, y2 = (shape["x1"] * scale, shape["y1"] * scale, shape["x2"] * scale, shape["y2"] * scale)
        return f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" {common} />'
    if kind == "rect":
        x, y, w, h = (shape["x"] * scale, shape["y"] * scale, shape["w"] * scale, shape["h"] * scale)
        return f'<rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" {common} />'
    if kind == "circle":
        cx, cy, r = (shape["cx"] * scale, shape["cy"] * scale, shape["r"] * scale)
        return f'<circle cx="{cx:.3f}" cy="{cy:.3f}" r="{r:.3f}" {common} />'
    if kind == "ellipse":
        cx, cy, rx, ry = (shape["cx"] * scale, shape["cy"] * scale, shape["rx"] * scale, shape["ry"] * scale)
        return f'<ellipse cx="{cx:.3f}" cy="{cy:.3f}" rx="{rx:.3f}" ry="{ry:.3f}" {common} />'
    if kind == "arc":
        cx, cy, r = shape["cx"], shape["cy"], shape["r"]
        start, end = math.radians(shape["start_angle"]), math.radians(shape["end_angle"])
        x1, y1 = (cx + r * math.cos(start)) * scale, (cy + r * math.sin(start)) * scale
        x2, y2 = (cx + r * math.cos(end)) * scale, (cy + r * math.sin(end)) * scale
        sweep_deg = (shape["end_angle"] - shape["start_angle"]) % 360
        large_arc = 1 if sweep_deg > 180 else 0
        r_scaled = r * scale
        d = f"M {x1:.3f},{y1:.3f} A {r_scaled:.3f},{r_scaled:.3f} 0 {large_arc} 1 {x2:.3f},{y2:.3f}"
        return f'<path d="{d}" {common} />'
    if kind == "text":
        x, y = shape["x"] * scale, shape["y"] * scale
        font_size = shape.get("font_size", 16) * scale
        text_color = shape.get("color", "#e7e9ee")
        content = (
            shape.get("content", "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return (
            f'<text x="{x:.3f}" y="{y:.3f}" font-size="{font_size:.3f}" '
            f'fill="{text_color}" dominant-baseline="hanging">{content}</text>'
        )
    return None


def _dimension_bounds(dim: dict) -> tuple[float, float, float, float] | None:
    """Bounding box of a resolved dimension's *rendered* extent -- the
    offset dimension line can sit well outside the two measured points
    themselves, so the viewBox padding calc needs this too, not just
    `a_pos`/`b_pos` directly."""
    a, b = dim.get("a_pos"), dim.get("b_pos")
    if a is None or b is None:
        return None
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    nx, ny = -dy / length, dx / length
    offset = dim.get("offset", 30.0)
    la = (a[0] + nx * offset, a[1] + ny * offset)
    lb = (b[0] + nx * offset, b[1] + ny * offset)
    xs = [a[0], b[0], la[0], lb[0]]
    ys = [a[1], b[1], la[1], lb[1]]
    return min(xs), min(ys), max(xs), max(ys)


def _dimension_svg_element(dim: dict, scale: float, units: str) -> str | None:
    """A dimension line + two extension lines + a value-label `<text>`,
    grouped in one `<g>` -- SVG has no native dimension-annotation
    concept the way DXF does, so this is a faithful line+text rendering
    per the brief, not an approximation of anything. `length` (used for
    both the offset dimension line's endpoints and the label's numeric
    value) is computed *after* scaling, so the label directly reads in
    the caller's chosen display unit with no separate conversion."""
    a, b = dim.get("a_pos"), dim.get("b_pos")
    if a is None or b is None:
        return None
    ax, ay = a[0] * scale, a[1] * scale
    bx, by = b[0] * scale, b[1] * scale
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    nx, ny = -dy / length, dx / length
    offset = dim.get("offset", 30.0) * scale
    lax, lay = ax + nx * offset, ay + ny * offset
    lbx, lby = bx + nx * offset, by + ny * offset
    mx, my = (lax + lbx) / 2, (lay + lby) / 2
    suffix = _SVG_UNIT_SUFFIX.get(units, "")
    label = f"{length:.2f}{suffix}"
    return (
        '<g class="dimension" stroke="#4dabf7" stroke-width="1" fill="none">'
        f'<line x1="{ax:.3f}" y1="{ay:.3f}" x2="{lax:.3f}" y2="{lay:.3f}" />'
        f'<line x1="{bx:.3f}" y1="{by:.3f}" x2="{lbx:.3f}" y2="{lby:.3f}" />'
        f'<line x1="{lax:.3f}" y1="{lay:.3f}" x2="{lbx:.3f}" y2="{lby:.3f}" />'
        f'<text x="{mx:.3f}" y="{my:.3f}" font-size="12" stroke="none" fill="#4dabf7" '
        f'text-anchor="middle">{label}</text>'
        "</g>"
    )


def _z_ordered(paths: list[dict], layer_order: list[str] | None) -> list[dict]:
    """Sorts paths by layer order, then creation order (Phase 15: fills
    need this to composite correctly -- an unfilled outline mostly
    doesn't reveal z-order bugs, an overlapping filled shape does).
    `paths` already arrives in creation order (see
    DrawingDocument.path_list's docstring on why `.to_set()` was wrong
    for this); `sorted()` is stable, so this only reorders across
    layers, never within one. Paths with no recognized/known layer sort
    last, defensively, rather than raising."""
    if not layer_order:
        return paths
    unknown = len(layer_order)

    def layer_index(p: dict) -> int:
        try:
            return layer_order.index(p.get("layer_id"))
        except ValueError:
            return unknown

    return sorted(paths, key=layer_index)


def drawing_to_svg_string(
    paths: list[dict],
    padding: float = 20.0,
    units: str = "px",
    dimensions: list[dict] | None = None,
    layer_order: list[str] | None = None,
) -> str:
    scale = 1.0 / px_per_unit(units)
    dimensions = dimensions or []
    paths = _z_ordered(paths, layer_order)
    all_points = [pt for p in paths for pt in p.get("points", [])]
    bounds = [_shape_bounds(p) for p in paths if p.get("shape")]
    bounds += [_dimension_bounds(d) for d in dimensions]
    xs, ys = [], []
    for pt in all_points:
        xs.append(pt[0])
        ys.append(pt[1])
    for b in bounds:
        if b is None:
            continue
        xs.extend([b[0], b[2]])
        ys.extend([b[1], b[3]])
    if xs:
        min_x, max_x = min(xs) - padding, max(xs) + padding
        min_y, max_y = min(ys) - padding, max(ys) + padding
    else:
        min_x, min_y, max_x, max_y = 0.0, 0.0, 100.0, 100.0
    min_x, min_y, max_x, max_y = min_x * scale, min_y * scale, max_x * scale, max_y * scale
    width, height = max_x - min_x, max_y - min_y
    suffix = _SVG_UNIT_SUFFIX.get(units, "")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.2f}{suffix}" height="{height:.2f}{suffix}" '
        f'viewBox="{min_x:.2f} {min_y:.2f} {width:.2f} {height:.2f}">',
    ]
    for p in paths:
        if p.get("shape"):
            el = _shape_svg_element(p, scale)
            if el is not None:
                lines.append(el)
                continue
        pts = p.get("points", [])
        if len(pts) < 2:
            continue
        scaled_pts = [(x * scale, y * scale) for x, y in pts]
        d = _path_d_string(scaled_pts, p.get("point_ids"), p, scale)
        color = p.get("color", "#111111")
        stroke_width = p.get("stroke_width", 2.5) * scale
        fill = _fill_attrs(p)
        dash = _dash_attr(p, scale)
        lines.append(
            f'<path d="{d}" {fill} stroke="{color}" stroke-width="{stroke_width:.3f}" {dash} '
            'stroke-linecap="round" stroke-linejoin="round" />'
        )
    for dim in dimensions:
        el = _dimension_svg_element(dim, scale, units)
        if el is not None:
            lines.append(el)
    lines.append("</svg>")
    return "\n".join(lines)


def _path_d_string(pts: list[Point], point_ids: list | None, props: dict, scale: float = 1.0) -> str:
    """Emits `M`/`L`/`C`/`Q` commands per segment, based on each point's
    `curve_prop_key` entry in `props` (absent entirely -- e.g. `point_ids`
    itself is None, as every pre-Phase-8 caller's hand-built dict has --
    is treated exactly like "no curve," so this is a pure superset of the
    old always-straight-lines output). `pts` is assumed already scaled by
    the caller (Phase 11 units); curve control points (stored raw,
    unscaled) are scaled here to match."""
    parts = [f"M {pts[0][0]:.2f},{pts[0][1]:.2f}"]
    for i in range(1, len(pts)):
        node_id = point_ids[i] if point_ids and i < len(point_ids) else None
        seg = props.get(curve_prop_key(node_id)) if node_id is not None else None
        x, y = pts[i]
        if seg is None:
            parts.append(f"L {x:.2f},{y:.2f}")
        elif seg["kind"] == "cubic":
            c1, c2 = seg["c1"], seg["c2"]
            parts.append(f"C {c1[0]*scale:.2f},{c1[1]*scale:.2f} {c2[0]*scale:.2f},{c2[1]*scale:.2f} {x:.2f},{y:.2f}")
        elif seg["kind"] == "quad":
            c = seg["c"]
            parts.append(f"Q {c[0]*scale:.2f},{c[1]*scale:.2f} {x:.2f},{y:.2f}")
        else:
            parts.append(f"L {x:.2f},{y:.2f}")
    return " ".join(parts)


def _parse_points_attr(points_str: str) -> list[Point]:
    tokens = [t for t in re.split(r"[\s,]+", points_str.strip()) if t]
    coords = [float(t) for t in tokens]
    return [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]


_CMD_CHARS = "MLCSQTAZmlcsqtaz"
_NUM_RE = r"-?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _reflect(pivot: Point, other: Point) -> Point:
    return (2 * pivot[0] - other[0], 2 * pivot[1] - other[1])


def _parse_path_d(d: str) -> tuple[list[Point], dict[int, dict]]:
    """Returns (points, curves) -- `points` is the flattened anchor-point
    sequence (the same shape the old M/L-only parser returned), `curves`
    maps a 0-based index into `points` to a curve payload matching
    `curve_prop_key`'s documented shape, for the segment arriving at that
    point. See the module docstring for why `A`/`a` stops parsing early
    rather than guessing.
    """
    tokens = re.findall(rf"[{_CMD_CHARS}]|{_NUM_RE}", d)
    points: list[Point] = []
    curves: dict[int, dict] = {}
    cur = (0.0, 0.0)
    cur_cmd: str | None = None
    prev_cubic_c2: Point | None = None  # for S/s reflection -- absolute
    prev_quad_c: Point | None = None  # for T/t reflection -- absolute
    i = 0

    def read_pair(relative: bool) -> Point:
        nonlocal i
        x, y = float(tokens[i]), float(tokens[i + 1])
        i += 2
        return (cur[0] + x, cur[1] + y) if relative else (x, y)

    while i < len(tokens):
        tok = tokens[i]
        if tok in _CMD_CHARS:
            if tok in ("A", "a"):
                # See module docstring -- stop rather than risk
                # misinterpreting the arc's flag digits as coordinates.
                break
            cur_cmd = tok
            i += 1
            continue
        if cur_cmd is None:
            i += 1  # stray number before any command -- ignore, malformed input
            continue

        relative = cur_cmd.islower()
        if cur_cmd in ("M", "m"):
            cur = read_pair(relative)
            points.append(cur)
            prev_cubic_c2 = prev_quad_c = None
            cur_cmd = "l" if relative else "L"  # subsequent pairs are implicit lineto
        elif cur_cmd in ("L", "l"):
            cur = read_pair(relative)
            points.append(cur)
            prev_cubic_c2 = prev_quad_c = None
        elif cur_cmd in ("C", "c"):
            c1 = read_pair(relative)
            c2 = read_pair(relative)
            end = read_pair(relative)
            points.append(end)
            curves[len(points) - 1] = {"kind": "cubic", "c1": c1, "c2": c2}
            cur = end
            prev_cubic_c2, prev_quad_c = c2, None
        elif cur_cmd in ("S", "s"):
            c1 = _reflect(cur, prev_cubic_c2) if prev_cubic_c2 is not None else cur
            c2 = read_pair(relative)
            end = read_pair(relative)
            points.append(end)
            curves[len(points) - 1] = {"kind": "cubic", "c1": c1, "c2": c2}
            cur = end
            prev_cubic_c2, prev_quad_c = c2, None
        elif cur_cmd in ("Q", "q"):
            c = read_pair(relative)
            end = read_pair(relative)
            points.append(end)
            curves[len(points) - 1] = {"kind": "quad", "c": c}
            cur = end
            prev_quad_c, prev_cubic_c2 = c, None
        elif cur_cmd in ("T", "t"):
            c = _reflect(cur, prev_quad_c) if prev_quad_c is not None else cur
            end = read_pair(relative)
            points.append(end)
            curves[len(points) - 1] = {"kind": "quad", "c": c}
            cur = end
            prev_quad_c, prev_cubic_c2 = c, None
        elif cur_cmd in ("Z", "z"):
            i += 1
        else:  # pragma: no cover - _CMD_CHARS is exhaustive
            i += 1
    return points, curves


def drawing_from_svg_string(svg_text: str) -> list[dict]:
    """Returns one `{"points": [...], "curves": {...}}` dict per parsed
    path/line/polyline/polygon -- `curves` is always present (empty for
    anything with no Bezier segments, i.e. every non-`<path>` element and
    any `<path>` using only M/L)."""
    root = ET.fromstring(svg_text)
    paths: list[dict] = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "line":
            x1, y1, x2, y2 = (float(elem.get(a, "0")) for a in ("x1", "y1", "x2", "y2"))
            paths.append({"points": [(x1, y1), (x2, y2)], "curves": {}})
        elif tag in ("polyline", "polygon"):
            pts = _parse_points_attr(elem.get("points", ""))
            if tag == "polygon" and pts:
                pts = [*pts, pts[0]]
            if len(pts) >= 2:
                paths.append({"points": pts, "curves": {}})
        elif tag == "path":
            pts, curves = _parse_path_d(elem.get("d", ""))
            if len(pts) >= 2:
                paths.append({"points": pts, "curves": curves})
    return paths
