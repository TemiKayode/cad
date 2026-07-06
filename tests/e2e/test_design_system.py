"""Part 3 Phase D8's performance/polish audit gate: screenshots of both
demos at four viewport widths in both themes (archived to
docs/screenshots/), plus automated checks the brief calls for --
focus-ring visibility, touch-target sizes on the mobile bottom bar,
aria-label presence on every icon-only button, and computed text
contrast >= 4.5:1 in both themes. See tests/test_frontend_kill_list.py
for the static-analysis half of this same audit (z-index scale, no
emoji, no bare outline:none, no layout-property transitions) that
doesn't need a real browser.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_DIR = REPO_ROOT / "docs" / "screenshots"

VIEWPORT_WIDTHS = [375, 768, 1280, 1920]
THEMES = ["dark", "light"]
DEMOS = [("2d", "2d"), ("3d", "3d")]


def _relative_luminance(rgb):
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(rgb1, rgb2):
    l1, l2 = _relative_luminance(rgb1), _relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _parse_rgb(css_color):
    # getComputedStyle always resolves to "rgb(r, g, b)" / "rgba(r, g, b, a)"
    nums = css_color[css_color.index("(") + 1 : css_color.index(")")].split(",")
    return tuple(int(float(n)) for n in nums[:3])


@pytest.mark.timeout(240)
def test_screenshot_matrix_at_every_viewport_and_theme(live_server, browser):
    """Not a pass/fail check on its own (screenshots are the artifact) --
    but does assert every page loads cleanly (reaches 'online', no
    console errors) at every combination, since a broken responsive
    layout would often also break something functional. 16 sequential
    page loads (2 demos x 2 themes x 4 viewports) comfortably exceeds
    the suite's default 30s per-test timeout, hence the override."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    console_errors = []

    for demo_label, path in DEMOS:
        for theme in THEMES:
            for width in VIEWPORT_WIDTHS:
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_init_script(f"localStorage.setItem('crdt_cad_theme', '{theme}')")
                page = context.new_page()
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
                room = f"e2e-design-system-{demo_label}-{theme}-{width}"
                page.goto(f"{live_server}/{path}?room={room}")
                page.wait_for_function(
                    "document.getElementById('statusText').textContent === 'online'", timeout=15000
                )
                page.wait_for_timeout(300)  # let the anti-FOUC theme + web fonts settle
                page.screenshot(path=str(SCREENSHOT_DIR / f"audit_{demo_label}_{width}_{theme}.png"))
                context.close()

    assert not console_errors, f"console errors during the screenshot matrix: {console_errors}"


def test_icon_only_buttons_have_aria_label(live_server, page):
    for path in ["/2d", "/3d", "/"]:
        page.goto(f"{live_server}{path}?room=e2e-design-system-aria")
        if path != "/":
            page.wait_for_function(
                "document.getElementById('statusText').textContent === 'online'", timeout=10000
            )
        offenders = page.evaluate("""
            () => [...document.querySelectorAll('button')]
                .filter(b => b.textContent.trim() === '' && b.querySelector('svg'))
                .filter(b => !b.getAttribute('aria-label') || !b.getAttribute('aria-label').trim())
                .map(b => b.id || b.className)
        """)
        assert not offenders, f"{path}: icon-only button(s) missing aria-label: {offenders}"


def test_focus_visible_ring_is_actually_visible(live_server, page):
    page.goto(f"{live_server}/2d?room=e2e-design-system-focus-ring")
    page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

    page.keyboard.press("Tab")  # skip-link
    page.keyboard.press("Tab")  # first real control
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    outline_width = page.evaluate("getComputedStyle(document.activeElement).outlineWidth")
    assert outline == "solid"
    assert outline_width != "0px"


def test_mobile_bottom_bar_touch_targets_meet_minimum_size(live_server, browser):
    context = browser.new_context(viewport={"width": 375, "height": 700})
    page = context.new_page()
    try:
        page.goto(f"{live_server}/2d?room=e2e-design-system-touch")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        sizes = page.evaluate("""
            () => [...document.querySelectorAll('.tool-rail button')].map(b => {
                const r = b.getBoundingClientRect();
                return [b.id, r.width, r.height];
            })
        """)
        assert sizes, "no tool-rail buttons found at the mobile breakpoint"
        for button_id, width, height in sizes:
            assert width >= 44, f"#{button_id} width {width}px is below the 44px touch-target minimum"
            assert height >= 44, f"#{button_id} height {height}px is below the 44px touch-target minimum"
    finally:
        context.close()


def test_text_contrast_meets_4_5_1_in_both_themes(live_server, page):
    for theme in THEMES:
        page.goto(f"{live_server}/2d?room=e2e-design-system-contrast-{theme}")
        page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        colors = page.evaluate("""
            () => {
                const s = getComputedStyle(document.documentElement);
                return {
                    primary: s.getPropertyValue('--text-primary').trim(),
                    secondary: s.getPropertyValue('--text-secondary').trim(),
                    bgApp: s.getPropertyValue('--bg-app').trim(),
                    bgPanel: s.getPropertyValue('--bg-panel').trim(),
                };
            }
        """)

        def hex_to_rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))

        primary_rgb = hex_to_rgb(colors["primary"])
        secondary_rgb = hex_to_rgb(colors["secondary"])
        bg_app_rgb = hex_to_rgb(colors["bgApp"])
        bg_panel_rgb = hex_to_rgb(colors["bgPanel"])

        for bg_name, bg_rgb in [("bg-app", bg_app_rgb), ("bg-panel", bg_panel_rgb)]:
            primary_ratio = _contrast(primary_rgb, bg_rgb)
            secondary_ratio = _contrast(secondary_rgb, bg_rgb)
            assert primary_ratio >= 4.5, f"{theme}: --text-primary vs --{bg_name} is only {primary_ratio:.2f}:1"
            assert secondary_ratio >= 4.5, f"{theme}: --text-secondary vs --{bg_name} is only {secondary_ratio:.2f}:1"
