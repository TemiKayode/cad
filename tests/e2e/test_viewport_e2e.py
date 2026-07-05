"""Browser-driven end-to-end tests for Phase 10's 2D viewport (pan/zoom/
grid/snap): the view transform is entirely client-local state (never
synced, per the brief), so the only way to verify it actually works is
through a real browser -- these protect the two riskiest behaviors to
regress: a remote presence cursor projecting correctly through a
*different, asymmetric* view transform on the receiving tab, and
snap-to-grid actually producing grid-aligned stored points (not just a
UI toggle that does nothing).
"""

import json

import pytest

pytestmark = pytest.mark.e2e


def test_default_view_is_backward_compatible_and_zoom_updates_on_wheel(live_server, browser):
    room = "e2e-viewport-zoom"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        assert page.locator("#zoomIndicator").inner_text() == "100%"

        canvas = page.locator("#canvas")
        box = canvas.bounding_box()
        page.mouse.move(box["x"] + 300, box["y"] + 300)
        page.mouse.wheel(0, -500)
        page.wait_for_timeout(100)
        assert page.locator("#zoomIndicator").inner_text() != "100%"
    finally:
        page.close()


def test_presence_cursor_renders_correctly_through_an_asymmetric_view_transform(live_server, browser):
    """Tab A zooms/pans away from the identity view; tab B's mouse moves
    to a known screen point at B's own (identity) view, so its world
    coordinates equal its screen coordinates. Tab A's rendered
    cursor-label for B must land at *A's own* worldToScreen projection
    of that point, not simply B's raw screen position -- the whole
    point of presence positions being stored in world coordinates
    (Phase 10) rather than screen pixels."""
    room = "e2e-viewport-presence"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_b.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas_a = page_a.locator("#canvas")
        box_a = canvas_a.bounding_box()
        page_a.mouse.move(box_a["x"] + 300, box_a["y"] + 300)
        page_a.mouse.wheel(0, -800)
        page_a.wait_for_timeout(100)
        assert page_a.locator("#zoomIndicator").inner_text() != "100%"

        canvas_b = page_b.locator("#canvas")
        box_b = canvas_b.bounding_box()
        world_x, world_y = 400, 250
        page_b.mouse.move(box_b["x"] + world_x, box_b["y"] + world_y)
        page_a.wait_for_function(
            "document.querySelectorAll('.cursor-label').length === 1", timeout=5000
        )

        expected = page_a.evaluate(f"worldToScreen({world_x}, {world_y})")
        actual_left = page_a.eval_on_selector(".cursor-label", "el => parseFloat(el.style.left)")
        actual_top = page_a.eval_on_selector(".cursor-label", "el => parseFloat(el.style.top)")
        assert abs(expected[0] - actual_left) < 1.0
        assert abs(expected[1] - actual_top) < 1.0
    finally:
        page_a.close()
        page_b.close()


def test_snap_to_grid_produces_grid_aligned_stored_points(live_server, browser):
    room = "e2e-viewport-snap"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#snapToggleBtn")
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()
        # A deliberately non-grid-aligned drag at the default (identity)
        # view -- world coords equal screen coords there.
        page.mouse.move(box["x"] + 137, box["y"] + 91)
        page.mouse.down()
        page.mouse.move(box["x"] + 260, box["y"] + 91)
        page.mouse.up()
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
        doc = json.loads(resp.text())
        points = [
            node["v"]
            for rga in doc["paths"].values()
            for node in rga["nodes"]
            if not node.get("db")
        ]
        assert points
        grid_step = page.evaluate("pickGridStep(1)")
        for x, y in points:
            assert x % grid_step < 1e-6 or grid_step - (x % grid_step) < 1e-6
            assert y % grid_step < 1e-6 or grid_step - (y % grid_step) < 1e-6
    finally:
        page.close()
