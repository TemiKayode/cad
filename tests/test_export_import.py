import io

import ezdxf
import pytest

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


# -- Shape primitives (Phase 11) -----------------------------------------------


def test_svg_export_line_shape_emits_a_native_line_element():
    paths = [{"shape": "line", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 20.0, "color": "#4dabf7"}]
    svg = drawing_to_svg_string(paths)
    assert '<line x1="0.000" y1="0.000" x2="10.000" y2="20.000"' in svg
    assert 'stroke="#4dabf7"' in svg


def test_svg_export_rect_shape_emits_a_native_rect_element():
    paths = [{"shape": "rect", "x": 5.0, "y": 5.0, "w": 40.0, "h": 20.0}]
    svg = drawing_to_svg_string(paths)
    assert '<rect x="5.000" y="5.000" width="40.000" height="20.000"' in svg


def test_svg_export_circle_shape_emits_a_native_circle_element():
    paths = [{"shape": "circle", "cx": 50.0, "cy": 50.0, "r": 25.0}]
    svg = drawing_to_svg_string(paths)
    assert '<circle cx="50.000" cy="50.000" r="25.000"' in svg


def test_svg_export_ellipse_shape_emits_a_native_ellipse_element():
    paths = [{"shape": "ellipse", "cx": 10.0, "cy": 20.0, "rx": 30.0, "ry": 15.0}]
    svg = drawing_to_svg_string(paths)
    assert '<ellipse cx="10.000" cy="20.000" rx="30.000" ry="15.000"' in svg


def test_svg_export_arc_shape_emits_an_elliptical_arc_path_command():
    paths = [{"shape": "arc", "cx": 0.0, "cy": 0.0, "r": 10.0, "start_angle": 0.0, "end_angle": 90.0}]
    svg = drawing_to_svg_string(paths)
    assert " A 10.000,10.000 0 0 1 " in svg


def test_svg_export_arc_shape_uses_large_arc_flag_for_sweeps_over_180_degrees():
    paths = [{"shape": "arc", "cx": 0.0, "cy": 0.0, "r": 10.0, "start_angle": 0.0, "end_angle": 270.0}]
    svg = drawing_to_svg_string(paths)
    assert " A 10.000,10.000 0 1 1 " in svg


# -- Document units (Phase 11) --------------------------------------------------


def test_svg_export_scales_coordinates_and_labels_units_for_mm():
    paths = [{"points": [(0.0, 0.0), (96.0, 96.0)], "color": "#fff", "stroke_width": 1.0}]
    svg = drawing_to_svg_string(paths, units="mm")
    # 96px == 1in == 25.4mm at the assumed 96px/in convention -- the path
    # data itself (unaffected by the viewBox's own padding) shows this
    # directly.
    assert "M 0.00,0.00 L 25.40,25.40" in svg
    assert 'width="' in svg and 'mm"' in svg


def test_svg_export_scales_shape_primitives_for_units_too():
    paths = [{"shape": "circle", "cx": 0.0, "cy": 0.0, "r": 96.0}]
    svg = drawing_to_svg_string(paths, units="in")
    assert '<circle cx="0.000" cy="0.000" r="1.000"' in svg


def test_svg_export_default_units_is_px_and_unchanged_scale():
    paths = [{"points": [(0.0, 0.0), (10.0, 10.0)], "color": "#ff0000", "stroke_width": 2.0}]
    assert drawing_to_svg_string(paths) == drawing_to_svg_string(paths, units="px")


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


def test_dxf_export_shape_primitives_as_native_entities():
    """Each shape kind (Phase 11) becomes its own native DXF entity type,
    not a flattened LWPOLYLINE -- confirmed by reading dxftype() back
    directly via ezdxf, not just checking the file parses."""
    paths = [
        {"shape": "line", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 10.0},
        {"shape": "rect", "x": 0.0, "y": 0.0, "w": 5.0, "h": 5.0},
        {"shape": "circle", "cx": 0.0, "cy": 0.0, "r": 3.0},
        {"shape": "ellipse", "cx": 0.0, "cy": 0.0, "rx": 4.0, "ry": 2.0},
        {"shape": "arc", "cx": 0.0, "cy": 0.0, "r": 3.0, "start_angle": 0.0, "end_angle": 90.0},
    ]
    data = drawing_to_dxf_bytes(paths)
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    kinds = [e.dxftype() for e in doc.modelspace()]
    assert kinds == ["LINE", "LWPOLYLINE", "CIRCLE", "ELLIPSE", "ARC"]


def test_dxf_export_sets_insunits_header_for_the_chosen_unit():
    for units, expected_code in (("px", 0), ("mm", 4), ("in", 1)):
        data = drawing_to_dxf_bytes([{"points": [(0.0, 0.0), (1.0, 1.0)]}], units=units)
        doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
        assert doc.header["$INSUNITS"] == expected_code


def test_dxf_export_scales_coordinates_for_units():
    data = drawing_to_dxf_bytes([{"shape": "circle", "cx": 0.0, "cy": 0.0, "r": 96.0}], units="in")
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    circle = next(iter(doc.modelspace()))
    assert circle.dxf.radius == pytest.approx(1.0)


# -- Dimension annotations (Phase 13) ---------------------------------------------


def test_svg_export_renders_a_resolved_dimension_as_a_line_and_text_group():
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (100.0, 0.0), "offset": 30.0}]
    svg = drawing_to_svg_string([], dimensions=dims)
    assert 'class="dimension"' in svg
    assert ">100.00<" in svg  # the measured distance, as the label text


def test_svg_export_skips_a_dimension_whose_anchor_is_unresolved():
    """A dimension whose anchor point was concurrently deleted has no
    `a_pos`/`b_pos` (see DrawingDocument.resolve_dimension_points) --
    export must skip it silently, not raise or emit a broken group."""
    dims = [{"a_path": "p", "a_node": [1, "a"], "b_path": "p", "b_node": [2, "a"], "offset": 30.0}]
    svg = drawing_to_svg_string([], dimensions=dims)
    assert "dimension" not in svg


def test_svg_export_dimension_label_scales_with_units():
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (96.0, 0.0), "offset": 30.0}]
    svg = drawing_to_svg_string([], dimensions=dims, units="mm")
    assert ">25.40mm<" in svg


def test_dxf_export_dimension_produces_a_real_dimension_entity():
    """Confirmed by reading the value back via ezdxf's own
    get_measurement(), not just checking a DIMENSION tag is present
    somewhere in the file."""
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (100.0, 0.0), "offset": 30.0}]
    data = drawing_to_dxf_bytes([], dimensions=dims)
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    dim_entities = [e for e in doc.modelspace() if e.dxftype() == "DIMENSION"]
    assert len(dim_entities) == 1
    assert dim_entities[0].get_measurement() == pytest.approx(100.0)


def test_dxf_export_skips_an_unresolved_dimension():
    dims = [{"a_path": "p", "a_node": [1, "a"], "b_path": "p", "b_node": [2, "a"], "offset": 30.0}]
    data = drawing_to_dxf_bytes([], dimensions=dims)
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    assert list(doc.modelspace()) == []


def test_dxf_export_dimension_scales_with_units():
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (96.0, 0.0), "offset": 30.0}]
    data = drawing_to_dxf_bytes([], dimensions=dims, units="in")
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    dim_entities = [e for e in doc.modelspace() if e.dxftype() == "DIMENSION"]
    assert dim_entities[0].get_measurement() == pytest.approx(1.0)


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
