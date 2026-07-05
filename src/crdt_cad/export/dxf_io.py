"""DXF export/import for 2D sketch paths, via ``ezdxf``.

Each freehand/polygon path becomes one ``LWPOLYLINE`` entity on export.
A shape primitive (Phase 11 -- ``props["shape"]`` present) becomes the
matching native DXF entity instead (``LINE``, a closed ``LWPOLYLINE``
for a rectangle, ``CIRCLE``, ``ELLIPSE``, ``ARC``), so the exported file
is a faithful, editable shape in any real CAD tool rather than always a
flattened polyline. Import reads ``LWPOLYLINE``, ``LINE``, and legacy
``POLYLINE`` entities back into plain point lists (shape primitives do
not currently round-trip back into ``props["shape"]`` on import -- see
the module's Phase 11 note below).

``LWPOLYLINE`` has no Bezier concept, so a freehand/polygon path's curve
segments (Phase 8; see ``crdt_cad.crdt.document``'s module docstring)
are flattened into a dense sampled polyline via
``flatten_path_to_polyline`` before export -- an approximation, not a
re-derivation of true curve geometry, but a faithful-looking one at 12
samples per segment. DXF import does not reconstruct curves from the
flattened result (there's no marker in the DXF distinguishing "this was
originally a curve" from "this was always a polyline") -- reimporting a
DXF this project exported gets back a denser polyline, not the original
Bezier.

Document units (Phase 11) scale every exported coordinate by
``1/px_per_unit(units)`` and set the ``$INSUNITS`` header variable
(0=unitless, 4=millimeters, 1=inches) so a real CAD tool interprets the
numbers correctly -- "px" stays unitless (0), matching this project's
behavior before units existed at all.

Dimension annotations (Phase 13, ``crdt_cad.crdt.document``'s
``dimensions`` component) export as real ``DIMENSION`` entities via
``ezdxf``'s ``add_linear_dim`` -- a genuine two-step create-then-render
API (confirmed by actually building, rendering, and reading one back:
the reloaded file contains a real ``DIMENSION`` entity whose
``get_measurement()`` matches the two points' distance), not a
hand-drawn line-and-text approximation. Only already-resolved
dimensions (``a_pos``/``b_pos`` present -- see ``dimension_list``) are
exported; one whose anchor point was concurrently deleted is silently
skipped, the same "can't currently render this" contract
``resolve_dimension_points`` documents.

Phase 15 designer features:

- **Fills** (``fill``/``fill_opacity`` path_props) export as a real
  ``HATCH`` entity bounded by the shape's own outline (Rect exactly;
  Circle/Ellipse sampled at 64 points around the rim, the same
  "flatten rather than build exact arc geometry" choice already made
  for curve export -- confirmed a real, readable HATCH by actually
  building and reloading one, including its ``true_color``/
  ``transparency`` fields). Line/Arc/Text have no meaningful enclosed
  area (the same judgment call the Measure tool's Area/Perimeter mode
  already makes, Phase 13) and are never filled regardless of the
  `fill` prop.
- **Stroke styles** (``dash``: ``solid``/``dashed``/``dotted``) map onto
  ``ezdxf``'s own standard linetype library (``ezdxf.new(setup=True)``
  loads it) -- ``DASHED``/``DOT`` are real, named DXF linetypes a CAD
  tool already knows how to render, not a hand-approximated dash
  pattern.
- **Text** exports as a real ``TEXT`` entity.
- Every entity now also gets an explicit z-order (Phase 15 needs
  correct compositing for overlapping fills to look right) -- see
  ``_z_ordered``, sorting by layer order then creation order, mirroring
  ``svg_io``'s identical helper.
"""

from __future__ import annotations

import io
import math

import ezdxf

from crdt_cad.crdt.document import flatten_path_to_polyline, px_per_unit

Point = tuple[float, float]

_DXF_INSUNITS = {"px": 0, "mm": 4, "in": 1}
_DXF_LINETYPE = {"dashed": "DASHED", "dotted": "DOT"}
_FILLABLE_SHAPE_KINDS = {"rect", "circle", "ellipse"}


def _z_ordered(paths: list[dict], layer_order: list[str] | None) -> list[dict]:
    """See svg_io._z_ordered -- identical rationale, duplicated rather
    than shared across modules to keep each export format's dependency
    surface independent (dxf_io shouldn't import from svg_io just for
    this one small, format-agnostic helper)."""
    if not layer_order:
        return paths
    unknown = len(layer_order)

    def layer_index(p: dict) -> int:
        try:
            return layer_order.index(p.get("layer_id"))
        except ValueError:
            return unknown

    return sorted(paths, key=layer_index)


def _dash_dxfattribs(props: dict) -> dict:
    dash = props.get("dash", "solid")
    linetype = _DXF_LINETYPE.get(dash)
    return {"linetype": linetype} if linetype else {}


def _hex_to_true_color(hex_color: str) -> int:
    return int(hex_color.lstrip("#"), 16)


def _circle_boundary(cx: float, cy: float, rx: float, ry: float, samples: int = 64) -> list[Point]:
    return [
        (cx + rx * math.cos(2 * math.pi * i / samples), cy + ry * math.sin(2 * math.pi * i / samples))
        for i in range(samples)
    ]


def _shape_fill_boundary(shape: dict, scale: float) -> list[Point] | None:
    """Point loop to bound a HATCH fill -- Line/Arc/Text have no
    meaningful enclosed area (see the module docstring) and return
    None, meaning "never filled" regardless of the `fill` prop."""
    kind = shape.get("shape")
    if kind == "rect":
        x, y, w, h = shape["x"] * scale, shape["y"] * scale, shape["w"] * scale, shape["h"] * scale
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    if kind == "circle":
        return _circle_boundary(shape["cx"] * scale, shape["cy"] * scale, shape["r"] * scale, shape["r"] * scale)
    if kind == "ellipse":
        return _circle_boundary(shape["cx"] * scale, shape["cy"] * scale, shape["rx"] * scale, shape["ry"] * scale)
    return None


def _add_fill_hatch(msp, boundary: list[Point], props: dict) -> None:
    fill = props.get("fill")
    if not fill or fill == "none" or not boundary:
        return
    dxfattribs = {"true_color": _hex_to_true_color(fill)}
    opacity = props.get("fill_opacity")
    if opacity is not None and opacity < 1.0:
        # float2transparency's argument is a *transparency* fraction (0 =
        # opaque, 1 = fully transparent) -- the inverse of fill_opacity's
        # SVG-matching convention (1.0 = opaque), so invert it here.
        dxfattribs["transparency"] = ezdxf.colors.float2transparency(1.0 - max(0.0, min(1.0, float(opacity))))
    hatch = msp.add_hatch(dxfattribs=dxfattribs)
    hatch.paths.add_polyline_path(boundary, is_closed=True)


def _add_dimension_entity(msp, dim: dict, scale: float) -> bool:
    """Adds a real `DIMENSION` entity for one resolved dimension
    (`a_pos`/`b_pos` present). Returns False (adds nothing) if the
    dimension isn't currently resolvable, or its two anchors coincide
    (a zero-length "dimension" has no direction to offset along)."""
    a, b = dim.get("a_pos"), dim.get("b_pos")
    if a is None or b is None:
        return False
    ax, ay = a[0] * scale, a[1] * scale
    bx, by = b[0] * scale, b[1] * scale
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return False
    nx, ny = -dy / length, dx / length  # unit perpendicular
    offset = dim.get("offset", 30.0) * scale
    base = (ax + nx * offset, ay + ny * offset)
    dim_entity = msp.add_linear_dim(base=base, p1=(ax, ay), p2=(bx, by), dimstyle="EZDXF")
    dim_entity.render()
    return True


def _add_shape_entity(msp, shape: dict, scale: float) -> bool:
    """Adds the native DXF entity matching `shape["shape"]`, scaled by
    `scale`, plus a HATCH fill (if `fill` is set and the kind has a
    meaningful enclosed area) and the `dash` prop as a real linetype.
    Returns False (and adds nothing) if `shape` isn't a recognized shape
    kind -- the caller falls back to the flattened-polyline path every
    freehand/polygon path already uses."""
    kind = shape.get("shape")
    attribs = {"layer": "0", **_dash_dxfattribs(shape)}
    if kind == "line":
        msp.add_line(
            (shape["x1"] * scale, shape["y1"] * scale),
            (shape["x2"] * scale, shape["y2"] * scale),
            dxfattribs=attribs,
        )
    elif kind == "rect":
        x, y, w, h = shape["x"] * scale, shape["y"] * scale, shape["w"] * scale, shape["h"] * scale
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs=attribs)
    elif kind == "circle":
        msp.add_circle((shape["cx"] * scale, shape["cy"] * scale), shape["r"] * scale, dxfattribs=attribs)
    elif kind == "ellipse":
        msp.add_ellipse(
            (shape["cx"] * scale, shape["cy"] * scale),
            major_axis=(shape["rx"] * scale, 0, 0),
            ratio=shape["ry"] / shape["rx"] if shape["rx"] else 1.0,
            dxfattribs=attribs,
        )
    elif kind == "arc":
        msp.add_arc(
            (shape["cx"] * scale, shape["cy"] * scale),
            shape["r"] * scale,
            shape["start_angle"],
            shape["end_angle"],
            dxfattribs=attribs,
        )
    elif kind == "text":
        text_entity = msp.add_text(
            shape.get("content", ""),
            dxfattribs={"height": shape.get("font_size", 16) * scale, **attribs},
        )
        # DXF TEXT places by baseline-left by default; matching the
        # canvas/SVG "top-left" convention (see svg_io._shape_bounds)
        # would need vertical-alignment attributes ezdxf's set_placement
        # doesn't expose directly for plain TEXT -- placing at (x, y) as
        # DXF's own baseline convention instead is an accepted, honest
        # difference from the canvas/SVG rendering, not a bug.
        text_entity.set_placement((shape["x"] * scale, shape["y"] * scale))
    else:
        return False
    return True


def drawing_to_dxf_bytes(
    paths: list[dict],
    units: str = "px",
    dimensions: list[dict] | None = None,
    layer_order: list[str] | None = None,
) -> bytes:
    scale = 1.0 / px_per_unit(units)
    doc = ezdxf.new(setup=True)  # loads the standard linetype library -- needed for dashed/dotted strokes
    doc.header["$INSUNITS"] = _DXF_INSUNITS.get(units, 0)
    msp = doc.modelspace()
    for p in _z_ordered(paths, layer_order):
        if p.get("shape"):
            if _add_shape_entity(msp, p, scale):
                boundary = _shape_fill_boundary(p, scale)
                if boundary:
                    _add_fill_hatch(msp, boundary, p)
                continue
        pts = p.get("points", [])
        if len(pts) < 2:
            continue
        flattened = flatten_path_to_polyline(pts, p.get("point_ids"), p)
        scaled = [(x * scale, y * scale) for x, y in flattened]
        msp.add_lwpolyline(scaled, dxfattribs={"layer": "0", **_dash_dxfattribs(p)})
        # A freehand/polygon path is only meaningfully fillable when
        # it's actually closed (first point == last point) -- an open
        # path has no well-defined interior to hatch.
        if len(scaled) > 2 and math.hypot(scaled[0][0] - scaled[-1][0], scaled[0][1] - scaled[-1][1]) < 1e-6:
            _add_fill_hatch(msp, scaled, p)
    for dim in dimensions or []:
        _add_dimension_entity(msp, dim, scale)
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def drawing_from_dxf_bytes(data: bytes) -> list[list[Point]]:
    text = data.decode("utf-8", errors="replace")
    doc = ezdxf.read(io.StringIO(text))
    msp = doc.modelspace()
    paths: list[list[Point]] = []
    for entity in msp:
        kind = entity.dxftype()
        if kind == "LWPOLYLINE":
            paths.append([(pt[0], pt[1]) for pt in entity.get_points()])
        elif kind == "LINE":
            paths.append(
                [
                    (entity.dxf.start.x, entity.dxf.start.y),
                    (entity.dxf.end.x, entity.dxf.end.y),
                ]
            )
        elif kind == "POLYLINE":
            paths.append([(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices])
    return paths
