"""Browser-driven end-to-end tests for Part 3 Phase D5: the connection/
save status cluster and its popover, the upgraded toast system (queued
one-at-a-time, an inline "Undo" action, pausable on hover), empty-state
hints on a fresh room, and the geometry-rejection red flash paired with
its "Rejected: ..." toast. All pure client-side behavior -- the only
server-side involvement is the pre-existing save/reject/offline
protocol these features surface, not anything new to that protocol.
"""

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=5):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_status_cluster_shows_live_and_popover_explains_it(live_server, browser):
    room = "e2e-state-status-popover"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        assert page.eval_on_selector("#statusLabel", "el => el.textContent") == "Live"
        assert page.eval_on_selector("#statusDot", "el => el.className") == "status-dot status-dot-online"
        # #statusText keeps its exact raw machine word so the many other
        # e2e tests waiting on it are unaffected by this phase's relabel.
        assert page.eval_on_selector("#statusText", "el => el.textContent") == "online"

        page.click("#statusCluster")
        page.wait_for_function("getComputedStyle(document.getElementById('statusPopover')).display !== 'none'", timeout=2000)
        detail = page.eval_on_selector("#statusPopoverDetail", "el => el.textContent")
        assert "durably persisted" in detail

        page.keyboard.press("Escape")
        page.wait_for_function("getComputedStyle(document.getElementById('statusPopover')).display === 'none'", timeout=2000)
        assert page.evaluate("document.activeElement.id") == "statusCluster"
    finally:
        page.close()


def test_status_cluster_shows_offline_with_queued_count(live_server, browser):
    room = "e2e-state-status-offline"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#offlineToggle")
        page.wait_for_function("document.getElementById('statusText').textContent === 'offline'", timeout=5000)
        page.wait_for_timeout(300)
        assert page.eval_on_selector("#statusLabel", "el => el.textContent") == "Offline"

        page.click("#toolRect")
        canvas = page.locator("#canvas")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_timeout(500)
        label = page.eval_on_selector("#statusLabel", "el => el.textContent")
        assert label.startswith("Offline -- ") and "queued" in label
        assert page.eval_on_selector("#statusDot", "el => el.className") == "status-dot status-dot-offline"
    finally:
        page.close()


def test_save_label_updates_after_explicit_save(live_server, browser):
    room = "e2e-state-save-label"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        # merely connecting already marks a save-state baseline (whatever
        # was just loaded IS durably persisted) -- confirm that first.
        page.wait_for_function("document.getElementById('saveLabel').textContent.startsWith('Saved')", timeout=3000)

        page.click("#saveBtn")
        page.wait_for_function("document.getElementById('saveLabel').textContent === 'Saved just now'", timeout=3000)
    finally:
        page.close()


def test_empty_state_hint_shows_then_hides_after_first_path(live_server, browser):
    room = "e2e-state-empty-2d"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page.wait_for_function(
            "getComputedStyle(document.getElementById('emptyCanvasHint')).display !== 'none'", timeout=2000
        )
        hint_text = page.eval_on_selector("#emptyCanvasHint", "el => el.textContent")
        assert "shortcuts" in hint_text

        page.click("#toolRect")
        canvas = page.locator("#canvas")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function(
            "getComputedStyle(document.getElementById('emptyCanvasHint')).display === 'none'", timeout=2000
        )
    finally:
        page.close()


def test_3d_empty_state_hint_and_status_cluster(live_server, browser):
    room = "e2e-state-empty-3d"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page.wait_for_function(
            "getComputedStyle(document.getElementById('emptyCanvasHint')).display !== 'none'", timeout=2000
        )
        assert page.eval_on_selector("#statusLabel", "el => el.textContent") == "Live"

        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 50)
        page.wait_for_function(
            "getComputedStyle(document.getElementById('emptyCanvasHint')).display === 'none'", timeout=2000
        )
    finally:
        page.close()


def test_toast_queue_shows_one_at_a_time(live_server, browser):
    room = "e2e-state-toast-queue"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.evaluate("showToast('first', 'info'); showToast('second', 'info'); showToast('third', 'info');")
        page.wait_for_timeout(200)
        assert page.evaluate("document.querySelectorAll('#toastContainer .toast').length") == 1
        assert page.eval_on_selector("#toastContainer", "el => el.getAttribute('aria-live')") == "polite"

        first_text = page.eval_on_selector("#toastContainer .toast", "el => el.textContent")
        assert first_text == "first"
        # hovering pauses the dismiss timer -- it should still be showing
        # "first" well past its normal 4s duration.
        page.hover("#toastContainer .toast")
        page.wait_for_timeout(4200)
        assert page.eval_on_selector("#toastContainer .toast", "el => el.textContent") == "first"
    finally:
        page.close()


def test_delete_shows_undo_toast_that_restores_the_path(live_server, browser):
    room = "e2e-state-undo-toast"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toolRect")
        canvas = page.locator("#canvas")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)

        page.click('#pathList [data-act="del"]')
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 0", timeout=3000)
        page.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('Path deleted')",
            timeout=3000,
        )
        page.click(".toast-action")
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=3000)
    finally:
        page.close()


def test_geometry_rejection_shows_toast_and_flashes_the_path(live_server, browser):
    room = "e2e-state-rejection-flash"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.click("#toolPolygon")
        canvas = page.locator("#canvas")
        box = canvas.bounding_box()
        # a self-intersecting bowtie
        for x, y in [(100, 100), (300, 100), (100, 300), (300, 300)]:
            page.mouse.click(box["x"] + x, box["y"] + y)
        page.mouse.click(box["x"] + 100, box["y"] + 100)  # close it

        page.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('Rejected')",
            timeout=3000,
        )
        # the flash is tracked client-side (flashingPathIds) rather than
        # asserted on canvas pixels, same reasoning as the D3 hover-halo
        # test: canvas pixels aren't a reliable DOM assertion target.
        assert page.evaluate("flashingPathIds.size") == 1
        page.wait_for_function("flashingPathIds.size === 0", timeout=2000)
    finally:
        page.close()
