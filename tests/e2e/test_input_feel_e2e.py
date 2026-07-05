"""Browser-driven end-to-end tests for Part 3 Phase D3: per-tool
cursors and the hover halo (Select tool feedback for what a click would
select, distinct from the existing selection glow). Pure client-side
canvas/cursor behavior with no server counterpart -- these need a real
browser to read `getComputedStyle(...).cursor` and internal hover state,
neither of which a unit test can observe.
"""

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=3):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_2d_cursor_follows_tool_and_hover_state(live_server, browser):
    room = "e2e-input-feel-cursor"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # Pen (the default tool) draws fresh geometry -- crosshair.
        assert page.eval_on_selector("#canvas", "el => getComputedStyle(el).cursor") == "crosshair"

        canvas = page.locator("#canvas")
        _drag(page, canvas, 150, 150, 300, 250)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        # Select tool: default arrow when idle...
        page.click("#toolSelect")
        assert page.eval_on_selector("#canvas", "el => getComputedStyle(el).cursor") == "default"

        # ...but "move" while hovering a hit-testable path (drawn as a
        # freehand stroke, so its own boundary is what was drawn).
        box = canvas.bounding_box()
        page.mouse.move(box["x"] + 150, box["y"] + 150)
        page.wait_for_timeout(150)
        assert page.eval_on_selector("#canvas", "el => getComputedStyle(el).cursor") == "move"

        # moving away from any geometry (but still within the canvas --
        # a coordinate exceeding the canvas's own rendered height would
        # move the pointer off it entirely, onto whatever's behind it,
        # and this element's pointermove would then correctly never
        # fire at all) reverts to the default arrow.
        assert box["height"] > 550, "test assumes empty space fits within the canvas bounds"
        page.mouse.move(box["x"] + 600, box["y"] + 500)
        page.wait_for_timeout(150)
        assert page.eval_on_selector("#canvas", "el => getComputedStyle(el).cursor") == "default"

        # holding Space shows the pan "grab" cursor regardless of tool.
        page.keyboard.down("Space")
        page.wait_for_timeout(100)
        assert page.eval_on_selector("#canvas", "el => getComputedStyle(el).cursor") == "grab"
        page.keyboard.up("Space")
    finally:
        page.close()


def test_hover_halo_is_distinct_from_selection_and_does_not_apply_to_selected_shape(live_server, browser):
    """A selected shape already gets a stronger glow; hovering a
    *different*, unselected shape should also mark it as hovered
    (confirmed via the internal hoveredPathId, since the halo itself is
    canvas pixels a DOM assertion can't read directly) without touching
    the current selection."""
    room = "e2e-input-feel-hover"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)
        page.click("#toolCircle")
        _drag(page, canvas, 400, 130, 440, 130)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolSelect")
        page.mouse.click(box["x"] + 100, box["y"] + 100)  # select the rect
        page.wait_for_timeout(100)
        selected_count = page.evaluate("ui.selectedPaths.size")
        assert selected_count == 1

        page.mouse.move(box["x"] + 440, box["y"] + 130)  # hover the circle
        page.wait_for_timeout(150)
        hovered_id = page.evaluate("hoveredPathId")
        assert hovered_id is not None

        # the selection is untouched by hovering a different shape.
        assert page.evaluate("ui.selectedPaths.size") == 1
        selected_id = page.evaluate("[...ui.selectedPaths][0]")
        assert hovered_id != selected_id
    finally:
        page.close()


def test_3d_cursor_follows_tool(live_server, browser):
    room = "e2e-input-feel-3d"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # Vertex (the default tool) places fresh geometry -- crosshair.
        assert page.eval_on_selector("#canvas3d", "el => getComputedStyle(el).cursor") == "crosshair"

        page.click("#toolMove")
        assert page.eval_on_selector("#canvas3d", "el => getComputedStyle(el).cursor") == "default"

        page.click("#toolBox")
        assert page.eval_on_selector("#canvas3d", "el => getComputedStyle(el).cursor") == "crosshair"
    finally:
        page.close()
