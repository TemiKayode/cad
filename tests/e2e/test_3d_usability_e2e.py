"""Browser-driven end-to-end tests for Phase 16: 3D usability --
parametric primitives (Box/Cylinder/Pyramid/Plane) built from the same
batched-op/composite-undo idiom as `extrudeFace`, grid/vertex snapping,
and the axis-aligned view buttons. These need a real browser because
`mesh3d.js` is loaded as an ES module: its state lives in module scope,
invisible to `page.evaluate()`, so assertions read back through the DOM
(vertex/face counts, the Vertices panel's own coordinate inputs) exactly
as a user would see them -- which is also what caught the stale-panel-
after-drag bug during manual verification of this phase.
"""

import pytest

pytestmark = pytest.mark.e2e


def _vertex_positions(page):
    inputs = page.locator("#vertexList .vertex-coord")
    vals = [float(inputs.nth(i).input_value()) for i in range(inputs.count())]
    return [tuple(vals[i:i + 3]) for i in range(0, len(vals), 3)]


def test_box_primitive_creates_in_one_click_and_undo_redo_is_atomic(live_server, browser):
    room = "e2e-3d-box-primitive"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        page.click("#toolBox")
        page.wait_for_selector(".primField", timeout=3000)
        page.mouse.click(cx, cy)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '8'", timeout=5000)
        page.wait_for_function("document.getElementById('faceCount').textContent === '6'", timeout=5000)

        # A single Undo removes the WHOLE box (all 8 vertices + 6 faces),
        # proving it was pushed as one composite undo entry, not eight.
        page.click("#undoBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '0'", timeout=5000)
        page.wait_for_function("document.getElementById('faceCount').textContent === '0'", timeout=5000)

        page.click("#redoBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '8'", timeout=5000)
        page.wait_for_function("document.getElementById('faceCount').textContent === '6'", timeout=5000)
    finally:
        page.close()


def test_cylinder_pyramid_plane_primitives_produce_expected_topology(live_server, browser):
    room = "e2e-3d-other-primitives"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        # Cylinder with segments overridden to 8: 16 ring vertices,
        # 8 side faces + 2 caps = 10 faces.
        page.click("#toolCylinder")
        page.wait_for_selector(".primField", timeout=3000)
        fields = page.locator(".primField")
        fields.nth(2).fill("8")
        fields.nth(2).dispatch_event("change")
        page.mouse.click(cx, cy)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '16'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "10"
        page.click("#undoBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '0'", timeout=5000)

        # Pyramid, default 4-segment base: base ring (4) + apex (1) = 5
        # vertices; 4 side faces + 1 base face = 5 faces.
        page.click("#toolPyramid")
        page.wait_for_selector(".primField", timeout=3000)
        page.mouse.click(cx, cy)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '5'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "5"
        page.click("#undoBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '0'", timeout=5000)

        # Plane: 4 corner vertices, 1 face.
        page.click("#toolPlane")
        page.wait_for_selector(".primField", timeout=3000)
        page.mouse.click(cx, cy)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '4'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "1"
    finally:
        page.close()


def test_repeated_extrude_clicks_build_upward_without_a_validity_warning(live_server, browser):
    """Regression test for two compounding bugs a real screenshot caught:
    repeatedly clicking Extrude -- the natural "keep building the wall
    taller" motion -- produced a scary non-manifold validity warning on
    completely ordinary, single-user, sequential clicks.

    Root causes, both fixed in extrudeFace()/renderFacePanel():
    1. Every extrude's side/top faces were wound with the SAME sense as
       their base instead of the opposite one manifold winding requires
       -- a real, always-reproducible defect (not a concurrent-edit
       corner case), independently verified against
       crdt_cad.geometry.mesh_validity.
    2. ui.selectedFace stayed pointed at the original base face after
       extruding (compounded by renderFacePanel()'s in-progress-edit
       focus guard blocking its own refresh, since the just-clicked
       Extrude button is itself inside the panel and still focused), so
       a second click re-extruded the SAME base loop.
    3. Even with (2) fixed, extruding a face that IS itself a bare,
       never-customized cap from a prior extrude now merges that cap
       away instead of keeping it -- two Extrude clicks in a row builds
       one continuous wall, not two solids sharing an internal floor
       (which is unavoidably an edge shared by 3+ faces).

    A face the user has since colored is treated as a real floor and is
    kept, and extruding from it correctly still surfaces the (now
    accurate, not spurious) shared-edge warning -- verified below too."""
    room = "e2e-3d-extrude-repeat"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        # Plane: 4 corner vertices, 1 face -- the simplest possible
        # extrude starting point.
        page.click("#toolPlane")
        page.wait_for_selector(".primField", timeout=3000)
        page.mouse.click(cx, cy)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '4'", timeout=5000)

        page.click("#toolMove")
        page.click("#faceList .path-row .name")
        page.wait_for_selector("#extrudeBtn", timeout=5000)

        # First extrude: +4 top vertices, +4 side faces +1 top face.
        page.click("#extrudeBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '8'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "6"

        # Second click on the SAME (unmoved) button, with no other
        # interaction in between -- exactly the trap this regression
        # test is for. If selection (and the button's closure) had
        # stayed on the base face, this click would re-extrude it
        # instead of the new top face. The bare top cap from extrude 1
        # merges away (6 - 1 + 5 = 10, not 6 + 5 = 11): one continuous
        # 2-story wall, no internal floor left behind.
        page.click("#extrudeBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '12'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "10"

        # A third click continues the same merge-away behavior.
        page.click("#extrudeBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '16'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "14"

        page.wait_for_timeout(500)
        assert page.locator(".validity-banner").count() == 0, (
            "repeated Extrude clicks on a freshly-placed plane should never trip the mesh validity warning"
        )

        # Coloring the current top cap makes it a "real floor" -- opts
        # OUT of the merge-away behavior. Extruding again keeps it, and
        # the resulting internal shared floor is genuinely, correctly
        # non-manifold: that warning is now accurate, not a false
        # positive, and should still appear.
        page.fill("#faceColorInput", "#336699")
        page.locator("#faceColorInput").dispatch_event("input")
        page.wait_for_timeout(300)
        page.click("#extrudeBtn")
        page.wait_for_function("document.getElementById('vertexCount').textContent === '20'", timeout=5000)
        assert page.locator("#faceCount").inner_text() == "19"  # kept, not merged: 14 + 5
        page.wait_for_timeout(500)
        assert page.locator(".validity-banner").count() == 1, (
            "extruding from a face the user has customized should keep it as a real floor, "
            "and the resulting internal shared-edge warning is correct, not spurious"
        )
    finally:
        page.close()


def test_grid_snap_aligns_placement_to_integer_coordinates(live_server, browser):
    room = "e2e-3d-grid-snap"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        page.click("#toolVertex")
        page.click("#snapToggleBtn3d")
        # An arbitrary, non-grid-aligned click should still land on an
        # integer X/Z once Snap is on.
        page.mouse.click(cx + 33, cy + 17)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '1'", timeout=5000)
        gx, gy, gz = _vertex_positions(page)[0]
        assert gx == round(gx) and gz == round(gz), f"grid-snapped vertex should land on integer X/Z, got {(gx, gy, gz)}"
    finally:
        page.close()


def test_vertex_snap_drag_lands_exactly_on_target_vertex_and_refreshes_panel(live_server, browser):
    """Also covers a real bug this phase's verification caught: the
    Vertices side panel never re-rendered after a drag completed, so it
    kept showing the pre-drag coordinate even though the 3D scene and
    underlying state were already correct. Asserting on the panel's own
    `.vertex-coord` inputs (not on the 3D scene) is what would have
    caught it."""
    room = "e2e-3d-vertex-snap"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        canvas = page.locator("#canvas3d")
        box = canvas.bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

        page.click("#toolVertex")
        # Place A and B with snap OFF, so both land exactly at their raw
        # click coordinates (grid-snapping either would make the re-click
        # below miss its sphere and create a third vertex instead).
        ax, ay = cx - 60, cy - 60
        bx, by = cx + 80, cy + 80
        page.mouse.click(ax, ay)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '1'", timeout=5000)
        page.mouse.click(bx, by)
        page.wait_for_function("document.getElementById('vertexCount').textContent === '2'", timeout=5000)

        page.click("#snapToggleBtn3d")  # ON, only for the drag itself

        # Grab B and drag it back to A's exact original screen position --
        # both were computed from the same ray-plane intersection, so this
        # is guaranteed to land within the vertex-snap threshold.
        page.mouse.move(bx, by)
        page.mouse.down()
        page.mouse.move(ax, ay, steps=5)
        page.mouse.up()
        page.wait_for_timeout(150)

        positions = _vertex_positions(page)
        assert len(positions) == 2, f"the drag must have re-grabbed B, not created a third vertex -- got {positions}"
        assert positions[0] == positions[1], "dragging a vertex near another should snap onto its exact position"
    finally:
        page.close()


def test_view_buttons_reposition_camera_without_error(live_server, browser):
    room = "e2e-3d-view-buttons"
    page = browser.new_page()
    try:
        page.goto(f"{live_server}/3d?room={room}")
        page.wait_for_function("document.getElementById('statusText').textContent === 'online'", timeout=10000)
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        for view_id in ["viewTop", "viewFront", "viewRight", "viewPerspective"]:
            page.click(f"#{view_id}")
            page.wait_for_timeout(100)

        assert not console_errors, f"view buttons must not raise: {console_errors}"
    finally:
        page.close()
