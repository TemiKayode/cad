"""Verifies the offline outbox survives a hard refresh (Phase 3): queued
edits made while offline are persisted to IndexedDB and recovered on
reload, rather than silently lost. Opt-in via `-m e2e`; see
tests/e2e/conftest.py for the live_server fixture.
"""

import json

import pytest

pytestmark = pytest.mark.e2e


def _draw_stroke(page, dx=(0, 0)):
    canvas = page.locator("#canvas")
    box = canvas.bounding_box()
    cx, cy = box["x"] + box["width"] / 2 + dx[0], box["y"] + box["height"] / 2 + dx[1]
    page.mouse.move(cx - 40, cy - 20)
    page.mouse.down()
    page.mouse.move(cx, cy + 20, steps=4)
    page.mouse.move(cx + 40, cy - 20, steps=4)
    page.mouse.up()


def test_offline_edit_survives_hard_refresh(live_server, page):
    room = "e2e-offline-durability"
    page.goto(f"{live_server}/2d?room={room}")
    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

    page.click("#offlineToggle")
    page.wait_for_function("document.getElementById('statusText').textContent === 'offline'", timeout=5000)

    _draw_stroke(page)
    page.wait_for_timeout(300)  # let the IndexedDB persist (send() -> persistOfflineState) settle

    # confirm nothing reached the server yet -- it's genuinely queued, not
    # just optimistically rendered
    resp = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
    before = json.loads(resp.text())
    assert len(before["path_index"]["entries"]) == 0

    # hard refresh -- a fresh page load, not just a reconnect click. The new
    # JS state starts with userWantsOffline=false, so it should auto-connect
    # (this is the reload count in this test, not a JS reload — Playwright's
    # page.reload() throws away all in-memory JS state but keeps IndexedDB,
    # exactly like a real browser hard refresh).
    page.reload()
    page.wait_for_selector("#canvas")

    # the recovered-edits toast should appear (a single dragged stroke
    # mints one op per mousemove step, so this is a double-digit op count,
    # not "1" -- assert on the stable prefix, not an exact number)
    page.wait_for_selector("text=Recovered", timeout=5000)

    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
    page.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 1", timeout=10000)

    resp2 = page.request.get(f"{live_server}/api/rooms/{room}/export/json")
    after = json.loads(resp2.text())
    assert len(after["path_index"]["entries"]) == 1


def test_no_persisted_state_is_a_silent_no_op(live_server, page):
    """A room nobody ever went offline in must not show a recovery toast
    or otherwise behave differently -- IndexedDB persistence is additive,
    not a new default behavior."""
    room = "e2e-offline-durability-clean"
    page.goto(f"{live_server}/2d?room={room}")
    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
    page.wait_for_timeout(500)
    toasts = page.locator("#toastContainer").count()
    assert toasts == 0
