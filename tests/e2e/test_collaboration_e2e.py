"""Browser-driven end-to-end tests against a live server. Opt-in only --
run with `pytest -m e2e tests/e2e` after `playwright install chromium`
(see tests/e2e/conftest.py for the live_server fixture, and
pyproject.toml for why these are excluded from a plain `pytest` run).

Ports the scenarios README's Testing section describes as manually
verified during development into a permanent, committed suite: two tabs
converging, offline + Time-Travel Merge, the strict Polygon rejection
round-trip, and the two-actor LWW tie-break regression
(LocalClock.observe()).
"""

import pytest

pytestmark = pytest.mark.e2e


def _draw_stroke(page, canvas_selector, dx=(0, 0), points=((-60, -30), (0, 30), (60, -20))):
    canvas = page.locator(canvas_selector)
    box = canvas.bounding_box()
    cx, cy = box["x"] + box["width"] / 2 + dx[0], box["y"] + box["height"] / 2 + dx[1]
    x0, y0 = points[0]
    page.mouse.move(cx + x0, cy + y0)
    page.mouse.down()
    for x, y in points[1:]:
        page.mouse.move(cx + x, cy + y, steps=4)
    page.mouse.up()


def test_two_tabs_drawing_concurrently_converge(live_server, browser):
    room = "e2e-two-tabs"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_b.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        _draw_stroke(page_a, "#canvas", dx=(-150, 0))
        _draw_stroke(page_b, "#canvas", dx=(150, 0))

        # give the relay a moment to broadcast both strokes to both tabs
        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 2", timeout=10000)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 2", timeout=10000)

        count_a = page_a.locator("#pathList .path-row").count()
        count_b = page_b.locator("#pathList .path-row").count()
        assert count_a == count_b == 2
    finally:
        page_a.close()
        page_b.close()


def test_offline_edit_then_reconnect_shows_time_travel_merge_and_converges(live_server, browser):
    room = "e2e-ttm"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_b.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.click("#offlineToggle")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'offline'", timeout=5000)

        _draw_stroke(page_a, "#canvas", dx=(-100, -50))  # queued in the offline outbox
        _draw_stroke(page_b, "#canvas", dx=(100, 50))  # reaches the server live

        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 1", timeout=10000)

        page_a.click("#offlineToggle")  # reconnect -- both branches changed -> merge preview
        page_a.wait_for_selector("#mergeProceedBtn", timeout=10000)
        page_a.click("#mergeProceedBtn")

        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 2", timeout=10000)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 2", timeout=10000)
        assert page_a.locator("#pathList .path-row").count() == page_b.locator("#pathList .path-row").count() == 2
    finally:
        page_a.close()
        page_b.close()


def test_strict_polygon_rejects_self_intersection_live(live_server, page):
    # The strict Polygon tool is click-to-place (each click adds one
    # vertex; clicking near the first vertex again closes the shape) --
    # not a click-and-drag gesture like the freehand Pen tool.
    page.goto(f"{live_server}/2d?room=e2e-strict-polygon")
    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
    page.click("#toolPolygon")

    canvas = page.locator("#canvas")
    box = canvas.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    # a genuine bowtie: P0->P1 and P2->P3 are the two diagonals of the same
    # rectangle, crossing exactly in the middle -- placing P3 must be
    # rejected server-side once the shape is closed and sent.
    pts = [(-80, -60), (80, 60), (80, -60), (-80, 60)]
    for x, y in pts:
        page.mouse.click(cx + x, cy + y)
        page.wait_for_timeout(100)
    page.mouse.click(cx + pts[0][0], cy + pts[0][1])  # close back near the first vertex
    page.wait_for_timeout(300)

    # the crossing 4th vertex must have been rejected -- only 3 of the 4
    # placed points landed (verified directly against debug output: a
    # non-intersecting polygon of the same length would show "4 pts")
    row_text = page.evaluate(
        "() => { const row = document.querySelector('#pathList .path-row'); return row ? row.textContent : null; }"
    )
    assert row_text is not None
    assert "3 pts" in row_text
    assert "4 pts" not in row_text


def test_fresh_client_editing_ai_generated_content_does_not_lose_lww_tiebreak(live_server, page):
    """Regression test for the LocalClock.observe() bug: a fresh client's
    Lamport counter must catch up to whatever it's seen (here, the AI
    generator's high-counter ops) before minting its own edit, or that
    edit silently loses the LWW tie-break and never takes effect."""
    room = "e2e-lww-tiebreak"
    page.goto(f"{live_server}/3d?room={room}")
    page.wait_for_selector("#genPromptInput")
    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

    page.fill("#genPromptInput", "a 1 bedroom house with a concrete floor")
    page.click("#genBtn")
    page.wait_for_function(
        "document.getElementById('genBtn').textContent === 'Generate' && !document.getElementById('genBtn').disabled",
        timeout=20000,
    )
    page.wait_for_timeout(300)

    page.click("#toolMove")
    page.click("#faceList .path-row .name")
    page.wait_for_selector("#faceMaterialInput", timeout=5000)
    page.fill("#faceMaterialInput", "lava")
    page.locator("#faceMaterialInput").blur()
    page.wait_for_timeout(300)

    face_list_html = page.locator("#faceList").inner_html()
    assert "lava" in face_list_html

    resp = page.request.get(f"{live_server}/api/mesh/{room}/export/json")
    doc = resp.json()
    materials = [
        e["v"]
        for m in doc.get("face_props", {}).values()
        for e in m.get("entries", [])
        if e.get("k") == "material" and not e.get("d")
    ]
    assert "lava" in materials
