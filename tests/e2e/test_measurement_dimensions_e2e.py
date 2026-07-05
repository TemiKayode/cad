"""Browser-driven end-to-end tests for Phase 13: measurement and
dimensions. Distance/Area measurement is genuinely client-only (no CRDT
op is ever sent), so a real browser run is the only way to confirm that
directly. Dimensions are persistent and shared, so these also cover the
real bug this phase's manual verification caught: moving a point via
the Constrain tool (movePathPoint) mints a *new* RGA node id, which
would silently orphan a dimension anchored to the old id -- "updates
automatically when the geometry moves" is the entire point of a
dimension, so this is the regression test for that fix
(remapDimensionAnchor), not just a happy-path check.
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


def test_measure_distance_is_read_only_and_never_sends_an_op(live_server, browser):
    room = "e2e-measure-distance"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 300, 100, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        # Any mouse movement (moving to a click target included) sends
        # throttled presence pings regardless of tool -- that's ordinary,
        # pre-existing background traffic, not something Measure adds.
        # "Read-only" specifically means no new *document* state (paths,
        # props, dimensions) gets created by measuring.
        doc_before = page.request.get(f"{live_server}/api/rooms/{room}/export/json").json()

        page.click("#toolMeasure")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 100)
        page.wait_for_timeout(150)

        panel_text = page.locator("#measurePanel").inner_text()
        assert "Distance: 200.00" in panel_text
        doc_after = page.request.get(f"{live_server}/api/rooms/{room}/export/json").json()
        assert doc_after["path_index"] == doc_before["path_index"]
        assert doc_after["path_props"] == doc_before["path_props"]
        assert doc_after["paths"] == doc_before["paths"]
        assert doc_after["dimensions"] == doc_before["dimensions"]
    finally:
        page.close()


def test_measure_area_and_perimeter_on_a_rect_shape(live_server, browser):
    room = "e2e-measure-area"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)  # w=100, h=60
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolMeasure")
        page.click("[data-mode='area']")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)  # rect's boundary corner
        page.wait_for_timeout(150)

        panel_text = page.locator("#measurePanel").inner_text()
        assert "6000.00" in panel_text  # area = 100 * 60
        assert "320.00" in panel_text  # perimeter = 2 * (100 + 60)
    finally:
        page.close()


def test_dimension_persists_and_syncs_to_a_second_tab(live_server, browser):
    room = "e2e-dimension-sync"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page_a.locator("#canvas")
        box = canvas.bounding_box()

        page_a.click("#toolPen")
        _drag(page_a, canvas, 100, 100, 300, 100, steps=5)
        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page_a.click("#toolDimension")
        page_a.mouse.click(box["x"] + 100, box["y"] + 100)
        page_a.mouse.click(box["x"] + 300, box["y"] + 100)
        page_a.wait_for_timeout(150)

        resp = page_a.request.get(f"{live_server}/api/rooms/{room}/export/json")
        doc = json.loads(resp.text())
        assert len(doc["dimensions"]["entries"]) == 1

        page_b.goto(f"{live_server}/?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function(
            "document.querySelectorAll('#dimensionList .path-row').length === 1", timeout=5000
        )
        assert "200.00" in page_b.locator("#dimensionList .path-row").inner_text()
    finally:
        page_a.close()
        page_b.close()


def test_dimension_auto_updates_after_its_anchor_point_moves_via_a_constraint(live_server, browser):
    """Regression test for a real bug caught during manual verification:
    moving a point via the Constrain tool re-inserts it under a *new*
    RGA node id (movePathPoint), which would silently orphan a
    dimension anchored to the old id -- defeating the entire point of
    referencing geometry instead of copying coordinates. Fixed by
    remapDimensionAnchor, called from movePathPoint itself."""
    room = "e2e-dimension-autoupdate"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 300, 100, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolDimension")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 100)
        page.wait_for_timeout(150)
        assert "200.00" in page.locator("#dimensionList .path-row").inner_text()

        # A third, far-away point -- Coincident pulls the dimensioned
        # anchor and this point together (to their midpoint), a real
        # move that changes the dimension's underlying node id.
        page.click("#toolPen")
        page.mouse.click(box["x"] + 100, box["y"] + 400)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolConstrain")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 100, box["y"] + 400)
        page.wait_for_selector("#constrainCoincident", timeout=3000)
        page.click("#constrainCoincident")

        # Anchor A moves from (100,100) to the midpoint (100,250); anchor
        # B is untouched at (300,100) -- new distance is exactly 250.
        page.wait_for_function(
            "document.querySelector('#dimensionList .path-row')?.innerText.includes('250.00')", timeout=5000
        )
        row_text = page.locator("#dimensionList .path-row").inner_text()
        assert "(geometry deleted)" not in row_text

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        doc = json.loads(resp.text())
        assert len(doc["dimensions"]["entries"]) == 1
    finally:
        page.close()


def test_dimension_export_contains_a_real_dxf_dimension_entity_and_svg_group(live_server, browser):
    room = "e2e-dimension-export"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 300, 100, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolDimension")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 100)
        page.wait_for_timeout(150)

        svg_resp = page.request.get(f"{live_server}/api/rooms/{room}/export/svg")
        assert 'class="dimension"' in svg_resp.text()

        dxf_resp = page.request.get(f"{live_server}/api/rooms/{room}/export/dxf")
        assert b"DIMENSION" in dxf_resp.body()
    finally:
        page.close()
