"""Part 3 Phase D8's "kill list sweep": static-analysis checks over the
frontend source that don't need a real browser (see tests/e2e/
test_design_system.py for the checks that do -- focus-ring visibility,
touch-target sizes, aria-label presence, computed contrast). Plain
file-content assertions, run as part of the fast suite.
"""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "demo" / "static"

EMOJI_PATTERN = re.compile(
    "["
    "\U0001f300-\U0001faff"  # symbols & pictographs, supplemental symbols, emoticons, transport, etc.
    "\U00002600-\U000027bf"  # misc symbols, dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicators (flag emoji)
    "]"
)


def _read(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_no_emoji_glyphs_anywhere_in_frontend_source():
    for path in sorted(STATIC_DIR.glob("*.html")) + sorted(STATIC_DIR.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        matches = EMOJI_PATTERN.findall(text)
        assert not matches, f"{path.name} still contains emoji glyph(s): {matches}"


def test_every_z_index_declaration_uses_the_token_scale():
    # The scale itself (tokens.css) is allowed to *define* --z-* values
    # with raw integers; every *consumer* elsewhere must reference one
    # of those custom properties, never a hardcoded number.
    for path in sorted(STATIC_DIR.glob("*.css")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("z-index"):
                # also catch z-index appearing mid-declaration, e.g. "transform: ...; z-index: 1;"
                if "z-index:" not in stripped or stripped.lstrip("/*").strip().startswith("z-index,"):
                    continue
            if path.name == "tokens.css":
                continue  # the scale's own definitions are raw integers by design
            assert "var(--z-" in line, f"{path.name}:{lineno}: z-index not using the token scale: {line.strip()!r}"


def test_outline_none_only_appears_as_the_focus_visible_complement():
    """The one legitimate use of `outline: none` is the modern
    :focus-visible pattern (`:focus:not(:focus-visible) { outline: none }`
    paired with a real `:focus-visible` ring elsewhere) -- anything else
    would be a genuine kill-list violation (a focus ring removed with no
    replacement)."""
    css = _read("styles.css")
    assert ":focus-visible {" in css and "outline: 2px solid" in css, "no :focus-visible ring rule found"
    outline_none_lines = [
        line.strip() for line in css.splitlines() if re.search(r"outline:\s*(none|0)\b", line)
    ]
    assert len(outline_none_lines) == 1, f"expected exactly one outline:none rule, found {outline_none_lines}"


def test_layout_property_transitions_are_the_one_documented_exception():
    """`grid-template-columns` (the panel-collapse transition, .body in
    styles.css) is the sole deliberate exception -- documented inline
    with why a transform-only alternative can't produce the same
    effect. Any other transition naming a layout-triggering property
    (width/height/top/left/right/bottom/margin*/padding*) would be a
    real, unaudited kill-list violation."""
    css = _read("styles.css")
    layout_props = (
        "width", "height", "top", "left", "right", "bottom",
        "margin", "padding", "font-size",
    )
    violations = []
    for lineno, line in enumerate(css.splitlines(), start=1):
        if "transition:" not in line:
            continue
        if "grid-template-columns" in line:
            continue  # the one documented exception
        for prop in layout_props:
            if re.search(rf"transition:\s*[^;]*\b{prop}\b", line) or re.search(rf",\s*{prop}\b", line):
                violations.append((lineno, line.strip()))
    assert not violations, f"transition(s) on a layout property found: {violations}"
