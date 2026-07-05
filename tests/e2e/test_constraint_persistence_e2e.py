"""Browser-driven end-to-end tests for Phase 14: interactive constraint
UI persistence, undo, tangent, and re-solve-on-drag -- extending
Phase 9's basic (apply-once, non-persistent) constraint UI. These cover
exactly the parts a real browser run is needed for: a constraint
surviving as real document state (not just a one-time visual effect), a
constraint-driven point move being undoable via the existing
inverted-op machinery, tangent needing a genuinely different picking
mechanism (a circle shape has no RGA point of its own), and dragging an
already-constrained point re-solving automatically on release without
creating duplicate persisted constraints.
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


def test_applying_a_constraint_persists_it_as_real_document_state(live_server, browser):
    room = "e2e-constraint-persist"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 100, 150, steps=5)
        _drag(page, canvas, 300, 300, 300, 350, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolConstrain")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 300)
        page.wait_for_selector("#constrainCoincident", timeout=3000)
        page.click("#constrainCoincident")
        page.wait_for_function("state.constraints.size === 1", timeout=5000)
        page.wait_for_timeout(300)  # let the WS round-trip actually reach the server

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        entries = [e for e in doc["constraints"]["entries"] if not e.get("d")]
        assert len(entries) == 1
        assert entries[0]["v"]["kind"] == "coincident"
        assert "coincident" in page.locator("#constraintsList .path-row").inner_text()
    finally:
        page.close()


def test_undo_reverts_a_constraint_driven_point_move(live_server, browser):
    """Regression coverage for a real, pre-existing gap: movePathPoint
    never pushed an undo entry before this phase, so a constraint
    "snapping points into place" was silently permanent. Fixed via
    movePathPointWithUndo."""
    room = "e2e-constraint-undo"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 100, 150, steps=5)
        _drag(page, canvas, 300, 300, 300, 350, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolConstrain")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 300)
        page.wait_for_selector("#constrainCoincident", timeout=3000)
        page.click("#constrainCoincident")
        page.wait_for_function("state.constraints.size === 1", timeout=5000)

        # Coincident moves both points to their shared midpoint (200,200).
        moved_points = page.evaluate("Array.from(state.pathIndex).flatMap((id) => pathPoints(id))")
        assert any(abs(x - 200) < 1 and abs(y - 200) < 1 for x, y in moved_points)

        page.click("#undoBtn")
        page.wait_for_timeout(200)
        after_undo = page.evaluate("Array.from(state.pathIndex).flatMap((id) => pathPoints(id))")
        # The undone point should be back near one of its original spots
        # (100,100) or (300,300) -- not still sitting at the midpoint.
        assert any((abs(x - 100) < 1 and abs(y - 100) < 1) or (abs(x - 300) < 1 and abs(y - 300) < 1) for x, y in after_undo)
    finally:
        page.close()


def test_tangent_constraint_picks_a_circle_shape_and_persists(live_server, browser):
    room = "e2e-constraint-tangent"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolCircle")
        _drag(page, canvas, 500, 150, 540, 150)  # center (500,150) r=40
        page.click("#toolPen")
        _drag(page, canvas, 600, 100, 600, 250, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolConstrain")
        page.mouse.click(box["x"] + 540, box["y"] + 150)  # the circle's boundary
        page.mouse.click(box["x"] + 600, box["y"] + 100)  # a point on the line
        page.wait_for_selector("#constrainTangent", timeout=3000)
        page.click("#constrainTangent")
        page.wait_for_function("state.constraints.size === 1", timeout=5000)
        page.wait_for_timeout(300)

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        entries = [e for e in doc["constraints"]["entries"] if not e.get("d")]
        assert len(entries) == 1
        assert entries[0]["v"]["kind"] == "tangent"
        assert entries[0]["v"]["anchors"]["circle"]["type"] == "shape_center"
    finally:
        page.close()


def test_dragging_a_constrained_point_resolves_on_release_without_duplicating(live_server, browser):
    room = "e2e-constraint-drag-resolve"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolPen")
        _drag(page, canvas, 100, 100, 100, 150, steps=5)
        _drag(page, canvas, 300, 300, 300, 350, steps=5)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolConstrain")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.mouse.click(box["x"] + 300, box["y"] + 300)
        page.wait_for_selector("#constrainCoincident", timeout=3000)
        page.click("#constrainCoincident")
        page.wait_for_function("state.constraints.size === 1", timeout=5000)

        # Drag the OTHER endpoint of one of the lines (not the constrained
        # point itself) -- this is still on a path with a constraint, so
        # releasing should trigger a fresh solve, without minting a
        # second constraint record.
        _drag(page, canvas, 100, 150, 120, 180, steps=5)
        page.wait_for_timeout(400)

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        entries = [e for e in doc["constraints"]["entries"] if not e.get("d")]
        assert len(entries) == 1, "dragging should re-solve the existing constraint, not create a new one"
    finally:
        page.close()
