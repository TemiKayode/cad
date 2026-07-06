"""Browser-driven end-to-end tests for Part 3 Phase D7: the redesigned
Time-Travel Merge preview (two-column change chips, per-author colors,
converge animation, non-"conflict" copy) and the staged AI-generation
experience (thinking shimmer, real-batch-driven progress line,
completion toast, failure/retry). Needs a real two-tab reconnect for
the merge modal (same recipe as test_collaboration_e2e.py's existing
Time-Travel Merge test) and a real generation round trip for the AI
staging -- both are genuinely server-driven flows, not fakeable from
one page alone.
"""

import pytest

pytestmark = pytest.mark.e2e


def _drag(page, canvas, x0, y0, x1, y1, steps=5):
    box = canvas.bounding_box()
    page.mouse.move(box["x"] + x0, box["y"] + y0)
    page.mouse.down()
    page.mouse.move(box["x"] + x1, box["y"] + y1, steps=steps)
    page.mouse.up()


def test_merge_modal_avoids_conflict_language_and_shows_per_author_chips(live_server, browser):
    room = "e2e-art-merge-chips"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_b.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.click("#offlineToggle")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'offline'", timeout=5000)

        _drag(page_a, page_a.locator("#canvas"), 100, 100, 200, 160)
        _drag(page_b, page_b.locator("#canvas"), 400, 100, 450, 160)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 1", timeout=10000)

        page_a.click("#offlineToggle")
        page_a.wait_for_selector("#mergeProceedBtn", timeout=10000)

        modal_text = page_a.eval_on_selector(".merge-modal", "el => el.textContent").lower()
        assert "conflict" not in modal_text
        assert "while you were away" in modal_text
        assert "meanwhile, in the room" in modal_text

        columns = page_a.query_selector_all(".merge-column")
        assert len(columns) == 2
        mine_chip_color = page_a.eval_on_selector(
            ".merge-column:first-child .merge-chip", "el => getComputedStyle(el).borderLeftColor"
        )
        theirs_chip_color = page_a.eval_on_selector(
            ".merge-column:last-child .merge-chip", "el => getComputedStyle(el).borderLeftColor"
        )
        assert mine_chip_color != theirs_chip_color
    finally:
        page_a.close()
        page_b.close()


def test_merge_converges_and_shows_combined_toast(live_server, browser):
    room = "e2e-art-merge-converge"
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.goto(f"{live_server}/2d?room={room}")
        page_b.goto(f"{live_server}/2d?room={room}")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        page_b.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page_a.click("#offlineToggle")
        page_a.wait_for_function("document.getElementById('statusText').textContent === 'offline'", timeout=5000)
        _drag(page_a, page_a.locator("#canvas"), 100, 100, 200, 160)
        _drag(page_b, page_b.locator("#canvas"), 400, 100, 450, 160)
        page_b.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 1", timeout=10000)

        page_a.click("#offlineToggle")
        page_a.wait_for_selector("#mergeProceedBtn", timeout=10000)
        page_a.click("#mergeProceedBtn")

        # the "two lines joining" converge animation is applied
        # synchronously on click, before the 220ms removal delay.
        assert page_a.eval_on_selector(".merge-modal", "el => el.classList.contains('merge-converging')")

        page_a.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('Merged')",
            timeout=8000,
        )
        toast_text = page_a.eval_on_selector("#toastContainer .toast", "el => el.textContent")
        assert "changes combined" in toast_text
        page_a.wait_for_function("document.querySelectorAll('#pathList .path-row').length >= 2", timeout=10000)
    finally:
        page_a.close()
        page_b.close()


def test_ai_generation_thinking_shimmer_then_building_progress(live_server, browser):
    room = "e2e-art-gen-staging"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.fill("#genPromptInput", "a 6 bedroom house with 4 floors and a tile floor")
        page.click("#genBtn")

        page.wait_for_function(
            "document.getElementById('genPromptInput').classList.contains('ai-thinking')", timeout=2000
        )
        assert page.eval_on_selector("#genStatus", "el => el.textContent") == "Interpreting your prompt..."

        # a big enough generation (several floors) that the real batches
        # take long enough for this genuinely arrival-driven state to be
        # observable, not just the thinking state jumping straight to
        # the final result.
        page.wait_for_function(
            "document.getElementById('genStatus').textContent.startsWith('Building')", timeout=15000
        )
        assert not page.eval_on_selector("#genPromptInput", "el => el.classList.contains('ai-thinking')")

        page.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('Built by')",
            timeout=15000,
        )
        toast_text = page.eval_on_selector("#toastContainer .toast", "el => el.textContent")
        assert "ai_generator_bot" in toast_text
        assert "vertices" in toast_text and "faces" in toast_text
    finally:
        page.close()


def test_ai_generation_failure_shows_danger_toast_with_retry_and_preserves_prompt(live_server, browser):
    """Triggers the real 429 rate-limit path (a token bucket, capacity 3,
    refilling at CRDT_CAD_GENERATE_PER_MINUTE / 60 tokens/sec -- see
    security.generate_rate_limiter) rather than mocking a failure -- the
    client's error handling is generic (any non-ok response), so this
    exercises the exact same code path a 422/504 would.

    mesh3d.js is loaded as a module, so its generateMesh() isn't callable
    from page.evaluate() (module-level bindings aren't globals -- see the
    identical constraint noted in the D6 presence tests), and clicking
    #genBtn repeatedly *serializes* requests instead of overlapping them
    (it stays disabled until its own fetch resolves) -- whether repeated
    clicks land inside the bucket's 3-request burst before it starts
    refilling would then depend on how long each generation happens to
    take server-side, exactly the kind of timing this suite has
    repeatedly found flaky under load. Exhausting the bucket directly via
    Playwright's own request context first (bypassing the page's JS
    entirely, 3 raw POSTs matching the bucket's exact capacity) makes the
    *one* real, UI-driven click deterministically 429 regardless of
    server speed, while still exercising the genuine client code path for
    that one click.
    """
    room = "e2e-art-gen-failure"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        for _ in range(3):
            page.request.post(f"{live_server}/api/mesh/{room}/generate", data='{"prompt": "warm-up"}', headers={"Content-Type": "application/json"})

        prompt = "a small house"
        page.fill("#genPromptInput", prompt)
        page.click("#genBtn")

        page.wait_for_function(
            "document.querySelector('#toastContainer .toast') && "
            "document.querySelector('#toastContainer .toast').textContent.includes('failed')",
            timeout=10000,
        )
        assert page.evaluate("!!document.querySelector('#toastContainer .toast-action')")
        assert page.eval_on_selector("#genPromptInput", "el => el.value") == prompt
        assert not page.eval_on_selector("#genPromptInput", "el => el.classList.contains('ai-thinking')")
    finally:
        page.close()
