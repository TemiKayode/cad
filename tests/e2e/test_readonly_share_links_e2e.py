"""Browser-driven end-to-end test for Phase 17's read-only share links:
an editor mints a view-only invite link, a completely fresh browser
context opens it with zero prompts, sees the document and a "view only"
badge, and cannot actually create geometry -- while the editor tab
stays fully functional. Needs a real two-tab browser run (not just the
server-side WS test in test_workspace.py) to prove the *client* side of
this actually holds: the toolbar is genuinely disabled (not just
visually dimmed), and a forced interaction still can't reach the
document, both locally and confirmed via the server's own export.

Needs `CRDT_CAD_SECRET` configured (share-link minting 400s without
it), so this uses `live_server_factory` instead of the plain
`live_server` fixture every other e2e test here uses.
"""

import pytest

pytestmark = pytest.mark.e2e

SECRET = "e2e-share-link-secret"


def _drag(page, canvas, x0, y0, x1, y1, steps=3):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_view_only_link_grants_read_access_but_blocks_editing(live_server_factory, browser):
    live_server = live_server_factory({"CRDT_CAD_SECRET": SECRET})
    room = "e2e-view-only"

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
        # A genuine prompt here would mean the invite link failed to carry
        # a working token -- fail loudly instead of hanging on a dialog
        # neither this test nor a real user watching would expect.
        viewer.on("dialog", lambda d: (_ for _ in ()).throw(AssertionError(f"unexpected prompt: {d.message}")))
        try:
            viewer.goto(view_only_url)
            viewer.wait_for_function(
                "document.getElementById('statusText').textContent === 'online'", timeout=10000
            )
            viewer.wait_for_function(
                "document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000
            )

            assert viewer.locator("#viewOnlyBadge").is_visible()

            pointer_events = viewer.eval_on_selector("#toolRect", "el => getComputedStyle(el).pointerEvents")
            assert pointer_events == "none"

            # even bypassing the toolbar (force-click) and dragging on the
            # canvas must create nothing -- the pointerdown handler itself
            # is gated by viewerMode, independent of the disabled toolbar.
            viewer.click("#toolRect", force=True)
            viewer_canvas = viewer.locator("#canvas")
            _drag(viewer, viewer_canvas, 300, 300, 400, 360)
            viewer.wait_for_timeout(300)
            assert viewer.locator("#pathList .path-row").count() == 1

            token = view_only_url.split("token=")[1]
            doc = viewer.request.get(f"{live_server}/api/rooms/{room}/export/json?token={token}").json()
            assert len(doc["path_index"]["entries"]) == 1
        finally:
            viewer.close()

        # editor tab is unaffected by any of the above.
        editor.click("#toolCircle")
        _drag(editor, canvas, 300, 100, 330, 100)
        editor.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 2", timeout=5000)
    finally:
        editor.close()


def test_viewer_role_token_is_rejected_from_editor_only_rest_endpoint(live_server_factory, browser):
    """Defense in depth beyond the WS boundary: a viewer-role token must
    also be refused by REST endpoints that mutate a room (rename here),
    not just have the WS ops path rejected."""
    live_server = live_server_factory({"CRDT_CAD_SECRET": SECRET})
    room = "e2e-view-only-rest-guard"

    editor = browser.new_page()
    editor.on("dialog", lambda d: d.accept(SECRET))
    try:
        editor.goto(f"{live_server}/2d?room={room}")
        editor.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        editor.click("#saveBtn")
        editor.wait_for_timeout(150)

        editor.context.grant_permissions(["clipboard-read", "clipboard-write"])
        editor.click("#shareViewOnlyBtn")
        editor.wait_for_timeout(300)
        view_only_url = editor.evaluate("navigator.clipboard.readText()")
        token = view_only_url.split("token=")[1]

        resp = editor.request.post(
            f"{live_server}/api/rooms/{room}/rename?token={token}",
            data='{"display_name": "Nope"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403
    finally:
        editor.close()
