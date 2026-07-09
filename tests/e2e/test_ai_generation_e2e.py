"""Browser-driven end-to-end test for Part 5 Phase G4: provenance (the
AI Generations panel), follow-up edits ("edit this spec" applied in
place), and one-unit undo (a single Ctrl+Z reverts a whole edit, both
its geometry and its spec-persistence record) -- a genuinely server-
driven round trip (two real /generate calls), not fakeable from static
markup alone.
"""

import pytest

pytestmark = pytest.mark.e2e


def test_generation_panel_edit_and_one_unit_undo(live_server, browser):
    room = "e2e-g4-provenance-edit-undo"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)

        page.fill("#genPromptInput", "a wooden table")
        page.click("#genBtn")
        page.wait_for_function(
            "document.getElementById('genStatus').textContent.startsWith('Last generation:')", timeout=15000
        )
        assert page.eval_on_selector("#generationCount", "el => el.textContent") == "1"

        # arm edit mode via the generation row's pencil button
        page.click("#generationList .path-row button[data-act='edit']")
        page.wait_for_function("document.getElementById('genBtn').textContent.includes('Apply edit')", timeout=2000)

        page.fill("#genPromptInput", "make it taller")
        page.click("#genBtn")
        page.wait_for_function(
            "document.getElementById('genStatus').textContent.startsWith('Edited:')", timeout=15000
        )
        # an edit refines the same generation -- never a second entry
        assert page.eval_on_selector("#generationCount", "el => el.textContent") == "1"

        vertex_ys_after_edit = page.eval_on_selector_all(
            "#vertexList .vertex-coord[data-axis='1']", "els => els.map(e => parseFloat(e.value))"
        )
        assert max(vertex_ys_after_edit) > 0.9  # the edited (taller) table's top

        page.keyboard.press("Control+z")
        page.wait_for_timeout(400)
        vertex_ys_after_undo = page.eval_on_selector_all(
            "#vertexList .vertex-coord[data-axis='1']", "els => els.map(e => parseFloat(e.value))"
        )
        assert max(vertex_ys_after_undo) < 0.8  # back to the original (pre-edit) height, in one step
        # the spec-persistence record reverted along with the geometry --
        # same composite undo entry, not a separate mechanism
        assert "wooden" in page.eval_on_selector("#generationList .path-row .name", "el => el.title")
    finally:
        page.close()
