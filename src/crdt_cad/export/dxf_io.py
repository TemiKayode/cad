"""DXF export/import for 2D sketch paths, via ``ezdxf``.

Each path becomes one ``LWPOLYLINE`` entity on export. Import reads
``LWPOLYLINE``, ``LINE``, and legacy ``POLYLINE`` entities back into
plain point lists.
"""

from __future__ import annotations

import io

import ezdxf

Point = tuple[float, float]


def drawing_to_dxf_bytes(paths: list[dict]) -> bytes:
    doc = ezdxf.new()
    msp = doc.modelspace()
    for p in paths:
        pts = p.get("points", [])
        if len(pts) < 2:
            continue
        msp.add_lwpolyline(pts, dxfattribs={"layer": "0"})
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
