"""Browser-driven end-to-end tests for Part 3 Phase D2: the icon-only
tool rail, collapsible secondary/right panels, the document-name rename
button, and the top-bar presence avatar stack. All pure client-side
layout/interaction behavior with no meaningful server counterpart
(rename reuses Phase 17's REST endpoint, tested there) -- these need a
real browser to catch what a unit test can't: whether a CSS grid column
actually collapses to zero width without its own content bleeding out
over the canvas (a real bug this phase's verification caught -- see
docs/design-system.md and the README's D2 section), and whether the
active-tool indicator actually follows tool switches.
"""

import pytest

pytestmark = pytest.mark.e2e


def test_tool_rail_active_state_follows_tool_switches(live_server, browser):
    room = "e2e-layout-rail"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolPen"
        page.click("#toolRect")
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolRect"
        assert page.locator(".tool-rail button.active").count() == 1
    finally:
        page.close()


def test_panel_collapse_does_not_leak_content_over_canvas(live_server, browser):
    """Regression test for a real bug: collapsing a panel via
    grid-template-columns alone left its own content visible (clipped
    mid-character) bleeding over the canvas, because CSS Grid's
    automatic-minimum-size rule only shrinks a column to 0 if the item
    inside has non-visible overflow on *both* axes -- the panel only had
    overflow-y set. Asserting the panel's own bounding box width is 0
    (not just that a class was toggled) is what would have caught this."""
    room = "e2e-layout-collapse"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toggleSecondaryPanelBtn")
        page.wait_for_timeout(300)
        width = page.eval_on_selector(".panel.left", "el => el.getBoundingClientRect().width")
        assert width == 0, f"collapsed panel should have zero width, got {width}"

        page.click("#toggleRightPanelBtn")
        page.wait_for_timeout(300)
        right_width = page.eval_on_selector(".panel.right", "el => el.getBoundingClientRect().width")
        assert right_width == 0

        # `\` restores both at once
        page.keyboard.press("Backslash")
        page.wait_for_timeout(300)
        left_width_after = page.eval_on_selector(".panel.left", "el => el.getBoundingClientRect().width")
        right_width_after = page.eval_on_selector(".panel.right", "el => el.getBoundingClientRect().width")
        assert left_width_after > 100
        assert right_width_after > 100
    finally:
        page.close()


def test_tooltip_appears_on_hover_with_correct_label(live_server, browser):
    room = "e2e-layout-tooltip"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        box = page.locator("#toolCircle").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_function("document.getElementById('__tooltip').classList.contains('visible')", timeout=2000)
        text = page.eval_on_selector("#__tooltip", "el => el.textContent")
        assert text == "Circle"
    finally:
        page.close()


def test_document_name_rename_persists_and_survives_reload(live_server, browser):
    room = "e2e-layout-docname"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # the room has never been saved yet -- the first rename attempt
        # 404s and common.js's renameRoom() force-saves then retries.
        page.once("dialog", lambda d: d.accept("Kitchen Remodel"))
        page.click("#docNameBtn")
        page.wait_for_function(
            "document.getElementById('docNameBtn').textContent === 'Kitchen Remodel'", timeout=3000
        )

        resp = page.request.get(f"{live_server}/api/workspace/rooms")
        rows = resp.json()
        row = next(r for r in rows if r["kind"] == "drawing" and r["room_id"] == room)
        assert row["display_name"] == "Kitchen Remodel"

        page.reload()
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page.wait_for_function(
            "document.getElementById('docNameBtn').textContent === 'Kitchen Remodel'", timeout=3000
        )
    finally:
        page.close()


def test_avatar_stack_shows_both_actors_in_a_shared_room(live_server, browser):
    room = "e2e-layout-avatars"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # presence only sends on mouse move -- both tabs need to move once
        for page in (page_a, page_b):
            canvas = page.locator("#canvas")
            box = canvas.bounding_box()
            page.mouse.move(box["x"] + 150, box["y"] + 150)

        page_a.wait_for_function("document.querySelectorAll('#avatarStack .avatar').length === 2", timeout=5000)
    finally:
        page_a.close()
        page_b.close()
