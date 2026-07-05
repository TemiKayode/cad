"""Browser-driven end-to-end tests for Part 3 Phase D4: the command
palette (Ctrl/Cmd+K), single-key tool shortcuts, arrow-key nudge,
Ctrl/Cmd+Z/Y now working in the 2D demo too, the grouped/searchable "?"
shortcut overlay, the skip-to-canvas link, and the keyboard-vs-viewer-
mode gating this phase adds (a read-only viewer could previously bypass
the mouse-level "editing disabled" restriction via a keyboard shortcut
-- harmless server-side, since the server rejects a viewer connection's
ops regardless, but confusing client-side). All pure client-side
interaction with no meaningful server counterpart other than the
viewer-mode case, which needs a real two-tab run the same way
test_readonly_share_links_e2e.py's does.
"""

import pytest

pytestmark = pytest.mark.e2e

SECRET = "e2e-keyboard-secret"


def _drag(page, canvas, x0, y0, x1, y1, steps=3):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_command_palette_filters_and_runs_a_command(live_server, browser):
    room = "e2e-kbd-palette-run"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.keyboard.press("Control+k")
        page.wait_for_selector("#paletteInput", state="visible", timeout=2000)
        page.keyboard.type("rectangle")
        page.wait_for_function("document.querySelectorAll('#paletteList .palette-row').length === 1", timeout=2000)
        page.keyboard.press("Enter")
        page.wait_for_function("!document.getElementById('paletteInput')", timeout=2000)
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolRect"
    finally:
        page.close()


def test_command_palette_opens_from_an_input_and_escape_restores_focus(live_server, browser):
    room = "e2e-kbd-palette-escape"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#roomInput")
        assert page.evaluate("document.activeElement.id") == "roomInput"

        # Ctrl/Cmd+K is deliberately NOT gated on "don't fire while typing"
        # like every other shortcut in this phase -- it's the escape hatch
        # reached for from exactly this situation.
        page.keyboard.press("Control+k")
        page.wait_for_selector("#paletteInput", state="visible", timeout=2000)
        assert page.evaluate("document.activeElement.id") == "paletteInput"

        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('paletteInput')", timeout=2000)
        assert page.evaluate("document.activeElement.id") == "roomInput"
    finally:
        page.close()


def test_single_key_tool_shortcut_switches_tool_but_not_while_typing(live_server, browser):
    room = "e2e-kbd-single-key"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.keyboard.press("r")
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolRect"

        # focused in the room-id text field -- the same "r" must type a
        # character, not switch tools.
        page.click("#roomInput")
        page.keyboard.press("Control+a")
        page.keyboard.press("r")
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolRect"
        assert "r" in page.eval_on_selector("#roomInput", "el => el.value")
    finally:
        page.close()


def test_ctrl_z_undo_redo_works_in_2d(live_server, browser):
    """sketch.js had no Ctrl+Z/Y binding at all before this phase (only
    the undo/redo *buttons* worked) -- mesh3d.js already had one."""
    room = "e2e-kbd-undo-redo"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toolRect")
        canvas = page.locator("#canvas")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.keyboard.press("Control+z")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 0", timeout=2000)

        page.keyboard.press("Control+Shift+z")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=2000)
    finally:
        page.close()


def test_arrow_key_nudge_moves_selected_path(live_server, browser):
    room = "e2e-kbd-nudge"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toolRect")
        canvas = page.locator("#canvas")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click("#toolSelect")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + 100, box["y"] + 100)
        page.wait_for_function("ui.selectedPaths.size === 1", timeout=2000)

        def tx_ty():
            return page.evaluate(
                "() => { const id = [...ui.selectedPaths][0]; "
                "const t = state.pathProps.get(id).transform || {tx:0, ty:0}; return [t.tx, t.ty]; }"
            )

        before = tx_ty()
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(100)
        after_right = tx_ty()
        assert after_right == [before[0] + 1, before[1]]

        page.keyboard.press("Shift+ArrowDown")
        page.wait_for_timeout(100)
        after_shift_down = tx_ty()
        assert after_shift_down == [after_right[0], after_right[1] + 10]
    finally:
        page.close()


def test_shortcut_overlay_is_searchable_and_closes_on_escape(live_server, browser):
    room = "e2e-kbd-overlay"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.keyboard.press("?")
        page.wait_for_selector("#shortcutSearchInput", state="visible", timeout=2000)
        assert page.evaluate("document.querySelectorAll('#shortcutList .palette-row').length") > 5

        page.keyboard.type("duplicate")
        page.wait_for_function("document.querySelectorAll('#shortcutList .palette-row').length === 1", timeout=2000)
        text = page.eval_on_selector("#shortcutList .palette-row", "el => el.textContent")
        assert "duplicate" in text.lower()

        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('shortcutSearchInput')", timeout=2000)
    finally:
        page.close()


def test_skip_link_is_first_tab_stop_and_focuses_canvas(live_server, browser):
    room = "e2e-kbd-skip-link"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.className") == "skip-link"
        page.keyboard.press("Enter")
        assert page.evaluate("document.activeElement.id") == "canvas"
    finally:
        page.close()


def test_3d_single_key_shortcut_and_shortcut_overlay(live_server, browser):
    room = "e2e-kbd-3d"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.keyboard.press("b")
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolBox"

        page.keyboard.press("?")
        page.wait_for_selector("#shortcutSearchInput", state="visible", timeout=2000)
        text = page.evaluate("document.getElementById('shortcutList').textContent")
        assert "Vertex" in text
        page.keyboard.press("Escape")
        page.wait_for_function("!document.getElementById('shortcutSearchInput')", timeout=2000)

        page.keyboard.press("Control+k")
        page.wait_for_selector("#paletteInput", state="visible", timeout=2000)
        page.keyboard.type("cylinder")
        page.wait_for_function("document.querySelectorAll('#paletteList .palette-row').length === 1", timeout=2000)
        page.keyboard.press("Enter")
        page.wait_for_function("!document.getElementById('paletteInput')", timeout=2000)
        assert page.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolCylinder"
    finally:
        page.close()


def test_viewer_mode_blocks_keyboard_tool_switch_and_duplicate(live_server_factory, browser):
    """The mouse-level "editing disabled" restriction (.viewer-mode CSS,
    Phase 17) has to hold for the keyboard too, or a viewer could switch
    tools and duplicate/delete geometry via keys alone -- harmless
    server-side (a viewer connection's ops are rejected regardless, see
    server/app.py), but a confusing client-side experience this phase's
    reachability audit is exactly meant to catch."""
    live_server = live_server_factory({"CRDT_CAD_SECRET": SECRET})
    room = "e2e-kbd-viewer-mode"

    editor = browser.new_page()
    editor.on("dialog", lambda d: d.accept(SECRET))
    try:
        editor.goto(f"{live_server}/2d?room={room}")
        editor.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = editor.locator("#canvas")
        editor.click("#toolRect")
        _drag(editor, canvas, 100, 100, 200, 160)
        editor.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        editor.context.grant_permissions(["clipboard-read", "clipboard-write"])
        editor.click("#shareViewOnlyBtn")
        editor.wait_for_timeout(300)
        view_only_url = editor.evaluate("navigator.clipboard.readText()")
        assert "token=" in view_only_url

        viewer_ctx = browser.new_context()
        viewer = viewer_ctx.new_page()
        viewer.on("dialog", lambda d: (_ for _ in ()).throw(AssertionError(f"unexpected prompt: {d.message}")))
        try:
            viewer.goto(view_only_url)
            viewer.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
            viewer.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
            assert viewer.locator("#viewOnlyBadge").is_visible()

            viewer.keyboard.press("r")
            viewer.wait_for_timeout(200)
            assert viewer.evaluate("ui.tool") == "pen"
            assert viewer.eval_on_selector(".tool-rail button.active", "el => el.id") == "toolPen"

            # a viewer's pointerdown gesture is blocked outright (Phase 17),
            # so there's no click-driven way to select a path to test
            # against -- set the selection directly to isolate exactly what
            # this phase changed: whether the *keyboard* Ctrl+D handler
            # itself respects viewerMode.
            viewer.evaluate("ui.selectedPaths = new Set(state.pathIndex)")
            viewer.keyboard.press("Control+d")
            viewer.wait_for_timeout(300)
            assert viewer.locator("#pathList .path-row").count() == 1
        finally:
            viewer.close()
    finally:
        editor.close()
