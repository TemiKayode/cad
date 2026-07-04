"""SVG export/import for 2D sketch paths.

Import is deliberately scoped to the common "reference geometry" case:
straight-segment entities (``<line>``, ``<polyline>``, ``<polygon>``,
and ``<path>`` using only ``M``/``L`` commands, absolute or relative).
Curves (``C``/``S``/``Q``/``T``/``A``) are not parsed -- handling them
properly means either reproducing them as Bezier/arc primitives (not
yet in the document model; today's paths are polylines) or flattening
them to line segments, and getting the flattening tolerance right is
its own feature. Silently truncating a path at the last straight point
was judged better than guessing.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

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
        d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        color = p.get("color", "#111111")
        stroke_width = p.get("stroke_width", 2.5)
        lines.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{stroke_width}" '
            'stroke-linecap="round" stroke-linejoin="round" />'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _parse_points_attr(points_str: str) -> list[Point]:
    tokens = [t for t in re.split(r"[\s,]+", points_str.strip()) if t]
    coords = [float(t) for t in tokens]
    return [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]


def _parse_path_d(d: str) -> list[Point]:
    tokens = re.findall(r"[MLmlZz]|-?\d*\.?\d+(?:[eE][-+]?\d+)?", d)
    points: list[Point] = []
    cur = (0.0, 0.0)
    cur_cmd: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("M", "L", "m", "l"):
            cur_cmd = tok
            i += 1
            continue
        if tok in ("Z", "z"):
            i += 1
            continue
        if cur_cmd is None:
            i += 1
            continue
        x, y = float(tok), float(tokens[i + 1])
        i += 2
        if cur_cmd in ("m", "l"):
            x, y = cur[0] + x, cur[1] + y
        cur = (x, y)
        points.append(cur)
    return points


def drawing_from_svg_string(svg_text: str) -> list[list[Point]]:
    root = ET.fromstring(svg_text)
    paths: list[list[Point]] = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "line":
            x1, y1, x2, y2 = (float(elem.get(a, "0")) for a in ("x1", "y1", "x2", "y2"))
            paths.append([(x1, y1), (x2, y2)])
        elif tag in ("polyline", "polygon"):
            pts = _parse_points_attr(elem.get("points", ""))
            if tag == "polygon" and pts:
                pts = [*pts, pts[0]]
            if len(pts) >= 2:
                paths.append(pts)
        elif tag == "path":
            pts = _parse_path_d(elem.get("d", ""))
            if len(pts) >= 2:
                paths.append(pts)
    return paths
