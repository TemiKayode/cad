"""Import/export plugins.

Implemented: SVG (export + a pragmatic straight-segment-only import),
DXF (export + import, via ``ezdxf``), STL (export, ASCII).

Not implemented: STEP/IGES. Both require a real B-Rep kernel
(``pythonOCC``), which has no usable PyPI wheel (conda-only in
practice) and no B-Rep representation exists yet in ``MeshCRDT`` for a
STEP writer to target in the first place -- see the README roadmap.
"""

from crdt_cad.export.dxf_io import drawing_from_dxf_bytes, drawing_to_dxf_bytes
from crdt_cad.export.stl_export import mesh_to_stl
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string

__all__ = [
    "drawing_to_svg_string",
    "drawing_from_svg_string",
    "drawing_to_dxf_bytes",
    "drawing_from_dxf_bytes",
    "mesh_to_stl",
]
