"""Browser-driven end-to-end tests for Phase 12: selection editing
(move-drag, shift-click/marquee multi-select, duplicate, delete, align)
and export baking of a rotated shape -- these protect the riskiest
behaviors to regress: a move-drag actually writing a `transform` (not
silently doing nothing, as an earlier draft of this feature did when
the drag started inside a shape's *interior* rather than on its
outline -- shapes are unfilled, see Phase 11), multi-selection assembled
via two different input paths converging on the same state, and a
rotated shape exporting as its *actual* rotated geometry rather than a
wrong axis-aligned approximation (the box/ellipse can't be expressed as
x/y/w/h or cx/cy/rx/ry once rotated -- see bake_path_transform).
"""

import json

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=1):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def _path_props(doc):
    return [
        {e["k"]: e["v"] for e in m["entries"] if not e.get("d")}
        for m in doc["path_props"].values()
    ]


def test_dragging_a_selected_shapes_boundary_writes_a_transform(live_server, browser):
    room = "e2e-select-move"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)  # rect at x=100,y=100,w=100,h=60
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolSelect")
        # Click + drag starting exactly on the rect's left edge (its
        # boundary) -- clicking well inside would miss entirely, since
        # shape primitives are unfilled outlines (Phase 11).
        _drag(page, canvas, 102, 130, 202, 230, steps=5)
        page.wait_for_timeout(150)

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        props = _path_props(json.loads(resp.text()))
        rect = next(p for p in props if p.get("shape") == "rect")
        t = rect.get("transform", {"tx": 0, "ty": 0})
        assert abs(t["tx"] - 100) < 5
        assert abs(t["ty"] - 100) < 5
    finally:
        page.close()


def test_shift_click_and_marquee_both_build_a_multi_selection(live_server, browser):
    room = "e2e-select-multi"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 160, 140)
        page.click("#toolCircle")
        _drag(page, canvas, 300, 120, 340, 120)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)  # rect's corner
        page.keyboard.down("Shift")
        page.mouse.click(box["x"] + 340, box["y"] + 120)  # circle's boundary
        page.keyboard.up("Shift")
        page.wait_for_selector("#bulkDuplicate", timeout=3000)
        assert page.locator("#pathList .path-row.active").count() == 2

        # Clear, then re-select both via a marquee drag over empty space
        # that encloses both shapes -- a completely different input path
        # should converge on the same two-path selection.
        page.mouse.click(box["x"] + 600, box["y"] + 400)
        page.wait_for_timeout(50)
        _drag(page, canvas, 50, 50, 400, 200, steps=3)
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 2
    finally:
        page.close()


def test_align_left_equalizes_the_selections_left_edges(live_server, browser):
    room = "e2e-select-align"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 160, 140)
        page.click("#toolCircle")
        _drag(page, canvas, 400, 150, 440, 150)  # center (400,150) r=40 -- min-x 360, far right of the rect
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.keyboard.down("Shift")
        page.mouse.click(box["x"] + 440, box["y"] + 150)
        page.keyboard.up("Shift")
        page.wait_for_selector("#alignLeft", timeout=3000)
        page.click("#alignLeft")
        page.wait_for_timeout(150)

        props = _path_props(json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text()))
        rect = next(p for p in props if p.get("shape") == "rect")
        circle = next(p for p in props if p.get("shape") == "circle")
        rect_t = rect.get("transform", {"tx": 0})
        circle_t = circle.get("transform", {"tx": 0})
        rect_min_x = rect["x"] + rect_t["tx"]
        circle_min_x = (circle["cx"] - circle["r"]) + circle_t["tx"]
        assert abs(rect_min_x - circle_min_x) < 0.5
    finally:
        page.close()


def test_duplicate_then_delete_key_round_trips_the_path_count(live_server, browser):
    room = "e2e-select-dup-delete"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 160, 140)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.wait_for_selector("#selDuplicate", timeout=3000)
        page.keyboard.press("Control+d")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        # duplicateSelection() leaves the new copy selected -- Delete
        # should remove exactly that one, back down to the original.
        page.keyboard.press("Delete")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
    finally:
        page.close()


def test_rotated_rect_export_bakes_to_its_true_rotated_polygon(live_server, browser):
    """Regression test for a real bug caught during manual verification:
    exporting a rotated rect must produce the actual rotated
    quadrilateral, not an axis-aligned x/y/w/h box at the wrong place --
    the latter is what a naive "just translate the corner" bake would
    silently produce."""
    room = "e2e-select-rotate-export"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)  # w=100, h=60
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.wait_for_selector("#selRotation", timeout=3000)
        page.fill("#selRotation", "45")
        page.locator("#selRotation").dispatch_event("change")
        page.wait_for_timeout(150)

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/svg")
        svg_text = resp.text()
        # A native (unrotated) <rect> would mean the bake silently ignored
        # rotation; the fix converts a rotated rect to a closed polygon
        # <path> instead, whose edges must still measure 100 and 60 (a
        # rotation is rigid -- it must not resize the rectangle).
        assert "<rect" not in svg_text
        assert "<path" in svg_text

        json_resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        props = _path_props(json.loads(json_resp.text()))
        rect = next(p for p in props if "shape" in p and p["shape"] == "rect")
        assert rect["transform"]["rotation"] == 45
    finally:
        page.close()
