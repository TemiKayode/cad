"""Browser-driven end-to-end tests for Phase 17's workspace home page:
listing real rooms with a live-rendered 2D thumbnail and a 3D
placeholder, rename, and version history + restore-as-a-fork. These
need a real browser because the home page's room cards, thumbnails, and
modals are pure client-side rendering (home.js) driven by REST calls
that only make sense once real rooms with real content exist.
"""

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=3):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_home_page_lists_rooms_with_kind_badges_and_thumbnails(live_server, browser):
    room2d = "e2e-home-2d"
    room3d = "e2e-home-3d"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room2d}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.click("#saveBtn")
        page.wait_for_timeout(150)

        page.goto(f"{live_server}/3d?room={room3d}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas3d = page.locator("#canvas3d")
        box3d = canvas3d.bounding_box()
        page.click("#toolBox")
        page.wait_for_selector(".primField", timeout=3000)
        page.mouse.click(box3d["x"] + box3d["width"] / 2, box3d["y"] + box3d["height"] / 2)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '8'", timeout=5000)
        page.click("#saveBtn")
        page.wait_for_timeout(150)

        page.goto(f"{live_server}/")
        page.wait_for_selector(".room-card", timeout=5000)

        card2d = page.locator(".room-card", has_text=room2d)
        assert card2d.locator(".room-kind-badge.drawing").count() == 1
        assert card2d.locator(".room-thumb img").count() == 1

        card3d = page.locator(".room-card", has_text=room3d)
        assert card3d.locator(".room-kind-badge.mesh").count() == 1
        assert card3d.locator(".placeholder-icon").count() == 1
    finally:
        page.close()


def test_rename_persists_and_updates_the_home_page(live_server, browser):
    room = "e2e-home-rename"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page.click("#saveBtn")
        page.wait_for_timeout(150)

        page.goto(f"{live_server}/")
        page.wait_for_selector(".room-card", timeout=5000)
        card = page.locator(".room-card", has_text=room)
        card.get_by_role("button", name="Rename").click()
        page.fill("#renameInput", "Renamed Project")
        page.click("#renameSaveBtn")
        page.wait_for_selector(".room-card-name >> text=Renamed Project", timeout=3000)

        resp = page.request.get(f"{live_server}/api/workspace/rooms")
        rows = resp.json()
        row = next(r for r in rows if r["room_id"] == room)
        assert row["display_name"] == "Renamed Project"
    finally:
        page.close()


def test_history_lists_a_checkpoint_and_restore_forks_a_new_room(live_server, browser):
    room = "e2e-home-history"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas")
        page.click("#toolRect")
        _drag(page, canvas, 100, 100, 200, 160)
        page.wait_for_function("document.querySelectorAll('#pathList .path-row').length === 1", timeout=5000)
        page.click("#saveBtn")
        page.wait_for_timeout(150)

        page.goto(f"{live_server}/")
        page.wait_for_selector(".room-card", timeout=5000)
        card = page.locator(".room-card", has_text=room)
        card.get_by_role("button", name="History").click()
        page.wait_for_selector(".version-row", timeout=3000)
        assert page.locator(".version-row").count() >= 1

        with page.expect_navigation():
            page.locator(".version-row").first.get_by_role("button", name="Restore").click()
        assert "-restored-" in page.url
        forked_room = page.url.split("room=")[1]

        forked_doc = page.request.get(f"{live_server}/api/rooms/{forked_room}/export/json").json()
        assert len(forked_doc["path_index"]["entries"]) == 1

        original_doc = page.request.get(f"{live_server}/api/rooms/{room}/export/json").json()
        assert len(original_doc["path_index"]["entries"]) == 1
    finally:
        page.close()


def test_actor_rename_button_updates_label_and_syncs_to_presence(live_server, browser):
    room = "e2e-home-actor-rename"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.goto(f"{live_server}/2d?room={room}")
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.once("dialog", lambda d: d.accept("Ada Lovelace"))
        page_a.click("#renameActorBtn")
        page_a.wait_for_timeout(150)
        assert "Ada Lovelace" in page_a.locator("#actorLabel").inner_text()

        # nudge presence so the new name reaches page_b without waiting for
        # an unrelated mouse move
        canvas_a = page_a.locator("#canvas")
        box_a = canvas_a.bounding_box()
        page_a.mouse.move(box_a["x"] + 50, box_a["y"] + 50)
        page_b.wait_for_function(
            "document.getElementById('presenceList').textContent.includes('Ada Lovelace')", timeout=5000
        )
    finally:
        page_a.close()
        page_b.close()
