from crdt_cad.export.dxf_io import drawing_from_dxf_bytes, drawing_to_dxf_bytes
from crdt_cad.export.stl_export import mesh_to_stl
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string


def _approx_points_equal(a, b, tol=0.01):
    if len(a) != len(b):
        return False
    return all(abs(x1 - x2) < tol and abs(y1 - y2) < tol for (x1, y1), (x2, y2) in zip(a, b))


# -- SVG ----------------------------------------------------------------------


def test_svg_export_contains_viewbox_and_path_data():
    paths = [{"points": [(0.0, 0.0), (10.0, 10.0)], "color": "#ff0000", "stroke_width": 2.0}]
    svg = drawing_to_svg_string(paths)
    assert "<svg" in svg
    assert "viewBox" in svg
    assert 'stroke="#ff0000"' in svg
    assert "M 0.00,0.00 L 10.00,10.00" in svg


def test_svg_export_empty_paths_still_produces_valid_document():
    svg = drawing_to_svg_string([])
    assert "<svg" in svg and "</svg>" in svg


def test_svg_import_parses_line_element():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="5" y2="5"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [[(0.0, 0.0), (5.0, 5.0)]]


def test_svg_import_parses_polyline_element():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><polyline points="0,0 1,1 2,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [[(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)]]


def test_svg_import_parses_polygon_closes_loop():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><polygon points="0,0 1,0 1,1"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [[(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]]


def test_svg_import_parses_path_absolute_moveto_lineto():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 L 10 0 L 10 10"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]]


def test_svg_import_parses_path_relative_lineto():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 l 10 0 l 0 10"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]]


def test_svg_roundtrip_preserves_geometry():
    original = [{"points": [(1.0, 2.0), (3.0, 4.0), (5.0, 1.0)], "color": "#00ff00", "stroke_width": 1.5}]
    svg = drawing_to_svg_string(original)
    imported = drawing_from_svg_string(svg)
    assert _approx_points_equal(imported[0], original[0]["points"])


# -- DXF ------------------------------------------------------------------------


def test_dxf_export_produces_readable_bytes():
    paths = [{"points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]}]
    data = drawing_to_dxf_bytes(paths)
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_dxf_roundtrip_lwpolyline():
    original = [{"points": [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)]}]
    data = drawing_to_dxf_bytes(original)
    imported = drawing_from_dxf_bytes(data)
    assert len(imported) == 1
    assert _approx_points_equal(imported[0], original[0]["points"])


def test_dxf_roundtrip_multiple_paths():
    original = [
        {"points": [(0.0, 0.0), (1.0, 1.0)]},
        {"points": [(2.0, 2.0), (3.0, 3.0), (4.0, 2.0)]},
    ]
    data = drawing_to_dxf_bytes(original)
    imported = drawing_from_dxf_bytes(data)
    assert len(imported) == 2


# -- STL --------------------------------------------------------------------------


def test_stl_export_triangle_produces_one_facet():
    positions = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)}
    faces = {"f1": ["a", "b", "c"]}
    stl = mesh_to_stl(positions, faces)
    assert stl.startswith("solid ")
    assert stl.strip().endswith("endsolid crdt_cad_mesh")
    assert stl.count("facet normal") == 1
    assert stl.count("vertex") == 3
    assert "1.000000 0.000000 0.000000" in stl


def test_stl_export_quad_produces_two_facets():
    positions = {
        "a": (0.0, 0.0, 0.0),
        "b": (1.0, 0.0, 0.0),
        "c": (1.0, 1.0, 0.0),
        "d": (0.0, 1.0, 0.0),
    }
    faces = {"f1": ["a", "b", "c", "d"]}
    stl = mesh_to_stl(positions, faces)
    assert stl.count("facet normal") == 2
    assert stl.count("endfacet") == 2


def test_stl_export_skips_degenerate_faces_with_missing_vertices():
    positions = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0)}
    faces = {"f1": ["a", "b", "missing"]}
    stl = mesh_to_stl(positions, faces)
    assert stl.count("facet normal") == 0
