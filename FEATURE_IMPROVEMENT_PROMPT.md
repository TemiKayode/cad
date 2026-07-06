# Feature brief: make crdt-cad genuinely usable by engineers, designers, and everyone else

> **Status (2026-07-05): COMPLETE.** All phases 10–17 are implemented and
> committed (see git log). This file is retained as the record of what was
> asked; do not re-execute it.

## Context

This is Part 2 of the improvement plan for `crdt-cad`. Part 1
(`IMPROVEMENT_PROMPT.md`) hardens the platform — security, CI, offline
durability, scaling, mesh validity. This file covers **product features**:
the editing capabilities real users need. Read `README.md` first, and read
Part 1's "Non-negotiable working rules" — every rule there (no unverifiable
features, no JS CRDT engine, tests + README update + browser verification per
feature, pip-only / no-build-step frontend, no Claude co-author trailer,
phase-by-phase commits) applies to this file too.

Part 1 phases 1–2 (security, CI) must land before starting this file. After
that, Part 1 and Part 2 phases may be interleaved by value.

**The core problem this file fixes:** the collaboration engine is more
advanced than the CAD editor sitting on top of it. Today the 2D demo has
exactly three tools (freehand Pen, Select, strict Polygon), draws in raw
screen pixels with no zoom/pan, no snapping, no shape primitives, no numeric
input, and no units. The 3D demo requires placing every vertex by hand. A
fully tested five-constraint Gauss-Newton solver sits on the server with no
UI to reach it. Rooms are bare URL strings with no browser, history, or
access levels.

**CRDT design rule for every feature below:** prefer representing new state
as *independently-mutable fields* in the existing `LWWMap` prop bags
(`path_props`, `face_props`, `layer_props`) or as new parallel CRDT
components, so concurrent edits merge field-wise for free. Never invent a
merge strategy in JS; the browser stays a thin renderer that mints ops and
renders confirmed state.

## Phase 10 — A real 2D viewport (blocks everything else)

The canvas currently maps document coordinates 1:1 to screen pixels, so the
drawable universe is one window. Fix this first because every later tool
depends on world coordinates.

- Introduce a client-side view transform (pan x/y + zoom) in `sketch.js`.
  All stored geometry becomes **world coordinates**; only rendering and
  input mapping go through the transform. The view transform is
  client-local state — it is *not* CRDT data and must not sync.
- Mouse-wheel zoom (centered on cursor), middle-drag / space-drag pan,
  a **fit-to-content** button, and a zoom-level indicator.
- Live cursor coordinate readout in the status area.
- Grid rendering that adapts to zoom (fade minor lines out as they compress),
  plus an optional **snap-to-grid** toggle.
- Remote presence cursors and comments must render correctly through the
  transform (they already live in world/document coordinates once this
  lands).
- Existing rooms hold pixel-space data; that's fine — world space is a
  superset. Note in README that pre-existing drawings keep their coordinates.

## Phase 11 — Shape primitives, numeric input, and units

- New tools: **Line, Rectangle, Circle, Ellipse, Arc** (3-point or
  center+radius+angles).
- Representation: store the parametric definition in `path_props`
  (e.g. `{"shape": "circle", "cx": ..., "cy": ..., "r": ...}`) and derive
  the polyline/curve rendering and exports from it. Because `LWWMap` merges
  per-field, two users concurrently editing a circle's radius and its center
  merge conflict-free with zero new CRDT code. The RGA point list remains
  the representation for freehand/polygon paths only.
- **Precise numeric input**: while a shape tool is active, an inline input
  accepts typed dimensions (width × height for rectangle, radius for circle,
  length + angle for line). Tab cycles fields; Enter commits.
- **Document units**: add a document-level `LWWMap` for settings
  (`units: "mm" | "in" | "px"`, grid spacing, snap step) — a new component
  on `DrawingDocument` with the same serialization/merge/ops_since treatment
  as the others, plus Python tests. The coordinate readout, numeric inputs,
  and dimension features (Phase 13) all display in document units.
- SVG/DXF export must respect units (DXF `$INSUNITS`, SVG `viewBox` scale).

## Phase 12 — Selection editing: transform, duplicate, snap

- **Move / rotate / scale** for a selected path or multi-selection
  (marquee select + shift-click). Represent whole-path transforms as a
  `transform` field in `path_props` (translation + rotation + uniform scale)
  rather than rewriting every RGA point: an LWW field write merges cleanly
  against a concurrent point-append to the same path, which point-rewriting
  would not. Bake the transform into coordinates only at export time.
- **Duplicate** (Ctrl+D) and **copy/paste** (Ctrl+C/V, JSON on the
  clipboard, works across rooms), **Delete** key removes the selection.
- **Align / distribute** actions for multi-selections (left/center/right,
  top/middle/bottom, equal spacing).
- **Object snapping**: while drawing or moving, snap to endpoints, midpoints,
  and circle centers of nearby geometry, with the standard visual glyphs
  (square = endpoint, triangle = midpoint, circle = center). Snapping is
  client-side input assistance only — no CRDT changes.
- Keyboard shortcut overlay (`?` key) documenting all bindings.

## Phase 13 — Measurement and dimensions (engineers)

- **Measure tools** (read-only, client-local): distance between two picked
  points, angle between two segments, area/perimeter of a closed path.
  Results show in document units.
- **Dimension annotations** (shared, persistent): a linear dimension object
  that references two anchor points (by path id + RGA element id, the same
  referencing pattern `comments` already uses) and renders as a standard
  dimension line with extension lines and a value label. Because it
  references geometry rather than copying coordinates, it **updates
  automatically when the geometry moves**. Store dimensions in a new
  document component (`LWWMap[dim_id, payload]`).
- Export dimensions to DXF as `DIMENSION` entities where `ezdxf` supports
  it; render them into SVG export as line+text groups.

## Phase 14 — Interactive constraint UI (the solver finally earns its keep)

The Gauss-Newton solver (`geometry/constraints.py`) and `POST /api/solve`
are built and tested; no UI reaches them. This is the highest-leverage
engineering feature in the project.

- A **Constrain** mode in the 2D demo: pick two entities (path endpoints,
  segments), then apply one of the five constraint kinds the solver supports
  (coincident / tangent / perpendicular / parallel / fixed-distance, with a
  numeric input for the distance).
- On apply: client sends current points + all constraints on the affected
  paths to `/api/solve`; solved positions come back and are committed as
  ordinary point-move ops through the normal relay path (so collaborators
  see geometry snap into place, and undo works via the existing inverted-op
  machinery).
- Constraints persist in a new document component
  (`LWWMap[constraint_id, spec]`), render as small badge glyphs near the
  constrained entities, and can be selected + deleted.
- Re-solve automatically when a constrained point is dragged (drag emits the
  solve request on pointer-up, not per-frame).
- Honest scope note for README: this is a *sketch* constraint system (solve
  on demand), not a full parametric feature tree.

## Phase 15 — Designer features

- **Text tool**: a text object with position, content, font size, color —
  all fields in its prop bag. Render in canvas; export to SVG `<text>`
  (DXF `TEXT` where feasible). Concurrent edits to the *content* string are
  last-writer-wins (an LWW field), not collaborative rich text — say so in
  the README rather than half-building a text CRDT.
- **Fills**: closed paths get `fill` + `fill_opacity` props; render with
  correct z-order (layer order, then creation order). Export to SVG.
- **Stroke styles**: dash patterns (`solid | dashed | dotted`) as a prop.
- **Groups**: a `group_id` field in `path_props` plus a groups component
  (`LWWElementSet`). Selecting any member selects the group; transforms
  (Phase 12) apply group-wide; ungroup clears the field.
- **PNG export**: client-side `canvas.toBlob()` of the current view plus a
  fit-to-content variant — no server work needed.
- Curves (Bezier support) are Part 1 Phase 8; if not yet done, do them
  before or with this phase — text + fills + curves together is what makes
  the 2D tool credible for design work.

## Phase 16 — 3D usability (stop making users place every vertex)

- **Parametric primitives**: Box, Cylinder (n-segment prism), Pyramid,
  and Plane tools that generate vertices/faces through the same batched-op
  path the AI generator already uses (`Room.commit_ops_batched` /
  `generate`-style op minting client-side). Dimensions typed numerically
  before placement.
- **Precise extrude**: the existing Extrude gains a numeric distance input
  (currently implicit).
- **3D snapping**: snap placed vertices to grid intersections and to
  existing vertices; snap dragged vertices likewise.
- **Orthographic views**: Top / Front / Right / Perspective buttons that
  reposition the Three.js camera (client-local, no CRDT).
- **Undo/redo for 3D** is Part 1 Phase 4 — it must land with or before this
  phase; primitives without undo are dangerous.
- Explicitly out of scope (README roadmap material, do not attempt here):
  CSG booleans, revolve/loft/sweep, and any B-Rep representation — all
  blocked on the data-model questions Part 1 Phase 6 and the DAG-B-Rep
  roadmap item document.

## Phase 17 — Workspace: rooms become projects

- **Home page** (`/`, moving the 2D demo to `/2d`): lists existing rooms
  using the already-built `DocumentStore.list_rooms()`, with kind badge
  (2D/3D), last-modified time, a rename action, a "new drawing" button, and
  a thumbnail (server-side: render a small SVG/preview from the snapshot;
  3D can use a placeholder or client-captured thumbnail on save).
- **Version history**: keep the last N snapshots per room (new columns/rows
  in `SQLiteStore` with timestamps, pruned to N) instead of overwriting one
  row. A History panel lists them; **Restore** forks the chosen snapshot
  into a *new room* (safe and honest for a CRDT — no rewriting of a live
  room's causal history) with an optional advanced "restore in place"
  implemented as generated inverse ops if and only if it can be done through
  the normal op path. Document the fork-first choice in README.
- **Read-only share links**: extends Part 1 Phase 1's signed tokens with a
  `role: viewer | editor` claim. Viewer connections receive snapshots/deltas
  but any `ops` message from them is rejected server-side; the UI hides
  editing tools and shows a "view only" badge. Test the enforcement
  server-side (a hand-crafted viewer WS sending ops must be refused) — not
  just hidden buttons.
- **Display names**: a proper name prompt (persisted in `localStorage`)
  feeding the existing presence payloads, replacing raw actor ids anywhere
  they leak into the UI.

## Definition of done for this file

- Phases 10–17 implemented in order (or interleaved with Part 1 after its
  phases 1–2), each phase: pytest suite green, e2e/browser-verified,
  committed separately, README.md feature list + "Status at a glance" table
  updated.
- The README's framing must stay honest: features listed here that get
  descoped or partially built are recorded in the Roadmap with the same
  plainness the README uses today.
