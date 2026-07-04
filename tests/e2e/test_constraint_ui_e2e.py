"""Browser-driven end-to-end test for Phase 9's interactive constraint
UI (2D demo): select two points with the Constrain tool, apply
Coincident, and confirm the two points actually converge to the same
position via the server's own JSON export -- not just a client-side
visual effect, and visible to a second tab in the same room.
"""

import json
import math

import pytest

pytestmark = pytest.mark.e2e


def _draw_line(page, canvas_selector, x0, y0, x1, y1):
    canvas = page.locator(canvas_selector)
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    # A single move (default steps=1) -> exactly one appended point, so
    # each line is exactly {start, end} with no extra midpoint that would
    # make "the adjacent point" ambiguous for this test.
    page.mouse.move(box["x"] + x1, box["y"] + y1)
    page.mouse.up()


def test_coincident_constraint_converges_points_and_syncs_to_a_second_tab(live_server, browser):
    room = "e2e-constraint-coincident"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/?room={room}")
        page_b.goto(f"{live_server}/?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        _draw_line(page_a, "#canvas", 100, 100, 200, 100)
        _draw_line(page_a, "#canvas", 120, 250, 260, 300)
        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=10000)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=10000)

        page_a.click("#toolConstrain")
        canvas = page_a.locator("#canvas")
        box = canvas.bounding_box()
        page_a.mouse.click(box["x"] + 200, box["y"] + 100)
        page_a.mouse.click(box["x"] + 260, box["y"] + 300)
        page_a.wait_for_selector("#constrainCoincident", timeout=5000)
        page_a.click("#constrainCoincident")
        page_a.wait_for_function(
            "document.querySelectorAll('#constraintPanel .empty-hint').length === 1", timeout=5000
        )

        for page in (page_a, page_b):
            page.wait_for_timeout(500)
            resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
            doc = json.loads(resp.text())
            all_points = [
                node["v"]
                for rga in doc["paths"].values()
                for node in rga["nodes"]
                if not node.get("db")
            ]
            closest = min(
                math.hypot(all_points[i][0] - all_points[j][0], all_points[i][1] - all_points[j][1])
                for i in range(len(all_points))
                for j in range(i + 1, len(all_points))
            )
            assert closest < 0.01
    finally:
        page_a.close()
        page_b.close()
