"""Browser-driven end-to-end tests for Part 3 Phase D1: the design
system's theme toggle and icon sprite mechanics. These need a real
browser because both are pure client-side behavior with no server
counterpart: `localStorage` persistence across reload/navigation, and
whether an SVG icon actually renders (not just whether its DOM markup
is present -- the icon sprite's first draft had exactly this failure
mode: correct markup, nothing visible, no console error at all -- see
docs/design-system.md's "Icon sprite" section for the isolation test
that caught it).
"""

import pytest

pytestmark = pytest.mark.e2e


def test_theme_persists_across_reload_and_across_pages(live_server, browser):
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/")
        page.wait_for_selector("#themeToggleBtn", timeout=10000)

        initial = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert initial in ("dark", "light")

        page.click("#themeToggleBtn")
        page.wait_for_timeout(150)
        toggled = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert toggled != initial, "clicking the theme toggle should flip data-theme"

        stored = page.evaluate("localStorage.getItem('crdt_cad_theme')")
        assert stored == toggled

        # reload the same page -- must not revert to the original theme
        page.reload()
        page.wait_for_selector("#themeToggleBtn", timeout=10000)
        after_reload = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert after_reload == toggled

        # navigate to the 2D demo -- must inherit the same persisted theme
        room = "e2e-theme-persist"
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        on_2d = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert on_2d == toggled

        # and the 3D demo too
        page.goto(f"{live_server}/3d?room={room}-3d")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        on_3d = page.evaluate("document.documentElement.getAttribute('data-theme')")
        assert on_3d == toggled
    finally:
        page.close()


def test_icons_actually_render_not_just_present_in_dom(live_server, browser):
    """Regression test for the exact bug the icon sprite's first draft
    had: an external `<use href="/static/icons.svg#id">` reference had
    perfectly correct DOM markup and produced zero console errors, but
    rendered nothing at all. Asserting a non-zero bounding box on the
    referenced `<use>` element (once the sprite has loaded) is what
    would have caught that -- DOM presence alone would not have."""
    room = "e2e-icons-render"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        # the sprite is fetched+injected asynchronously by loadIconSprite()
        page.wait_for_function("document.getElementById('icon-pen') !== null", timeout=5000)

        icon_count = page.locator("svg.icon").count()
        assert icon_count > 15, f"expected many icon elements in the 2D toolbar, got {icon_count}"

        # a specific, known icon (#toolPen's pen icon) must have real
        # rendered geometry, not just exist as an empty <use> reference
        box = page.eval_on_selector(
            "#toolPen svg.icon use",
            "el => { const b = el.getBoundingClientRect ? el.getBoundingClientRect() : null; "
            "const svg = el.closest('svg'); const r = svg.getBBox ? svg.getBBox() : null; "
            "return r ? [r.width, r.height] : [0, 0]; }",
        )
        assert box[0] > 0 and box[1] > 0, f"icon-pen's <use> should resolve to real geometry, got bbox {box}"
    finally:
        page.close()


def test_no_emoji_glyphs_remain_in_toolbar_button_text(live_server, browser):
    room = "e2e-no-emoji"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/2d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        # every one of these buttons used to start with an emoji glyph
        # (e.g. "✏ Pen") -- confirm the visible text is now just the
        # plain label (the icon itself is a non-text <svg>, invisible to
        # .textContent). Checked individually since querySelectorAll
        # returns document order, not the order they're listed here.
        expected = {
            "#toolPen": "Pen", "#toolSelect": "Select", "#toolRect": "Rect",
            "#undoBtn": "Undo", "#redoBtn": "Redo", "#saveBtn": "Save", "#shareBtn": "Share",
        }
        for selector, label in expected.items():
            text = page.eval_on_selector(selector, "e => e.textContent")
            assert text == label, f"{selector}: expected {label!r}, got {text!r}"
    finally:
        page.close()
