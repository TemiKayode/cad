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

import re
from xml.etree import ElementTree as ET

from crdt_cad.crdt.document import curve_prop_key

Point = tuple[float, float]


def drawing_to_svg_string(paths: list[dict], padding: float = 20.0) -> str:
    all_points = [pt for p in paths for pt in p.get("points", [])]
    if all_points:
        xs = [pt[0] for pt in all_points]
        ys = [pt[1] for pt in all_points]
        min_x, max_x = min(xs) - padding, max(xs) + padding
        min_y, max_y = min(ys) - padding, max(ys) + padding
    else:
        min_x, min_y, max_x, max_y = 0.0, 0.0, 100.0, 100.0
    width, height = max_x - min_x, max_y - min_y

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x:.2f} {min_y:.2f} {width:.2f} {height:.2f}">',
    ]
    for p in paths:
        pts = p.get("points", [])
        if len(pts) < 2:
            continue
        d = _path_d_string(pts, p.get("point_ids"), p)
        color = p.get("color", "#111111")
        stroke_width = p.get("stroke_width", 2.5)
        lines.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{stroke_width}" '
            'stroke-linecap="round" stroke-linejoin="round" />'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _path_d_string(pts: list[Point], point_ids: list | None, props: dict) -> str:
    """Emits `M`/`L`/`C`/`Q` commands per segment, based on each point's
    `curve_prop_key` entry in `props` (absent entirely -- e.g. `point_ids`
    itself is None, as every pre-Phase-8 caller's hand-built dict has --
    is treated exactly like "no curve," so this is a pure superset of the
    old always-straight-lines output)."""
    parts = [f"M {pts[0][0]:.2f},{pts[0][1]:.2f}"]
    for i in range(1, len(pts)):
        node_id = point_ids[i] if point_ids and i < len(point_ids) else None
        seg = props.get(curve_prop_key(node_id)) if node_id is not None else None
        x, y = pts[i]
        if seg is None:
            parts.append(f"L {x:.2f},{y:.2f}")
        elif seg["kind"] == "cubic":
            c1, c2 = seg["c1"], seg["c2"]
            parts.append(f"C {c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} {x:.2f},{y:.2f}")
        elif seg["kind"] == "quad":
            c = seg["c"]
            parts.append(f"Q {c[0]:.2f},{c[1]:.2f} {x:.2f},{y:.2f}")
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
