"""PDF export for a 2D sketch "sheet" (Part 7 C3) -- a printable page
(page size, orientation, margin, drawing scale) with a title block,
rendered via ``reportlab``. A sheet is a CRDT-durable object
(``DrawingDocument.sheets``/``sheet_props``, see that module's
docstring) so its page setup and title-block text are collaboratively
editable and persist with the room, the same as every other document
component -- this module only turns one resolved sheet dict plus the
room's current paths into PDF bytes; it never touches document state.

Everything the drawing itself needs (curve segments, shape primitives,
dimensions) is rendered by sampling into ``reportlab``'s path/line
primitives rather than reaching for that library's own arc/ellipse
bounding-box primitives -- their angle convention is measured
counter-clockwise in PDF's y-up page space, while this project's stored
angles are measured in the document's own y-down canvas space, and
getting that sign flip right for every rotation would be needless risk
next to just sampling points through the same coordinate transform
every straight/curve segment already goes through. This is the same
"flatten what doesn't map directly onto the target format" choice this
project already makes for DXF's HATCH boundaries and SVG's Area/
Perimeter measure mode -- not a shortcut unique to this module.

Curve segments (quadratic or cubic, see ``crdt_cad.crdt.document``'s
Phase 8 docstring) render as real PDF bezier curves
(``PDFPathObject.curveTo``, which only accepts cubic control points) --
a quadratic segment's control point is degree-elevated to an exactly
equivalent cubic first (``C1 = P0 + 2/3*(C-P0)``, ``C2 = P1 +
2/3*(C-P1)``), the same exact (not approximate) elevation formula
``ezdxf.math.quadratic_to_cubic_bezier`` uses, verified during Part 7
C2's DXF SPLINE work.
"""

from __future__ import annotations

import math
from io import BytesIO

from reportlab.lib.pagesizes import A3, A4, LETTER, TABLOID
from reportlab.pdfgen import canvas as pdfcanvas

Point = tuple[float, float]

_PAGE_SIZES_PT = {"a4": A4, "a3": A3, "letter": LETTER, "tabloid": TABLOID}

# Applied at render time, never written into the CRDT at sheet-creation
# time -- DrawingDocument.add_sheet only ever writes "name" up front,
# same "the reader fills in defaults, the writer doesn't" choice
# `layer_props`'s `visible`/`color` defaults already make.
SHEET_DEFAULTS = {
    "page_size": "a4",
    "orientation": "landscape",
    "margin_mm": 10.0,
    # "fit": auto-scale the whole drawing to fill the sheet's drawing
    # area, centered, preserving aspect ratio. "custom": `scale_ratio`
    # (paper distance / real distance, e.g. 0.1 for a "1:10" print
    # scale) applied directly, anchored at the drawing area's top-left
    # corner -- a real drafting scale isn't supposed to auto-fit.
    "scale_mode": "fit",
    "scale_ratio": 1.0,
    "title": "",
    "drawn_by": "",
    "date": "",
    "drawing_number": "",
    "revision": "",
    "notes": "",
}

_PT_PER_MM = 72.0 / 25.4
# Stored geometry is always raw px at a fixed 96px/inch convention (see
# document.py's UNITS_PX_PER_UNIT) -- a PDF point is 1/72 inch, so this
# ratio is a fixed physical constant. It does NOT depend on the
# document's own display "units" setting: px/mm/in are all display
# labels layered over the same underlying 96dpi grid, so a "custom"
# print scale ratio means the same physical thing regardless of which
# unit the room happens to be displaying coordinates in.
_PT_PER_PX = 72.0 / 96.0

_TITLE_BLOCK_HEIGHT_MM = 30.0
_ARC_SAMPLES = 64


def _shape_bounds(shape: dict) -> tuple[float, float, float, float] | None:
    """See svg_io._shape_bounds -- identical rationale, duplicated
    rather than shared across export modules (see this project's
    established "don't cross-import one small format-agnostic helper"
    convention, e.g. dxf_io's own `_z_ordered`)."""
    kind = shape.get("shape")
    if kind == "line":
        xs, ys = [shape["x1"], shape["x2"]], [shape["y1"], shape["y2"]]
    elif kind == "rect":
        xs, ys = [shape["x"], shape["x"] + shape["w"]], [shape["y"], shape["y"] + shape["h"]]
    elif kind == "circle":
        xs = [shape["cx"] - shape["r"], shape["cx"] + shape["r"]]
        ys = [shape["cy"] - shape["r"], shape["cy"] + shape["r"]]
    elif kind == "ellipse":
        xs = [shape["cx"] - shape["rx"], shape["cx"] + shape["rx"]]
        ys = [shape["cy"] - shape["ry"], shape["cy"] + shape["ry"]]
    elif kind == "arc":
        xs = [shape["cx"] - shape["r"], shape["cx"] + shape["r"]]
        ys = [shape["cy"] - shape["r"], shape["cy"] + shape["r"]]
    elif kind == "text":
        font_size = shape.get("font_size", 16)
        content = shape.get("content", "")
        xs = [shape["x"], shape["x"] + len(content) * font_size * 0.6]
        ys = [shape["y"], shape["y"] + font_size * 1.2]
    else:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _dimension_bounds(dim: dict) -> tuple[float, float, float, float] | None:
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


def _drawing_bounds(paths: list[dict], dimensions: list[dict]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for p in paths:
        for pt in p.get("points", []):
            xs.append(pt[0])
            ys.append(pt[1])
    bounds = [_shape_bounds(p) for p in paths if p.get("shape")]
    bounds += [_dimension_bounds(d) for d in dimensions]
    for b in bounds:
        if b is None:
            continue
        xs.extend([b[0], b[2]])
        ys.extend([b[1], b[3]])
    if not xs:
        return 0.0, 0.0, 100.0, 100.0
    return min(xs), min(ys), max(xs), max(ys)


def _quad_to_cubic(p0: Point, c: Point, p1: Point) -> tuple[Point, Point]:
    c1 = (p0[0] + 2.0 / 3.0 * (c[0] - p0[0]), p0[1] + 2.0 / 3.0 * (c[1] - p0[1]))
    c2 = (p1[0] + 2.0 / 3.0 * (c[0] - p1[0]), p1[1] + 2.0 / 3.0 * (c[1] - p1[1]))
    return c1, c2


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return r / 255.0, g / 255.0, b / 255.0


def _circle_boundary(cx: float, cy: float, rx: float, ry: float, samples: int = _ARC_SAMPLES) -> list[Point]:
    return [
        (cx + rx * math.cos(2 * math.pi * i / samples), cy + ry * math.sin(2 * math.pi * i / samples))
        for i in range(samples)
    ]


def _arc_points(cx: float, cy: float, r: float, start_deg: float, end_deg: float, samples: int = _ARC_SAMPLES // 2) -> list[Point]:
    sweep = (end_deg - start_deg) % 360 or 360.0
    return [
        (
            cx + r * math.cos(math.radians(start_deg + sweep * i / samples)),
            cy + r * math.sin(math.radians(start_deg + sweep * i / samples)),
        )
        for i in range(samples + 1)
    ]


class _Transform:
    """Maps a raw document-space point (px, y-down) into PDF point-space
    (y-up), scaling and placing it within `area` -- see the module
    docstring's "fit" vs "custom" note for what `centered` controls."""

    def __init__(self, bounds: tuple[float, float, float, float], area: tuple[float, float, float, float], scale_pt_per_px: float, centered: bool) -> None:
        min_x, min_y, max_x, max_y = bounds
        area_x0, area_y0, area_w, area_h = area
        self.min_x, self.min_y = min_x, min_y
        self.s = scale_pt_per_px
        bbox_w, bbox_h = (max_x - min_x) * self.s, (max_y - min_y) * self.s
        pad_x = max(0.0, (area_w - bbox_w) / 2.0) if centered else 0.0
        pad_y = max(0.0, (area_h - bbox_h) / 2.0) if centered else 0.0
        self.origin_x = area_x0 + pad_x
        self.top_y = area_y0 + area_h - pad_y

    def __call__(self, pt: Point) -> Point:
        x = self.origin_x + (pt[0] - self.min_x) * self.s
        y = self.top_y - (pt[1] - self.min_y) * self.s
        return x, y


def _draw_path_stroke(c: pdfcanvas.Canvas, points: list[Point], point_ids: list, props: dict, tf: _Transform) -> None:
    from crdt_cad.crdt.document import curve_prop_key

    if len(points) < 2:
        return
    path = c.beginPath()
    path.moveTo(*tf(points[0]))
    for i in range(1, len(points)):
        node_id = point_ids[i] if point_ids and i < len(point_ids) else None
        seg = props.get(curve_prop_key(node_id)) if node_id is not None else None
        p0, p1 = points[i - 1], points[i]
        if seg is not None and seg.get("kind") == "cubic":
            path.curveTo(*tf(tuple(seg["c1"])), *tf(tuple(seg["c2"])), *tf(p1))
        elif seg is not None and seg.get("kind") == "quad":
            c1, c2 = _quad_to_cubic(p0, tuple(seg["c"]), p1)
            path.curveTo(*tf(c1), *tf(c2), *tf(p1))
        else:
            path.lineTo(*tf(p1))
    c.drawPath(path, stroke=1, fill=0)


def _apply_stroke_style(c: pdfcanvas.Canvas, props: dict, tf: _Transform) -> None:
    color = props.get("color", "#111111")
    c.setStrokeColorRGB(*_hex_to_rgb01(color))
    c.setLineWidth(max(0.1, props.get("stroke_width", 2.0) * tf.s))
    dash = props.get("dash", "solid")
    sw = max(0.5, props.get("stroke_width", 2.0) * tf.s)
    if dash == "dashed":
        c.setDash([sw * 4, sw * 3])
    elif dash == "dotted":
        c.setDash([sw * 0.1, sw * 2])
    else:
        c.setDash([])
    c.setLineCap(1)
    c.setLineJoin(1)


def _apply_fill_style(c: pdfcanvas.Canvas, props: dict) -> bool:
    fill = props.get("fill")
    if not fill or fill == "none":
        return False
    c.setFillColorRGB(*_hex_to_rgb01(fill), alpha=props.get("fill_opacity", 1.0))
    return True


def _draw_polyline(c: pdfcanvas.Canvas, pts: list[Point], tf: _Transform, closed: bool, fill: bool) -> None:
    if len(pts) < 2:
        return
    path = c.beginPath()
    path.moveTo(*tf(pts[0]))
    for pt in pts[1:]:
        path.lineTo(*tf(pt))
    if closed:
        path.close()
    c.drawPath(path, stroke=1, fill=1 if fill else 0)


def _draw_shape(c: pdfcanvas.Canvas, shape: dict, tf: _Transform) -> bool:
    kind = shape.get("shape")
    _apply_stroke_style(c, shape, tf)
    if kind == "line":
        c.line(*tf((shape["x1"], shape["y1"])), *tf((shape["x2"], shape["y2"])))
    elif kind == "rect":
        x, y, w, h = shape["x"], shape["y"], shape["w"], shape["h"]
        fill = _apply_fill_style(c, shape)
        _draw_polyline(c, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)], tf, closed=True, fill=fill)
    elif kind == "circle":
        fill = _apply_fill_style(c, shape)
        _draw_polyline(c, _circle_boundary(shape["cx"], shape["cy"], shape["r"], shape["r"]), tf, closed=True, fill=fill)
    elif kind == "ellipse":
        fill = _apply_fill_style(c, shape)
        _draw_polyline(c, _circle_boundary(shape["cx"], shape["cy"], shape["rx"], shape["ry"]), tf, closed=True, fill=fill)
    elif kind == "arc":
        pts = _arc_points(shape["cx"], shape["cy"], shape["r"], shape["start_angle"], shape["end_angle"])
        _draw_polyline(c, pts, tf, closed=False, fill=False)
    elif kind == "text":
        x, y = tf((shape["x"], shape["y"]))
        font_size = shape.get("font_size", 16) * tf.s
        c.setFillColorRGB(*_hex_to_rgb01(shape.get("color", "#111111")))
        c.setFont("Helvetica", max(1.0, font_size))
        # DXF/SVG-consistent honest baseline difference (see dxf_io's
        # own TEXT note): placed at the transformed top-left point
        # directly, not offset by real font-ascent metrics.
        c.drawString(x, y - font_size, shape.get("content", ""))
    else:
        return False
    return True


def _draw_dimension(c: pdfcanvas.Canvas, dim: dict, tf: _Transform) -> None:
    a, b = dim.get("a_pos"), dim.get("b_pos")
    if a is None or b is None:
        return
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return
    nx, ny = -dy / length, dx / length
    offset = dim.get("offset", 30.0)
    la = (a[0] + nx * offset, a[1] + ny * offset)
    lb = (b[0] + nx * offset, b[1] + ny * offset)
    mid = ((la[0] + lb[0]) / 2.0, (la[1] + lb[1]) / 2.0)
    c.setStrokeColorRGB(0.30, 0.67, 0.97)
    c.setLineWidth(max(0.3, 1.0 * tf.s))
    c.setDash([])
    c.line(*tf(a), *tf(la))
    c.line(*tf(b), *tf(lb))
    c.line(*tf(la), *tf(lb))
    mx, my = tf(mid)
    c.setFillColorRGB(0.30, 0.67, 0.97)
    c.setFont("Helvetica", max(1.0, 9.0 * tf.s))
    c.drawCentredString(mx, my, f"{length * tf.s / _PT_PER_PX:.2f}")


def _draw_title_block(c: pdfcanvas.Canvas, x0: float, y0: float, width: float, height: float, fields: dict) -> None:
    c.setLineWidth(1.0)
    c.setDash([])
    c.setStrokeColorRGB(0.0, 0.0, 0.0)
    c.rect(x0, y0, width, height, stroke=1, fill=0)
    title_row_h = height * 0.5
    c.line(x0, y0 + title_row_h, x0 + width, y0 + title_row_h)
    c.setFillColorRGB(0.0, 0.0, 0.0)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x0 + 6, y0 + title_row_h + title_row_h / 2 - 4, fields["title"] or "(untitled sheet)")
    if fields["notes"]:
        c.setFont("Helvetica", 7)
        c.drawString(x0 + 6, y0 + title_row_h + 3, fields["notes"][:110])
    cells = [("DRAWN BY", fields["drawn_by"]), ("DATE", fields["date"]), ("DWG NO", fields["drawing_number"]), ("REV", fields["revision"])]
    col_w = width / len(cells)
    c.setFont("Helvetica", 6)
    for i, (label, value) in enumerate(cells):
        cx = x0 + i * col_w
        if i > 0:
            c.line(cx, y0, cx, y0 + title_row_h)
        c.setFont("Helvetica", 6)
        c.drawString(cx + 4, y0 + title_row_h - 10, label)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(cx + 4, y0 + 6, str(value) if value else "")


def sheet_to_pdf_bytes(paths: list[dict], sheet: dict, dimensions: list[dict] | None = None, layer_order: list[str] | None = None) -> bytes:
    dimensions = dimensions or []
    fields = {**SHEET_DEFAULTS, **sheet}
    page_wh = _PAGE_SIZES_PT.get(fields["page_size"], A4)
    if fields["orientation"] == "landscape":
        page_w, page_h = max(page_wh), min(page_wh)
    else:
        page_w, page_h = min(page_wh), max(page_wh)

    buf = BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(page_w, page_h))

    margin = fields["margin_mm"] * _PT_PER_MM
    printable_x0, printable_y0 = margin, margin
    printable_w, printable_h = page_w - 2 * margin, page_h - 2 * margin
    c.setLineWidth(1.2)
    c.setStrokeColorRGB(0.0, 0.0, 0.0)
    c.rect(printable_x0, printable_y0, printable_w, printable_h, stroke=1, fill=0)

    title_block_h = min(_TITLE_BLOCK_HEIGHT_MM * _PT_PER_MM, printable_h * 0.4)
    _draw_title_block(c, printable_x0, printable_y0, printable_w, title_block_h, fields)

    area_y0 = printable_y0 + title_block_h
    area = (printable_x0, area_y0, printable_w, printable_h - title_block_h)

    if fields["scale_mode"] == "custom":
        scale_pt_per_px = _PT_PER_PX * float(fields["scale_ratio"])
        centered = False
    else:
        scale_pt_per_px = None
        centered = True
    bounds = _drawing_bounds(paths, dimensions)
    bbox_w, bbox_h = bounds[2] - bounds[0], bounds[3] - bounds[1]
    if scale_pt_per_px is None:
        if bbox_w > 1e-6 and bbox_h > 1e-6:
            scale_pt_per_px = min(area[2] / bbox_w, area[3] / bbox_h)
        else:
            scale_pt_per_px = _PT_PER_PX

    c.saveState()
    p = c.beginPath()
    p.rect(area[0], area[1], area[2], area[3])
    c.clipPath(p, stroke=0, fill=0)
    tf = _Transform(bounds, area, scale_pt_per_px, centered)

    ordered = paths
    if layer_order:
        unknown = len(layer_order)

        def _layer_index(pth: dict) -> int:
            try:
                return layer_order.index(pth.get("layer_id"))
            except ValueError:
                return unknown

        ordered = sorted(paths, key=_layer_index)

    for p_entry in ordered:
        if p_entry.get("shape") and _draw_shape(c, p_entry, tf):
            continue
        pts = p_entry.get("points", [])
        if len(pts) < 2:
            continue
        _apply_stroke_style(c, p_entry, tf)
        _draw_path_stroke(c, pts, p_entry.get("point_ids"), p_entry, tf)

    for dim in dimensions:
        _draw_dimension(c, dim, tf)

    c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()
