"""Browser-driven end-to-end tests for Phase 11: shape primitives
(drag-to-create for each kind, hit-testing against their real boundary,
standalone numeric-input creation) and document units (grid step +
cursor readout + export scaling) -- these are genuinely client-side
concerns (shape rendering/hit-testing math, unit conversion) that only
a real browser run can verify.
"""

import json

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1)
    page.mouse.up()


def test_all_five_shape_kinds_can_be_drag_created(live_server, browser):
    room = "e2e-shapes-create"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolLine")
        _drag(page, canvas, 100, 100, 200, 150)
        page.click("#toolRect")
        _drag(page, canvas, 250, 100, 350, 180)
        page.click("#toolCircle")
        _drag(page, canvas, 450, 150, 500, 150)
        page.click("#toolEllipse")
        _drag(page, canvas, 550, 150, 600, 190)
        page.click("#toolArc")
        _drag(page, canvas, 650, 150, 700, 150)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 5", timeout=5000)

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        doc = json.loads(resp.text())
        kinds = set()
        for _pid, props in doc["path_props"].items():
            entries = {e["k"]: e["v"] for e in props["entries"] if not e.get("d")}
            if "shape" in entries:
                kinds.add(entries["shape"])
        assert kinds == {"line", "rect", "circle", "ellipse", "arc"}
    finally:
        page.close()


def test_select_tool_hit_tests_a_circles_actual_boundary(live_server, browser):
    """Shapes are unfilled outlines -- clicking well inside a circle
    must NOT select it, only clicking near its boundary stroke should,
    same as hit-testing any other unfilled path."""
    room = "e2e-shapes-hittest"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolCircle")
        _drag(page, canvas, 450, 150, 500, 150)  # center (450,150), radius 50
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 475, box["y"] + 150)  # well inside, not on the boundary
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 0

        page.mouse.click(box["x"] + 500, box["y"] + 150)  # exactly on the boundary
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 1
    finally:
        page.close()


def test_numeric_panel_creates_a_shape_without_any_drag(live_server, browser):
    room = "e2e-shapes-numeric"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toolRect")
        page.wait_for_selector("#shapeInputPanel input", timeout=3000)
        inputs = page.locator("#shapeInputPanel input.shapeField")
        inputs.nth(0).fill("123.4")
        inputs.nth(1).fill("56.7")
        page.click("#shapeCommitBtn")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        doc = json.loads(resp.text())
        entries = {}
        for _pid, props in doc["path_props"].items():
            entries = {e["k"]: e["v"] for e in props["entries"] if not e.get("d")}
        assert entries.get("shape") == "rect"
        assert abs(entries["w"] - 123.4) < 0.5
        assert abs(entries["h"] - 56.7) < 0.5
    finally:
        page.close()


def test_document_units_affect_cursor_readout_and_are_shared_across_tabs(live_server, browser):
    room = "e2e-shapes-units"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/?room={room}")
        page_b.goto(f"{live_server}/?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.select_option("#unitsSelect", "mm")
        canvas_a = page_a.locator("#canvas")
        box_a = canvas_a.bounding_box()
        page_a.mouse.move(box_a["x"] + 300, box_a["y"] + 300)
        page_a.wait_for_timeout(100)
        assert "mm" in page_a.locator("#cursorCoords").inner_text()

        # The units *setting* is a real document setting (Phase 11), so it
        # must sync to tab B like any other CRDT-backed prop -- not local
        # UI-only state the way pan/zoom (Phase 10) deliberately is.
        page_b.wait_for_function("document.getElementById('unitsSelect').value === 'mm'", timeout=5000)
    finally:
        page_a.close()
        page_b.close()
