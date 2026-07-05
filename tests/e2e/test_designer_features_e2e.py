"""Browser-driven end-to-end tests for Phase 15: designer features --
text, fills (and the hit-testing upgrade a real fill needs), stroke
styles, groups, and client-side PNG export. All of this is either
purely client-side (PNG, the fill hit-test change) or needs a real
document round-trip to prove it actually persisted (text/fill/dash/
group props are just path_props fields, easy to get "looks right in the
browser but never actually sent" wrong).
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


def _path_props_by_shape(doc, shape):
    for m in doc["path_props"].values():
        entries = {e["k"]: e["v"] for e in m["entries"] if not e.get("d")}
        if entries.get("shape") == shape:
            return entries
    raise AssertionError(f"no path with shape={shape!r} found")


def test_text_tool_creates_and_edits_persist(live_server, browser):
    room = "e2e-text-tool"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolText")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.wait_for_selector("#selContent", timeout=3000)
        page.fill("#selContent", "Hello CRDT")
        page.locator("#selContent").dispatch_event("change")
        page.fill("#selFontSize", "24")
        page.locator("#selFontSize").dispatch_event("change")
        page.wait_for_timeout(150)

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        text_props = _path_props_by_shape(doc, "text")
        assert text_props["content"] == "Hello CRDT"
        assert text_props["font_size"] == 24
    finally:
        page.close()


def test_filled_shape_is_selectable_from_its_interior(live_server, browser):
    """Regression-relevant behavior change: an unfilled shape is
    boundary-only clickable (Phase 11), but once it has a real fill it
    visibly looks like solid content -- clicking its interior must now
    select it too."""
    room = "e2e-fill-hittest"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolRect")
        _drag(page, canvas, 300, 100, 400, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.click("#toolSelect")
        page.mouse.click(box["x"] + 300, box["y"] + 100)
        page.wait_for_selector("#selFill", timeout=3000)

        # Before filling: clicking well inside must NOT select it.
        page.mouse.click(box["x"] + 700, box["y"] + 400)
        page.wait_for_timeout(50)
        page.mouse.click(box["x"] + 350, box["y"] + 130)
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 0

        page.mouse.click(box["x"] + 300, box["y"] + 100)
        page.wait_for_selector("#selFill", timeout=3000)
        page.fill("#selFill", "#ff8800")
        page.locator("#selFill").dispatch_event("change")
        page.wait_for_timeout(150)

        page.mouse.click(box["x"] + 700, box["y"] + 400)
        page.wait_for_timeout(50)
        page.mouse.click(box["x"] + 350, box["y"] + 130)  # well inside, no longer near any edge
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 1

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        assert _path_props_by_shape(doc, "rect")["fill"] == "#ff8800"
    finally:
        page.close()


def test_dash_style_persists(live_server, browser):
    room = "e2e-dash-style"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolLine")
        _drag(page, canvas, 100, 100, 200, 100)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.click("#toolSelect")
        page.mouse.click(box["x"] + 150, box["y"] + 100)
        page.wait_for_selector("#selDash", timeout=3000)
        page.select_option("#selDash", "dashed")
        page.wait_for_timeout(150)

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        assert _path_props_by_shape(doc, "line")["dash"] == "dashed"
    finally:
        page.close()


def test_grouping_makes_selecting_one_member_select_all_and_ungroup_reverts_it(live_server, browser):
    room = "e2e-groups"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 150, 150)
        page.click("#toolCircle")
        _drag(page, canvas, 300, 100, 320, 100)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)

        page.click("#toolSelect")
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.keyboard.down("Shift")
        page.mouse.click(box["x"] + 320, box["y"] + 100)
        page.keyboard.up("Shift")
        page.wait_for_selector("#bulkGroup", timeout=3000)
        page.click("#bulkGroup")
        page.wait_for_timeout(150)

        doc = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        assert len([e for e in doc["groups"]["entries"] if not e.get("d")]) == 1

        page.mouse.click(box["x"] + 700, box["y"] + 400)  # clear selection
        page.wait_for_timeout(50)
        page.mouse.click(box["x"] + 100, box["y"] + 100)  # click only the rect
        page.wait_for_timeout(100)
        assert page.locator("#pathList .path-row.active").count() == 2, "selecting one member should select the whole group"

        page.wait_for_selector("#bulkUngroup", timeout=3000)
        page.click("#bulkUngroup")
        page.wait_for_timeout(150)
        doc2 = json.loads(page.request.get(f"{live_server}/api/rooms/{room}/export/json").text())
        assert all(e.get("d") for e in doc2["groups"]["entries"])
    finally:
        page.close()


def test_png_export_downloads_view_and_fit_variants_and_restores_the_view(live_server, browser):
    room = "e2e-png-export"
    page = browser.new_page(accept_downloads=True)
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()

        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.mouse.click(box["x"] + 700, box["y"] + 400)  # deselect

        with page.expect_download() as dl_info:
            page.click("#downloadPngBtn")
        assert dl_info.value.suggested_filename.endswith(".png")

        zoom_before = page.locator("#zoomIndicator").inner_text()
        with page.expect_download() as dl_info2:
            page.click("#downloadPngFitBtn")
        assert dl_info2.value.suggested_filename.endswith(".png")
        page.wait_for_timeout(150)
        assert page.locator("#zoomIndicator").inner_text() == zoom_before, (
            "the fit-to-content PNG export must restore the user's original view afterward, "
            "not leave the canvas zoomed to fit"
        )
    finally:
        page.close()
