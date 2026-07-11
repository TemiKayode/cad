import re

import pytest

from crdt_cad.export.pdf_io import (
    SHEET_DEFAULTS,
    _drawing_bounds,
    _hex_to_rgb01,
    _quad_to_cubic,
    _Transform,
    sheet_to_pdf_bytes,
)


def _mediabox(data: bytes) -> tuple[float, float, float, float]:
    m = re.search(rb"/MediaBox \[([^\]]+)\]", data)
    assert m is not None
    return tuple(float(x) for x in m.group(1).split())


# -- pure geometry helpers ----------------------------------------------------


def test_quad_to_cubic_exact_degree_elevation():
    """The standard exact quad->cubic elevation formula, same one
    ezdxf.math.quadratic_to_cubic_bezier uses (verified in Part 7 C2) --
    for (0,0)-(5,10)-(10,0), interior controls sit 2/3 of the way from
    each anchor to the quadratic's own control point."""
    c1, c2 = _quad_to_cubic((0.0, 0.0), (5.0, 10.0), (10.0, 0.0))
    assert c1 == pytest.approx((10.0 / 3, 20.0 / 3))
    assert c2 == pytest.approx((20.0 / 3, 20.0 / 3))


def test_hex_to_rgb01_full_and_shorthand():
    assert _hex_to_rgb01("#ff0000") == pytest.approx((1.0, 0.0, 0.0))
    assert _hex_to_rgb01("#000000") == pytest.approx((0.0, 0.0, 0.0))
    assert _hex_to_rgb01("#0f0") == pytest.approx((0.0, 1.0, 0.0))


def test_drawing_bounds_covers_points_shapes_and_dimensions():
    paths = [
        {"points": [(0.0, 0.0), (10.0, 5.0)]},
        {"shape": "circle", "cx": 50.0, "cy": 50.0, "r": 10.0},
    ]
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (100.0, 0.0), "offset": 30.0}]
    bounds = _drawing_bounds(paths, dims)
    # circle spans x:[40,60] y:[40,60]; dimension's offset line spans y up
    # to 30 at x in [0,100] -- overall bounds must cover all three sources.
    assert bounds[0] <= 0.0
    assert bounds[2] >= 100.0
    assert bounds[3] >= 60.0


def test_drawing_bounds_degenerates_to_a_fixed_box_when_nothing_to_draw():
    assert _drawing_bounds([], []) == (0.0, 0.0, 100.0, 100.0)


def test_transform_fit_mode_centers_and_scales_uniformly():
    # a 200x100 bbox fit into a 400x400 area: width-constrained (scale 2),
    # so it should be vertically centered with padding top and bottom.
    tf = _Transform((0.0, 0.0, 200.0, 100.0), (0.0, 0.0, 400.0, 400.0), scale_pt_per_px=2.0, centered=True)
    x0, y0 = tf((0.0, 0.0))
    x1, y1 = tf((200.0, 100.0))
    assert x1 - x0 == pytest.approx(400.0)  # fills width exactly
    assert abs(y0 - y1) == pytest.approx(200.0)  # scaled height
    # vertical padding above and below should be equal (centered) -- y0 is
    # doc-space y=0 (the bbox's top edge), which maps to the *larger* pdf
    # y-coordinate since pdf space is y-up while doc space is y-down.
    pad_below = min(y0, y1) - 0.0
    pad_above = 400.0 - max(y0, y1)
    assert pad_below == pytest.approx(pad_above)
    assert pad_below == pytest.approx(100.0)


def test_transform_custom_mode_anchors_top_left_no_centering():
    tf = _Transform((0.0, 0.0, 50.0, 50.0), (10.0, 10.0, 300.0, 300.0), scale_pt_per_px=1.0, centered=False)
    x0, y0 = tf((0.0, 0.0))
    # top-left of the bbox lands exactly at the area's top-left corner
    # (area top = y0_area + height = 10 + 300 = 310).
    assert x0 == pytest.approx(10.0)
    assert y0 == pytest.approx(310.0)


# -- sheet_to_pdf_bytes ---------------------------------------------------


def test_sheet_to_pdf_bytes_produces_readable_pdf():
    paths = [{"points": [(0.0, 0.0), (100.0, 0.0), (100.0, 80.0)], "point_ids": ["a", "b", "c"], "color": "#ff0000"}]
    data = sheet_to_pdf_bytes(paths, {"id": "s1", "name": "Sheet 1"})
    assert data[:4] == b"%PDF"
    assert len(data) > 500


def test_sheet_to_pdf_bytes_handles_an_empty_drawing():
    data = sheet_to_pdf_bytes([], {"id": "s1", "name": "Empty"})
    assert data[:4] == b"%PDF"


@pytest.mark.parametrize(
    "page_size,orientation",
    [("a4", "landscape"), ("a4", "portrait"), ("a3", "landscape"), ("letter", "portrait"), ("tabloid", "landscape")],
)
def test_sheet_to_pdf_bytes_page_size_matches_orientation(page_size, orientation):
    data = sheet_to_pdf_bytes([], {"id": "s1", "name": "S", "page_size": page_size, "orientation": orientation})
    x0, y0, x1, y1 = _mediabox(data)
    w, h = x1 - x0, y1 - y0
    if orientation == "landscape":
        assert w > h
    else:
        assert h > w


def test_sheet_to_pdf_bytes_unknown_page_size_falls_back_to_a4():
    data = sheet_to_pdf_bytes([], {"id": "s1", "name": "S", "page_size": "poster", "orientation": "portrait"})
    x0, y0, x1, y1 = _mediabox(data)
    assert (x1 - x0) == pytest.approx(595.2755905511812, abs=0.01)


def test_sheet_to_pdf_bytes_renders_curve_segments_shapes_and_dimensions_without_error():
    paths = [
        {
            "points": [(0.0, 0.0), (100.0, 0.0)],
            "point_ids": ["n0", "n1"],
            "color": "#ff00ff",
            "stroke_width": 2.0,
            "curve:n1": {"kind": "quad", "c": (50.0, 40.0)},
        },
        {"shape": "rect", "x": 10.0, "y": 10.0, "w": 30.0, "h": 20.0, "color": "#111111", "fill": "#00ff00", "fill_opacity": 0.5},
        {"shape": "circle", "cx": 60.0, "cy": 60.0, "r": 15.0, "color": "#0000ff"},
        {"shape": "ellipse", "cx": 80.0, "cy": 30.0, "rx": 10.0, "ry": 5.0, "color": "#0000ff"},
        {"shape": "arc", "cx": 20.0, "cy": 20.0, "r": 10.0, "start_angle": 0, "end_angle": 180, "color": "#111111"},
        {"shape": "text", "x": 5.0, "y": 5.0, "content": "Label", "font_size": 12, "color": "#111111"},
        {"points": [(0.0, 0.0), (10.0, 10.0)], "point_ids": ["m0", "m1"], "color": "#111111", "dash": "dashed"},
        {"points": [(0.0, 0.0), (10.0, 10.0)], "point_ids": ["p0", "p1"], "color": "#111111", "dash": "dotted"},
    ]
    dims = [{"a_pos": (0.0, 0.0), "b_pos": (100.0, 0.0), "offset": 20.0}]
    data = sheet_to_pdf_bytes(paths, {"id": "s1", "name": "S"}, dimensions=dims, layer_order=["layerA"])
    assert data[:4] == b"%PDF"


def test_sheet_to_pdf_bytes_custom_scale_mode_does_not_error_even_when_oversized():
    paths = [{"points": [(0.0, 0.0), (5000.0, 5000.0)], "point_ids": ["a", "b"], "color": "#111111"}]
    data = sheet_to_pdf_bytes(paths, {"id": "s1", "name": "S", "scale_mode": "custom", "scale_ratio": 5.0})
    assert data[:4] == b"%PDF"


def test_sheet_to_pdf_bytes_applies_defaults_for_missing_fields():
    # `sheet` normally comes from DrawingDocument.sheet_list(), which may
    # only have "id"/"name" set (see add_sheet's docstring) -- every other
    # field must come from SHEET_DEFAULTS without raising a KeyError.
    assert set(SHEET_DEFAULTS) >= {"page_size", "orientation", "margin_mm", "scale_mode", "scale_ratio", "title", "drawn_by", "date", "drawing_number", "revision", "notes"}
    data = sheet_to_pdf_bytes([], {"id": "s1", "name": "Bare Sheet"})
    assert data[:4] == b"%PDF"
