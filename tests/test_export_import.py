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
    assert paths == [{"points": [(0.0, 0.0), (5.0, 5.0)], "curves": {}}]


def test_svg_import_parses_polyline_element():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><polyline points="0,0 1,1 2,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [{"points": [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)], "curves": {}}]


def test_svg_import_parses_polygon_closes_loop():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><polygon points="0,0 1,0 1,1"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [{"points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)], "curves": {}}]


def test_svg_import_parses_path_absolute_moveto_lineto():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 L 10 0 L 10 10"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [{"points": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], "curves": {}}]


def test_svg_import_parses_path_relative_lineto():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0 0 l 10 0 l 0 10"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths == [{"points": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], "curves": {}}]


def test_svg_roundtrip_preserves_geometry():
    original = [{"points": [(1.0, 2.0), (3.0, 4.0), (5.0, 1.0)], "color": "#00ff00", "stroke_width": 1.5}]
    svg = drawing_to_svg_string(original)
    imported = drawing_from_svg_string(svg)
    assert _approx_points_equal(imported[0]["points"], original[0]["points"])


# -- SVG curve support (Phase 8) -----------------------------------------------


def test_svg_import_parses_absolute_cubic_bezier():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 C 1,1 2,1 3,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (3.0, 0.0)]
    assert paths[0]["curves"] == {1: {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)}}


def test_svg_import_parses_relative_cubic_bezier():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 c 1,1 2,1 3,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (3.0, 0.0)]
    assert paths[0]["curves"] == {1: {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)}}


def test_svg_import_parses_absolute_quadratic_bezier():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 Q 1,2 2,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (2.0, 0.0)]
    assert paths[0]["curves"] == {1: {"kind": "quad", "c": (1.0, 2.0)}}


def test_svg_import_smooth_cubic_reflects_previous_control_point():
    """S's implicit first control point is the reflection of the
    previous C/S segment's second control point around the shared
    anchor -- exactly the case real design-tool exports rely on for a
    visually smooth join between two cubic segments."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 C 1,1 2,1 3,0 S 5,-1 6,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (3.0, 0.0), (6.0, 0.0)]
    # reflection of (2,1) around the shared anchor (3,0) is (4,-1)
    assert paths[0]["curves"][2] == {"kind": "cubic", "c1": (4.0, -1.0), "c2": (5.0, -1.0)}


def test_svg_import_smooth_cubic_without_preceding_curve_uses_current_point():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 S 1,1 2,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["curves"][1] == {"kind": "cubic", "c1": (0.0, 0.0), "c2": (1.0, 1.0)}


def test_svg_import_smooth_quadratic_reflects_previous_control_point():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 Q 1,2 2,0 T 4,0"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (2.0, 0.0), (4.0, 0.0)]
    # reflection of (1,2) around the shared anchor (2,0) is (3,-2)
    assert paths[0]["curves"][2] == {"kind": "quad", "c": (3.0, -2.0)}


def test_svg_import_stops_at_arc_command_instead_of_corrupting_the_rest():
    """A/a aren't parsed (see module docstring) -- the points/curves
    accumulated before the arc are kept, and nothing after it is
    (mis)parsed as coordinates."""
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M 0,0 L 1,1 A 5,5 0 0,1 10,10 L 99,99"/></svg>'
    paths = drawing_from_svg_string(svg)
    assert paths[0]["points"] == [(0.0, 0.0), (1.0, 1.0)]


def test_svg_curve_roundtrips_through_export_and_reimport():
    """`drawing_to_svg_string` takes `path_list()`-shaped input, where a
    curve's payload lives at the *flat* `curve_prop_key(node_id)` key
    (path_props are spread into the top-level dict) -- not the index-keyed
    `"curves"` shape `add_path`/`drawing_from_svg_string` use, which only
    exists as a construction-time convenience before stable node ids are
    assigned. See test_document.py for a test spanning both shapes via
    the real DrawingDocument/add_path/path_list pipeline."""
    original = [
        {
            "points": [(0.0, 0.0), (3.0, 0.0)],
            "point_ids": ["n0", "n1"],
            "curve:n1": {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)},
        }
    ]
    svg = drawing_to_svg_string(original)
    assert "C 1.00,1.00 2.00,1.00 3.00,0.00" in svg
    imported = drawing_from_svg_string(svg)
    assert imported[0]["points"] == [(0.0, 0.0), (3.0, 0.0)]
    assert imported[0]["curves"] == {1: {"kind": "cubic", "c1": (1.0, 1.0), "c2": (2.0, 1.0)}}


def test_svg_export_without_point_ids_still_emits_plain_lines():
    """A hand-built dict with no `point_ids` (every pre-Phase-8 caller)
    must render exactly as before -- no curve lookup is even attempted."""
    paths = [{"points": [(0.0, 0.0), (10.0, 10.0)], "color": "#ff0000", "stroke_width": 2.0}]
    svg = drawing_to_svg_string(paths)
    assert "M 0.00,0.00 L 10.00,10.00" in svg


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


def test_dxf_export_flattens_curve_segments_into_a_denser_polyline():
    """DXF's LWPOLYLINE has no Bezier concept -- a curve segment gets
    sampled into 12 intermediate points (see flatten_path_to_polyline)
    rather than being reduced to a straight line between its two
    anchors, which would look visibly wrong for anything but a very
    gentle curve."""
    original = [
        {
            "points": [(0.0, 0.0), (10.0, 0.0)],
            "point_ids": ["n0", "n1"],
            "curve:n1": {"kind": "quad", "c": (5.0, 10.0)},
        }
    ]
    data = drawing_to_dxf_bytes(original)
    imported = drawing_from_dxf_bytes(data)
    assert len(imported) == 1
    assert len(imported[0]) == 13  # 1 start anchor + 12 sampled points
    # the midpoint of a quadratic through (0,0)-(5,10)-(10,0) bulges well
    # above a straight line's y=0 -- confirms real sampling happened, not
    # just a pass-through of the two anchors.
    mid = imported[0][6]
    assert mid[1] > 3.0


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
