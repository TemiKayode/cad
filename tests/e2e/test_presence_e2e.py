"""Browser-driven end-to-end tests for Part 3 Phase D6: smooth remote-
cursor interpolation and idle-fade, the avatar stack's join toast and
entrance animation, remote-selection outlines and remote-edit flashes
(2D), and client-local viewport follow mode. Needs two real tabs in
the same room -- presence is entirely a client-side convention riding
the generic op pipe (no server-side presence logic to unit-test
against), so a real two-browser-context run is the only way to verify
one tab's rendering reacts correctly to another's live state.

mesh3d.js is loaded as an ES module (`<script type="module">`), so its
top-level state (ui, state, controls, followingActorId, etc.) is
module-scoped and unreachable via `page.evaluate` -- unlike sketch.js's
classic script, where module-level bindings are ordinary globals. The
3D tests below only assert DOM-observable signals (toast text, CSS
classes, rendered cursor-label position) for that reason; they don't
duplicate the 2D tests' internal-state checks.
"""

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=5):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_actor_colors_are_eight_mid_tone_hues(live_server, browser):
    """Regression test for the real contrast bug this phase fixed: the
    old 10-color palette was bright/pastel, tuned only for the dark
    theme (e.g. #ffd43b yellow was a 13:1 vs the dark canvas but only
    1.4:1 vs the light one -- functionally invisible there)."""
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room=e2e-presence-colors")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        colors = page.evaluate("ACTOR_COLORS")
        assert len(colors) == 8
        assert len(set(colors)) == 8
        assert "#ffd43b" not in colors
    finally:
        page.close()


def test_join_toast_and_avatar_entrance_in_2d(live_server, browser):
    room = "e2e-presence-join-2d"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas_a = page_a.locator("#canvas")
        box_a = canvas_a.bounding_box()
        page_a.mouse.move(box_a["x"] + 200, box_a["y"] + 200)  # seed A's own presence first

        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas_b = page_b.locator("#canvas")
        box_b = canvas_b.bounding_box()
        page_b.mouse.move(box_b["x"] + 300, box_b["y"] + 300)

        page_a.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('joined')",
            timeout=5000,
        )
        page_a.wait_for_function("document.querySelectorAll('#avatarStack .avatar').length === 2", timeout=5000)
        # the newly-joined avatar (not "me") gets the entrance animation class
        assert page_a.evaluate(
            "[...document.querySelectorAll('#avatarStack .avatar')].some(el => el.classList.contains('avatar-enter'))"
        )
    finally:
        page_a.close()
        page_b.close()


def test_remote_cursor_interpolates_and_idle_fades_in_2d(live_server, browser):
    room = "e2e-presence-cursor-2d"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas_b = page_b.locator("#canvas")
        box_b = canvas_b.bounding_box()
        page_b.mouse.move(box_b["x"] + 100, box_b["y"] + 100)
        page_a.wait_for_function("document.querySelectorAll('.cursor-label').length === 1", timeout=5000)

        # a big jump shouldn't land instantly -- the eased position right
        # after the jump should still be well short of the target.
        page_b.mouse.move(box_b["x"] + 600, box_b["y"] + 600, steps=1)
        page_a.wait_for_timeout(20)
        early_x = page_a.eval_on_selector(".cursor-label", "el => parseFloat(el.style.left)")
        page_a.wait_for_timeout(500)
        late_x = page_a.eval_on_selector(".cursor-label", "el => parseFloat(el.style.left)")
        assert late_x - early_x > 20, "cursor should still be easing toward the target shortly after a jump"

        # idle fade: the name pill hides after 3s of no movement, and
        # coming back is instant (no re-fade-in delay needed to assert).
        page_a.wait_for_function(
            "document.querySelector('.cursor-label').classList.contains('idle')", timeout=4000
        )
        page_b.mouse.move(box_b["x"] + 610, box_b["y"] + 610, steps=1)
        page_a.wait_for_timeout(100)
        assert not page_a.eval_on_selector(".cursor-label", "el => el.classList.contains('idle')")
    finally:
        page_a.close()
        page_b.close()


def test_remote_selection_outline_and_edit_flash_in_2d(live_server, browser):
    room = "e2e-presence-remote-selection-2d"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas_b = page_b.locator("#canvas")
        page_b.click("#toolRect")
        _drag(page_b, canvas_b, 100, 100, 200, 160)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page_b.click("#toolSelect")
        box_b = canvas_b.bounding_box()
        page_b.mouse.click(box_b["x"] + 100, box_b["y"] + 100)
        page_b.wait_for_function("ui.selectedPaths.size === 1", timeout=2000)

        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page_a.wait_for_timeout(500)  # the 400ms heartbeat carries `sel` over even without A moving
        color = page_a.evaluate("remoteSelectionColorFor([...state.pathIndex][0])")
        assert color is not None

        # remote edit flash: B drags the (still-selected) path
        page_b.mouse.move(box_b["x"] + 100, box_b["y"] + 100)
        page_b.mouse.down()
        page_b.mouse.move(box_b["x"] + 130, box_b["y"] + 130, steps=3)
        page_b.mouse.up()
        page_a.wait_for_function("remoteEditFlashes.size === 1", timeout=2000)
        page_a.wait_for_function("remoteEditFlashes.size === 0", timeout=2000)
    finally:
        page_a.close()
        page_b.close()


def test_follow_mode_recenters_view_and_exits_on_manual_zoom_in_2d(live_server, browser):
    room = "e2e-presence-follow-2d"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        canvas_b = page_b.locator("#canvas")
        box_b = canvas_b.bounding_box()
        page_b.mouse.move(box_b["x"] + 200, box_b["y"] + 200)
        page_a.wait_for_function("document.querySelectorAll('#avatarStack .avatar-clickable').length === 1", timeout=5000)

        page_a.click("#avatarStack .avatar-clickable")
        assert page_a.eval_on_selector("#avatarStack .avatar-clickable", "el => el.classList.contains('following')")
        assert page_a.evaluate("followingActorId") is not None

        pan_before = page_a.evaluate("[view.panX, view.panY]")
        page_b.mouse.move(box_b["x"] + 500, box_b["y"] + 60, steps=1)
        page_a.wait_for_timeout(300)
        pan_after = page_a.evaluate("[view.panX, view.panY]")
        assert pan_after != pan_before, "following should re-pan the view toward the followed actor"

        canvas_a = page_a.locator("#canvas")
        box_a = canvas_a.bounding_box()
        page_a.mouse.move(box_a["x"] + 300, box_a["y"] + 300)
        page_a.mouse.wheel(0, -200)
        page_a.wait_for_timeout(100)
        assert page_a.evaluate("followingActorId") is None
    finally:
        page_a.close()
        page_b.close()


def test_3d_presence_sent_immediately_on_connect_and_join_toast(live_server, browser):
    """Regression test for a real gap this phase found: 3D only ever
    sent presence at discrete commit points (placing/dragging a vertex),
    never on connect -- a collaborator who joined and just looked around
    stayed completely invisible (no avatar, no cursor) until their first
    edit. onRole now sends one ping immediately on connecting."""
    room = "e2e-presence-3d-immediate"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/3d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/3d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # B does nothing at all -- no click, no drag.
        page_a.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('joined')",
            timeout=5000,
        )
        page_a.wait_for_function("document.querySelectorAll('#avatarStack .avatar').length === 2", timeout=5000)
        page_a.wait_for_function("document.querySelectorAll('.cursor-label').length === 1", timeout=5000)
    finally:
        page_a.close()
        page_b.close()


def test_3d_cursor_idle_fade(live_server, browser):
    room = "e2e-presence-3d-idle"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/3d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/3d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.wait_for_function("document.querySelectorAll('.cursor-label').length === 1", timeout=5000)
        page_a.wait_for_function(
            "document.querySelector('.cursor-label').classList.contains('idle')", timeout=4000
        )

        canvas_b = page_b.locator("#canvas3d")
        box_b = canvas_b.bounding_box()
        page_b.mouse.click(box_b["x"] + box_b["width"] / 2, box_b["y"] + box_b["height"] / 2 + 50)
        page_a.wait_for_timeout(200)
        assert not page_a.eval_on_selector(".cursor-label", "el => el.classList.contains('idle')")
    finally:
        page_a.close()
        page_b.close()


def test_3d_follow_mode_centers_camera_on_followed_actor(live_server, browser):
    room = "e2e-presence-3d-follow"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/3d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/3d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_a.wait_for_function("document.querySelectorAll('#avatarStack .avatar-clickable').length === 1", timeout=5000)

        page_a.click("#avatarStack .avatar-clickable")
        assert page_a.eval_on_selector("#avatarStack .avatar-clickable", "el => el.classList.contains('following')")

        canvas_b = page_b.locator("#canvas3d")
        box_b = canvas_b.bounding_box()
        page_b.mouse.click(box_b["x"] + box_b["width"] / 2 + 250, box_b["y"] + box_b["height"] / 2 + 120)
        page_a.wait_for_timeout(600)

        canvas_a = page_a.locator("#canvas3d")
        box_a = canvas_a.bounding_box()
        label_pos = page_a.eval_on_selector(".cursor-label", "el => [parseFloat(el.style.left), parseFloat(el.style.top)]")
        # following keeps the OrbitControls target on the followed actor,
        # so their own cursor should now project near the viewport center.
        assert abs(label_pos[0] - box_a["width"] / 2) < 100
        assert abs(label_pos[1] - box_a["height"] / 2) < 100
    finally:
        page_a.close()
        page_b.close()
