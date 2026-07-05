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
"""

from __future__ import annotations

import io

import ezdxf

from crdt_cad.crdt.document import flatten_path_to_polyline, px_per_unit

Point = tuple[float, float]

_DXF_INSUNITS = {"px": 0, "mm": 4, "in": 1}


def _add_shape_entity(msp, shape: dict, scale: float) -> bool:
    """Adds the native DXF entity matching `shape["shape"]`, scaled by
    `scale`. Returns False (and adds nothing) if `shape` isn't a
    recognized shape kind -- the caller falls back to the flattened-
    polyline path every freehand/polygon path already uses."""
    kind = shape.get("shape")
    attribs = {"layer": "0"}
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
    else:
        return False
    return True


def drawing_to_dxf_bytes(paths: list[dict], units: str = "px") -> bytes:
    scale = 1.0 / px_per_unit(units)
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = _DXF_INSUNITS.get(units, 0)
    msp = doc.modelspace()
    for p in paths:
        if p.get("shape") and _add_shape_entity(msp, p, scale):
            continue
        pts = p.get("points", [])
        if len(pts) < 2:
            continue
        flattened = flatten_path_to_polyline(pts, p.get("point_ids"), p)
        scaled = [(x * scale, y * scale) for x, y in flattened]
        msp.add_lwpolyline(scaled, dxfattribs={"layer": "0"})
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
