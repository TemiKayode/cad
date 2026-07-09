# crdt-cad

A real-time, browser-based collaborative CAD engine whose geometric data
is represented entirely as CRDTs (Conflict-free Replicated Data Types),
implemented from scratch in pure Python. Two collaborators can edit the
same drawing while one of them is completely offline -- reachable
neither by the server nor by their peer -- and when they reconnect their
edits merge automatically with no conflict-resolution step and no lost
work. A "Time-Travel Merge" panel shows exactly what happened on each
branch before the merge completes.

This README documents what's built, how the CRDT engine and sync
protocol work, and how to run the workspace home page and its two live
demos (2D sketch, 3D mesh). It also says plainly what's still missing
and why, rather than papering over gaps.

Four stages of the same tool, in order of a real session: land on the
**workspace**, open a **2D sketch** with shapes/fills/dimensions, open a
**3D mesh** built by the AI generator, and reconnect after editing
offline to see the **Time-Travel Merge** panel.

<p align="center">
  <img src="docs/screenshots/workspace_home.png" width="24%" alt="Workspace home page listing a 2D and a 3D room, each with a kind badge, real thumbnail, rename, and history action">
  <img src="docs/screenshots/2d_sketch_demo.png" width="24%" alt="2D sketch demo: a rect and line floor plan with a filled circle, the full shape/measure/constrain toolset, and the selection inspector showing fill/dash/rotation">
  <img src="docs/screenshots/3d_mesh_ai_generated_house.png" width="24%" alt="3D mesh demo: a single-room cottage built by the AI text-to-3D generator, alongside the Box/Cylinder/Pyramid/Plane primitive tools and view/snap controls">
  <img src="docs/screenshots/time_travel_merge.png" width="24%" alt="Time-Travel Merge panel showing two branches diffed before an automatic conflict-free merge">
</p>

## Status at a glance

| Area | Status |
|---|---|
| CRDT core (vector clocks, Lamport `OpId`, LWW-Register/Map/Set, RGA) | **Done**, Hypothesis-fuzzed for convergence |
| Tombstone value-compaction (bounded RGA memory growth) | **Done** -- see "Responses to the architecture critique" below |
| Mesh CRDT (vertices/edges/face boundaries/per-face properties) + presence | **Done**, composed from the primitives above |
| Mesh undo/redo (incl. bundled extrude, Ctrl+Z/Ctrl+Y in the 3D demo) | **Done** -- inverted ops, not snapshots, same pattern as 2D |
| Cross-component mesh validity ("Validation Fork" / "Extrusion Nightmare") | **Done** -- post-merge warning broadcast, not a rejection gate, see below |
| AI text-to-3D generation (`src/crdt_cad/ai/`) | **Done, Phase G1-G4 (Part 5)** -- a 14-generator registry (house, table, chair, shelf, stairs, column, arch, door/window w/ real CSG cuts, fence, box/cylinder/cone/torus), Claude Fable 5 dispatch via real tool-use (heuristic fallback, no API key required), multi-object **scene composition** with a deterministic layout solver ("a table with four chairs around it"), a **sandboxed JSON geometry DSL** for open-vocabulary shapes no generator covers (hard-capped, validate→repair→fallback loop), **provenance + spec persistence + follow-up edits + one-unit undo** (select/edit/undo a whole generation, not vertex by vertex), pre-commit watertight/manifold/bounds validation + optional `pymeshlab` print-repair, injected as batched (per-object-staged, for scenes) CRDT ops -- see below for exact scope; G5-G7 (report-card UI, eval harness, hosted ML tier) not started |
| `DrawingDocument` (layers, paths, props, comments, presence, undo/redo) | **Done** |
| Geometry kernel: constraint solver (coincident/tangent/perpendicular/parallel/fixed-distance), numpy+numba | **Done**, own test suite incl. an independent Pythagorean-triple correctness check |
| Interactive constraint UI (2D demo **Constrain** tool) | **Done** (Phase 9, extended Phase 14) -- coincident/parallel/perpendicular/fixed-distance/tangent, persistent + undoable + badge glyphs + re-solve-on-drag, see below |
| 2D viewport: pan/zoom/adaptive grid/snap-to-grid (Phase 10) | **Done** -- client-local view transform, never synced; live-verified incl. presence cursors through an asymmetric transform, see below |
| Shape primitives: Line/Rect/Circle/Ellipse/Arc, numeric input, document units (Phase 11) | **Done** -- parametric `path_props` fields, native render/hit-test/SVG/DXF export per shape, see below |
| Selection editing: move/rotate/scale, multi-select, duplicate, copy/paste, align/distribute, object snap (Phase 12) | **Done** -- `transform` path_prop baked only at export time, canvas's own nested transform for rendering, see below |
| Measurement (Distance/Angle/Area, read-only) + Dimension annotations (Phase 13) | **Done** -- dimensions reference geometry by (path id, RGA node id), auto-updating live and exporting as real DXF `DIMENSION` entities, see below |
| Persistent sketch constraints, tangent, re-solve-on-drag (Phase 14) | **Done** -- new `constraints` document component, `movePathPoint` now undoable and remaps both dimensions and constraints onto a moved point's new node id, see below |
| Designer features: text, fills, stroke styles, groups, PNG export (Phase 15) | **Done** -- new `groups` component, filled shapes now hit-test their interior (not just the boundary), real z-order (layer then creation order) fixed both client- and server-side, see below |
| 3D usability: parametric primitives, snapping, axis-aligned views (Phase 16) | **Done** -- Box/Cylinder/Pyramid/Plane built from the same batched-op/composite-undo idiom as `extrudeFace`, no new CRDT machinery; view buttons reposition the existing perspective camera (not a true orthographic swap, see below) |
| Workspace: home page, version history, read-only share links, display names (Phase 17) | **Done** -- rooms are no longer bare URLs to remember: a home page lists/renames them with a real thumbnail, restore forks a version into a new room (never rewrites live history), and viewer-role tokens are enforced server-side at both the WS and REST layer, see below |
| Design system: color/type/space/motion tokens, dark+light theme, icon sprite (Part 3 Phase D1) | **Done** -- every emoji glyph replaced with a hand-authored SVG icon, `docs/design-system.md`, see below |
| Layout: icon tool rail, collapsible panels, document name, avatar stack (Part 3 Phase D2) | **Done** -- canvas gets most of the width back; responsive down to 375px, see below |
| Input feel: per-tool cursors, hover halo, token-correct snap/marquee colors (Part 3 Phase D3) | **Done** -- no drag-to-resize handles (a pre-existing, deliberate numeric-input-only scope decision, not reopened here), see below |
| Keyboard-first: command palette, single-key shortcuts, keyboard reachability audit (Part 3 Phase D4) | **Done** -- Ctrl/Cmd+K palette, per-tool single-key shortcuts, arrow-key nudge, Ctrl/Cmd+Z/Y now in the 2D demo too, grouped/searchable "?" overlay, skip-to-canvas link, focus-trapped modals, see below |
| State legibility: connection/save status cluster, toast queueing, empty states, undo-toasts (Part 3 Phase D5) | **Done** -- honest "Saved"/"Reconnecting" copy matching the real durability model, geometry-rejection red flash (2D only -- 3D has no pre-commit validity gate to hang it off), see below |
| Multiplayer presence: smooth cursors, avatar delight, remote selection/edit highlights, follow mode (Part 3 Phase D6) | **Done** -- 8-color both-theme-verified actor palette (was 10, pastel, dark-theme-only), a real "3D presence never sent until first edit" gap found and fixed, see below |
| Signature moments: Time-Travel Merge redesign, staged AI generation (Part 3 Phase D7) | **Done** -- per-author-colored merge chips, converge animation, copy audited to never say "conflict"; AI-gen progress genuinely driven by real WS batch arrival (not a fake timer), stage labels derived from real face-material data, see below |
| Performance and polish audit -- gate before "done" (Part 3 Phase D8) | **Done** -- **Part 3 (D1-D8) complete.** 2D render() measured at ~1.1ms/call (500 paths); 3D's equivalent hit a real sandbox limit (SwiftShader software WebGL, documented, not papered over); CLS 0.002-0.01 on all 3 pages; kill-list sweep found and fixed several real, pre-existing gaps (see below) |
| Hosted ML mesh-gen adapter (Meshy, `MESHY_API_KEY`) | **Built, not verified** (Phase 9) -- no API key available to test against the live service; fallback-to-procedural path is verified, see below |
| Geometry validity gate (reject zero-length / self-intersecting) | **Done**, server-side pre-commit gate; demoed live via the strict Polygon tool |
| WebSocket relay server (rooms, snapshots, delta resync) | **Done**, FastAPI/asyncio |
| Bounded periodic self-heal traffic (frontier ping, not a full snapshot) | **Done** -- O(actor count), not O(document size), see below |
| Security hardening (opt-in room tokens, CORS lockdown, rate limits, resource ceilings) | **Done** -- off/wide-open by default, see below |
| Durable persistence (SQLite snapshots, survives restart) | **Done**; optional `PostgresStore` for multi-process sharing, see below |
| Save / download (JSON, SVG, DXF for 2D; JSON, STL for 3D) | **Done** |
| Import (SVG, DXF reference geometry) | **Done** -- SVG now includes quadratic/cubic Bezier curves (Phase 8), see below; arcs and DXF splines/arcs remain unsupported |
| WebRTC P2P direct sync, WS-relayed signaling, relay fallback | **Done**, verified with a real two-tab RTCPeerConnection/DataChannel |
| "Time-Travel Merge" branch-preview UI | **Done** |
| Offline outbox durability (survives a hard refresh/closed tab) | **Done** -- IndexedDB, no JS CRDT engine added, see below |
| Prometheus metrics (`prometheus_client`) | **Done** |
| CI (GitHub Actions: pytest/ruff, e2e, Docker build) | **Done** -- `.github/workflows/ci.yml` |
| Committed browser e2e suite (`tests/e2e/`, Playwright) | **Done**, opt-in via `-m e2e`, 89 tests |
| Docker image + Compose stack | **Done**, built and run-verified, persistence-across-restart verified |
| Kubernetes manifests | **Validated on a real cluster** (kind) -- Mode A (1 replica/SQLite) and Mode B (3 replicas/Postgres+Redis, cross-pod fan-out, rolling restarts) both verified, HPA (CPU-based, scale 1->6->1 confirmed under real load), TLS ingress (`wss://` end-to-end), CI smoke test on every push -- see `k8s/README.md` |
| STEP export (`build123d`) | **Done** -- faceted B-Rep from `MeshCRDT`, optional extra, see below; IGES and STEP *import* not built |
| True horizontal scaling of room state (multi-pod) | **Done** -- optional `PostgresStore` + Redis pub/sub fan-out, opt-in via env vars, live-verified with two real server processes *and* against real Kubernetes pods (Phase 18) -- see below and `k8s/README.md` |
| TLS front door (Caddy + Docker Compose, VPS path) | **Done** -- `docker-compose.prod.yml` + `Caddyfile`, verified end-to-end: real HTTPS, `wss://` through the proxy, auth token flow, `X-Forwarded-For`-aware rate limiter -- see `docs/deployment.md` |
| Fly.io config | **Config provided, not live-deployed** -- `fly.toml` checked for valid TOML against Fly's documented schema by hand; no Fly account/token available to run `fly config validate`/`fly deploy` against the real platform -- see `docs/deployment.md` |
| Backups (SQLite online-backup API + Postgres `pg_dump`/`pg_restore`) | **Done** -- `scripts/backup_sqlite.py`, both with automated byte-for-byte restore tests -- see `docs/deployment.md` |
| Graceful shutdown (SIGTERM persists unsaved rooms, clean WS close) | **Done** -- verified against a real `docker stop` on a real container, not just a unit test -- see `docs/deployment.md` |
| Monitoring (Grafana dashboard + Prometheus alerts) | **Done** -- `monitoring/grafana-dashboard.json` + `monitoring/alerts.yml`, optional `docker-compose.monitoring.yml`, verified live with real traffic (screenshot: `docs/screenshots/grafana_dashboard.png`) -- see `docs/deployment.md` |
| Load/soak testing | **Done** -- `scripts/load_test.py` found and fixed a real ceiling bug (persist-per-message; see "Horizontal scaling seam" below), post-fix results (lossless to 600 concurrent clients, ~100-200 comfortable on a shared dev machine) recorded honestly in `docs/deployment.md` |
| Published container image (GHCR) | **CI workflow committed, not yet run** -- `.github/workflows/release.yml` builds+pushes on a `v*` tag; no tag has been pushed yet, so `ghcr.io/temikayode/crdt-cad` does not exist as of this writing -- see `docs/deployment.md`'s Quickstart note |
| Pyodide/WASM client-side engine | **Not built** -- deliberate; see rationale below |

## Quickstart

### Local (Python venv)

```bash
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"      # Windows; use .venv/bin/pip on macOS/Linux

./.venv/Scripts/python -m pytest tests/ -v   # 371 tests, ~25s

./.venv/Scripts/python -m uvicorn crdt_cad.server.app:app --reload
```

Everything above works with zero extra configuration. A few genuinely
optional, heavier capabilities are separate extras so the default
install stays lean: `pip install crdt-cad[postgres]` (shared room state
across processes), `[redis]` (cross-process broadcast fan-out), `[step]`
(STEP export -- a large `build123d`/OpenCascade dependency tree),
`[meshy]` (the hosted ML mesh-gen adapter's `requests` dependency, plus
`MESHY_API_KEY` set in the environment). None of them change default
behavior when omitted.

### Docker

```bash
docker compose up --build
```

Or, once a `v*` release tag has been pushed (which publishes the image
to GitHub Container Registry via `.github/workflows/release.yml` --
Phase 19.6), without cloning anything:

```bash
docker run -p 8000:8000 -v crdt-cad-data:/data \
  -e CRDT_CAD_DB_PATH=/data/crdt_cad.db \
  ghcr.io/temikayode/crdt-cad:latest
```

(No release tag has been pushed yet as of this writing, so the `ghcr.io`
image doesn't exist until the first `git tag v0.1.0 && git push origin
v0.1.0` -- the workflow is committed and structurally standard, but
honestly: it has not run yet. The package also needs to be flipped to
public in the GitHub UI after its first publish for anonymous pulls.)

Either way, start at the workspace home page, which lists every room
that's ever been saved (Phase 17 -- see below):

- `http://127.0.0.1:8000/` -- workspace home (room list, rename, history)
- `http://127.0.0.1:8000/2d` -- 2D sketch demo
- `http://127.0.0.1:8000/3d` -- 3D mesh demo

Open two browser tabs on the same `/2d?room=` or `/3d?room=` name to see
each other's edits live (over a direct WebRTC data channel when
negotiation succeeds, and always over the WebSocket relay too). Click **"Go offline"** in one tab, keep
editing, then click it again to reconnect -- if the other tab also
changed something while you were away, a **Time-Travel Merge** panel
shows both branches before merging; otherwise it merges instantly.

## Architecture

```
 browser tab A                                    browser tab B
 ┌───────────────────────┐                    ┌───────────────────────┐
 │ sketch.js / mesh3d.js  │◄──WebRTC DataChannel──►│ sketch.js / mesh3d.js │
 │  - mints OpIds         │   (direct, when it     │  - mints OpIds         │
 │  - optimistic render   │    negotiates)         │  - optimistic render   │
 └──────────┬────────────┘                    └──────────┬────────────┘
            │  WebSocket (JSON ops + signaling)            │
            ▼                                              ▼
      ┌────────────────────────────────────────────────────────────┐
      │  FastAPI/asyncio relay (crdt_cad.server.app)                  │
      │  Room = { DrawingDocument | MeshCRDT, clients }                │
      │  - geometry validity gate (crdt_cad.geometry) before apply     │
      │  - applies accepted ops to its authoritative copy, persists    │
      │  - relays ops + WebRTC signaling verbatim to the rest of room  │
      │  - answers snapshot / delta-since-vector-clock requests        │
      │  - export/import (SVG/DXF/STL/JSON), /api/solve                │
      └──────────────────────────┬─────────────────────────────────┘
                                  ▼
                     SQLiteStore (crdt_cad.persistence)
                     -- one row per room, swappable for Postgres
```

The server is the single authoritative merge point (per the brief:
"server is the authoritative source"). The browser client never
implements CRDT conflict resolution -- it only ever needs to (a) mint
new ops with a locally-unique, monotonically increasing `OpId`, and (b)
render whatever the server confirms. This is why the JS side is a thin,
easily-auditable renderer instead of a second parallel CRDT
implementation that could drift from the Python one -- see the design
note at the top of `demo/static/common.js`.

## The CRDT layer (`src/crdt_cad/crdt/`)

### Causal metadata (`clock.py`)

Two distinct clock concepts, used for two different jobs:

- **`OpId`** = `(lamport_counter, actor_id)`. A strict total order every
  replica computes identically, used to break ties deterministically
  inside a CRDT ("which concurrent write wins"). This *is* the Lamport
  timestamp requirement from the brief.
- **`VectorClock`** = `{actor: highest_counter_seen}`. Tracks what a
  replica has already seen, used purely for delta sync ("send me what I
  don't have yet") and for detecting concurrent (neither-side-is-an-
  ancestor) states -- exactly the situation an offline edit produces.

### `LWWRegister` / `LWWMap` / `LWWElementSet` (`lww.py`)

Last-writer-wins family, all built on one rule: every write (including a
delete, represented as a tombstone value) is stamped with an `OpId`;
whichever stamped value has the greater `OpId` wins. `LWWMap` is the
general "independently-mutable field bag" used for object/layer
properties -- concurrent edits to *different* fields of the same object
never conflict, only concurrent edits to the *same* field need a winner.
`LWWElementSet` is `LWWMap` specialized to membership, used for "does
this layer/path/face id currently exist."

### `RGA` -- Replicated Growable Array (`rga.py`)

The ordered-sequence CRDT, used for sketch path points and mesh face
boundary loops. Every element gets a globally unique `OpId` and
remembers the id of the element immediately to its left at insertion
time (`origin`, or `None` for "head of list"). Deleting only tombstones
an element (it keeps serving as a stable anchor for anything inserted
next to it later, even from a replica that hasn't heard about the
delete yet).

The integration rule (Roh et al.'s original RGA algorithm): when
integrating a new element, walk right from its origin; for each
existing element found there sharing the *same* origin, the one with
the **higher `OpId` stays left** -- the deterministic tie-break for
"who typed here first." Elements whose origin is deeper (a different,
already-resolved insertion) are skipped over as a block rather than
compared directly. Because an element's `OpId` is always causally
greater than its origin's (a Lamport clock guarantee), replaying *any*
known set of elements in ascending-`OpId` order always integrates
origins before the children that reference them -- which is what makes
the state-based `merge()` simple: union the element records by id, then
replay in `OpId` order. That replay is deterministic, so it converges
regardless of merge order.

This is tested directly: `tests/test_rga.py` includes a Hypothesis
property test that generates random insert/delete programs across three
simulated replicas in arbitrary interleavings and asserts they always
converge to an identical sequence.

**Known limitation, stated plainly:** pure RGA (unlike newer algorithms
such as YATA/Fugue) can *interleave* concurrent multi-element insertions
at the same anchor in a way that doesn't always match either author's
intended order, even though it always converges. For this project's
shapes (mostly append-only pen strokes, mesh boundary edits) that
anomaly essentially never manifests, but it's a known, published
trade-off of the algorithm the brief asked for by name, not an
oversight.

### `MeshCRDT` (`mesh.py`)

Rather than inventing a bespoke mesh-merge algorithm from scratch -- mesh
CRDTs are genuinely open research territory, and a hand-rolled one would
be easy to get subtly wrong -- this **composes** the primitives above:

- `vertices`: `LWWMap[vertex_id, (x, y, z)]`
- `edges`: `LWWElementSet[canonical "a\x1fb" string]` (wireframe existence)
- `face_index`: `LWWElementSet[face_id]` (does this face currently exist)
- `faces`: one `RGA[vertex_id]` per face -- its ordered boundary loop, so
  two people can concurrently insert a vertex into the *same* face
  boundary (e.g. both splitting an edge while offline) without
  clobbering each other
- `face_props`: one `LWWMap` per face id (`material`, `color`, ...) --
  the same "independently-mutable field bag" pattern as `path_props` on
  the 2D side. This is what lets the AI generator (below) tag a floor
  face `material="wood"` without that write ever conflicting with a
  concurrent edit to the face's boundary or a different property.
- `presence`: `LWWMap[actor_id, payload]`, same pattern as the 2D document

Merging a mesh is merging each component independently, which inherits
convergence from each component's own guarantee. What this layer
deliberately does **not** do: enforce manifoldness, winding, planarity,
or reject self-intersecting topology for 3D -- that validation exists
today only for the 2D case (see the geometry kernel section below);
extending it to meshes is on the roadmap.

**Undo/redo** ports `DrawingDocument`'s exact pattern (see below) to the
mesh: `MeshCRDT.undo()`/`redo()` never touch history directly, they
synthesize the opposite edit with a fresh `OpId` and run it through the
same primitives as any other change. Vertex creation and vertex *move*
are tracked as distinct undo entries even though both go through the
same `add_vertex()` call (undoing a move restores the previous position;
undoing a creation deletes the vertex outright) -- the same
previous-value bookkeeping `DrawingDocument.set_path_prop`'s undo entry
already uses. A composite entry kind bundles multi-op actions into one
undo step: `extrude_face()` creates a full ring of new vertices, side
faces, and a capping face in one call, and undoing it removes every bit
of that in one `undo()`, regardless of what a collaborator concurrently
changed elsewhere (`tests/test_mesh_undo.py::
test_undo_extrude_does_not_clobber_concurrent_vertex_move` exercises
exactly that race, mirroring `test_undo_does_not_clobber_concurrent_
remote_edit`). As with the 2D case, this Python implementation is a
tested reference for the algorithm's safety property; the live demo's
actual undo/redo is `mesh3d.js`'s independent client-side
reimplementation of the same entry kinds and composite-bundling rule
(Ctrl+Z/Ctrl+Y or Ctrl+Shift+Z, plus Undo/Redo buttons) -- exactly the
same relationship `sketch.js` has to `DrawingDocument.undo()`/`redo()`.

### `DrawingDocument` (`document.py`)

The 2D document model: `layers` (`LWWElementSet`) + `layer_props`
(`LWWMap` per layer) + `path_index` (`LWWElementSet`) + `paths` (`RGA`
per path -- the polyline points) + `path_props` (`LWWMap` per path:
colour, width, which layer, and the strict-polygon flag used by the
validity gate) + `comments` (`LWWMap`, CRDT-based annotations pinned to
geometry) + `presence` (`LWWMap` keyed by actor).

**Undo/redo** is implemented as the brief requires: *inverted ops, not
snapshots*. `undo()` doesn't roll back to a saved state -- it looks up
what the last local edit changed, synthesizes the opposite edit (a
fresh op with a brand-new `OpId`, minted now), and applies it through
the same local-mutation path as any other edit. That new op merges like
any other op, which is what makes it safe: undoing *your* change never
rolls back a collaborator's unrelated concurrent change (see
`test_undo_does_not_clobber_concurrent_remote_edit` in
`tests/test_document.py`, which exercises exactly that race).

### Serialization & sync

Every CRDT supports `to_dict()`/`from_dict()` (JSON, used over the
wire), `to_bytes()`/`from_bytes()` (MessagePack, used for durable
snapshots), `ops_since(vector_clock)` (incremental delta), and
`frontier()` (current `VectorClock`, sent alongside every
snapshot/delta).

## Geometry kernel (`src/crdt_cad/geometry/`)

### Constraint solver (`constraints.py`)

A `Sketch` holds named 2D points and `Constraint`s of exactly the five
kinds named in the brief: `coincident`, `tangent`, `perpendicular`,
`parallel`, `fixed_distance`. `sketch.solve()` runs Gauss-Newton: a
numba-jitted function assembles the residual vector for all constraints
in one compiled pass, a central-difference Jacobian (also numba-jitted)
is computed around it, and `numpy.linalg.lstsq` provides the
minimum-norm update each iteration -- appropriate for the "small
systems" the brief describes, and it falls back to plain Python
automatically if numba isn't importable.

Why central differences instead of hand-derived analytic Jacobians for
each of the five constraint kinds: an analytic Jacobian bug is exactly
the kind of thing that's hard to notice, because Newton-Raphson can
still limp toward *a* solution with a slightly-wrong Jacobian. A
central-difference Jacobian is automatically consistent with whatever
the residual function computes, so getting the (much simpler to reason
about and unit-test) residual right is sufficient. `tests/test_constraints.py`
includes an independent correctness check beyond "residual went to
zero": constraining only the two legs of a right triangle (via
`perpendicular` + two `fixed_distance`s) and then checking the
*unconstrained* hypotenuse comes out to exactly 5 (a 3-4-5 triangle) --
that can only pass if the constraint semantics mean what they claim.

This is also exposed as `POST /api/solve` (stateless -- send points +
constraints, get back solved positions).

### Interactive constraint UI (Phase 9)

The 2D demo's **Constrain** tool is a client-side workflow on top of
`/api/solve` -- no new server-side state or CRDT primitive. Click two
points (any paths, same or different); the panel offers **Coincident**,
**Parallel**, **Perpendicular**, and **Fixed distance**. Parallel/
perpendicular relate two *lines*, not bare points, so each selected
point's line is inferred from its live neighbor (the next point if it
has one, else the previous one) -- `findAdjacentPoint` in `sketch.js`.
The solved positions come back from the same tested solver; applying
them is where it gets interesting: `path_geom` (an `RGA`) has no
in-place "move" the way `MeshCRDT`'s vertex `LWWMap` does -- a value is
immutable once inserted -- so `movePathPoint` moves a point via
delete-then-reinsert at the same slot, same as `MeshCRDT` vertices
*would* have to if they were RGA-backed too. The point's node id changes
as a result; if a curve segment (Phase 8) was attached to the old id,
it's orphaned (harmless dead weight in `path_props`, but that segment
visually reverts to a straight line) -- an accepted, documented
trade-off for the common case this solver targets (straight-line
CAD-style sketches), not one worth solving here.

Live-verified with two independently-checkable scenarios: a Coincident
pair converging to the same position (checked via the server's own
`/export/json`, not just visually, and confirmed synced to a second
tab in the same room), and a Parallel pair's resulting direction
vectors reaching a cross product of ~0. Both exercise a genuinely
different code path (2-point vs. the 4-point/adjacent-inference
branch), and a regression from the first attempt is worth recording:
the first Parallel verification run used a drag gesture that
synthesized an extra intermediate point, so "the adjacent point" wasn't
what the test assumed -- not a solver or CRDT bug, a test-fixture
artifact, caught by checking the actual math (cross product) rather
than eyeballing "did the line look about right."

### Validity gate (`validity.py`)

`validate_new_point()` rejects a zero-length segment (always) and,
opt-in, a self-intersecting path (`check_self_intersection=True`).
This is wired into the server's `_handle_message` in `app.py` as a
genuine pre-commit gate: a `path_geom` insert is validated *before*
`room.doc.apply(op)` runs, and if it fails, the op is dropped (never
applied, never broadcast) and the origin client gets back
`{"type": "rejected", "reason": ...}`.

Self-intersection isn't enforced for the freehand Pen tool (crossing
your own doodle is normal and shouldn't be blocked) -- it's opt-in per
path via a `strict` property, which the 2D demo's **Polygon (strict)**
tool sets. Draw a self-crossing polygon with it and watch the closing
edge get rejected in real time; the client optimistically shows the
point until the rejection arrives, then removes it -- an honest,
visible "eventual consistency of the UI" moment rather than pretending
client-side prediction is authoritative.

## Persistence (`src/crdt_cad/persistence/`)

`DocumentStore` is a four-method interface (`save`/`load`/`list_rooms`/
`delete`) implemented by `SQLiteStore` -- one row per `(room kind, room
id)` holding the latest MessagePack snapshot, zero required
infrastructure to run this locally. Rooms hydrate from their last
snapshot when a server (re)starts, and every accepted ops batch triggers
an (awaited, not fire-and-forget -- see the note in `Room.persist_async`
about why that distinction mattered) persist. A client can also force
one via the **Save** button (`{"type": "save"}` -> `{"type": "saved", ...}`).

### Horizontal scaling seam: `PostgresStore` + Redis pub/sub

The brief asks for "PostgreSQL (JSONB) or an append-only event log" and a
pub/sub broker for room broadcast -- the two things that let more than one
server *process* (several k8s replicas behind one Service, say) share the
same room state, which `SQLiteStore` and single-process `Room.broadcast`
fundamentally can't do. Both are now real and opt-in via environment
variable; unset either one and behavior is exactly what it was before
this existed.

**`PostgresStore`** (set `CRDT_CAD_DATABASE_URL`) implements the same
`DocumentStore` interface against a real Postgres table (`BYTEA` snapshot
column) via `asyncpg`. `asyncpg` is async-only; the interface above it is
synchronous by design (so `Room`/`RoomManager`/the REST routes don't need
to change or even know which backend is active), so `PostgresStore`
bridges the two with a dedicated background thread running its own event
loop, forwarding every call through `asyncio.run_coroutine_threadsafe(...)
.result()`. That blocks the calling thread until the query completes --
the same trade-off `SQLiteStore` already makes (a blocking call during
room hydration and every persist), just against a network round-trip
instead of local disk. `asyncpg` is intentionally not a core dependency
(install with `pip install crdt-cad[postgres]`) -- the zero-config local
demo has no reason to pull in a Postgres driver it will never use, same
reasoning `pymeshlab` already gets.

**Redis pub/sub fan-out** (set `CRDT_CAD_REDIS_URL`) fills the other gap:
even with Postgres, a client connected to process A would never see a
process B client's edits, because `Room.broadcast()` only iterated its
own process's local `self.clients`. Now, `broadcast()` also publishes to
`room:{kind}:{room_id}`; every process's `Room` for that same room
subscribes to that channel and, on receiving another process's publish,
both applies the ops to its *own* `self.doc` (not just forwards the raw
message to its local clients -- more on why that distinction matters
below) and relays them locally. Each process tags its own publishes with
a process-unique id so its own relay loop recognizes and skips messages
it already delivered directly, instead of double-delivering every
locally-originated op to its own clients. `redis-py` is likewise not a
core dependency (`pip install crdt-cad[redis]`).

**A real bug this caught, not a hypothetical:** the first version of the
Redis relay only forwarded the raw WebSocket message to local clients,
without applying the ops to the *receiving* process's own `self.doc`.
Verified purely with the mocked-Room unit tests, that looked fine.
Verified live -- two genuinely separate `uvicorn` processes, a real
Postgres, a real Redis, and a real WebSocket client on each -- it broke
immediately: a *new* client connecting to process B after process A's
edit got a stale snapshot, because process B's server-side document had
never actually changed. Fixed by having the relay loop apply incoming ops
to its own document (and persist, and re-run the mesh validity check)
before relaying, exactly like `_handle_message` does for ops arriving
directly over a client's own WebSocket. This is the concrete reason this
project insists on live verification beyond unit tests for anything
crossing a real process/network boundary, not just anything
browser-facing.

Live-verified end to end (see `tests/test_postgres_store.py` and
`tests/test_redis_fanout.py` for the committed, skip-if-unavailable unit
suite, and the design note above for the two-process run that caught the
bug): two real `uvicorn` processes on different ports, both pointed at
one Postgres and one Redis container, each driven by its own raw
WebSocket client. An edit sent to process A's client arrived at process
B's client via Redis; a fresh third connection to process B afterward
correctly saw the data process A had persisted to the shared Postgres
store. See `k8s/README.md` for what this means for the included
Kubernetes manifests and exactly what was/wasn't validated there.

## Import / export (`src/crdt_cad/export/`)

- **SVG**: export always; import supports `<line>`, `<polyline>`,
  `<polygon>`, and `<path>` using `M`/`L` (absolute or relative) plus, as
  of Phase 8, quadratic and cubic Beziers -- `C`/`S`/`Q`/`T`, including
  the `S`/`T` "smooth" variants' reflected control point, the case real
  design-tool exports (Illustrator, Inkscape, Figma) actually rely on for
  a visually smooth join between segments. Elliptical arcs (`A`/`a`)
  are still not parsed: unlike every other unhandled case, an arc's
  flag arguments are single 0/1 digits that can appear with no
  separating whitespace or comma in real SVGs, so a tokenizer that
  didn't know to stop there would silently misinterpret them as
  coordinates and corrupt everything after -- worse than an honest
  partial import. Encountering one stops parsing that `<path>` and keeps
  whatever was accumulated before it, same as the original M/L-only
  importer's "truncate rather than guess" choice for anything else
  unhandled. Curves live in the document model as an ordinary
  `path_prop` per segment, keyed by that segment's own stable anchor
  node id (`curve_prop_key` in `crdt_cad.crdt.document`) -- not a new
  CRDT primitive, and every path created before this existed is still
  valid data (no curve prop = straight line). This also means two
  concurrent edits curving *different* segments of the same path are
  independent LWWMap writes, same guarantee color/width already have --
  verified with a real two-replica merge, not just asserted in a
  comment (`test_concurrent_curve_edits_to_different_segments_dont_clobber_each_other`
  in `tests/test_document.py`). One known, pre-existing gap this didn't
  touch: SVG import has never carried a source file's per-path
  stroke color/width into the document (every imported path gets the
  same default styling regardless of the source's own colors) -- caught
  while live-verifying curve rendering (an imported curve using the
  default near-black color was invisible against the canvas's own
  near-black background until manually recolored), not a Phase 8
  regression, just a pre-existing limitation this made newly visible.
- Both SVG and DXF export also render any dimension annotations
  (Phase 13) as, respectively, a `<g class="dimension">` line+text
  group and a real `DIMENSION` entity -- see "Measurement and
  dimensions" below for the full design.
- **DXF**: export/import via `ezdxf` (`LWPOLYLINE`/`LINE`/`POLYLINE`).
  `LWPOLYLINE` has no Bezier concept, so a curve segment is flattened
  into a dense sampled polyline (12 points per segment,
  `flatten_path_to_polyline`) on export -- an approximation, not a
  re-derivation of the true curve, but a faithful-looking one. DXF
  import does not (and cannot) reconstruct a curve from the flattened
  result -- reimporting a DXF this project exported gets back a denser
  polyline, not the original Bezier. This is exactly the trade-off the
  brief allows ("DXF export may flatten curves to polylines"); the
  validity gate stays polyline-only too, unchanged by any of this -- it
  only ever sees the raw anchor-point sequence a `path_geom` insert
  builds, regardless of whether a later, separate `path_prop` op turns
  the segment arriving at that anchor into a curve.
- **STL**: ASCII export for the 3D mesh, fan-triangulated per face (same
  technique the Three.js renderer uses client-side -- correct for the
  convex/simple polygons the Face tool and Extrude produce).
- **STEP (Phase 9, re-evaluated): built.** The README used to say
  `pythonOCC`/`cadquery` had no usable PyPI wheel and was conda-only in
  practice -- re-checked rather than left as a stale assumption, and it
  turns out `build123d` (which pulls in `cadquery-ocp-novtk`, a real
  wheel) installs and works fine via `pip install crdt-cad[step]` in
  this environment (Windows, Python 3.14), confirmed by actually
  building and exporting a real STEP (AP214) file, not assumed.
  `crdt_cad.export.step_export.mesh_to_step_bytes` turns each
  `MeshCRDT` face loop into one planar `Face`; nothing in this project
  enforces face planarity (see `crdt_cad.geometry.mesh_validity`'s
  module docstring), so a face that's drifted non-planar falls back to
  a fan triangulation (confirmed directly: `build123d`'s `Face`
  constructor raises `ValueError: Cannot build face(s): wires not
  planar` for a non-planar loop, rather than silently misbuilding it).
  If every face joins into one closed, positive-volume solid, that's
  written as a real `MANIFOLD_SOLID_BREP`; an incomplete/WIP mesh
  (`Solid(Shell(...))` silently gives volume `0.0` for an open shell
  instead of raising -- confirmed directly, not assumed) is written as
  an open `Compound` of faces instead of falsely claiming a closed
  solid. `build123d` is a genuinely heavy optional dependency (pulls in
  `scipy`/`scikit-learn`/`sympy`/`ipython`), so it's an opt-in extra,
  not a core dependency, and deliberately not part of the `dev` extra
  either (unlike the much lighter `asyncpg`/`redis`) -- a contributor
  who wants to exercise `tests/test_step_export.py` for real installs it
  separately. STEP *import* and IGES (either direction) remain
  unbuilt -- not asked for by the brief's re-evaluation.

All of this is reachable from the UI: **Save** (explicit durable
snapshot), download buttons for every format above (the 2D demo:
**.json/.svg/.dxf**; the 3D demo: **.json/.stl/.step**), and (2D only)
an **Import SVG/DXF** file picker that broadcasts the imported paths to
everyone currently in the room.

## AI text-to-3D generation (`src/crdt_cad/ai/`)

Type a prompt like *"a wooden dining table"* or *"a 4 bedroom house with
a gable roof and a garage"* into the **AI Generate** box on the `/3d`
demo, and a real, watertight, collaboratively-editable mesh appears in
the scene for everyone in the room -- built and synced through the
exact same CRDT machinery as a hand-placed vertex.

**Scope, stated honestly up front:** the always-available, fully-tested
pipeline does *not* run TripoSR, Hunyuan3D, or any other multi-GB GPU
mesh-diffusion model locally -- this project runs in a sandboxed,
CPU-only environment with no way to responsibly download, run, or
verify a several-gigabyte GPU checkpoint. What's built instead is a
genuinely working split of the same problem into its two real
sub-problems, now (Phase G1, Part 5) generalized from one hardcoded
archetype to a **registry of generators**:

1. **Language understanding and dispatch** -- turning "a wooden dining
   table" into *which generator* (table, not chair or house) plus that
   generator's own bounded structured parameters (`width_m=1.4`, ...)
   is squarely an LLM-shaped problem, and is where **Claude Fable 5** is
   actually used (`crdt_cad/ai/interpreter.py`, `_llm_interpret`): the
   registry is presented to the model as a **tool catalog** -- one tool
   per generator, each with its own spec's JSON schema, via real
   `tools`/`tool_choice` Anthropic tool-use, not one hand-widened union
   schema that would need editing every time a generator is added. The
   model's own server-side refusal-fallback
   (`betas=["server-side-fallback-2026-06-01"]` +
   `fallbacks=[{"model": "claude-opus-4-8"}]`) is enabled by default,
   and `interpret_prompt()` falls back to a keyword dispatcher
   (`_heuristic_interpret`, `registry.dispatch_by_keyword`) on *any*
   failure -- missing credentials, a network error, a safety refusal, a
   malformed response. That fallback isn't a rare degraded corner case:
   it's what actually runs in any environment without
   `ANTHROPIC_API_KEY` configured (including this one), so the feature
   is fully functional and fully tested with zero external credentials
   -- for the house generator specifically the heuristic also does real
   field extraction (bedrooms/floors/material/style/roof/garage/area,
   unchanged from before Phase G1); every other generator gets its
   spec's bounded defaults without an API key, a real working object
   with reduced vocabulary, never a broken result.
2. **3D construction** -- this is *not* asked of the model, for any
   generator. Every entry in `crdt_cad/ai/registry.py`'s `REGISTRY` is a
   `(bounded pydantic spec, deterministic build function)` pair: every
   vertex, edge, and face loop is computed geometry, never a
   hallucinated guess.

### The generator registry: from one archetype to a vocabulary (Phase G1)

Fourteen generators as of this phase, each in `crdt_cad/ai/generators/`,
each an assembly of the shared primitive builders in `mesh_builder.py`
(`add_box`/`add_cylinder`/`add_cone`/`add_torus`/`add_extruded_polygon`/
`add_extruded_profile_xy`) -- building each primitive as its own closed,
independently-watertight solid means an *assembly* (a table's top plus
four legs, a fence's posts plus rails) validates as one watertight mesh
with no boolean union needed, since `trimesh.Trimesh.is_watertight` is a
global edge-count property that several disjoint closed bodies already
satisfy:

- **`house`** -- the original generator (`procedural_house.py`), now
  enriched: `roof_type` (`flat`/`gable`/`hip`, real pitched geometry on
  the top floor, not just a slab), `garage` (an attached box),
  `bedrooms_per_floor` (each floor its own footprint, not just its own
  bedroom count -- "stories with distinct footprints"), `floor_area_sq_m`
  (uniformly scales the room grid so "a 30 square meter cabin" is
  honored to the square metre, not just a bedroom count), `wall_material`/
  `roof_material` (previously hardcoded), and `front_door`/
  `front_windows` -- real CSG-cut openings (see below) on the ground
  floor's front wall specifically, not yet every wall on every floor
  (documented honestly in `build_house_mesh`'s own docstring, not
  silently pretended).
- **Primitives** (`generators/primitives.py`): `box`, `cylinder`, `cone`,
  `torus` -- explicit dimensions in metres, segment counts bounded per
  spec.
- **Furniture** (`generators/furniture.py`): `table` (four legs + top),
  `chair` (four legs + seat + optional backrest -- `back_height_m=0`
  is a real, valid backless stool, matching its own "stool" dispatch
  keyword), `shelf` (two side panels + back panel + evenly spaced
  shelves).
- **Architectural elements** (`generators/architectural.py`): `stairs`
  (each step a solid box down to the ground), `column` (base plinth +
  shaft + capital, three stacked cylinders), `arch` (a genuinely
  non-convex annulus-segment profile extruded into a solid archway
  shape -- see below for why that specific shape is what caught a real
  triangulation bug), `fence` (evenly spaced posts + horizontal rails).
- **Wall openings** (`generators/wall_opening.py`): `door`, `window` --
  the one pair that needs a real boolean cut, not just an assembly of
  disjoint solids, since a door is a *hole through* a wall. Uses
  `trimesh`'s `manifold3d`-backed boolean engine (a pip-installable,
  compiled CSG library, no external CAD program) to subtract an opening
  box from a wall box, extended slightly past both wall faces so the
  cut boundary is never exactly coplanar with the wall's own faces (a
  classic degenerate-triangle trap in every CSG engine). `cut_wall_openings`
  generalizes to *multiple* openings cut from one wall in one pass
  (a door plus several windows), which is what the house generator's
  `front_door`/`front_windows` actually calls.

### Pre-commit validation: watertight/manifold/bounds, never a silent bad mesh

Every generator's output goes through `crdt_cad/ai/validation.py` before
it's ever turned into ops: bounding-box and vertex/face-count ceilings
(catches a runaway spec before it reaches the browser), a real
`trimesh`-backed watertightness check (no boundary edges -- an open
hole in the solid), a winding-consistency check (no non-manifold
topology), and a degenerate-triangle check (zero-area). A failure
raises `GenerationValidationError` (typed, not a bare exception),
which `POST /api/mesh/{room_id}/generate` turns into a `422` whose
`detail` is the full structured report (`errors`, `watertight`,
`manifold`, `within_bounds`), not just a joined string -- the AI
Generate panel renders it directly rather than a generic failure
message. The **house generator is the one documented exception**: it
predates this module, and its own docstring already describes why it
isn't (and wasn't designed to be) strictly watertight or
winding-consistent -- interior-wall "T-junctions" against the floor/roof
slabs, an accepted, pre-existing gap (confirmed pre-Phase-5, not a
regression: the exact same inconsistency reproduces against the
pre-Phase-5 code). `generator.py` relaxes both checks specifically for
`"house"` (and for a hosted-API mesh from Meshy, whose geometry isn't
this project's own code); every generator introduced in Phase G1 is
held to the full, strict bar.

**A real bug this caught during development, not a hypothetical:** the
arch generator's silhouette is a genuinely non-convex polygon (an
annulus segment), and a hand-rolled fan triangulation from one vertex --
exact for a convex polygon, wrong for a non-convex one -- produced
degenerate triangles. The fix was switching `add_extruded_polygon`/
`add_extruded_profile_xy` to delegate real triangulation to
`trimesh.creation.extrude_polygon` (via `shapely`), which handles convex
and non-convex simple polygons correctly; `tests/test_ai_mesh_builder.py`
pins an explicit non-convex L-shape regression case so this can't
silently regress. **A second real bug**, also caught by the validation
module rather than assumed away: `add_box`/`add_cylinder`/`add_cone`'s
initial winding was uniformly inverted (confirmed via `trimesh`'s
`is_volume` property, not just `is_watertight` -- a mesh can be
watertight and still have every normal facing inward), which is exactly
why `is_volume`/orientation is checked explicitly rather than trusting
`is_watertight` alone.

`generator.py` orchestrates prompt -> `(generator, spec)` -> mesh ->
validation -> a **chronological batch of CRDT ops**, minted under a
dedicated `ai_generator_bot` actor identity via its own `LamportClock`,
built against a throwaway `MeshCRDT` scratch instance so every `OpId`
is correctly ordered before any of it touches a live room:

- every vertex becomes a `vertices` (`LWWMap`) write,
- every face becomes a `face_index` add plus an ordered `faces` (`RGA`)
  insert per boundary vertex,
- every face with a material gets `face_prop` writes for `material` and
  a matching `color`, which `mesh3d.js` reads to render it in its
  actual material color instead of the default per-face hash palette.

**The batching answer, concretely:** `POST /api/mesh/{room_id}/generate`
(`app.py`) runs `generate_mesh_ops` in a worker thread via
`asyncio.to_thread` (under an `asyncio.wait_for` timeout, so a hung LLM
call can't tie up a thread-pool slot forever), then hands the resulting
op list to `Room.commit_ops_batched()` -- which applies+broadcasts them
in fixed-size chunks (150 ops by default,
`CRDT_CAD_GENERATION_BATCH_SIZE`), `await`ing `asyncio.sleep(0)` between
each chunk so one large generation never monopolizes the event loop or
arrives at clients as a single giant WebSocket frame; clients instead
watch the object build itself in visible stages. A validation failure
returns `422` with the structured report above; a malformed-geometry or
empty result also returns `422`; a timeout returns `504`; all covered
in `tests/test_generation_endpoint.py`. Live-verified in a real browser
across five generator types (house, table, box, door, chair) with a
screenshot confirming the door generator's CSG-cut opening actually
rendered as a real hole through the wall, not just validated as one on
paper.

### Scene composition: SceneSpec + a deterministic layout solver (Phase G2)

A single generator call handles "a wooden dining table," but not "a
table with four chairs around it" -- that's *multiple* objects in a
stated spatial relationship, and Phase G2 adds exactly that without
bending the "the LLM never emits geometry" rule: Claude (or the
heuristic fallback) only ever names *which* generators, *how many*, and
states the relation in plain terms (`around`/`on_top_of`/`row`/`beside`/
`none`); turning that into real `(x, y, z)` coordinates is ordinary
deterministic code in `scene_layout.py`, never the model.

- **`scene.py`**: `SceneSpec` is a bounded list (1-10) of
  `SceneObjectSpec` (generator name, its own nested spec re-validated
  against that generator's real pydantic model -- not accepted as an
  opaque dict, so a malformed nested spec fails with the same clear
  error a standalone generation would; a relation; a `target_index`
  into an *earlier* entry; a `count` 1-12 for "four chairs"-style
  repetition; a `spacing_m` for rows). A `model_validator` rejects
  forward/self references and a relation without a target at
  construction time, before any geometry is built. `expand_scene`
  builds every object's own mesh (dispatching `count` into that many
  copies, all sharing one relation/target so the solver can distribute
  them together) but positions nothing -- building what vs. placing it
  are kept as separately-testable steps.
- **`scene_layout.py`**: `solve_layout` computes one translation per
  object from its *actual measured bounding box*, never an assumed
  size -- ground-plane snapping (every object's Y sets its AABB's
  bottom to 0, except `on_top_of`, which snaps to the **target's own
  measured top after its own placement**, not a guessed height),
  `around` (the `count` copies distributed evenly around the target's
  center at a radius derived from both AABBs so they clear the target
  regardless of its actual size), `row` (evenly spaced along X by
  `spacing_m`, using each object's own measured width), `beside`
  (offset from the target's measured edge plus a margin), and `none`
  (a shared left-to-right placement cursor, so independent objects
  never collide with each other by construction). A final bounded
  (6-pass) pairwise-AABB-overlap sweep pushes apart anything from
  *different* relation groups that could still end up overlapping in
  the XZ plane (e.g. an `around` circle swinging into a later
  standalone object's path), skipping pairs that are intentionally
  related (an `on_top_of` object is *supposed* to overlap its target
  in Y). `merge_placed_objects` then combines every positioned object
  into one mesh with globally unique vertex/face ids (each generator's
  own `build` restarts its own ids at v1/f1, so a shared `MeshBuilder`
  is what makes a multi-object scene collision-free), returning
  `per_object_ids` -- which ids in the merged mesh belong to which
  input object, in build order.
- **Provenance, ahead of Phase G4's fuller system**: `generator.py`'s
  `_generate_scene_ops` uses `per_object_ids` to tag every face with a
  `set_face_prop(face_id, "scene_object", "<index>")` CRDT op alongside
  the existing `material`/`color` props -- `MeshCRDT.face_props` is a
  genuine per-face map (unlike `GeneratedMesh.face_materials`, a single
  string per face), so it already supports all three at once with no
  schema change.
- **Visible, object-by-object staging**: the same `per_object_ids`
  grouping also drives `GenerationResult.object_ops` -- `ops` grouped
  by object rather than one flat list. `Room.commit_ops_grouped_batched`
  (new in `app.py`, alongside the existing `commit_ops_batched`) forces
  a batch boundary between every object *in addition to* the usual
  size-based chunking within one, so a "table with four chairs around
  it" visibly places one object, then the next, regardless of how small
  any single object's own op count is -- extending the Phase D7 staged-
  build UI pattern. `mesh3d.js`'s `noteGenerationBatch` tracks distinct
  `scene_object` values seen across arriving batches for an honest
  "Placing object N..." progress line (a real count of batches that have
  actually arrived over the WS relay, not a fake timer), and
  `describeGeneratedSpec` gained a `"scene"` branch (object counts by
  generator, e.g. "2 object group(s): table, 4x chair") instead of
  falling through to the single-object dimension-guessing fallback.
- **Heuristic (no-API-key) scene parsing** (`interpreter.py`,
  `_heuristic_scene_interpret`): a handful of plain-English patterns --
  "a table with four chairs around it," "four chairs around a table,"
  "a box on top of a table," "a row of three shelves" -- parsed with
  regex and mapped to generator names through the *same* keyword table
  `dispatch_by_keyword` already uses (so scene parsing never maintains
  its own separate noun list), with word-or-digit counts
  (`"four"`/`"4"`) clamped to the same 1-12 range the spec enforces. A
  prompt that doesn't match any of these shapes just falls through to
  ordinary single-object dispatch -- reduced vocabulary, never a
  broken or silently-wrong result. The LLM path gets a `scene` tool
  added to its catalog (`_SCENE_TOOL`, `SceneSpec.model_json_schema()`
  as the input schema) alongside every generator's own tool, with the
  system prompt telling Claude when to reach for it.
- **Known scope limitation, found during live verification and stated
  here rather than glossed over**: `solve_layout` only reasons about
  the objects in *one* generation call -- it has no visibility into
  geometry a room already contains from an earlier generation or
  manual edits, so a new scene's placement can end up spatially
  overlapping pre-existing room content (this is *not* a validation
  gap: `validate_or_raise` still runs and still catches genuinely
  malformed geometry -- it's a layout gap, positioning against an
  empty-room assumption). Scoped out of G2 deliberately rather than
  half-solved; revisit if/when a phase needs multi-generation spatial
  awareness.

**Live-verified in a real browser** across all four relations: "a table
with four chairs around it" (four chairs at equal radius around the
table, ground-snapped, ai_generator_bot-built, 232 vertices/174 faces,
watertight), "a box on top of a table" (the box's bottom sits exactly
on the table's measured top), and "a row of three shelves" (evenly
spaced, no overlap) -- each confirmed both via screenshot and via the
status line's honest `N object group(s)` / `N batch(es)` summary.
19 new tests in `tests/test_ai_scene.py` (spec validation,
`expand_scene`'s target remapping when a `count>1` object is itself a
target, every relation's placement math, the cross-group overlap
sweep, `merge_placed_objects`'s id-uniqueness and translation
correctness) plus heuristic-parsing and LLM-tool-dispatch tests in
`test_interpreter.py`, scene-specific `generate_mesh_ops`/endpoint
tests in `test_generator.py`/`test_generation_endpoint.py` (object-op
grouping, `scene_object` tagging, forced per-object batch boundaries
even with an oversized batch-size ceiling) -- 500+ tests green,
zero regressions.

### Open vocabulary via a sandboxed geometry DSL (Phase G3, the step-change)

Every generator so far is a *fixed* archetype -- a table is always four
legs and a top. Phase G3 adds a genuine open-vocabulary escape hatch
for a prompt no registry generator or scene fits ("a bracket with a
mounting hole," an odd mechanical part, an abstract form) without
loosening rule 1 for a moment: the model still never emits raw
geometry, only a small **JSON program** in a closed grammar
(`crdt_cad/ai/dsl.py`) -- never Python, never vertices.

- **The grammar**: primitives `box`/`prism` (regular N-gon
  extrusion)/`cylinder`/`extrude` (arbitrary polygon footprint, all
  centered in X/Z and based at Y=0, the same "stands on the local
  origin, gets moved into place afterward" convention
  `scene_layout.py` already uses); transforms `translate`/`rotate`/
  `scale` (each wraps one child); combinators `union`/`difference`
  (real CSG booleans -- the exact same `trimesh`/`manifold3d` engine
  `wall_opening.py` already uses for door/window cuts, proven, not new)
  and `group`/`repeat` (cheap non-boolean concatenation, `repeat`
  being a bounded loop of translated copies). Every primitive is built
  via the *existing, already-tested* helpers in `mesh_builder.py`
  (`add_box`/`add_cylinder`/`add_extruded_polygon`) -- this module adds
  the JSON grammar and sandboxing around them, not new triangulation
  code, so it inherits their correctness rather than re-risking it.
- **Sandboxed by construction, not by a sandbox process**: the
  interpreter is a small pure function over the JSON tree (`_validate_node`
  then `_execute_node`, both plain recursive Python with a fixed
  `"op"` dispatch) -- no `eval`/`exec`, no imports driven by program
  content, no filesystem/network access, no unbounded recursion (tree
  depth is capped and checked before any geometry is built).
- **Hard caps, checked twice**: a validation pass first (structural
  checks -- unknown op, wrong field types, out-of-range values, max
  *textual* node count 48, max tree depth 16, max repeat count 24 --
  so a malformed program fails fast with a specific message before any
  geometry work), then live budget checks during execution (max
  *expanded* node count 400 -- catches nested repeats multiplying out
  past their individually-legal counts, e.g. 20×20 nested repeats each
  under the cap of 24 but 400+ combined; max 20,000 vertices/faces,
  tighter than `validation.py`'s general 50k scene ceiling since one
  DSL program is a single open-vocabulary object; a 5-second wall-clock
  budget; a 50m-per-axis bounding-box cap checked after *every* node,
  not just at the end). Every cap raises `DSLValidationError` or
  `DSLBudgetExceededError` (both `DSLError`) with the specific message
  that gets fed back to the model on repair.
- **The retry loop** (`generator.py`'s `_generate_dsl_ops`): execute →
  validate (the same `validate_or_raise` every other path uses) → on
  failure, one repair call (`interpreter.llm_repair_dsl_program`,
  forced back onto the `dsl` tool via `tool_choice={"type": "tool",
  "name": "dsl"}`, sent as a standard tool_use → tool_result turn with
  the *exact* error text and `is_error: true`) → on a second failure,
  fall back to the closest registry archetype by keyword match on the
  *original* prompt (or `house` if nothing matches) rather than a bare
  error -- a real, working object with reduced fidelity to the request
  beats a dead end, the same "never a broken/silent result" rule every
  other path already follows. Every attempt's outcome is recorded in
  `GenerationResult.dsl_attempts` (`{"attempt", "outcome", "error"}`
  each) -- not yet surfaced in the UI (that's G5's report card), but
  already there for it to consume, and already logged via the standard
  logger in the meantime.
- **The heuristic (no-API-key) path never attempts DSL synthesis** --
  stated as a rule in the brief, enforced by construction: `_heuristic_interpret`
  has no DSL branch at all, so an unmatched prompt degrades straight to
  the registry (`house`), exactly like every prompt the heuristic
  couldn't fully parse before this phase existed. Confirmed by a
  dedicated test (`test_heuristic_interpret_never_attempts_dsl_synthesis`),
  not just by the absence of code.
- **Not live-verified against the real API** (no `ANTHROPIC_API_KEY` in
  this environment, same honesty rule as `meshy_adapter.py`): the
  `dsl`/repair tool-use flow is exercised only via a mocked `anthropic`
  client in tests. What *is* live-verified in a real browser: the
  entire rest of the pipeline with `interpret_prompt`/`llm_repair_dsl_program`
  monkeypatched to return real (and, for one run, deliberately invalid)
  DSL programs -- confirming end to end that a genuinely open-vocabulary
  shape (a box+cylinder union with a cylindrical hole cut through it,
  62 vertices/124 faces, watertight) renders correctly after a real
  repair-loop recovery from an oversized first attempt, and that the
  fallback path (every repair attempt still invalid) renders a real
  registry object (a keyword-matched `chair`, not an error) rather than
  leaving the user with nothing. `mesh3d.js`'s `describeGeneratedSpec`
  gained a `"dsl"` branch (program node count + top-level op + material,
  e.g. "custom difference shape, 7 node(s), metal") instead of falling
  through to the numeric-dimension fallback that has nothing to show
  for a DSL spec.
- **44 unit tests** (`tests/test_ai_dsl.py`) covering every primitive/
  transform/combinator producing valid watertight geometry, every
  validation error (unknown op, missing/malformed fields, out-of-range
  values, wrong types not coerced), every hard cap (textual nodes, tree
  depth, repeat count, expanded nodes via nested repeats, vertices/
  faces, execution time, per-node bounding box), and adversarial shapes
  (missing `op`, non-dict child, `bool` where an `int` count is
  required, 1000-deep nesting) -- plus repair/fallback orchestration
  tests in `test_generator.py` (first-try success, one-repair recovery,
  exhausted-repairs fallback with a keyword-matched archetype, the
  source≠"llm" defensive guard never calling the LLM) and an
  end-to-end HTTP test in `test_generation_endpoint.py`.

### Provenance, follow-up edits, and one-unit undo (Phase G4)

Every generation up through G3 was a one-shot, anonymous drop into the
room -- no way to select "everything that AI generation built," no way
to say "make it taller" without retyping the whole prompt, no way to
undo a hundred-face generation except vertex by vertex. Phase G4 adds
all three without inventing new CRDT machinery: it reuses the exact
per-face property bag and undo/redo primitives every other phase
already built on.

- **Provenance**: every generation mints a `generation_id`
  (`crdt.mesh.new_id("gen")`) and tags *every face it produces* with it
  via `set_face_prop(face_id, "generation_id", generation_id)` -- the
  same `face_props` LWWMap-per-face mechanism already carrying
  "material"/"color"/"scene_object", now with one more key. A whole
  scene (Phase G2) is one generation, one id -- undoing it removes
  every object at once, matching what the user actually asked for. The
  AI Generate panel gained an **AI Generations** list (`mesh3d.js`'s
  `renderGenerationList`): each row's eye button toggles a highlight
  (a distinct color override in `faceColor()`, same precedence tier as
  the existing remote-edit flash) over every face tagged with that
  generation -- "select everything from this generation," live-
  verified to persist correctly across a regenerate (the highlight
  survives an edit because the new geometry is tagged with the *same*
  id).
- **Spec persistence**: a new top-level `MeshCRDT.generations: LWWMap[str, dict]`
  (a new `"generation"` `MeshOp` target, wired through `apply`/`merge`/
  `frontier`/`ops_since`/`to_dict`/`from_dict` exactly like every
  existing sub-CRDT) stores each generation's *final* prompt/generator/
  spec as one record, overwritten wholesale on edit rather than
  appended to -- "final spec," not a history, per the brief's own
  wording. Whole-record LWW (not per-field, unlike `face_props`) is the
  right granularity here: an edit always replaces the entire record at
  once, there's no scenario where two fields of the same generation's
  spec need to merge independently.
- **Follow-up edits**: clicking a generation's pencil button arms the
  Generate button as "Apply edit" (`ui.editingGenerationId`, toggled
  off by clicking the same pencil again); the next prompt goes to
  `interpret_edit` (`interpreter.py`) as *edit this spec*, not a fresh
  dispatch -- the generator never changes (a table stays a table). The
  LLM path sends the current spec JSON plus the edit instruction,
  forced back onto the *same* generator's tool
  (`tool_choice={"type":"tool","name":entry.name}`), asking for the
  complete updated spec. The heuristic (no-API-key) path
  (`_heuristic_edit`) covers the brief's own examples directly: generic
  scale keywords ("taller"/"wider"/"bigger"/...) adjust only the
  `_m`-suffixed field(s) a keyword hints at (or every one, for an
  axis-agnostic "bigger"/"smaller"), and house-specific overrides
  ("5 bedrooms instead", "give it a gable roof") reuse
  `_heuristic_house_spec`'s own extraction regexes, applied as
  overrides on top of the prior spec rather than a fresh default-filled
  one -- every field the edit text doesn't mention survives untouched.
  `generate_edit_ops` regenerates the mesh from the new spec and
  applies the delta as ordinary CRDT ops: remove the old generation's
  geometry, add the new geometry under the *same* generation id.
  **Scoped to single-object (registry) generations only** for this
  phase -- editing a scene or a DSL program raises
  `EditNotSupportedError` (a clear 422, never a silent no-op or a
  wrong result) rather than a half-implemented "which sub-object?"
  guess; a documented scope boundary, revisit if a future phase needs
  it.
- **A real, pre-existing bug found and fixed while building this**:
  every generator's own `build()` restarts vertex/face ids at v1/f1,
  which is harmless in isolation but means two *separate* generations
  landing in the same room used to silently collide -- a second
  generation's `add_vertex("v1", ...)` doesn't create a new vertex, it
  overwrites the first's (an LWWMap `set` on an existing key is a move,
  not a create), corrupting the earlier generation's geometry. This
  predates G4 (confirmed against G1/G2 code) but only became obvious
  while reasoning through the edit path's own id-collision risk against
  a live room document. Fixed once, centrally, in `_mint_ops_for_mesh`'s
  new `_fresh_ids` remap (every generation path -- single-object, scene,
  DSL, DSL-fallback -- routes through it), with a dedicated regression
  test (`test_two_separate_generations_in_the_same_room_do_not_collide`)
  generating a table then a chair into one document and asserting both
  survive at their full vertex/face counts.
- **A second correctness subtlety, also caught before it shipped**: an
  edit's *removal* ops need to out-rank the room's already-applied
  *creation* ops from the same `ai_generator_bot` actor identity, but a
  brand-new `LamportClock(actor=...)` always starts at counter 0 --
  fine for the fresh ids every other path mints (nothing to race
  against yet), but a removal op with a low counter silently loses the
  LWW comparison against a much-higher-counter original creation,
  leaving "removed" geometry still live. `generate_edit_ops` seeds its
  clock from `room.doc.frontier().get(actor_id)`; a dedicated
  regression test deliberately passes an *unseeded* counter (0) and
  confirms the old geometry incorrectly survives, proving the seeded
  path is the one that actually matters, not just a defensive-looking
  no-op.
- **One-unit undo**: extends the existing client-side undo/redo
  reimplementation (`mesh3d.js`, mirroring `MeshCRDT.undo`/`redo`'s own
  `{"kind": "composite", "entries": [...]}` grouping already used for
  extrude/primitive placement) rather than inventing new undo
  machinery. `undoEntryForIncomingOp` converts each raw op arriving
  during *this client's own* in-flight generation/edit request
  (`generationInFlight`, computed from **pre**-apply state -- undo
  entries are built inline in `applyIncomingOps`'s loop, one op ahead
  of `applyOp`, not in a separate pass afterward that would only ever
  see already-mutated state) into the same entry shapes a local
  mutation already produces, pushed as one composite entry when the
  request completes. Scoped to the requesting client only -- a
  collaborator just watching a generation arrive never gets it pushed
  onto *their* undo stack, since `generationInFlight` is only ever true
  on the tab that clicked Generate/Apply edit. Because an edit is
  "remove old ops + add new ops + overwrite the generation record,"
  all three fall out of the *same* conversion mechanism automatically
  -- undoing an edit restores the old geometry **and** the old spec-
  persistence record (prompt included) in the same single step, with
  no separate code path needed for that combination.
- **Live-verified in a real browser**, screenshot-confirmed at every
  step: generate a table (0.75m tall) → the AI Generations panel shows
  one entry → click Select (table renders in the highlight color) →
  click Edit (button becomes "Apply edit", placeholder changes) → type
  "make it taller" → Apply edit (table visibly taller, 0.975m, still in
  the highlight color since the new geometry inherited the same
  generation id, panel entry updates to "table: make it taller",
  generation count stays at 1, not 2) → **Ctrl+Z** → table reverts to
  its original height (0.75m, confirmed via the vertex list's own Y
  values) *and* the panel entry reverts to "table: a wooden table" --
  one undo, both the geometry and the record. A dedicated e2e test
  (`tests/e2e/test_ai_generation_e2e.py`) pins this exact round trip in
  CI. 23 new unit/integration tests across `test_mesh.py` (the
  `generations` CRDT itself: set/read/merge/serialize/undo/redo),
  `test_generator.py` (provenance tagging on every path, the collision
  regression, `generate_edit_ops` success/rejection/seeded-counter
  cases), `test_interpreter.py` (heuristic and mocked-LLM edit
  interpretation), and `test_generation_endpoint.py` (the `/generate`
  endpoint's new `edit_of` parameter end to end).

**3D-print preparation** (`mesh_repair.py`) is a separate, opt-in path
-- *not* part of the CRDT-injection pipeline, per the brief's own
framing of it as pre-print cleanup, not a live-editing step. It uses
`pymeshlab` to remove duplicate vertices/faces and repair non-manifold
edges/vertices, with Screened Poisson surface reconstruction available
but **off by default**: empirically, running Poisson on this
generator's crisp 4-bedroom house ballooned it from 18 vertices to
2,338 (depth=6), visibly rounding off architectural edges that were
already correct -- the right trade for genuinely messy/incomplete input
(a hypothetical scanned or ML-hallucinated mesh), not for a
procedurally-exact one. Falls back to dependency-free fan triangulation
if `pymeshlab` isn't installed or its pipeline raises for any reason.

### Optional hosted ML mesh-gen adapter (Phase 9, `meshy_adapter.py`)

**Not verified against the live API** -- read this before trusting it.
Setting `MESHY_API_KEY` makes `generate_mesh_ops` try Meshy AI's hosted
text-to-3D API first: create a generation task, poll until it succeeds,
download the resulting GLB, parse it with `trimesh` (already a core
dependency) into the same vertex/face dict shape the procedural
pipeline produces, then inject it through the identical
`commit_ops_batched` path -- same batching, same actor identity, same
everything downstream of "here is a mesh." No Meshy API key was
available in the environment this was built in, so the request/response
handling is implemented against my best understanding of Meshy's
documented API, not confirmed with a real call. If that understanding is
wrong, the most likely failure is an HTTP error or a `KeyError` reading
an unexpected JSON shape -- both are caught by a broad `except
Exception` in `generate_mesh_via_meshy`, logged, and treated exactly
like "not configured": generation falls back to the deterministic
procedural pipeline, the same as today, never raising up to the user.

What genuinely *is* verified (`tests/test_meshy_adapter.py`,
`tests/test_generator.py`): the key-unset path takes zero network
action; every failure mode (HTTP error, a `FAILED` task status, an
unexpected JSON shape) returns `None` rather than raising, exercised via
a mocked `requests` module standing in for the real one; and mesh-file
parsing itself is checked against a *real* GLB -- built and exported by
`trimesh` directly, not a hand-typed fixture -- so the one fully-local,
fully-checkable piece of this pipeline (turning mesh bytes into a
vertex/face dict) has no unverified gap. `generate_mesh_ops`'s own
wiring (use Meshy's mesh when the adapter returns one, fall back to
procedural when it returns `None`) is tested directly by monkeypatching
the adapter call, independent of whether Meshy's real API cooperates.
The result's new `mesh_source` field (`"meshy"` or `"procedural"`) is
surfaced in the AI Generate panel's status line so it's never ambiguous
which one actually produced what's on screen. `requests` (the `meshy`
extra) is a light dependency, included in `dev` alongside `asyncpg`/
`redis`; `trimesh` needs nothing extra since it's already core.

## WebRTC P2P (`demo/static/common.js: P2PManager`)

Direct browser-to-browser sync via `RTCPeerConnection` + a
`DataChannel`, with the existing WebSocket relay carrying only the
signaling handshake (a generic `{"type": "signal", "to": ..., "data": ...}`
envelope the server forwards to one specific peer without inspecting
it -- `_handle_message` in `app.py`). This is genuinely verified: a
Playwright test establishes a real P2P connection between two headless
Chrome tabs and confirms both report a connected data channel.

Every op is still *also* sent over the WebSocket relay -- P2P is a
latency optimization layered on top, never a replacement (the relay is
what persists state and serves late joiners). Connections are attempted
opportunistically when a peer's presence becomes known, with a
lexicographic actor-id tiebreak so both sides don't race to send an
offer simultaneously. Going "offline" tears down any open P2P
connections too (an early version of this only closed the WebSocket,
which silently let an already-established P2P channel keep syncing
behind the offline toggle's back -- caught by re-verification and
fixed; see `P2PManager.disconnectAll()`).

`aiortc` (a *Python* WebRTC peer implementation) is a project
dependency but isn't used for the browser-to-browser signaling path --
that's just message relay, which the existing generic WS layer already
does. `aiortc` would matter if a Python process ever needed to join a
room as a full WebRTC peer itself (e.g. a server-side recording bot);
that's not built, since nothing in this project needs it yet.

## Time-Travel Merge (the differentiator)

When a client reconnects after an offline stretch, if *both* it and the
room changed something while it was away, `RelayConnection` (in
`common.js`) doesn't auto-apply the delta -- it calls
`onMergePreview(myOfflineOps, theirOps, proceed)`. Both demos wire this
to `showMergePreviewModal()`, which summarizes each branch in plain
language ("added a layer", "extended a path (×4)", ...) side by side.
Clicking **"Merge now"** calls `proceed()`, which applies the remote
delta and flushes the offline queue -- ops that, being CRDT operations,
merge losslessly regardless of when the button is clicked. The panel is
a *review* step, not a manual conflict-resolution step: the guarantee
it's visualizing (automatic, order-independent convergence) already
holds before the button is pressed.

## Sync protocol (`src/crdt_cad/server/app.py`)

One FastAPI WebSocket room per document (`/ws/{room_id}` for 2D,
`/ws/mesh/{room_id}` for 3D -- both served by the same generic
`Room`/`RoomManager`).

```
client -> server   {"type": "hello", "actor": "<id>", "token": "<signed token>" | null, "known_frontier": {...} | null}
server -> client   {"type": "snapshot", "doc": {...}, "frontier": {...}}   # new client, or resync last resort
server -> client   {"type": "delta", "ops": [...], "frontier": {...}}     # reconnect, or resync catch-up
either direction    {"type": "ops", "ops": [...], "from": "<actor id>"}    # live broadcast
either direction    {"type": "signal", "to": "<actor>", "data": {...}}     # WebRTC signaling relay
client -> server    {"type": "save"}               -> {"type": "saved", "at": <unix time>}
server -> client     {"type": "rejected", "reason": "...", "op": {...}}    # geometry validity gate, malformed op, or rate limit refused this op
server -> client     {"type": "frontier", "frontier": {...}}              # lightweight periodic self-heal ping
client -> server      {"type": "resync", "known_frontier": {...} | null}  # "catch me up" -- see below
server -> client     {"type": "validity_warning", "faces": [...], "problems": [...]}  # mesh rooms only, see below
```

The server also broadcasts a lightweight `frontier` ping to every client
in a room every `CRDT_CAD_SNAPSHOT_INTERVAL_SECONDS` (default 30s, only
when something changed since the last one), so a late joiner or a client
that missed something for any reason self-heals -- see "Snapshot ->
frontier ping" below for why this used to be a full document snapshot
and no longer is. The server is a **relay with one pre-commit gate**,
not an OT-style authority: it never rewrites or reorders client ops
(the validity gate only *rejects*, never modifies).

### Snapshot -> frontier ping (bounded periodic traffic)

The periodic self-heal broadcast used to be a full `{"type": "snapshot",
"doc": {...}}` -- correct, but O(document size) x O(connected clients)
of traffic every interval, for *every* room, regardless of whether
anything was actually missed. It's now a `{"type": "frontier", ...}`
ping carrying just the room's current `VectorClock` -- O(actor count),
not O(document size). Each client compares it against its own recorded
frontier (`FrontierTracker.isBehind()` in `common.js`) and only asks for
a real catch-up -- a `{"type": "resync", "known_frontier": {...} |
null}` request -- on an actual mismatch; the server replies with a
`delta` (via the same `ops_since()` used for reconnects) or, if the
client has no recorded frontier at all yet, a full `snapshot` as the
response of last resort. A resync's `delta` reply flows through the
exact same client-side handling a reconnect delta does, Time-Travel
Merge preview included -- an already-online client's outbox is normally
empty, so in the common case it's just applied directly; the merge
preview only appears in the same rare case it always did.

Tested server-side (`tests/test_frontier_resync.py`): a quiescent room
sends nothing on its periodic tick; a dirty room's tick is a `frontier`
ping, never a `doc`; and a client that missed a live broadcast (a
network blip is simulated by briefly removing it from the room's
in-memory client list while another actor edits, then "reconnecting"
it) receives the next `frontier` ping and correctly self-heals via
`resync` -> `delta`, converging without ever needing a full snapshot.

### Cross-component mesh validity ("Validation Fork")

Mesh rooms get one more broadcast: after any accepted op that could
create or reveal a cross-component inconsistency (see
`_touches_mesh_topology` in `app.py` -- a face created/removed/boundary
edit, or a vertex *deletion*; plain vertex moves are excluded since
they fire on every ~80ms drag tick and can't make a vertex stop
existing), the server re-checks the room's *entire* merged mesh with
`crdt_cad.geometry.mesh_validity.check_mesh_validity` (`trimesh`-backed:
degenerate faces, non-manifold edges, inconsistent winding, and face
boundaries referencing a deleted vertex). Any problems found are
broadcast as `{"type": "validity_warning", "faces": [...], "problems":
[{"faces": [...], "problem": "..."}]}`. This is a **warning, never a
gate** -- unlike the 2D `path_geom` validity gate in `_validate_op`,
which rejects an individual op *before* it's merged, a mesh merge has
already happened by the time this runs and can't be rejected without
breaking convergence. The 3D demo (`mesh3d.js`) renders every face
listed in a `validity_warning` with a red outline (kept until
dismissed or the face stops existing) and a dismissible banner naming
the specific problem(s); nothing is auto-fixed or blocked -- a human
decides whether to fix or delete the affected face. See "Responses to
the architecture critique" claim 3 above for the full rationale,
including why watertightness is deliberately not one of the checks.

## Security hardening (`src/crdt_cad/server/security.py`)

The zero-config local demo (`git clone && pip install -e . && uvicorn ...`,
no secrets to manage) is completely unchanged from every earlier section
of this README -- everything below is **opt-in via environment
variable**, off (or wide open, matching today's behavior) until a
deployer configures it.

**Shared-secret room tokens.** Set `CRDT_CAD_SECRET` to require a token
to join any room. A token is a signed (`itsdangerous`), room-and-kind
-scoped credential -- one minted for `(mesh, "roomA")` grants no access
to `(mesh, "roomB")` or `(drawing, "roomA")` -- with a configurable
expiry (`CRDT_CAD_TOKEN_MAX_AGE_SECONDS`, default 24h). `GET
/api/auth/required` tells a client whether it needs one at all;
`POST /api/auth/token` exchanges the shared secret (compared with
`hmac.compare_digest`, not `==`, to avoid a timing side-channel) for a
token. Every room-scoped REST endpoint (export/import/generate) and the
WS `hello` handshake enforce it identically. The demo frontend
(`ensureRoomAccess()` in `common.js`) checks `/api/auth/required` on
load, reuses a token already in the URL or `localStorage`, and otherwise
prompts once for the secret -- the **Share** button embeds the token in
the invite link so a recipient never has to know or enter the secret
themselves. A rejected/expired token (WS close code `4401`) clears the
stored copy and re-prompts, rather than silently retrying forever.

**CORS.** Wide open (`*`) only when no secret is configured (today's
default); locked to same-origin (`CORS_ORIGINS=[]`) the moment a secret
is set, or an explicit list via `CRDT_CAD_CORS_ORIGINS` (comma-separated)
always wins. Unlike every other check in this section, this one is only
evaluated once, at process startup, when `CORSMiddleware` is
constructed -- Starlette has no mechanism to reconsider allowed origins
per request, so changing these env vars requires a restart.

**Rate limiting.** A small hand-rolled `TokenBucket` (continuous refill,
no external dependency) rather than `slowapi`, which doesn't fit the
WebSocket path cleanly. Three independent limits: per-connection ops/sec
on the WS relay (`CRDT_CAD_WS_OPS_PER_SECOND`/`_BURST`), a per-room
ops/minute ceiling shared across every connection to that room
(`CRDT_CAD_MAX_OPS_PER_ROOM_PER_MINUTE`), and a per-client-IP limit on
`POST /generate` (`CRDT_CAD_GENERATE_PER_MINUTE`/`_BURST`) -- the last
one applies **even when room auth is off**, since an LLM call and CPU
mesh-construction cost real money/time regardless of access control.
Exceeding any WS-side limit yields `{"type": "rejected", "reason": "..."}`
(never a silent drop); exceeding the generate limit returns HTTP 429.

**Resource ceilings**, all env-tunable with sane defaults: max raw
WebSocket frame size (`CRDT_CAD_MAX_WS_MESSAGE_BYTES`, checked before any
JSON parsing is attempted), max ops in one message
(`CRDT_CAD_MAX_OPS_PER_MESSAGE`), max distinct rooms per server process
(`CRDT_CAD_MAX_ROOMS_PER_SERVER` -- bounds *new* rooms, never blocks
access to one that already exists), and max simultaneous clients per
room (`CRDT_CAD_MAX_CLIENTS_PER_ROOM`). Exceeding a WS-level ceiling
closes the connection with a distinct code (`4413`/`4429`/`4503`) rather
than a silent drop or an unbounded queue.

**A bug this section's own tests caught**: a malformed op (missing or
wrong-shaped fields) used to raise an uncaught exception deep inside the
per-op apply loop, which propagated out of the entire WebSocket receive
loop and silently ended the connection -- no `rejected` reply, nothing
the client could react to, just a dead socket. Found while writing a
rate-limit test that (accidentally, at first) sent a payload missing its
`id` field. Fixed: a malformed op is now rejected the same clean way a
geometry-invalid op is (see `test_malformed_op_is_rejected_cleanly...`
in `tests/test_security.py`).

## The home page and the two demos

All three pages are plain HTML/CSS/vanilla-JS (`demo/static/`) -- no
build step, no npm project. The 3D demo additionally loads Three.js +
OrbitControls from a CDN via an import map (the only external runtime
dependency any of the three frontends has).

**Workspace home (`/`, Phase 17 -- see below)**: lists every room
that's ever been saved, each with a kind badge (2D/3D), a real
server-rendered SVG thumbnail for 2D rooms (a static placeholder icon
for 3D), last-modified time, **Rename**, and **History** (lists
checkpoint snapshots with a one-click **Restore**, which forks the
chosen version into a brand-new room rather than rewriting the live
one -- see "Workspace" below for why). "New 2D drawing"/"New 3D mesh"
buttons prompt for a room name and open straight into it.

**2D sketch (`/2d`)**: pen tool, a **select** tool with multi-select
(shift-click/marquee), move/rotate/scale, duplicate, copy/paste,
align/distribute, and object snapping (Phase 12 -- see below), a strict
**Polygon** tool that demonstrates the geometry validity gate,
**Line/Rect/Circle/Ellipse/Arc** shape tools with numeric dimension
input (Phase 11 -- see below), a **Constrain** tool (Phase 9, extended
Phase 14 -- coincident/parallel/perpendicular/fixed-distance/tangent
via the tested Gauss-Newton solver, now persistent, undoable, badged,
and re-solving automatically when a constrained point is dragged), a
**Measure** tool (read-only Distance/Angle/
Area-Perimeter) and a **Dimension** tool for persistent, auto-updating
annotations (Phase 13 -- see below), a real pan/zoom/grid/snap
**viewport** and a **document units** (px/mm/in) selector (Phase
10/11 -- see below), a **Text** tool, per-shape **fill**/**fill
opacity**/**stroke style** (solid/dashed/dotted), and **Group**/
**Ungroup** for multi-selections (Phase 15 -- see below), per-layer
visibility, undo/redo, live multi-user cursors, comments,
**Save**/**.json/.svg/.dxf/.png download**/**Import SVG or DXF**/
**Share** (copies a full-access invite link) and **View-only link**
(Phase 17 -- copies a read-only one instead, see below), a keyboard
shortcut overlay (`?`), a display-name **✎** button next to the actor
label in the status bar (Phase 17 -- prompts for a name, persisted in
`localStorage`, fed into presence/comments the same way the randomly-
generated "Guest ###" default always was), and an offline toggle that
closes both the WebSocket and any P2P connection.

**3D mesh (`/3d`)**: click the ground grid to place vertices, click 3+
vertices in order (then the first one again, or "Finish") to build a
face, drag a vertex to move it (or type exact X/Y/Z into the vertex
list), select a face to **recolor it, tag its material, extrude it
into a prism, or delete it**, **Undo/Redo** buttons and Ctrl+Z/Ctrl+Y
(or Ctrl+Shift+Z) for every one of those actions -- extrude included,
as one bundled undo step -- the same **Save**/download(**.json/.stl**)/**Share**/
**View-only link**/display-name/offline toggle set as the 2D demo,
plus an **AI Generate** box -- describe a house in plain English and a
real procedurally-built mesh streams into the scene as CRDT ops,
exactly like any other collaborator's edit. **Box**/**Cylinder**/
**Pyramid**/**Plane** parametric primitive tools (Phase 16 -- see
below) drop a fully-formed shape with one click, a **Snap** toggle for
grid- and vertex-snapped placement/dragging, and **Top**/**Front**/
**Right**/**Persp.** view buttons for quickly squaring up the camera.
Every one of these -- including a face's color and material -- is a
`face_prop` `LWWMap` write, so recoloring a face you didn't create
merges the same conflict-free way a vertex move does.

## Design system (Part 3, Phase D1)

All three pages share one hand-written CSS custom-property system --
`demo/static/tokens.css` (color for both a dark and a light theme,
typography, a 4px spacing grid, three radii, two shadow levels, a
six-level z-index scale, and motion durations) plus a ~35-icon SVG
sprite (`demo/static/icons.svg`) that replaced every emoji glyph the UI
used to render as a tool/action icon. No build step, no Tailwind/
PostCSS -- matching the project's stated architecture. Full token
tables, the exact WCAG contrast numbers behind the color choices, and
an honest list of what's explicitly *not* covered yet (that's D2-D8's
job) are in **[`docs/design-system.md`](docs/design-system.md)**.

One thing worth calling out here rather than only in that doc: the
first attempt at the icon sprite used a genuinely external reference
(`<use href="/static/icons.svg#icon-name">`), which is the standard,
well-documented SVG sprite pattern -- and it silently rendered nothing
in this environment, confirmed by an isolated test page with no
console error at all. Fixed by having `common.js` fetch the sprite's
raw markup once and inject it into the page, so every icon instead
references a same-document fragment (`href="#icon-name"`) -- see the
design-system doc's "Icon sprite" section for the full isolation test
that found this.

## Layout: the canvas is the hero (Part 3, Phase D2)

Both demos share the same restructured shell: a slim 48px icon-only
**tool rail** (was a 220px column of labeled buttons) with hover
tooltips, a **document name** in the top bar that's click-to-rename
(reuses Phase 17's `/rename` endpoint -- see below for a real gap this
surfaced), a compact **presence avatar stack** next to it, and two
independently **collapsible panels** either side of the canvas.
Collapsing both (via their top-bar toggle buttons, or the `\` shortcut
for both at once) gives a near-full-bleed canvas. Below 900px
the panels overlay the canvas instead of squeezing it; below 480px the
tool rail becomes a bottom bar and the top bar sheds its least-essential
controls (the raw room switcher, the cross-demo nav link's label) to
stay usable.

- **A real, reasonably serious bug this phase's verification caught**:
  collapsing a panel worked in the sense that the CSS grid column
  shrank, but the panel's *own content* stayed visible -- clipped
  mid-character, bleeding out over the canvas at the column boundary.
  Cause: CSS Grid's automatic-minimum-size rule only lets a track
  collapse below its content's min-content size if the grid item's
  `overflow` is non-`visible` on *both* axes; `.panel` only ever set
  `overflow-y: auto`, leaving `overflow-x` at its default `visible`.
  Fixed with `overflow-x: hidden` + an explicit `min-width: 0` on
  `.panel`. A related, smaller version of the same class of bug: even
  after that fix, the collapsed panel measured 1px wide instead of a
  true 0, because `.panel.collapsed` cleared the border to `transparent`
  instead of `border-width: 0` -- a transparent border still occupies
  its width in the box model. `tests/e2e/test_layout_e2e.py` asserts the
  collapsed panel's actual `getBoundingClientRect().width` is exactly 0,
  specifically because a weaker assertion (just checking the CSS class
  got toggled) would have missed both of these.
- **A real gap the document-name button surfaced**: renaming reuses
  Phase 17's REST endpoint, which 404s for a room that's never had a
  single op committed yet (it doesn't exist in the store's `documents`
  table until the first persist) -- meaning the very reasonable "name a
  brand-new room before you start drawing" case would have silently
  failed. Fixed in `common.js`'s shared `renameRoom()`: on a 404, force
  an explicit save (the same request the Save button makes) and retry
  once before giving up.
- **Collapse buttons live in the top bar, not inside the panels they
  collapse** -- a button inside a panel that shrinks to zero width
  becomes unreachable the instant it collapses, with no way to click it
  back open (only the `\` shortcut would still work). Keeping them in
  the (never-collapsing) top bar avoids that trap entirely.
- **Tooltips** are a small shared component (`initTooltips()` in
  `common.js`, not a per-button native `title`): 500ms delay before
  first appearing, but instant if another tooltip was dismissed within
  the last 300ms, so sweeping across the tool rail shows each label
  immediately after the first -- matching how desktop app toolbars
  actually behave. Tool-rail tooltips currently show only the tool name,
  not a keyboard shortcut chip, honestly -- there are no single-key tool
  shortcuts yet (that's Phase D4); the tooltip copy will grow the chip
  once those exist, rather than showing one that doesn't work yet.
- **Deliberately deferred to later phases**: the avatar stack here is
  the plain, functional version (overlapping color circles with
  initials, capped at 4 + a "+N" overflow) -- join/leave animation,
  hover previews, and follow mode are Phase D6. The connection/save
  status pill is visually unchanged from before D2; redesigning it is
  Phase D5.
- **A real regression the full e2e suite caught, not just the
  phase's own new tests**: Phase 17's read-only viewer mode disables
  every editing control in `.panel.left` via a CSS rule and a matching
  `applyViewerModeUI()` selector -- moving the actual tool buttons out
  of `.panel.left` into the new `.tool-rail` silently exempted every one
  of them from that rule, so a viewer connection could see and click
  tool-rail buttons again (the deeper `pointerdown`/`sendOps` guards
  still blocked any actual edit from reaching the network, so this was
  a UX regression, not a security one -- but a real one). Fixed by
  extending both the JS (`applyViewerModeUI` toggles `.viewer-mode` on
  `.tool-rail` too) and the CSS selector to cover both containers.
  Caught by running the *entire* e2e suite after the phase's own new
  tests already passed, not just the new ones -- worth doing after any
  change that moves markup existing selectors depend on, not only after
  one that adds new markup.

## Input feel: cursors, hover, snap/selection color (Part 3, Phase D3)

- **Per-tool cursors**: crosshair for tools that place brand-new
  geometry (2D: Pen/Polygon/Line/Rect/Circle/Ellipse/Arc/Text/Measure/
  Dimension; 3D: Vertex/Box/Cylinder/Pyramid/Plane), a default arrow for
  tools that only interact with what already exists (2D Select; 3D
  Face/Move), and `grab`/`grabbing` for panning -- this last one, and
  the drawing-tool crosshair, already existed before this phase; what
  D3 actually added is the Select tool's *idle-vs-hovering* distinction
  (see below) and setting the correct initial cursor on page load
  (previously the canvas just showed the browser's default arrow until
  the first tool switch, even though Pen/Vertex -- both crosshair tools
  -- are each demo's own starting default). Never the text I-beam --
  there was never a code path that could produce one over a `<canvas>`.
- **Hover halo**: hovering unselected geometry with the Select tool now
  shows a subtle accent-colored halo (thinner, lower-opacity than the
  existing selection glow) and switches the cursor to `move` -- "this is
  what a click would select," distinct from "this is already selected."
  Genuinely new tracking (`hoveredPathId`, updated on every idle
  `pointermove`, cleared on tool switch and on `pointerleave`) -- there
  was no hover-state concept in the codebase before this phase.
- **Snap glyphs and marquee selection now use `--accent`/
  `--accent-muted`, not hardcoded colors**: the endpoint (square) /
  midpoint (triangle) / center (circle) snap-glyph system from Phase 12
  already existed and already matched the brief's glyph shapes -- it
  was just hardcoded to a green (`#51cf66`) never tied to any token, and
  the marquee-select rectangle was a hardcoded rgba matching the
  dark-theme accent's own value, so neither ever actually responded to
  a theme switch. Both now read the live token via `canvasColor(...)`.
  **Deliberately not added**: a fourth "grid dot" glyph for plain
  grid-snap (only object-snap -- endpoint/midpoint/center -- has a
  glyph). Grid-snap happens transparently inside `worldPoint()`, before
  any glyph-aware code runs, and every caller already receives an
  already-grid-snapped point by the time it could decide whether to
  show a glyph -- restructuring that composition order for a fourth
  glyph kind wasn't judged worth the risk this phase, so it's
  documented here rather than silently skipped.
- **No drag-to-resize/rotate handles**: the brief describes "8 square
  handles" on a selection bounding box. This project deliberately
  doesn't have those -- Phase 12 chose numeric Rotation/Scale fields
  over interactive gizmos, a documented, tested, already-shipped
  architectural decision (see the Selection Editing section below), not
  something D3 reopened. The existing accent-colored selection glow
  already satisfies the *spirit* of "accent-colored selection
  indication" without a literal bounding box + handles.
- **Buttons and inputs**: D1 already added hover/active(scale
  0.97)/focus-visible(2px accent ring)/disabled(50% opacity) to
  buttons; D3 filled the remaining gap -- `<select>` now gets the same
  background/border/radius treatment as text inputs (previously just
  bare browser-default styling), and both text inputs and `<select>`
  get a visible hover state (border lightens), matching what buttons
  already had.
- **`user-select: none`** on `.canvas-wrap` and `.tool-rail` -- dragging
  a vertex, marquee-selecting, or panning must never trigger a stray
  text selection on whatever's nearby.
- **A test-authoring lesson worth recording, not a product bug**: the
  first draft of this phase's own verification moved the mouse to a
  coordinate exceeding the canvas element's actual rendered *height*
  (e.g. testing "cursor reverts when hovering empty space" by moving
  to a point 700px down inside a canvas only ~650px tall) -- the
  pointer left the canvas element entirely, landing on whatever's
  rendered below it, so the canvas's own `pointermove` listener
  correctly never fired again, and its cursor style stayed at whatever
  it was last set to. Traced by checking every piece of local drag
  state (`selectDrag`/`constrainDrag`/`shapeDraft`/`drawing`) individually
  before finding the actual cause was geometric, not a stale-state bug
  -- fixed by using an in-bounds coordinate instead. Also prompted one
  genuine, if minor, correctness fix either way: `pointerleave` now
  also resets the cursor to `default` (it previously only cleared the
  hover halo), so the canvas's internal cursor state can't drift stale
  while the pointer is elsewhere.

## Keyboard-first: palette, shortcuts, reachability audit (Part 3, Phase D4)

- **Command palette (Ctrl/Cmd+K)**: a fuzzy-searchable list of every
  action in both demos -- tool switching, undo/redo, duplicate/copy/
  paste/delete, fit/zoom-to-100%, panel toggles, save/export (json/svg/
  dxf/png/stl/step), import, AI Generate (3D), share/view-only links,
  rename room, change display name, go offline/reconnect, toggle theme,
  open the shortcut overlay, and cross-page navigation. Each row shows
  an icon, the action name, and a mono-font shortcut chip where one
  exists. Arrow keys move the highlighted row, Enter runs it, Esc
  closes. This is the discoverability answer for every action that
  never had a toolbar button (export formats, rename, going offline)
  and not just a faster path to the ones that do. The list is rebuilt
  fresh every time the palette opens (`buildCommands()` in
  sketch.js/mesh3d.js), so it always reflects live state -- the offline
  toggle's current label, and, more importantly, viewer-mode: editing
  commands are dropped entirely for a read-only viewer rather than
  shown-but-broken, and a generic `clickCmd()` helper additionally
  skips any command backed by a button the existing `.viewer-mode` CSS
  has currently disabled (`pointer-events: none`), so the list can't
  silently drift out of sync with that CSS as new buttons are added.
- **Single-key tool shortcuts**: 2D -- V select, P pen, L line, R
  rectangle, C circle, O ellipse, A arc, T text, G polygon, K
  constrain, M measure, D dimension. 3D -- V vertex, F face, M move, B
  box, C cylinder, P pyramid, L plane. Letters were picked to avoid
  colliding with the existing Ctrl/Cmd-modified shortcuts (Ctrl+D
  duplicate vs. plain D dimension, Ctrl+C copy vs. plain C circle,
  etc.) -- a bare key only switches tools when no modifier is held.
  Also added: `Ctrl/Cmd+Z`/`Ctrl/Cmd+Shift+Z` (undo/redo) in the 2D
  demo -- the 3D demo already had this, the 2D demo never did, only its
  Undo/Redo *buttons* worked -- `Ctrl/Cmd+0` (fit to content) and
  `Ctrl/Cmd+1` (zoom to 100%, a new `zoomTo()` helper anchored on the
  viewport's own center rather than the cursor, since there's no cursor
  position to anchor a keyboard-triggered zoom on), and arrow-key
  nudge for the 2D Select tool's current selection (1px per press,
  10px with Shift), reusing Phase 12's existing `nudgePathTransform` --
  each nudge is its own undo step for free, since `setPathProp` already
  pushes to the undo stack internally. Tool tooltips (`data-tooltip`,
  shown on hover) now also display their shortcut key/chord, e.g. "Rect
  (R)" or "Undo (Ctrl+Z)" -- `aria-label` is left as the plain action
  name so Phase D1's emoji-glyph regression test keeps checking exactly
  what it always checked.
- **Viewer-mode keyboard gating -- a real, if low-severity, gap this
  phase's own audit found and closed**: the mouse-level "editing
  disabled" restriction for a read-only viewer (Phase 17's
  `.viewer-mode` CSS) was never checked by the keyboard shortcut
  handlers -- a viewer could press Ctrl+D or Delete and the client
  would optimistically apply the edit locally (creating a phantom
  local-only change that would only reveal itself as wrong once the
  server's rejection of a viewer connection's `ops` message -- already
  enforced server-side, see `server/app.py` -- diverged the client from
  the authoritative state on the next sync). Not a security hole (the
  server was always the actual authority), but a confusing client-side
  experience, and exactly the kind of thing a "full keyboard
  reachability audit" is for. Both demos' keydown handlers now check
  `viewerMode` before running any mutating shortcut (single-key tool
  switches, Ctrl+D/V, Delete, arrow-nudge, Ctrl+Z/Y), matching what the
  mouse-level CSS already blocked -- Copy and the "?"/Ctrl+K/Ctrl+0/
  Ctrl+1 shortcuts stay available to a viewer since they're genuinely
  read-only. One related, narrower fix: 2D's "Fit" button and 3D's Top/
  Front/Right/Perspective view buttons were previously disabled for
  viewers too (they live inside `.panel.left`, which the viewer-mode
  CSS blanket-disables) even though a camera-only operation is harmless
  for a viewer to trigger -- marked `always-enabled`, matching how the
  Save/export buttons already were.
- **Grouped, searchable "?" shortcut overlay, styled like the palette**:
  previously a plain, ungrouped list, and 2D-only (the 3D demo had no
  overlay at all before this phase). Both demos now share one
  implementation (`showShortcutOverlay(groups)` in common.js) grouped
  into Tools/Editing/Selection/View/General (2D) or Tools/Editing/View/
  General (3D), with a search box filtering across every row's key
  *and* description live as you type -- "what does Ctrl+D do again" and
  "how do I duplicate something" are the same question from two
  directions. A real bug found writing this: the "?" keydown handler
  never called `preventDefault()`, so the same keystroke's default
  text-insertion action ran *after* the handler had already created and
  focused the overlay's search input -- typing a literal "?" into it
  and instantly self-filtering the list down to the one row that
  happens to contain a "?" character. Fixed by adding
  `preventDefault()`, same as every other shortcut branch already had.
- **Full keyboard reachability audit**: a skip-to-canvas link (`.skip-
  link`, off-screen until focused, first element in the DOM on both
  demo pages) lets Tab jump straight past the top bar and tool rail;
  the equivalent "Skip to rooms" link exists on the workspace home
  page. Tab order was confirmed to already match DOM order everywhere
  (no `order:` CSS property is used anywhere in styles.css, so visual
  and DOM order can't have silently diverged). Every custom overlay in
  the app -- the command palette, the shortcut overlay, the Time-Travel
  Merge preview, and the workspace home page's rename/version-history
  modals (which predate this phase and previously had no focus
  management at all) -- now traps Tab inside itself via a single shared
  `trapFocusIn()` (common.js) and restores focus to whatever triggered
  it on close; the home page's two modals also gained Esc-to-close and
  backdrop-click-to-close for the first time, matching every other
  overlay in the app.
- **A real bug the D3-established testing discipline caught again**:
  same story as before, verified systematically rather than assumed --
  see the "?" `preventDefault()` bug above, found by an e2e test
  asserting the shortcut-overlay's row count rather than just that the
  overlay opened.

## State legibility: status, toasts, empty states (Part 3, Phase D5)

- **Connection/save status cluster** (top bar, replacing the old
  `#statusPill`): one compact button -- a colored dot + human label
  (`Live`, `Connecting...`, `Reconnecting...`, `Offline`, or `Offline --
  N edits queued` once something's actually queued) plus a separate
  `Saved <relative time>` / `Saving...` sub-label -- that opens a
  popover on click explaining the current state in a sentence and
  closes on Esc or an outside click, restoring focus to the button
  either way (`trapFocusIn`-adjacent, though this is a plain toggle
  popover rather than a full modal). `#statusText` -- the exact raw
  machine word ("online"/"offline"/etc.) dozens of existing e2e tests
  across the whole suite wait on -- is kept, unchanged, as a visually
  hidden (`.sr-only`) element alongside the new human-facing
  `#statusLabel`, rather than repurposed: changing what dozens of tests
  key off of for a cosmetic relabel wasn't worth the blast radius, and
  decoupling "the machine-readable state hook" from "the copy a human
  reads" is good practice regardless.
  - **A genuinely new distinction, not just a relabel**: `RelayConnection`
    (common.js) previously reported the exact same `"connecting"` status
    for the very first connection attempt and for every automatic retry
    after a drop. It now tracks `_everConnected` and reports
    `"reconnecting"` for every attempt after the first, so "just opened
    the page" and "the connection just dropped and is retrying" read
    differently, matching the brief's pulsing `Reconnecting...` state.
  - **The save-state label is honest about what this architecture
    actually guarantees**, not a generic autosave-spinner clone: the
    server already durably persists every accepted op immediately (see
    `RelayConnection.save()`'s own docstring) -- there is no "unsaved
    changes" window to fake a spinner for. `"Saving..."` only shows for
    the genuine, if usually brief, round trip of an explicit Save click
    (which forces an early version-history checkpoint); reaching
    `"online"` (a fresh snapshot/delta just arrived) is itself treated
    as an honest "saved just now" moment, since whatever's now on
    screen IS the server's current durable state, even if the user
    never explicitly clicked Save this session. The popover's own
    explanation text spells this distinction out, per the brief's own
    framing: "this is where the project's honest offline/merge model
    becomes visible UX."
  - Two view-only-camera-operation buttons were found, while wiring
    this, to be needlessly disabled for a read-only viewer (2D's Fit
    button, 3D's Top/Front/Right/Perspective view buttons -- both live
    inside `.panel.left`, which Phase 17's `.viewer-mode` CSS disables
    wholesale) -- marked `always-enabled`, matching Save/export, a small
    fix in the same spirit as D4's keyboard-parity one.
- **Toast system upgrade** (`showToast`, common.js): now a real FIFO
  queue -- one toast shown at a time (a burst of events, e.g. importing
  several files back to back, reads as a sequence instead of a pile),
  4s auto-dismiss that's genuinely pausable on hover (the *remaining*
  time survives the hover, it isn't just reset), and the container
  carries `aria-live="polite"` + `role="status"` so a screen reader
  hears each one without focus ever needing to move to it. Every toast
  in the app already ran through this one function (save confirmations,
  share-link-copied, import results, solver failures, generation
  success/failure -- all pre-existing), so upgrading it upgraded all of
  them at once; the only genuinely new call site is the rejection flash
  below. A new `showUndoToast(message, undoFn, undoTimes)` adds an
  inline "Undo" action button to a toast -- see destructive
  confirmations below.
- **Destructive confirmations**: there was no blocking `confirm()`
  dialog anywhere in the app to remove (deletes already went through
  the existing CRDT undo stack silently) -- what was actually missing
  was any *visible* way back out of one. Deleting a path (2D: the path
  list's own delete button, the Selection panel's "Delete path", the
  bulk "Delete N paths" button, and the Delete/Backspace key) and
  deleting a face or vertex (3D) now show a `showUndoToast(...)` whose
  "Undo" button calls the existing `undo()` exactly enough times to
  cover the whole batch (each deleted item is its own undo-stack entry,
  so restoring 3 of them takes 3 calls) -- no new undo machinery, just
  a visible affordance for machinery that was already there.
- **Empty states**: a centered, quiet `.empty-canvas-hint` over the
  canvas -- "Press P and drag to draw &middot; ? for shortcuts" (2D) /
  "Click the grid to place a vertex, or try AI Generate" (3D) -- shown
  whenever the document is empty (`state.pathIndex.size === 0` / 
  `state.vertices.size === 0`, checked on the same 400ms interval that
  already drove the old ops/offline counters) and hidden the instant
  there's something there. `pointer-events: none` so it never intercepts
  the click/drag that would dismiss it.
- **Geometry-rejection red flash**: pairs the existing "Rejected: ..."
  toast (already shown for every `path_geom` rejection -- self-
  intersecting strict polygons, zero-length segments) with a brief
  (600ms) `--danger`-colored halo around the specific path the
  rejection was about, drawn the same way as the D3 hover halo (an
  `isFlashing` parameter threaded through `drawShapePath` and the
  freehand render branch) so no geometry needs re-tracing. Guarded to
  only fire if the path survived the rejection with live points (a
  brand-new strict polygon whose every point gets rejected has nothing
  left to flash, and that's fine -- the toast alone still explains what
  happened). **3D has no equivalent**: mesh rooms have no pre-commit
  validity gate at all (a CRDT merge can't be rejected without breaking
  convergence -- see `_validate_op`'s own docstring), so there's no
  `onRejected` path to hang a flash off of; this is an architectural
  fact, not a scope choice.

tests/e2e/test_state_legibility_e2e.py adds 8 browser tests. Full e2e
suite (72 tests) and full non-e2e suite pass.

## Multiplayer presence: cursors, avatars, follow mode (Part 3, Phase D6)

- **A real, both-theme contrast bug found while doing the brief's own
  "checked against both themes" step**: the pre-existing 10-color
  `ACTOR_COLORS` (common.js) were bright/pastel hues tuned only for the
  dark theme -- e.g. `#ffd43b` yellow cleared 13:1 against the dark
  canvas but only 1.4:1 against the light one, i.e. functionally
  invisible there. Computing the WCAG relative-luminance contrast ratio
  for all 10 against both `--bg-canvas` values (same formula used for
  `--accent` in D1) confirmed every one of them failed the light theme.
  Replaced with **8 mid-tone, fully saturated hues** (red/orange/amber/
  green/teal/blue/indigo/pink), each individually verified to clear
  3:1+ against both canvas backgrounds *and* 4:1+ with solid white
  text -- pastel colors can't do this at all (a light background
  against a light color, or a dark background against a dark one, is
  the failure mode either way), which is what makes "one hex value
  works on both a near-black and a near-white canvas" require a
  deliberately mid-luminance band rather than a wide, pretty palette.
  The avatar-stack initials' text color (previously hardcoded near-
  black, correct only for the old pastel palette) is now white to
  match.
- **Smooth remote cursors**: each demo's cursor-position rendering was
  rewritten from "rebuild every DOM node from scratch every render,
  snapped directly to the latest presence value" into its own
  independent loop (2D: a dedicated `requestAnimationFrame` loop,
  decoupled from the reactive main canvas `render()`; 3D: folded into
  the existing per-frame `animate()`) that exponentially eases a
  persistent per-actor DOM element toward the latest target position
  (~80ms ease, frame-rate-independent) rather than teleporting, and
  fades the name label to a bare colored dot after 3s of no movement,
  instantly reappearing on the next update. 3D eases in *world* space
  before each frame's camera projection, not screen space, so the ease
  stays correct even while the camera itself is moving. `.cursor-label`
  (the element `test_viewport_e2e.py` already asserts `style.left`/
  `style.top` against) keeps its exact class name and positioning
  contract; the dot/name split lives in new nested `.cursor-dot`/
  `.cursor-name` children instead.
- **Avatar stack**: a new arrival gets a scale/fade entrance
  (`.avatar-enter`, a `@keyframes` reduced-motion already neutralizes
  globally) and a quiet "`X` joined" toast -- tracked via a
  previously-seen-actor-ids `Set` in `renderAvatarStack` that seeds
  itself silently on the very first render (so nobody already in the
  room gets greeted as "joining" the moment *you* connect) and forgets
  an id once they leave, so a rejoin is greeted again.
- **Remote selection/edit visibility (2D only)**: presence now also
  broadcasts the sender's own `ui.selectedPaths` (riding the same
  throttled payload, plus a 400ms heartbeat fallback for a selection
  change with no mouse movement after it, e.g. clicking a path-list
  row); any path another actor currently has selected gets a
  constant-width hairline outline in their color
  (`remoteSelectionColorFor`), and any path touched by an incoming op
  from another actor (`onOps`'s now-threaded `from` parameter) briefly
  flashes the same way in their color for 600ms (`flashRemoteEdit`) --
  the identical self-clearing mechanic as D5's rejection flash, just
  keyed by *who* instead of a fixed danger color. **No 3D equivalent**:
  mesh rooms have no per-path-boundary selection concept broadcastable
  the same way (`ui.selectedFace` is a single id, and highlighting a
  Three.js face in a collaborator's color would need real
  material/outline work distinct from the 2D canvas-stroke approach) --
  a deliberate scope trim, not attempted here. 3D *does* get the
  edit-flash (vertex/face material color, briefly, on an incoming op
  from another actor) since that reuses the exact same op-and-color
  plumbing, just applied to `vertexColor()`/`faceColor()` instead of a
  canvas stroke -- and fixed a real, adjacent bug while wiring it up:
  an existing face mesh's material color was previously only ever set
  once at mesh *creation*, never re-applied on a later `syncScene()`,
  so the flash would have been a silent no-op for any face that
  already existed.
- **A real gap this phase's own two-tab verification found**: 3D only
  ever sent presence at discrete commit points (placing/dragging a
  vertex) -- unlike 2D, which sends continuously on mousemove. A
  collaborator who joined and just looked around stayed completely
  invisible (no avatar, no cursor) to everyone else until their first
  edit. `onRole` now sends one presence ping immediately once
  connected (not gated on viewer role -- a read-only viewer is still a
  real participant worth seeing), matching what a live two-tab test
  actually exposed rather than what the code looked like it should do
  in isolation. The same pass also found both demos' WebRTC
  `onPeerData` callbacks silently discarding the sending peer's actor
  id (`_peerActorId` was prefixed-unused) -- fixed so the edit-flash
  also works for P2P-sourced ops, not just server-relayed ones.
- **Follow mode** (stretch goal within this phase): clicking another
  actor's avatar toggles `followingActorId`; every frame (the same
  cursor-easing loop above), the local viewport re-centers on their
  eased position -- 2D re-pans `view.panX/panY` at the current zoom
  level, 3D moves the `OrbitControls` target (so the camera keeps
  orbiting around wherever they are). Neither a full camera *pose* is
  ever broadcast (viewport state is explicitly client-local-only, per
  the brief, and per this project's design since Phase 10) -- follow
  mode can only mean "keep their point centered," not "see exactly what
  they see," and the popover-adjacent `.following` ring on the avatar
  is the only UI claiming anything more specific than that. Any manual
  pan/zoom/orbit (wheel, drag-pan, Fit/Zoom-100%, the 3D view buttons,
  or OrbitControls' own "start" event) exits it -- verified via
  OrbitControls' event only firing for a genuine user gesture, never
  for follow mode's own programmatic `controls.target.set()`, so it
  can't immediately undo itself.

tests/e2e/test_presence_e2e.py adds 8 browser tests -- the 3D ones are
necessarily DOM-signal-only (toast text, CSS classes, rendered
`cursor-label` position), since mesh3d.js's `<script type="module">`
scoping makes its internal state (unlike sketch.js's classic-script
globals) unreachable from `page.evaluate`. Full e2e suite (80 tests)
and full non-e2e suite pass.

## Signature moments: Time-Travel Merge and AI generation (Part 3, Phase D7)

**Time-Travel Merge redesign** (`showMergePreviewModal`, common.js): a
two-column branch comparison -- "While you were away" (this actor's own
offline edits) and "Meanwhile, in the room" (everyone else's) -- each a
timeline of change chips (an icon per op kind: `plus`/`x`/`pen` for
add/remove/edit) rather than the previous plain bulleted list, with a
`.merge-spine` connecting them down into a single accent "Merge now"
button, the only primary button on screen.
- **Chips are colored per individual author, not one flat color per
  column.** `describeDocOps`/`describeMeshOps` were restructured to
  group by `(actorId, label)` instead of `label` alone -- every op's
  `payload.id` is already `[counter, actorId]` (the same
  `LocalClock.tick()` shape used everywhere else), so each chip's
  `colorForActor(actorId)` reuses D6's own presence-color
  infrastructure. This matters concretely once more than one
  collaborator was active during the same offline stretch: two
  different authors' edits in the "Meanwhile" column render as two
  differently-colored chip groups, not one undifferentiated pile.
- **"Two lines joining" on merge**: clicking "Merge now" adds a
  `.merge-converging` class (both columns slide/fade toward the
  spine, 220ms = `--t-med`, reduced-motion-safe for free via
  tokens.css's existing global rule) before the overlay is actually
  removed and a "Merged -- N changes combined" success toast fires.
- **Copy audited end to end for the brief's explicit constraint**: this
  is a preview of an automatic, lossless merge, never a "conflict" to
  "resolve" -- the word "conflict" does not appear anywhere in the new
  copy (verified by an e2e test asserting exactly that), matching how
  the pre-D7 modal already got this right and D7 didn't regress it.

**AI generation staging** (`generateMesh`, mesh3d.js): a distinct
"thinking" state (an animated gradient border on the prompt input, the
standard double-background-layer CSS trick, `.ai-thinking`) while the
prompt is being interpreted, replaced the instant the *first real ops
batch* arrives by a progress line and a ~15° camera orbit.
- **The progress line is driven by genuinely arriving data, not a fake
  timer**: `Room.commit_ops_batched` (server/app.py) really does
  broadcast the newly-generated mesh's ops in separate WS messages as
  it commits them (150 ops/batch by default), with no `exclude` on that
  broadcast -- so the requesting tab's own WebSocket connection
  receives every batch while its own `fetch()` for `/generate` is still
  pending. `applyIncomingOps`'s already-threaded `from` parameter
  (Phase D6) makes it trivial to recognize these as `ai_generator_bot`
  ops and react to them.
- **The stage labels ("Building floor, roof, walls...") are derived
  from real data, not invented**: `generate_mesh_ops` flattens every
  floor's vertices, then every floor's faces, into one flat op list
  *before* `commit_ops_batched` ever chunks it by raw op count -- there
  is no per-batch "this batch is the floor" tag server-side, and batch
  boundaries fall at arbitrary points relative to construction stages.
  Each face op does carry a real `material` `face_prop`, though
  (`procedural_house.py`: the user's chosen floor material, `"roof"`/
  `"concrete"`, or `"exterior_wall"`/`"interior_wall"`), so the client
  classifies each arriving batch's materials into floor/roof/walls and
  accumulates a `Set` of stages actually seen so far -- in whatever
  order they truly land (floor, then roof, then walls, in practice --
  not necessarily the brief's illustrative wording), rather than
  guessing at a schedule. For a small, fast local generation this
  state can last under half a second (verified directly: a 6-bedroom/
  4-floor generation showed "Building floor, roof, walls..." for about
  340ms before the final result replaced it) -- genuinely brief, not
  hidden or faked to look longer.
- **Completion toast names the actual actor**: "Built by
  ai_generator_bot -- N vertices, M faces", using the server's own
  `result.actor` field (already `"ai_generator_bot"`,
  `crdt_cad.ai.generator.DEFAULT_ACTOR_ID`) rather than a hardcoded
  string.
- **Failures render as a danger toast with the server's own reason, an
  inline Retry, and the prompt preserved** -- reusing `showToast`'s
  generic `{actionLabel, onAction}` option (the same mechanism D5's
  undo-toasts already use) rather than adding new toast plumbing;
  verified against a real 429 (the rate limiter, not a mock), since the
  client's error handling is generic across every non-ok response and
  a 422/504 would hit the identical code path.
- **The camera orbit is a real Three.js camera move, not a CSS
  animation** -- tokens.css's global `prefers-reduced-motion` rule
  can't neutralize it the way it does everywhere else motion appears in
  this app, so `orbitCameraDuringGeneration()` checks
  `window.matchMedia("(prefers-reduced-motion: reduce)")` directly and
  no-ops entirely when it matches. Runs on a fixed ~4s tween from
  whenever generation starts (not tied to exact batch timing) and holds
  its final position once done. **Not covered by the committed e2e
  suite**: `camera`/`controls` are module-scoped in mesh3d.js and
  unreachable from `page.evaluate` (same constraint noted in D6's
  section) with no DOM-observable proxy for "did the camera actually
  rotate" the way there was for follow mode's cursor-centering --
  verified only via live screenshots during this phase, a documented
  gap rather than a skipped-silently one.

tests/e2e/test_art_direction_e2e.py adds 4 browser tests. Full e2e
suite (84 tests) and full non-e2e suite pass.

## Performance and polish audit (Part 3, Phase D8 — gate before "done")

- **60fps verification, measured directly rather than assumed**: a
  500-path 2D document's own `render()` call, timed in-page across 20
  repeated calls, takes **0.7-3.2ms** (avg ~1.1ms) -- comfortably inside
  the 16ms/frame budget. Presence updates and the cursor coordinate
  readout (previously two separate synchronous calls directly inside
  the `pointermove` handler, with no bound on how often a high-poll-
  rate mouse could trigger them) are now coalesced into a single
  `requestAnimationFrame` callback (`scheduleCursorReadoutAndPresence`),
  and the canvas redraw itself during pan/select-drag/constrain-drag/
  shape-draft/drawing/zoom is similarly coalesced (`requestRender`) so
  several `pointermove`/`wheel` events landing in the same frame only
  trigger one redraw, not one each.
  - **The 3D demo's equivalent verification hit a real environment
    limit, documented rather than papered over**: `camera`/`renderer`
    are module-scoped in mesh3d.js and unreachable from `page.evaluate`
    (the same constraint noted repeatedly since D6), and a scripted
    orbit/zoom/pan gesture profiled via the browser's own `longtask`
    `PerformanceObserver` showed 50-119ms tasks even on a modest
    160-vertex mesh. Checking `WEBGL_debug_renderer_info` confirmed why:
    this sandboxed environment's Chromium falls back to **SwiftShader**
    (`ANGLE ... SwiftShader Device ... SwiftShader driver`) -- a
    CPU-only software WebGL implementation with no real GPU behind it,
    roughly 50-100x slower than hardware-accelerated rendering for
    anything WebGL. This makes genuine 60fps validation of the 3D
    demo's actual frame cost impossible to obtain honestly in this
    specific environment (real end-user hardware has a GPU), so it
    isn't claimed here -- the 2D Canvas2D path doesn't depend on GPU
    acceleration the same way and was verified directly and
    conclusively instead. Attempting to reach the brief's literal
    1,000-vertex target also surfaced a smaller, separate, informational
    finding: repeated AI-generation calls in the same room don't
    accumulate vertices (each run stayed at exactly 160) -- deterministic
    vertex ids derived from spec+index rather than randomized, so
    repeat generations from a similar prompt overwrite the same LWW
    slots instead of adding new ones. Not a bug worth chasing down
    mid-audit, just noted for whoever next needs a large synthetic mesh
    to test against.
- **No layout shift**: measured with the real Layout Instability API
  (`PerformanceObserver({type: 'layout-shift'})`) across all three
  pages during page load and web-font swap -- cumulative layout shift
  scores of 0.002-0.010, an order of magnitude under the "good" 0.1
  threshold, so no `size-adjust`/`ascent-override` font-face metric
  tuning was needed on top of the existing `font-display: swap` +
  metric-compatible system-font fallback stacks.
- **`tests/e2e/test_design_system.py`** (5 browser tests): a screenshot
  matrix (2 demos x 4 viewport widths [375/768/1280/1920] x 2 themes =
  16 images, archived to `docs/screenshots/audit_*.png`) that also
  asserts every combination loads cleanly with no console errors;
  every icon-only button across all three pages carries a non-empty
  `aria-label`; the first real Tab stop shows a visible
  (`outline-style: solid`, non-zero width) focus ring; the mobile
  bottom tool-rail's buttons are all >=44x44 CSS px at the 375px
  breakpoint; and `--text-primary`/`--text-secondary` clear 4.5:1
  against both `--bg-app` and `--bg-panel` in both themes (computed via
  the same WCAG relative-luminance formula used throughout this whole
  Part, read live from `getComputedStyle` rather than eyeballed from
  the token values).
- **`tests/test_frontend_kill_list.py`** (4 fast, non-browser tests,
  static analysis over the source): no emoji glyph anywhere in
  `demo/static/*.html`/`*.js`; every `z-index` declaration outside
  `tokens.css` itself routes through a `var(--z-*)` token; the sole
  `outline: none` in the whole stylesheet is the modern
  `:focus:not(:focus-visible)` complement to a real `:focus-visible`
  ring (paired, not a bare removal); and the sole transition on a
  layout-triggering property (`.body`'s `grid-template-columns`, the
  panel-collapse animation) is a documented, deliberate exception --
  the entire point of that transition is the canvas actually reflowing
  to reclaim the freed width, which a transform-only fake (sliding the
  panel off-screen while its grid track stays full width) can't
  produce, and it fires once per manual toggle rather than on a
  continuous per-frame hot path.
- **Real, if minor, bugs this sweep actually found and fixed**, not
  just confirmed clean:
  - `.avatar:hover`'s `z-index: 1` was a raw, unscaled value (should
    have been `var(--z-toolbar)`), and the skip-link animated `top` (a
    layout property) instead of `transform` for an identical visual
    result at lower cost -- both predate this phase.
  - Follow mode's avatar click targets (D6) had no keyboard path at all
    -- a bare `onclick` on a `<div>` with no `tabindex`, caught by
    exactly the kind of audit this phase is for. Now `role="button"`,
    `tabindex="0"`, an `aria-label` naming who it follows, and an
    Enter/Space keydown handler alongside the existing click.
  - Several canvas-drawn UI elements -- constraint-relation badges,
    dimension lines/labels, the in-progress polygon/shape-draft preview,
    and the constrain/measure/dimension tools' own selection markers --
    were never migrated to `canvasColor()` during D1's original token
    migration, so they silently never followed a theme switch (one,
    the dimension-line blue, happened to equal the dark theme's
    `--accent` by coincidence, masking the gap there specifically).
    All now read live theme tokens.
  - Three remaining hardcoded `#fff` values (avatar initials, remote-
    cursor name pills, the danger button) were consolidated into a new
    `--on-solid-fill` token -- distinct from `--accent-on` because none
    of these three backgrounds are `--accent` itself, and (verified)
    white clears 4:1+ against all of them in both themes without
    needing `--accent-on`'s per-theme flip.

**Definition of done, D1-D8**: every phase is committed individually
(see the git log); the full pytest suite (375 tests) and the full e2e
suite (89 tests, opt-in via `-m e2e`) both pass; both demos share the
same `tokens.css` with zero remaining hard-coded colors/spacing/
z-index outside it (verified above); both themes pass this phase's own
audit; `docs/screenshots/` is current (the 4 README hero images plus
16 new audit screenshots); and this README's own
[Design system](#design-system-part-3-phase-d1) section has linked
`docs/design-system.md` since D1.

## 2D viewport: pan, zoom, grid, snap (Phase 10, `sketch.js`)

Before this, the canvas mapped document coordinates 1:1 to screen
pixels -- the drawable universe was exactly one browser window. A
client-local `view = { panX, panY, zoom }` transform fixes that, and per
the brief's own framing, it is deliberately **not** CRDT data: it never
syncs, never touches `applyOp`, never appears in a snapshot. All stored
and sent geometry (path points, presence cursor positions) is genuinely
**world coordinates** now; only rendering (`ctx.translate`/`ctx.scale`
around the world-space drawing pass) and input mapping
(`screenToWorld`/`worldToScreen`) go through the transform. A fresh
view is the identity transform (`panX=0, panY=0, zoom=1`), so every
room's pre-existing pixel-space data (drawn before this phase existed)
renders exactly as it always did -- world space is a strict superset of
the old pixel space, not a breaking migration.

- **Zoom**: mouse wheel, centered on the cursor -- re-anchors `panX`/`panY`
  each tick so the world point under the cursor never jumps, clamped to
  [5%, 2000%]. A **Fit** button frames all visible (non-hidden-layer)
  geometry with padding; an empty document resets to the identity view
  rather than leaving a stale pan/zoom behind.
- **Pan**: middle-mouse-drag or Space+left-drag (the Space handler is
  careful not to fire while a text input/textarea has focus, and
  `preventDefault()`s only then, so it doesn't fight normal typing or
  scroll the page). The `click` a drag-release still fires is explicitly
  suppressed via a `justPanned` flag, so panning never gets
  misinterpreted as "place a polygon vertex" or "select a path."
- **Adaptive grid**: `pickGridStep` picks a "nice" world-space step
  (1/2/5 x10^n) so its on-screen spacing stays in a fixed, readable pixel
  range regardless of zoom -- a minor grid at that step, a major grid at
  5x it, with the minor lines' opacity fading to zero as their on-screen
  spacing compresses below ~8px (exactly the brief's "fade minor lines
  out as they compress," not just a single static grid).
- **Snap-to-grid** (toggle button): reuses the same `pickGridStep` so
  snapping always matches whatever grid is currently visible; applied at
  point-placement time (pen strokes, polygon vertices), not as a
  separate CRDT concept.
- **Live cursor coordinate readout** in the status bar, in world units.
- **Hit-testing stays screen-space-relative**: `hitTestPath`/`hitTestPoint`
  project each candidate point to screen via `worldToScreen` and compare
  against a constant *screen*-pixel threshold, so click targets don't
  become impossibly small when zoomed out or absurdly oversized when
  zoomed in -- the correct behavior for a CAD-style viewport, and why
  the constraint-selection highlight circles and polygon vertex markers
  are deliberately drawn *outside* the canvas transform (screen space,
  constant radius) while the actual path geometry is drawn *inside* it
  (world space, so `stroke_width` correctly scales with zoom like real
  ink would).
- **Remote presence cursors render correctly through the transform**:
  presence positions are stored/sent in world coordinates now, and
  `renderPresence()`'s DOM overlay (not itself inside the canvas
  transform) applies its own `worldToScreen` conversion per cursor.
  Live-verified with a two-tab, *asymmetric*-transform test: tab A
  zooms/pans away from the identity view, tab B's mouse moves to a known
  screen point at B's own identity view (so its world coordinates equal
  its screen coordinates there), and tab A's rendered cursor-label for B
  is checked against **A's own** `worldToScreen` projection of that
  point -- not simply B's raw screen position, which is the whole reason
  presence needed to move to world coordinates in the first place. Also
  committed as `tests/e2e/test_viewport_e2e.py`, alongside a
  snap-to-grid check confirming stored points actually land on grid
  multiples (not just that the toggle button changes its own CSS class).

## Shape primitives, numeric input, document units (Phase 11)

**Representation**: a shape (Line, Rectangle, Circle, Ellipse, Arc) is a
path whose parametric definition lives entirely in `path_props` (e.g.
`{"shape": "circle", "cx":, "cy":, "r":}`) -- its RGA point list (`paths`)
stays empty. This is deliberate, not a shortcut: `path_props` is already
an `LWWMap`, so two users concurrently editing (say) a circle's radius
and its color merge field-wise for free, with **zero new CRDT code** --
exactly the reason the brief asks for this representation over storing
shapes as sampled point lists. Freehand/polygon paths are completely
unaffected; they still use `path_geom` exclusively, and every existing
code path that touches it (undo/redo, curves, the validity gate) doesn't
need to know shapes exist at all.

- **Creation**: click-drag with the matching tool (Line/Rect/Circle/
  Ellipse/Arc) -- release commits the shape at the dragged size. A
  negligible drag (effectively a plain click) commits nothing, so a
  stray click with a shape tool active can't leave a zero-size shape
  behind.
- **Numeric input**: an inline panel (`#shapeInputPanel`) shows the
  shape's defining dimensions (Width/Height for Rect; Radius for Circle;
  Radius X/Y for Ellipse; Length/Angle for Line -- a more natural way to
  type a line than two endpoints; Radius/Start/End angle for Arc) --
  live and read-only while dragging, or freely editable (with a
  **Create** button, and Enter-to-commit in any field) when no drag is
  active, creating a new shape at the current view's center with
  exactly the typed values. Tab-cycling between fields is just the
  browser's own focus order -- nothing extra was needed for that part of
  the brief.
- **Rendering & hit-testing are native per shape kind**, not always
  faceted to a polyline: `ctx.rect`/`ctx.arc`/`ctx.ellipse` for drawing
  (so a stored Circle looks like an actual circle at any zoom, not a
  faceted approximation), and dedicated boundary math per kind for
  `hitTestPath` (a normalized-distance check for Circle/Ellipse/Arc, an
  angular-sweep check added for Arc specifically, point-to-segment for
  Line, four point-to-segment checks for Rect). Shapes are unfilled
  outlines today (fills are Phase 15), so hit-testing correctly responds
  only near the boundary stroke, not the interior -- live-verified with
  exactly that distinction: clicking well inside a circle does *not*
  select it, clicking on its actual boundary does.
- **SVG/DXF export are native too**: `<line>`/`<rect>`/`<circle>`/
  `<ellipse>`/an elliptical-arc `<path>` command in SVG, and
  `LINE`/a closed `LWPOLYLINE`/`CIRCLE`/`ELLIPSE`/`ARC` in DXF (`ezdxf`
  has no native rectangle entity) -- a faithful, editable shape in any
  real vector/CAD tool, not a flattened approximation. Freehand/polygon
  paths' own export path (including Phase 8's curve segments) is
  unchanged; shapes just take a different branch checked first.

### Document units (`px` | `mm` | `in`)

A new `settings: LWWMap[str, object]` component on `DrawingDocument` --
the same serialization/merge/`ops_since` treatment as every other
component, with its own Python tests, including one confirming a
snapshot persisted *before* this existed loads cleanly (defaults to an
empty map rather than `KeyError`). Stored/CRDT geometry is **always**
raw px-equivalent world units regardless of this setting, at every
zoom level and in every export -- `units` is a pure *display*-layer
conversion (cursor readout, the numeric shape panel, SVG/DXF export
scale), never a migration of existing coordinates. The conversion table
(`UNITS_PX_PER_UNIT` in `document.py`, mirrored exactly in `sketch.js`)
assumes the same 96px/inch convention CSS itself uses, so `px` needs no
special-casing anywhere that already assumed today's raw-pixel
behavior.

- The adaptive grid (Phase 10's `pickGridStep`) is unit-aware: with
  `units="mm"`, the grid lands on nice round millimeters (1/2/5/10mm),
  not nice round pixels that happened to look reasonable on screen.
  Snap-to-grid reuses the identical step, so it always matches whatever
  unit is active.
- SVG export scales every coordinate by `1/px_per_unit(units)` and adds
  real `width`/`height` attributes with the matching unit suffix
  alongside the (still-unitless, per SVG convention) `viewBox`. DXF
  export scales identically and sets the `$INSUNITS` header variable
  (0=unitless for `px`, 4=millimeters, 1=inches) so a real CAD tool
  interprets the numbers correctly -- exactly the brief's ask.
- The `units` setting itself is a real CRDT-backed document setting
  (unlike Phase 10's view transform, which is deliberately client-local
  and never synced) -- live-verified with two tabs: changing units in
  tab A is visible in tab B's own units dropdown, not just tab A's
  cursor readout.

## Selection editing: transform, duplicate, snap (Phase 12)

**Representation**: a path's move/rotate/scale lives entirely in a new
`transform` field on `path_props` -- `{tx, ty, rotation (degrees),
scale}`, absent/identity by default so every path that predates this
feature (and every path nobody has moved) renders exactly as before.
Per the brief, this deliberately never rewrites the underlying RGA
points or a shape's own parametric fields: an LWW field write merges
cleanly against a concurrent point-append to the same path, or a
concurrent color/width edit, which rewriting every point on every move
would not -- the same "independently-mutable prop-bag field" rationale
already used for Phase 11's shape fields and Phase 8's curve segments,
applied to a new kind of edit instead of new data.

- **Rendering** wraps each path's *existing, unchanged* drawing code in
  canvas's own nested transform stack (translate to the pivot,
  translate by `tx/ty`, rotate, scale, translate back) rather than
  manually transforming every point -- works uniformly for freehand
  curves and shape primitives alike, and correctly scales `stroke_width`
  the same way real ink would (`beginPathTransform` in `sketch.js`).
  The pivot is the shape's own natural center (line: midpoint; rect:
  `x+w/2, y+h/2`; circle/ellipse/arc: `cx,cy`) or a freehand/polygon
  path's live bounding-box center, recomputed fresh from *base*
  (untransformed) geometry every time so it never drifts.
- **Hit-testing** can't use the canvas transform (it doesn't run inside
  a `render()` call), so it forward-transforms points/shape-fields
  explicitly (`applyPathTransform`/`transformedShapeProps`) and reuses
  the exact same per-kind boundary math Phase 11 already built. Accepted
  approximation, unchanged in scope from Phase 11: a rotated Rect/
  Ellipse/Arc's *hitbox* still uses axis-aligned math, exact for
  translate/scale-only transforms and only approximate once rotation is
  non-zero -- a slightly-off click radius on a rotated shape, not a
  data-correctness problem (rendering and export are both exact
  regardless of rotation, see below).
- **Multi-selection**: `ui.selectedPaths` is a `Set`, built via a
  plain click (replaces the selection), shift-click (toggles a path
  into/out of it without starting a move), or a marquee drag over empty
  canvas (selects everything whose *transformed* bounding box
  intersects the drawn rectangle). Dragging a path that's already part
  of the current selection moves the whole group together -- live-
  previewed locally frame-by-frame, committed as one `transform` write
  per path *on release*, not per `pointermove`, so a drag never floods
  the relay with ops.
- **Numeric rotate/scale**: the single-selection panel gets Rotation
  (degrees) and Scale fields writing straight to `transform` -- a
  deliberate, documented scope reduction from interactive drag handles/
  gizmos, consistent with Phase 11's numeric-input-first approach to
  shape editing.
- **Duplicate** (`Ctrl`/`Cmd`+`D`, or a button in either selection
  panel) and **copy/paste** (`Ctrl`/`Cmd`+`C`/`V`, plain JSON on the
  system clipboard, so it works across rooms and tabs since it only
  ever mints fresh ids) both deep-copy full `path_props` -- including
  `transform` and shape fields -- offset by a small delta via
  `transform`, never by rewriting base geometry. A freehand/polygon
  path's curve segments (Phase 8, keyed by their anchor's *old* node id)
  are explicitly remapped onto the copy's *new* node ids; without that
  remap the copy would silently lose its curves, since none of its
  fresh ids would match a carried-over `curve:` key.
- **Delete** (`Delete`/`Backspace`) removes the entire current
  selection; both it and duplicate reuse the existing per-path
  `addPath`/`removePath` undo-stack entries rather than inventing a new
  batch-undo mechanism -- undoing a multi-path move/duplicate/delete
  takes one Undo click per path, consistent with how every other
  multi-op action in this codebase already works (there was no
  batch-undo grouping anywhere before this phase either).
- **Align** (left/center/right, top/middle/bottom) and **distribute**
  (3+ paths, horizontal/vertical even spacing) compute a new `tx/ty`
  per selected path from the group's bounding box, using each path's
  *transformed* (on-screen) geometry, not its raw stored points.
- **Object snapping**: while dragging a selection or drawing a new
  shape, the cursor snaps to endpoints/midpoints/centers of *other*
  nearby (transformed) geometry, with a small glyph showing what it
  snapped to (square=endpoint, triangle=midpoint, circle=center) --
  client-side input assistance only, no CRDT changes, per the brief.
- **A keyboard shortcut overlay** (`?`) lists every binding above plus
  the pre-existing pan/zoom/select ones, dismissed by `?` again or a
  click outside it.

**Export baking** (`bake_path_transform` in `document.py`): SVG/DXF
have no `transform` concept of their own, so it's applied to plain
coordinates *only at export time*, mirroring `sketch.js`'s
`getTransform`/`pathBaseCenter`/`applyPathTransform` math exactly. This
caught a real bug during verification, not just in code review: a
naive bake that only translated a rect's `(x, y)` corner and scaled
`w`/`h` produces the *wrong shape* under rotation, because a rotated
box simply isn't expressible as an axis-aligned `x/y/w/h` box any more.
Fixed by having `bake_path_transform` detect a non-zero rotation on a
Rect or Ellipse specifically and convert it to a plain closed-point
boundary instead (the rect's 4 actual rotated corners; the ellipse
sampled at 64 points around its rim) -- both forward-transformed like
any other point, then exported through the exact same point-list
fallback path a freehand path already uses. Line, Circle, and Arc never
need this: a line is just 2 points (any rotation is exact as-is), a
circle is rotation-invariant, and an arc's rotation is exactly "add
rotation to both `start_angle`/`end_angle`" -- all three stay native
shape elements at any transform, confirmed with a dedicated regression
test (`test_bake_path_transform_rotates_an_arc_exactly_via_its_angles_not_flattening`)
alongside the ones proving the rect/ellipse fallback actually
preserves edge lengths (a rotation is rigid -- it must not resize
anything) rather than just "doesn't crash."

## Measurement and dimensions (Phase 13)

**Measure tool** (read-only, purely client-local -- no CRDT op is ever
sent, confirmed by an e2e test diffing the whole document before/after):
Distance and Angle pick up to two points the same way Constrain does
(including reusing `findAdjacentPoint` for Angle -- each point's *line*
is inferred from its live neighbor, not just the bare point); Area/
Perimeter instead picks one whole path or shape directly. Rect/Circle/
Ellipse use their own exact formulas (Ellipse's perimeter via
Ramanujan's approximation); a freehand/polygon path uses the shoelace
formula for area and summed segment length for perimeter, implicitly
treating it as closed either way; Line/Arc are correctly called out as
having no enclosed area rather than showing a meaningless number.

**Dimension annotations** (persistent, shared): a new
`dimensions: LWWMap[dim_id, payload]` component on `DrawingDocument`,
serialized/merged/synced exactly like every other component (including
the same backward-compatible "absent from an old snapshot" default as
`settings`). A dimension references its two anchor points by
**`(path_id, RGA node id)`** -- deliberately *not* the `point_index`
`comments` uses, even though the brief describes both as "the same
referencing pattern": a node id survives a concurrent insert/delete
anywhere else in the same path, where an index would silently drift
onto the wrong point. `RGA.value_at(op_id)` (a new O(1) accessor) makes
this resolution cheap; `resolve_dimension_points` returns `None` --
never raises -- when either anchor no longer exists, and every caller
(rendering, the panel list, export) is required to treat that as "can't
currently show this," the same contract `curve_prop_key` lookups
already follow elsewhere in this file.

- **Rendering**: `livePosOf` (already built for Constrain) resolves
  both anchors fresh on every frame, so a dimension's line/extension-
  lines/label track the actual current geometry -- move the geometry,
  the dimension moves with it, with zero polling or explicit
  invalidation needed.
- **A real bug this phase's own verification caught**: moving a point
  via the Constrain tool (`movePathPoint`) is a CRDT-safe delete +
  reinsert -- RGA values are immutable once inserted, so the point gets
  a *brand new* node id. A dimension anchored to the *old* id would
  silently stop resolving the moment its point was ever constrained,
  defeating "updates automatically when the geometry moves" -- the
  entire reason a dimension references geometry instead of copying
  coordinates. Fixed by `remapDimensionAnchor`, called from
  `movePathPoint` itself: it scans `state.dimensions` for any anchor
  matching the old id and rewrites that dimension's payload onto the
  new one, in the same move. (Curve segments, Phase 8, still *do*
  orphan the same way on a point move -- an accepted, pre-existing,
  and separately documented trade-off for that more cosmetic feature;
  dimension tracking is this phase's headline behavior, so it earned
  the extra remap step curve segments didn't.)
- **Export**: DXF gets a real `DIMENSION` entity per resolved dimension
  via `ezdxf`'s `add_linear_dim` -- confirmed by actually building,
  rendering, and reading one back (the reloaded entity's own
  `get_measurement()` matches the two points' true distance), not
  assumed from the API docs. SVG has no native dimension-annotation
  concept, so it renders a `<g class="dimension">` with two extension
  lines, the offset dimension line, and a `<text>` value label -- a
  faithful line+text rendering per the brief, not an approximation of
  anything. An unresolved dimension (a concurrently deleted anchor) is
  silently skipped by both exporters, never raising or emitting broken
  geometry.

## Interactive constraint UI: persistence, tangent, re-solve-on-drag (Phase 14)

Phase 9 already built the **Constrain** tool (coincident/parallel/
perpendicular/fixed-distance) against the tested Gauss-Newton solver,
but applying one was a one-off visual effect -- nothing durable, no
badge, nothing to select or delete, and moving a point this way was not
undoable at all (`movePathPoint` never pushed an undo entry). Phase 14
is what makes the solver "finally earn its keep," per the brief: every
constraint now persists, and constrained geometry stays constrained.

- **Persistent constraints**: a new `constraints: LWWMap[constraint_id,
  spec]` document component -- same serialization/merge/backward-compat
  treatment as `dimensions`. A constraint's `spec` is `{kind, anchors,
  param}`; each anchor is either `{"type": "point", "path_id",
  "node_id"}` (an RGA node id, exactly `dimensions`' anchoring rationale
  from Phase 13) or `{"type": "shape_center", "path_id"}` -- a circle
  has no RGA point of its own to anchor to, which is exactly why
  `tangent` needed a second anchor shape at all.
- **Tangent, the fifth kind**: needed a circle (available since Phase
  11) and a genuinely different picking mechanism from the other four
  kinds -- `pickConstraintEntity` tries the existing point pick first,
  then falls back to a circle-only shape hit-test (boundary-only, same
  convention as every other shape interaction here). Picking one circle
  and one point (whose neighbor defines the line, same inference
  parallel/perpendicular already use) shows a Tangent button with the
  circle's live radius pre-filled and editable; two circles, or two
  points, correctly show neither Tangent nor the other four buttons --
  each combination only offers the constraint kinds that actually apply
  to it.
- **Undo/redo fix**: `movePathPoint` is now split into a raw primitive
  (still undo-free, so `undo()`/`redo()` can call it directly without
  recursively pushing their own undo entry) and
  `movePathPointWithUndo`, which every constraint-application and
  constrain-tool drag call site uses instead. A constraint-driven move
  is undoable the same way every other edit here is -- one Undo click
  per point moved, consistent with this codebase's existing "no
  batch-undo grouping" pattern (duplicate/delete already work this way
  too).
- **Badge glyphs**: a small symbol per kind (coincident/parallel/
  perpendicular/fixed-distance/tangent each get their own glyph) at the
  centroid of a constraint's live-resolved anchors, silently skipped if
  any anchor no longer resolves -- same "can't currently render this"
  contract as Phase 13's dimensions and export baking.
- **Select + delete**: a **Constraints** panel lists every persisted
  constraint with a delete button, mirroring the Dimensions list.
- **Re-solve on drag**: dragging a point while the Constrain tool is
  active previews locally (substituting the live drag position only
  for rendering, exactly how Phase 12's selection move-drag already
  avoids flooding the relay) and, on release, gathers *every* persisted
  constraint touching that point's path and re-solves them **together**
  in one `/api/solve` call, not one at a time -- constraints sharing a
  point need to be solved jointly to stay consistent. The drag emits
  the solve request on pointer-up, never per-frame, per the brief. A
  point with no constraints yet is just an ordinary (still undoable)
  move -- no wasted solve request.
- **Honest scope note** (the brief asks for this explicitly): this is a
  *sketch* constraint system -- solve on demand against the current
  geometry -- not a full parametric feature tree with a dependency
  graph, rebuild order, or partial/over-constrained diagnostics beyond
  "did the solver converge." Reapplying the same constraint twice
  creates two persisted records rather than de-duplicating; both are
  individually valid and solve correctly, just redundant -- a known,
  minor limitation, not a correctness bug.

## Designer features: text, fills, strokes, groups, PNG export (Phase 15)

- **Text tool**: a text object is a path whose whole definition lives in
  `path_props` (`{"shape": "text", "x", "y", "content", "font_size",
  "color"}`) -- the exact same "no new CRDT primitive" representation
  Phase 11's shape primitives already established, so concurrent edits
  to *different* fields (content vs. font size vs. color) merge
  field-wise for free. A click places one with sensible defaults;
  content/font size are edited afterward via the selection panel, the
  same "create with defaults, edit via panel" pattern every shape tool
  already uses. Concurrent edits to the *content string itself* are
  plain last-writer-wins (an ordinary LWW field) -- **not** collaborative
  rich text, and deliberately not built as one, per the brief.
- **Fills** (`fill`/`fill_opacity` path_props) apply to Rect/Circle/
  Ellipse always, and a freehand/polygon path only when it's actually
  closed (first point equals last, e.g. the strict Polygon tool) --
  Line/Arc have no meaningful enclosed area (the same judgment call the
  Measure tool's Area/Perimeter mode already makes, Phase 13) and are
  never filled regardless of the prop. **Real hit-testing change**: a
  filled shape's *interior* is now clickable, not just its boundary
  stroke -- an unfilled outline correctly stays boundary-only (Phase
  11's original behavior, unchanged), but once something visibly looks
  like solid content, requiring a boundary click would feel broken.
  Confirmed live: clicking well inside an unfilled rect does nothing;
  filling it, then clicking the exact same interior point, selects it.
- **Stroke styles** (`dash`: `solid`/`dashed`/`dotted`) render as a
  canvas line-dash pattern sized off the path's own stroke width (client)
  and a real DXF linetype (`DASHED`/`DOT`, from `ezdxf`'s own standard
  linetype library) or SVG `stroke-dasharray` (export) -- three
  independent renderers, one shared sizing convention.
- **Groups**: a `group_id` path_prop plus a new `groups: LWWElementSet`
  component (existence only, mirroring `layers` -- the actual grouping
  data lives entirely in each member's own `group_id` field). Clicking
  any member of a group selects every member; the Select tool's
  multi-selection panel gains **Group**/**Ungroup** buttons, and
  transforms (Phase 12: move/rotate/scale, align, distribute) already
  apply group-wide for free, since they operate over whatever
  `ui.selectedPaths` currently holds regardless of how it got built.
- **PNG export**: two buttons, both pure client-side `canvas.toBlob()`
  -- the current view as-is, and a fit-to-content variant that
  temporarily re-frames the view, captures, and restores the user's
  actual pan/zoom afterward. `canvas.toBlob()` is asynchronous, which
  caught a real bug in the first draft: restoring the view *immediately
  after* calling `fitToContent()` (rather than inside `toBlob`'s own
  callback) re-rendered the canvas back to the original view **before**
  the capture actually read it, silently exporting the wrong framing --
  fixed by moving the restore into the callback, confirmed live by
  checking the zoom indicator is back to its original value only
  *after* the download fires, not immediately after the button click.
- **Z-order, fixed for real** (needed for fills to composite correctly
  -- an unfilled outline mostly doesn't reveal z-order bugs, an
  overlapping filled shape does): a genuine pre-existing bug surfaced
  while implementing this -- `DrawingDocument.layer_list()`/`path_list()`
  iterated `LWWElementSet.to_set()`, which converts to a real Python
  `set` and **does not preserve insertion order**, so their output order
  was an accident of string hashing, not creation order, even before
  this phase. Fixed by iterating the `LWWElementSet` directly instead
  (it's backed by an `LWWMap` whose dict preserves each element's
  first-added position, so this *is* genuine creation order) -- a
  regression test now pins exactly this
  (`test_layer_list_and_path_list_preserve_creation_order`). Both
  exporters and the canvas renderer now sort paths by layer order, then
  creation order (a stable sort, so it only reorders *across* layers,
  never within one).

## 3D usability: parametric primitives, snapping, views (Phase 16)

Entirely a `mesh3d.js` client-side phase -- no Python touched, no new
CRDT component, no new document schema. Every primitive is minted as
ordinary `vertex`/`edge`/`face` ops, reusing exactly the pattern
`extrudeFace` already established for building several mesh pieces at
once: mint via the existing op constructors, apply each locally,
**one** `sendOps(ops)` call, **one** composite `pushUndo` entry. The
server never sees "a box" as a concept, only a batch of ordinary ops it
already knows how to apply and merge -- the same reason
`crdt_cad.ai.generator.generate_mesh_ops` doesn't need its own special
undo/redo handling either.

- **Box/Cylinder/Pyramid/Plane**: each tool has a small numeric field
  panel (`renderPrimitivePanel`, seeded from per-shape defaults --
  width/height/depth for Box, radius/height/segments for Cylinder and
  Pyramid, width/depth for Plane); clicking the ground grid raycasts a
  center point and builds the whole shape there in one click. Segment
  count is adjustable for Cylinder/Pyramid (verified live with an
  8-segment cylinder producing exactly 16 vertices and 10 faces --
  8 side quads plus 2 caps -- and a default 4-segment pyramid producing
  5 vertices/5 faces). Undo/redo reuses the existing `composite`,
  `vertex_create`, `edge_add`, and `face_add` undo-entry kinds --
  no new "kind" string was needed, since a primitive is just several
  ops of the same shapes undo already knew how to invert.
- **Snapping** (`snapPosition3D`, a `Snap` toggle button): existing-
  vertex snapping (0.3 world-unit threshold) takes priority over a
  1-unit grid snap, applied uniformly to new-vertex placement, vertex
  dragging, and primitive placement. An arbitrary, non-grid-aligned
  click with Snap on lands on an exact integer X/Z; dragging a vertex
  close to another snaps it onto that vertex's *exact* position rather
  than stopping just short of it.
- **Axis-aligned views**: Top/Front/Right/Perspective buttons
  reposition the camera to a standard framing. **Deliberate scope
  reduction, stated plainly**: this repositions the existing single
  `THREE.PerspectiveCamera` (`camera.up`/`camera.position`/
  `camera.lookAt`) rather than swapping in a true
  `THREE.OrthographicCamera`. `camera` is referenced directly by name
  throughout raycasting, the render loop, resize handling, and the
  presence-cursor overlay; a real second camera would need a "current
  camera" indirection threaded through all of those call sites, which
  wasn't judged worth the risk for what these buttons are actually for
  (quickly squaring up a view to place or inspect geometry, not a
  literal orthographic projection). Precise numeric extrude distance
  was already present from an earlier phase -- confirmed via research
  before starting this one, so no new work was needed there.
- **A real bug this phase's verification caught**: dragging a vertex
  updated the 3D scene immediately (`pointermove`'s lightweight
  `syncFacesTouching` calls) but `pointerup` never refreshed the
  Vertices side panel afterward, so its coordinate `<input>`s kept
  showing the *pre-drag* position even though `state.vertices` and the
  rendered mesh were already correct -- a real user dragging a vertex
  would see a stale number in the list right after letting go. Fixed by
  calling `renderPanels()` at the end of `pointerup`'s drag-completion
  branch.
- **A second, pre-existing bug found (and fixed) along the way**: a
  `pageerror: Cannot read properties of undefined (reading 'outbox')`
  turned up mid-verification, traced to the status bar's `setInterval`
  reading `conn.outbox.length` while `conn` -- assigned inside an async
  bootstrap that awaits `ensureRoomAccess()` -- was still `undefined` on
  an early tick. Confirmed via `git stash`/`git stash pop` that this
  exact code predates this phase entirely (a genuine, unrelated race,
  not something introduced here); fixed anyway with a minimal
  `conn && conn.outbox.length` guard, consistent with this project's
  practice of fixing real bugs found incidentally during verification
  rather than filing them away.
- **No new Python unit tests**: nothing in `src/crdt_cad/` changed, so
  the full 328-test non-e2e suite runs unmodified and green. This phase
  is covered entirely by `tests/e2e/test_3d_usability_e2e.py` (see
  below), since every behavior here -- primitive topology, snapping,
  camera framing -- only exists in the client.

## Workspace: rooms as projects (Phase 17)

The final phase of the brief: rooms stop being bare URLs you have to
remember and start being a real workspace -- a home page, version
history, read-only share links, and proper display names. Every piece
here builds on machinery that already existed (`DocumentStore`, the
Phase 1 signed room tokens, `Room`'s own persist cycle) rather than
inventing new infrastructure.

- **Home page** (`/`, moving the 2D demo to `/2d`): `GET
  /api/workspace/rooms` lists every room across both kinds via a new
  `DocumentStore.list_rooms_detailed()`, sorted newest-first. Each card
  shows a kind badge, last-modified time, **Rename**, and **History**.
  2D rooms get a real thumbnail -- `GET /api/rooms/{id}/thumbnail.svg`
  reuses the *exact same* `drawing_to_svg_string` export path the
  `.svg` download button already calls, just rendered small by CSS, so
  there's no second rendering path to keep in sync. 3D rooms get a
  static placeholder icon instead -- the brief explicitly allows this,
  and a real 3D preview would need either an offscreen Three.js
  renderer (a second render path to maintain) or a client-captured-on-
  save screenshot (a new upload endpoint) for comparatively little
  payoff for a home-page icon.
- **Display names**: `getOrCreateActorName()` (localStorage-persisted)
  already existed and already fed presence/comments -- what was
  actually missing, per the brief, was a "proper name prompt": before
  this phase there was no UI to ever change the randomly-generated
  "Guest ###" default. A **✎** button next to the actor label now
  prompts for a name and updates it immediately, including for anyone
  already watching this actor's presence.
- **Version history**: a new `room_versions` table (SQLite/Postgres;
  `InMemoryStore` mirrors it with a plain list) holds immutable
  checkpoint snapshots, separate from the existing `documents` table's
  single overwritten "latest" row used for hydration -- `Room.
  checkpoint_version` writes one, pruned to `CRDT_CAD_MAX_VERSIONS_PER_ROOM`
  (default 20). Deliberately **not** taken on every `persist()` call
  (which fires after nearly every accepted ops batch -- i.e. on every
  drag tick): that would make "version history" indistinguishable from
  "every keystroke." Instead a checkpoint is taken at a much coarser,
  independent cadence -- periodically (`CRDT_CAD_VERSION_CHECKPOINT_INTERVAL_SECONDS`,
  default 300s, only when something actually changed since the last
  one) and immediately on an explicit **Save**/Ctrl+S, which is exactly
  the kind of intentional checkpoint a user expects to be able to
  return to.
- **Restore forks, it doesn't rewrite**: clicking **Restore** on a
  version calls `POST /api/rooms/{id}/versions/{version_id}/restore`,
  which loads that version's bytes and saves them under a brand-new
  room id (`{id}-restored-{8 hex chars}`) -- the *original* room's own
  persisted state and in-memory `Room` are never touched. This is the
  brief's own reasoning, not a shortcut: a live room's causal history
  (its CRDT ops/frontier) can't be rewound in place without breaking
  convergence for anyone still connected to it or reconnecting later,
  so "restore" has to mean "start a new room from this old state,"
  not "make the old room's state current again." An advanced "restore
  in place via generated inverse ops" (the brief's own explicitly
  optional stretch, gated on "if and only if it can be done through
  the normal op path") was not attempted: there's no general way to
  invert an arbitrary historical diff back through RGA/LWW's normal op
  path for every op kind this document supports without inventing a
  bespoke, unverified merge strategy of its own -- exactly the kind of
  unverifiable feature this project avoids shipping, so it's documented
  here rather than half-built.
- **Read-only share links**: `mint_room_token`/`verify_room_token`
  (Part 1 Phase 1) gain a `role: "editor" | "viewer"` claim (a token
  with no `"role"` key -- i.e. every token minted before this phase --
  defaults to `"editor"`, so nothing pre-existing loses access). A new
  `POST /api/rooms/{id}/share-link` endpoint mints a token of either
  role, gated behind **editor** access itself (a viewer can't mint
  themselves, or anyone else, an escalated link -- confirmed by a
  dedicated test). The role travels to the client for real: the
  server's own `snapshot`/`delta` WS replies now carry a `"role"`
  field, so the client never decides its own permission level.
  Enforcement is layered:
  - **Server, the actual boundary**: a viewer-role WS connection still
    receives every snapshot/delta/ops/frontier message normally (full
    read access), but `_handle_message` refuses any `"ops"` message
    *from* it outright, before touching the document -- confirmed via a
    hand-crafted viewer WS sending ops in `tests/test_workspace.py`,
    per the brief's explicit "test the enforcement server-side" ask.
    The same viewer-role check also gates the REST endpoints that
    mutate a room (import, AI generate, rename, restore, minting
    further share links) via a new `require_editor_access` dependency,
    not just the WS ops path -- otherwise a "read-only" link's holder
    could bypass the WS restriction by calling those endpoints
    directly.
  - **Client, the UX**: `applyViewerModeUI` (`common.js`) dims and
    disables (`pointer-events: none`) the entire left toolbar except
    Save/downloads, and shows a "view only" badge. The canvas
    `pointerdown` handler in both demos also checks `viewerMode`
    directly, so a viewer can still pan/zoom/orbit but can never start
    an edit gesture regardless of which tool is nominally selected.
    `RelayConnection.send()`/`sendOps()` carry their own redundant
    guard on top of that (belt-and-suspenders): every mutating code
    path in either demo already funnels through `sendOps` before
    anything is transmitted, so gating it there is what guarantees a
    viewer's optimistic local edit can never leak out over *either* the
    WS or the direct WebRTC P2P channel, even if some exotic path ever
    slipped past the toolbar/pointerdown guards. **Accepted scope
    reduction**: the toolbar is disabled as one blanket rule rather than
    an exhaustive per-button allowlist (only Save/downloads are
    explicitly re-enabled) -- simpler and more robust than hand-
    maintaining an allowlist across a toolbar that's grown across six
    phases, at the cost of also disabling a merely-informational click-
    to-inspect-a-shape for a viewer, not just actual edits.
  - **Honest limitation**: the home page's room listing itself is
    ungated (like the pre-existing `/api/rooms`/`/api/mesh-rooms`, a
    room id/kind/last-modified time isn't scoped to one room the way
    its *contents* are), so with auth enabled, every room's existence
    is visible workspace-wide regardless of who's asking -- but a
    room's thumbnail, rename, and history remain individually token-
    gated, so a room this browser hasn't joined (no token in
    `localStorage`) shows a generic placeholder instead of erroring.
    Documented here rather than silently glossed over.

## Testing

```bash
./.venv/Scripts/python -m pytest tests/ -v
```

371 tests: unit tests per CRDT type and geometry module, serialization
round-trips, delta-sync correctness, a full-mesh (every-pair-order)
merge convergence test for RGA, a Hypothesis property test fuzzing
random concurrent insert/delete programs across 3 replicas, SVG/DXF/STL
import-export round-trips, the constraint solver's independent
correctness checks, persistence save/load/restart-hydration tests,
`fastapi.testclient`-based WebSocket tests covering the relay protocol,
reconnect-with-delta, the geometry validity gate's accept/reject paths,
the WebRTC signaling relay's targeted (not broadcast) delivery, tombstone
compaction's safety properties, and the full AI generation pipeline --
the heuristic and mocked-LLM interpreter paths, procedural house
geometry invariants (planarity, no duplicate vertices, correct vertical
stacking), `pymeshlab` repair with both a simulated missing dependency
and a simulated internal failure, the generator's CRDT-op output
reapplied to a fresh document, and the `/api/mesh/{room}/generate`
endpoint's success, empty-prompt, batching, failure-timeout, and
malformed-geometry paths. Also `tests/test_security.py` (27 tests):
token mint/verify/expiry and room-and-kind scoping, the no-secret
default's behavior is provably unchanged, every WS/REST auth gate,
each rate limit and resource ceiling actually tripping (oversized
frame, too-many-ops, per-connection/per-room/per-IP throttling,
room/client capacity), and the malformed-op-crashes-the-connection
regression this suite caught while being written. Also
`tests/test_mesh_undo.py` (10 tests): every undo/redo entry kind
round-tripping, the vertex create-vs-move distinction, extrude's
composite bundling actually undoing/redoing every vertex/edge/face it
created in one step, and the concurrent-safety property ported directly
from `DrawingDocument`'s own test -- undoing an extrude must not roll
back a collaborator's simultaneous, unrelated vertex move. Also
`tests/test_frontier_resync.py` (4 tests) and `tests/test_mesh_validity.py`
+ `tests/test_mesh_validity_integration.py` (15 tests total): every
`check_mesh_validity` problem class in isolation, a real concurrent
two-replica merge reproducing the "Extrusion Nightmare" end to end, and
the server-side `validity_warning` broadcast actually firing (or, for a
pure vertex move, correctly *not* firing) over a real WebSocket. Also
`tests/test_postgres_store.py` (7 tests) and `tests/test_redis_fanout.py`
(2 tests): both skip cleanly (a fast, sub-second TCP reachability probe,
not a slow per-test connection-failure retry) if no local Postgres/Redis
is reachable, so a plain `pytest tests/` from a fresh checkout never
needs either -- point `CRDT_CAD_TEST_DATABASE_URL`/
`CRDT_CAD_TEST_REDIS_URL` at real ones (e.g. via `docker run`) to
actually exercise save/load/list/delete round-trips, two `PostgresStore`
instances sharing state against the same DSN, and two `Room` instances
(standing in for two processes) actually relaying and applying each
other's ops through a real Redis. Also 15 new curve-support tests
(Phase 8) across `tests/test_export_import.py` and `tests/test_document.py`:
every SVG curve command (absolute/relative cubic, absolute quadratic,
`S`/`T` smooth-reflection with and without a preceding curve to reflect),
the arc-encountered-stops-parsing-cleanly behavior, a curve surviving a
real construction -> storage -> SVG-export -> reimport round trip through
the actual `DrawingDocument`/`add_path`/`path_list` pipeline (not just
`svg_io.py` in isolation), two replicas concurrently curving *different*
segments of the same path both surviving a real merge, and DXF's curve
flattening producing a visibly-bulging denser polyline rather than a
straight line between anchors. Also 18 new Phase 9 tests:
`tests/test_step_export.py` (6) and two more in `tests/test_server_features.py`
skip cleanly via `pytest.importorskip("build123d")` if the optional
`step` extra isn't installed, covering the planar/non-planar-fallback/
open-mesh-vs-closed-solid behavior described above; `tests/test_meshy_adapter.py`
(7) and three more in `tests/test_generator.py` cover the Meshy
adapter's key-unset and every-failure-mode-degrades-gracefully paths
(mocked HTTP layer) plus real GLB parsing (via a mesh `trimesh` itself
built and exported, not a hand-typed fixture) and `generate_mesh_ops`'s
own meshy-vs-procedural wiring -- none of this exercises Meshy's actual
live API, see the AI generation section's honest accounting of what
that means. Also 17 new Phase 11 tests: 5 in `tests/test_document.py`
cover the new `settings` component (independent-field concurrent merge,
serialization round-trip, and loading cleanly when a pre-Phase-11
snapshot has no `"settings"` key at all), and 12 across
`tests/test_export_import.py` cover every shape kind's native SVG/DXF
element/entity (checked by reading the actual element/`dxftype()` back,
not just that the file parses) and unit scaling (mm/in coordinate
scaling, the `$INSUNITS` header variable, and that `units="px"` is
byte-for-byte identical to the pre-Phase-11 default). Also 12 new
Phase 12 tests in `tests/test_document.py` for `bake_path_transform`:
identity/no-op short-circuit, freehand translate and bounding-box-pivot
rotation, circle/rect/arc scale-and-rotate, curve control points
transforming alongside their anchor points, and -- the two that caught
the rotated-rect/ellipse bug described above -- confirming a rotated
Rect/Ellipse actually flattens to its true rotated boundary (with edge
lengths preserved, not just "doesn't crash") while an unrotated one and
a rotated Arc both stay native shape elements as before. Also 14 new
Phase 13 tests: 8 in `tests/test_document.py` for the new `dimensions`
component and `RGA.value_at` -- live resolution, the same
absent-from-an-old-snapshot backward-compat default every other
component gets, merging field-wise like every other LWWMap, and (the
one that mirrors the real bug this phase's own e2e verification caught)
a dimension's anchor resolution being *unaffected* by an unrelated
insert elsewhere in the same path, versus correctly reporting
unresolvable once its own anchor point is actually deleted -- proving
node-id anchoring, not a `point_index`, is what makes "auto-updates
when geometry moves" true instead of just asserted. 6 more in
`tests/test_export_import.py` cover both exporters: a resolved
dimension producing a real SVG `<g class="dimension">` group and a real
DXF `DIMENSION` entity (`get_measurement()` checked directly, not just
that the tag exists), an unresolved one being silently skipped by both,
and the dimension's label/measurement scaling correctly with document
units. Also 6 new Phase 14 tests in `tests/test_document.py` for the
new `constraints` component: roundtripping kind/anchors/param, a
`shape_center` anchor (a circle has no RGA point to anchor to, for
`tangent`), merging field-wise like every other LWWMap, the
serialization roundtrip, the same absent-from-an-old-snapshot
backward-compat default every other component gets, and deletion. Also
19 new Phase 15 tests: 6 in `tests/test_document.py` for the new
`groups` component (existence tracking mirroring `layers`, merge,
serialization, backward-compat default, deletion) plus a dedicated
regression test for the `layer_list`/`path_list` creation-order bug
described below; 13 across `tests/test_export_import.py` covering both
exporters' text/fill/dash/z-order support -- a native `<text>` element
with its content HTML-escaped, `fill`/`fill-opacity` on both shapes and
freehand paths (and that an unfilled shape still renders exactly as
before), dashed/dotted producing a real `stroke-dasharray` (SVG) or a
real named `DASHED`/`DOT` linetype (DXF, confirmed by reading
`doc.linetypes` back, not just the entity's own attribute), a real
`HATCH` entity with the correct `true_color` for a filled shape (and
that Line/Arc are never filled regardless of the prop, matching the
Measure tool's own "no enclosed area" judgment call), and z-order
(layer then creation order) actually reordering emitted elements/
entities across layers. Also 43 new Phase 17 tests: 16 across
`tests/test_persistence.py` (parametrized over `InMemoryStore`/
`SQLiteStore`) for `list_rooms_detailed`/`set_display_name` (including
the missing-room-returns-`False` case) and `save_version`/
`list_versions`/`load_version` (roundtrip, newest-first ordering,
pruning beyond `keep`, per-room-and-kind scoping, and that deleting a
room also clears its version history); 2 more in
`tests/test_postgres_store.py` mirroring the same display-name/version
coverage against a real Postgres when one is reachable (skips cleanly
otherwise, same convention as the rest of that file); and 25 in the new
`tests/test_workspace.py` covering the REST/WS surface directly: the
combined-kinds workspace listing sorted newest-first, rename
(including 404 for a room that doesn't exist), a real SVG thumbnail
rendered from a live snapshot, an explicit save producing exactly one
version checkpoint (not one per idle periodic tick, and not one per
op), a periodic checkpoint firing only when the room is actually dirty,
pruning to a monkeypatched `max_versions_per_room`, restore forking a
new room while leaving the original's content untouched, share-link
minting's 400 when auth is disabled and its 403 when a viewer token
tries to mint one (of *either* role -- the privilege-escalation guard),
a viewer token being refused by an editor-only REST endpoint while an
editor token still works, the WS snapshot's `"role"` field for both a
viewer and the (default) editor case, a hand-crafted viewer WS's `ops`
message being rejected and confirmed never applied (a fresh connection
afterward sees the still-empty document), an editor's ops still being
accepted and persisted normally, a viewer token still being scoped to
its own room/kind (doesn't silently grant access elsewhere), and the
three route assertions (`/` now serves the workspace home page, `/2d`
serves the 2D demo, `/3d` unchanged).

Beyond unit tests, this was driven end-to-end with Playwright against a
live server multiple times during development: two tabs drawing
concurrently; one going fully offline mid-edit, drawing more, and
reconnecting through a real Time-Travel Merge panel; a real
`RTCPeerConnection` negotiating between two headless Chrome tabs; the
strict Polygon tool's rejection round-trip; every download/import/save
button; the security hardening's full opt-in flow (server started with
`CRDT_CAD_SECRET` set: a fresh tab prompts for the secret, a wrong guess
re-prompts, the correct one connects and stores a token, the Share
button's invite link lets a second, completely fresh browser context
join with *zero* prompts, and a deliberately-bad `?token=` in the URL
correctly clears itself and re-prompts instead of looping forever); the
3D demo's Undo/Redo buttons and Ctrl+Z/Ctrl+Y/Ctrl+Shift+Z shortcuts
(vertex create, extrude -- confirming a *single* undo click removes
every vertex/face an extrude created, not just the last one -- face
color/material, and a check that the shortcut is correctly ignored
while a text field has focus so it doesn't fight the browser's own
undo); the "Extrusion Nightmare" validity warning end-to-end (tab A
extrudes a face, tab B deletes one of that face's shared boundary
vertices, both tabs render the red outline and a banner naming the
exact face and problem, and dismissing it on one tab clears only that
tab's outline); the Docker image built, run, and checked for
persistence-across-container-restart; and the horizontal scaling seam
(two real `uvicorn` processes, a real Postgres, a real Redis, one raw
WebSocket client per process) with a genuine op sent to process A
arriving at process B and a fresh connection to process B afterward
correctly seeing what process A had persisted; and importing a real SVG
containing a cubic curve followed by a smooth (`S`) cubic, confirming it
renders as one continuous curve (via `bezierCurveTo`, not a jagged
straight-line facet) and that exporting it back out produces a real `C`
command, not a flattened polyline. Real bugs were caught this way that
no unit test had covered (a fire-and-forget background task that hung
the process at exit, "offline" not tearing down an already-open P2P
channel, a URL-token re-prompt loop where a proven-bad token never
actually left the address bar, the Redis relay loop initially
forwarding an incoming op to local clients without ever applying it to
the receiving process's own document -- see "Horizontal scaling seam"
above -- and, while verifying the curve import, a **pre-existing**
gap this didn't introduce: SVG import has never carried a source
file's per-path stroke color into the document, so the imported curve
was initially invisible against the canvas's own near-black default
color, not because the curve itself was wrong) -- all fixed (or, for
the pre-existing color gap, documented rather than silently left for
the next person to rediscover) and specifically regression-tested.
Phase 9's stretch items got the same treatment: the Constrain tool
(draw two lines with the Pen tool, select an endpoint from each, apply
Coincident -- confirmed converged via `/export/json`, not just visually,
and synced to a second tab; separately, Parallel -- confirmed via the
resulting direction vectors' cross product, which is what actually
caught the `steps=2`-drag-produces-an-extra-point test artifact
mentioned above), STEP export (built a real triangular face via the 3D
demo's Vertex/Face tools, clicked **.step**, confirmed the downloaded
file starts with `ISO-10303-21;` and contains real `ADVANCED_FACE`
records), and the AI Generate panel's new `mesh_source` label (generated
a house with `MESHY_API_KEY` unset -- the only configuration available
here -- and confirmed the status line correctly reads "mesh via the
procedural builder"). Phase 10's viewport and Phase 11's shapes/units
were verified together in one pass: pan/zoom (wheel centered on cursor,
Space-drag), Fit, snap-to-grid, all five shape kinds drag-created and
confirmed via `/export/json`, the numeric panel creating a shape with no
drag at all, Select correctly hit-testing a circle's real boundary (and
correctly *not* selecting it from well inside), and switching units to
mm live-updating the cursor readout -- all in the same real browser
session, with a screenshot confirming the rendered result actually
looks like five distinct, correctly-shaped, correctly-colored
primitives, not just that no exception was thrown. This pass itself
caught a real bug on first try: `shapeDraft` was referenced throughout
the new code (drag handlers, rendering, the numeric panel) but never
actually declared, a `ReferenceError` thrown at script load that broke
page initialization *entirely* -- not just the new shape tools. It
first surfaced as nearly the *entire* existing e2e suite failing
uniformly (9 of 10 tests, all with the same generic
`statusText` never-reaches-"online" timeout), which is exactly the
signature of "something threw during script load," not a feature-
specific regression -- confirmed by checking the browser console
directly rather than guessing from the test failure alone. `node
--check` (syntax-only) never had a chance of catching this, since it's
valid syntax, just a missing declaration; only actually running the
page did.

A third, more interesting one turned up while browser-verifying the
face color/material editor: `LocalClock` (`common.js`, shared by both
demos) minted OpIds by incrementing a plain local counter from zero,
without ever *observing* the counters on incoming ops the way the
Python `LamportClock.observe()` already does server-side. A fresh
client joining a room the AI generator (or an import, or any other
actor) had already populated with higher-numbered ops would have its
own edits silently lose the LWW `(counter, actor)` tie-break -- not an
error, just a change that never visibly took effect, exactly the kind
of bug a unit test wouldn't catch (it requires two real actors and a
real reconnect sequence) but a live two-actor Playwright run surfaces
immediately. Fixed by adding `LocalClock.observe()` and calling it from
both demos' `loadSnapshot()`/`applyOp()`, so a replica's clock always
catches up to the highest counter it has seen before minting its next
local op.

Phase 12's selection editing was verified in one long real-browser
pass: a rect dragged and rotated 45 degrees (screenshot-confirmed it
actually renders as a rotated diamond, not just the raw shape at a new
offset), align-left pulling a far-off circle flush with a rect's left
edge, shift-click and a marquee drag independently converging on the
same two-path selection, duplicate/copy/paste creating visibly offset
copies (confirmed via a "Copied N path(s)"/"Pasted N path(s)" toast,
not just a silent op count), Delete removing exactly the intended
selection, and the `?` shortcut overlay opening and closing. This pass
is what caught the rotated-rect/ellipse export bug described in the
"Selection editing" section above -- the in-app rendering and
hit-testing were both already correct (the canvas transform and
`applyPathTransform` math worked from the first try), so a
purely-unit-tested `bake_path_transform` looked fine in isolation until
the actual exported SVG was inspected and turned out to describe an
axis-aligned box sitting in the wrong place, not the rotated one
visible on screen -- a reminder that a function can pass every test
written against its own stated contract while still not doing what the
*whole system* needs, if nobody checks its output against the thing
it's supposed to mirror. It also surfaced a test-authoring mistake of
its own, worth recording alongside Phase 9's `steps=2` one: an early
verification script tried to drag-move a rectangle starting from a
point *inside* its interior, which correctly did nothing, since shapes
are unfilled outlines (Phase 11) and a drag has to *start* on the
actual boundary stroke to grab the shape at all -- not a bug, but
exactly the kind of assumption a live run catches that a purely
hand-reasoned test would not.

Phase 13's Measure/Dimension tools were verified live end to end: a
freehand path's Distance measurement (200px between two endpoints) and
a rect's Area/Perimeter (6000/320), confirmed via the panel text, not
just "no exception"; a dimension created between two points, confirmed
persisted via `/export/json` and synced to a second tab's own panel;
its SVG/DXF exports checked directly (a `<g class="dimension">` group,
and a real `DIMENSION` entity via `ezdxf`). This exact pass is what
caught the `movePathPoint`/anchor-orphaning bug described in the
"Measurement and dimensions" section above -- the first attempt at this
verification used the Constrain tool's Coincident action to actually
move one of the dimensioned points, expecting the dimension's shown
value to update, and it silently stayed at the old, now-wrong distance
instead. `page.evaluate()` dumping `state.dimensions`/`state.pathNodes`
directly (rather than guessing from the rendered text alone) showed
exactly why: the moved point's node id had changed, and the dimension
was still referencing the old, now-tombstoned one. Fixed by
`remapDimensionAnchor`; re-running the same verification afterward
showed the dimension correctly updating to the new (solver-computed
midpoint) distance. A second, smaller mistake surfaced in the same
pass: the initial verification assumed a Coincident constraint moves
one point *exactly onto* the other's original position, when the
solver actually moves both points toward their shared midpoint (least
total movement) -- corrected by computing the expected midpoint
directly rather than guessing, the same "verify the actual solver
behavior, don't assume" discipline Phase 9's own constraint testing
already established.

Phase 14's persistent constraints were verified live end to end: a
Coincident constraint applied between two lines' endpoints, confirmed
persisted via `/export/json` (not just a visual snap) and listed in the
Constraints panel; Undo/Redo exercised afterward and confirmed neither
threw; a circle drawn alongside a line, picking the circle (its
boundary, not its interior -- same convention as every other shape
interaction) plus a line point correctly surfacing a Tangent button
(and only Tangent, not the other four); the Tangent constraint applied
and persisted; dragging one of the tangent line's points and confirming
exactly one constraint still existed afterward (a re-solve, not a
duplicate); and deleting a constraint via its panel row. One real
timing lesson from this pass, worth recording since it's easy to
reproduce accidentally: checking `state.constraints.size` client-side
immediately after clicking Apply proves the *local, optimistic* apply
happened, not that the server has processed the corresponding WS
message yet -- an early verification script queried the server's own
`/export/json` in the same instant and intermittently saw one fewer
constraint than the client already showed. Fixed by giving the
WebSocket round-trip a short buffer after the client-side condition is
met before trusting server-side state, not by lengthening an already-
timing-based wait blindly.

Phase 15's designer features were verified live end to end: the Text
tool placing "Hello CRDT" and editing its font size to 24 via the
selection panel, confirmed via `/export/json`; a rect filled orange at
0.4 opacity, confirmed both persisted *and* now clickable from its
exact interior point that did nothing before the fill was applied (the
hit-testing change, not just the visual one); a dashed line persisting
its `dash` prop; grouping a rect and a circle, confirming clicking only
the rect selects both, then Ungroup reverting it; and both PNG export
buttons triggering a real download with a `.png` filename, with the
fit-to-content variant's zoom indicator confirmed back to its original
value only *after* the download actually fired (see the async
`toBlob()` bug below) -- a full screenshot afterward visually confirms
the text, filled rect, and dashed line all render correctly together
in one document. This pass is what caught the `canvas.toBlob()` timing
bug in the fit-to-content PNG export described above: the first draft
restored the view immediately after calling `fitToContent()`, which is
synchronous, but `toBlob()` isn't -- by the time its callback actually
ran, `render()` had already redrawn the canvas back to the original
(unfit) view, so the "fit" variant would have silently captured the
wrong framing. Caught by reasoning through the actual async ordering
before writing the verification, not by the verification itself
failing first -- worth recording as a case where careful code reading
(not just live-testing) found the bug, the reverse of most other
entries in this log.

Phase 16's primitives and snapping were verified live end to end: Box
created and its Undo removing all 8 vertices/6 faces in one click,
Redo restoring them; an 8-segment Cylinder producing exactly 16
vertices/10 faces; default Pyramid and Plane producing 5/5 and 4/1;
grid snap landing an arbitrary click on an integer coordinate; and a
vertex-to-vertex drag snapping exactly onto the target. Getting the
snapping checks right took three passes, each one a genuine lesson: the
first draft left Snap toggled on from the grid-snap check when it went
on to place two fresh vertices for the drag check, so those two landed
slightly away from their actual click coordinates -- meaning re-clicking
that same screen spot to grab one for the drag silently missed its
sphere and created a third vertex instead of dragging the second; fixed
by explicitly toggling Snap off before placing them. The second draft,
after that fix, dragged toward a nearby offset instead of the exact
target -- at this scene's camera distance a few screen pixels covers
more world-distance than the 0.3-unit snap threshold, so the drag
routinely landed just outside it; fixed by targeting the *exact*
original screen coordinates instead of an approximation, since both
points came from the same ray-plane intersection to begin with. The
third, and the one that mattered most: even with the drag correctly
landing inside the snap threshold, the Vertices panel kept showing the
pre-drag position -- which is what led to finding the genuine
`pointerup`/`renderPanels()` product bug described in the section
above, not a test artifact. All three were resolved by adding temporary
diagnostic logging and isolating one layer of the problem at a time
rather than guessing, then removing that instrumentation once each root
cause was confirmed.

Phase 17's workspace was verified live end to end, in two separate
passes since the second needs `CRDT_CAD_SECRET` configured (the first
doesn't). Pass one: a 2D room with a rect and a 3D room with a box were
each saved, then the home page confirmed both cards render (correct
kind badges, a real thumbnail `<img>` for the 2D one, the placeholder
icon for the 3D one); Rename was applied and the home page re-rendered
with the new name; History showed one checkpoint and Restore navigated
to a real forked room whose exported JSON contained the original rect,
while the source room's own export was confirmed unchanged; and the
in-editor **✎** rename button updated the status-bar label immediately.
Pass two (server started with `CRDT_CAD_SECRET` set): an editor drew a
shape and minted a view-only link via the clipboard; a *second,
completely fresh* browser context opened it with zero prompts, saw the
editor's shape, showed the "view only" badge, and confirmed via
`getComputedStyle` that the Rect tool button is genuinely
`pointer-events: none` (not just dimmed) -- interestingly, this was
confirmed *by Playwright's own click() call refusing to click it* on
the first real attempt (its actionability check kept retrying against
an element that correctly never becomes clickable, until it timed out)
-- a case where a test author's assumption ("I can `.click()` a
disabled button to prove it's disabled") was wrong, not the product;
fixed by asserting the computed style directly, then separately
force-clicking past the disabled button and dragging on the canvas
anyway to confirm the deeper guard (`viewerMode` gating `pointerdown`
itself) still lets nothing through, checked both client-side (no new
path row) and server-side (the room's own `/export/json` still shows
exactly the one shape). The editor tab was then confirmed still fully
functional (drew a second shape) throughout. **Unlike most previous
phases, this one did not surface a new product bug during verification**
-- worth stating plainly rather than manufacturing one just to match
the pattern of every other phase's writeup. The most plausible reason:
the 25 server-side tests in `tests/test_workspace.py` already exercised
exactly the boundary conditions a live pass would have caught first
(role escalation, WS rejection, restore isolation, pruning), so by the
time the browser was involved, what remained to check was mostly
"does the already-tested server behavior actually reach the UI" --
which it did, on the first real attempt in both passes.

### Committed e2e suite + CI

All of the ad-hoc Playwright verification above was, for most of this
project's life, exactly that -- ad-hoc, run by hand, never committed.
`tests/e2e/` (89 tests, opt-in via `pytest -m e2e`, excluded from a plain
`pytest tests/` run so a fresh checkout without Chromium installed still
passes) makes several of those scenarios permanent, regression-tested
code instead of tribal knowledge: two tabs drawing concurrently and
converging; the full offline -> edit both sides -> reconnect ->
Time-Travel Merge -> converge sequence; the strict Polygon tool's
self-intersection rejection (a genuine bowtie shape, verified against
the *real* click-to-place-vertex interaction, not a drag -- an earlier
draft of this test used the wrong gesture entirely and silently created
no path at all); the `LocalClock.observe()` regression above,
reproduced end-to-end (generate a house via AI, have a fresh client
edit a face's material, confirm the edit actually persists server-side);
the offline-outbox-survives-a-hard-refresh behavior
(`test_offline_durability_e2e.py`) -- go offline, draw, `page.reload()`,
confirm the edit is both visible locally and actually landed
server-side, plus a check that a room nobody ever went offline in shows
no recovery toast (persistence is additive, not a new default); and
(Phase 9) `test_constraint_ui_e2e.py` -- draw two lines, select an
endpoint from each with the Constrain tool, apply Coincident, and
confirm via the server's own `/export/json` (on *both* tabs) that the
two points actually converged, not just that the UI looked right; and
(Phase 10) `test_viewport_e2e.py` -- the default view stays
backward-compatible and the zoom indicator updates on wheel-zoom, a
remote presence cursor projects correctly through a *different,
asymmetric* view transform on the receiving tab (checked against that
tab's own live `worldToScreen`, not a hand-computed expectation), and
snap-to-grid produces genuinely grid-aligned stored points; and
(Phase 11) `test_shapes_e2e.py` -- all five shape kinds drag-created and
confirmed via `/export/json`, the Select tool correctly hit-testing a
circle's actual boundary stroke (not its unfilled interior -- clicking
well inside must *not* select it), the numeric panel creating a shape
with no drag at all, and the `units` document setting (a real CRDT
setting, unlike Phase 10's deliberately-local view transform) actually
syncing to a second tab's own dropdown; and (Phase 12)
`test_selection_editing_e2e.py` -- dragging a selected shape's boundary
actually writes a `transform` (not silently doing nothing), shift-click
and marquee independently building the same multi-selection,
align-left equalizing two shapes' left edges via the server's own JSON,
duplicate-then-Delete round-tripping the path count, and -- the
regression test for the export bug described below -- a rotated rect's
SVG export containing a `<path>` polygon and no `<rect>` at all; and
(Phase 13) `test_measurement_dimensions_e2e.py` -- Measure's Distance
and Area/Perimeter modes reading correctly (200px; 6000/320) and
provably sending zero new document state (a full before/after
`/export/json` diff, not just the ops counter, since presence pings
from ordinary mouse movement are expected background traffic
regardless of tool); a dimension persisting and syncing to a second
tab; and -- the regression test for the anchor-orphaning bug described
above -- a dimension correctly tracking its anchor point's new position
(not "(geometry deleted)", not the stale old value) after the
Constrain tool actually moves it; and (Phase 14)
`test_constraint_persistence_e2e.py` -- applying a constraint actually
persists it as real document state (not just a one-time visual
effect); Undo genuinely reverting a constraint-driven point move (a
real, pre-existing gap this phase closed -- `movePathPoint` never
pushed an undo entry before); Tangent's different picking mechanism
(a circle shape, not a point) producing a `shape_center` anchor;
and dragging an already-constrained point re-solving on release
without minting a duplicate persisted constraint; and (Phase 15)
`test_designer_features_e2e.py` -- the Text tool's placed content/font
size actually persisting; the fill hit-testing change (a click that
selects nothing before filling a shape does select it afterward, same
point, same test); a dash style persisting; grouping making one
member's click select the whole group and Ungroup reverting it; and
both PNG export buttons producing a real `.png` download, with the
fit-to-content variant's view confirmed restored afterward; and (Phase
16) `test_3d_usability_e2e.py` -- Box creating in one click with Undo/
Redo removing and restoring all 8 vertices/6 faces atomically;
Cylinder/Pyramid/Plane each producing the expected vertex/face counts;
grid snap landing an arbitrary click on an integer coordinate; a
vertex-to-vertex drag snapping exactly onto the target *and* the
Vertices panel reflecting the new position afterward (the regression
test for the stale-panel-after-drag bug above, which is exactly why
this test reads positions from the panel's own `.vertex-coord` inputs
rather than the 3D scene); and the four view buttons not throwing; and
(Phase 17) `test_workspace_home_e2e.py` -- the home page rendering a
real 2D thumbnail and a 3D placeholder icon with the correct kind
badges for genuinely-created rooms; rename persisting and the home
page's card re-rendering with the new name; History listing a
checkpoint and Restore navigating to a forked room whose export
contains the original content while the source room's own export is
unchanged; and the display-name **✎** button updating the actor label
and syncing to a second tab's presence list; plus
`test_readonly_share_links_e2e.py` (needs `live_server_factory` with
`CRDT_CAD_SECRET` set, unlike every other file here) -- a view-only
link opened by a completely fresh browser context grants read access
with zero prompts, shows the "view only" badge, and genuinely blocks
editing (`pointer-events: none` on a tool button, confirmed via
`getComputedStyle`; a force-clicked tool plus a canvas drag still
creates nothing, checked both client-side and via the server's own
export) while the editor tab stays fully functional throughout; and a
viewer-role token being refused (403) by an editor-only REST endpoint
(rename), not just the WS ops path; and (Part 3 Phase D1)
`test_theme_toggle_e2e.py` -- the theme toggle persists across a reload
*and* across navigation to all three pages (not just the one it was
clicked on), an icon actually renders non-zero geometry rather than
just existing as correct-but-invisible DOM markup (the regression test
for the external-sprite bug described above), and toolbar button text
no longer starts with an emoji glyph; and (Phase D2)
`test_layout_e2e.py` -- the tool rail's active-tool indicator follows
tool switches; a collapsed panel's own `getBoundingClientRect().width`
is actually 0 (the regression test for the content-bleed bug in the
Layout section above -- checking only that a CSS class got toggled
would have missed it) and the `\` shortcut restores both at once; a
tooltip appears with the correct label; the document-name rename
persists through the "room doesn't exist yet" 404-then-retry path and
survives a reload; and the top-bar avatar stack shows both actors once
each has sent at least one presence update; and (Phase D3)
`test_input_feel_e2e.py` -- the 2D canvas cursor follows the active
tool (crosshair for Pen, default arrow for idle Select) and switches to
"move" while hovering a hit-testable shape's own drawn boundary, then
correctly reverts once the pointer leaves that shape while still
staying within the canvas's own bounding box; a hovered shape's
`hoveredPathId` is distinct from and does not disturb an existing
selection; holding Space shows the "grab" pan cursor regardless of
tool; and the 3D demo's cursor likewise follows Vertex (crosshair) vs.
Move (default arrow); and (Phase D4) `test_keyboard_e2e.py` -- the
command palette filters and runs a command (a fuzzy "rectangle" query
switches to the Rect tool); Ctrl/Cmd+K opens the palette even while
focused in the room-id input, and Esc closes it and restores focus to
that same input; a single-key shortcut ("r") switches tools when
nothing is focused but types a literal "r" instead when the room-id
input has focus; Ctrl+Z/Ctrl+Shift+Z now undo/redo in the 2D demo;
arrow-key nudge moves a selected path's transform by 1px (10px with
Shift); the "?" overlay is searchable (a "duplicate" query narrows it
to exactly one row) and closes on Esc; the skip-link is the very first
Tab stop and activating it focuses the canvas; the 3D demo gets the
same single-key shortcuts, palette, and overlay; and a read-only
viewer's keyboard shortcuts are inert -- pressing "r" leaves the active
tool untouched and Ctrl+D does not duplicate a selection forced via
direct state manipulation (since a viewer's pointerdown gesture is
already blocked outright, there's no click-driven way to select
something to test against, so the test isolates exactly what this
phase changed: whether the keyboard handler itself checks
`viewerMode`); and (Phase D5) `test_state_legibility_e2e.py` -- the
status cluster reads "Live" once online (while `#statusText` keeps its
old raw "online" word unchanged) and its popover opens with a non-empty
explanation and closes on Esc, restoring focus to the button; going
offline and then queuing an edit updates the label to "Offline -- N
edits queued"; the save label reads "Saved just now" both right after
connecting and after an explicit Save click; the empty-canvas hint
shows on a fresh room and hides the instant something's drawn, in both
demos; three toasts queued back-to-back show exactly one at a time, and
hovering the visible one keeps it showing well past its normal 4s
duration; deleting a path shows an "Undo" toast whose action button
restores it; and a self-intersecting strict polygon shows a "Rejected"
toast while `flashingPathIds` briefly holds that path's id, then clears
itself; and (Phase D6) `test_presence_e2e.py` -- `ACTOR_COLORS` is
exactly 8 distinct hues, none the old failing yellow; a second tab
joining triggers a "joined" toast and an entrance-animated avatar on
the first; a remote cursor's rendered position keeps easing well after
a big jump rather than landing instantly, and its name label gains/
loses an `idle` class after 3s of no movement / on the next update;
`remoteSelectionColorFor` returns a color once another tab selects a
path, and `remoteEditFlashes` briefly holds that path's id when the
other tab edits it; clicking another actor's avatar re-pans the local
view toward them and a manual wheel-zoom clears `followingActorId`
again; and, for the 3D demo (DOM-signal-only, since its
`<script type="module">` scoping makes internal state unreachable from
`page.evaluate`, unlike sketch.js's classic-script globals), a second
tab that joins and does nothing at all still produces a "joined" toast
and a visible cursor (the regression test for the "3D presence was
never sent until the first edit" gap this phase found), the cursor
gains an `idle` class after 3s and loses it on the next update, and
follow mode leaves the followed actor's own rendered cursor position
near the viewport's center; and (Phase D7) `test_art_direction_e2e.py`
-- the Time-Travel Merge modal's text never contains "conflict" and
does contain both new column titles; a chip in the "while you were
away" column and a chip in the "meanwhile" column have different
computed `border-left-color`s (proving per-author, not per-column,
coloring); clicking "Merge now" immediately applies the
`merge-converging` class before the 220ms removal delay, and a
"changes combined" toast follows; a large-enough AI generation shows
the `ai-thinking` shimmer immediately, then a "Building..." progress
line before the shimmer clears, then a completion toast naming
`ai_generator_bot` with vertex/face counts; and a real 429 (the actual
rate limiter -- 3 direct warm-up requests via Playwright's own request
context exhaust its capacity-3 token bucket deterministically, since
repeated UI clicks would only serialize behind `#genBtn`'s own
disabled state and a fourth click's timing-dependent race was exactly
the kind of flakiness this suite has caught before; the fourth,
real, UI-driven click then reliably 429s) produces a danger toast with
an inline Retry action and leaves the prompt text untouched; and
(Phase D8) `test_design_system.py` -- a 16-image screenshot matrix (2
demos x 4 viewport widths x 2 themes) archived to
`docs/screenshots/audit_*.png`, asserting every combination loads with
no console errors; every icon-only button on all three pages has a
non-empty `aria-label`; the first real Tab stop shows a visible focus
ring; the 375px-breakpoint mobile tool-rail's buttons are all >=44px in
both dimensions; and `--text-primary`/`--text-secondary` clear 4.5:1
against both `--bg-app` and `--bg-panel` in both themes, computed live
from `getComputedStyle` rather than assumed from the token values.
`tests/test_frontend_kill_list.py` (4 tests, no browser needed, part of
the plain `pytest tests/` run) statically checks the same properties
the D8 brief's kill list names: no emoji glyph anywhere in
`demo/static/*.html`/`*.js`; every `z-index` outside `tokens.css`
itself uses a `var(--z-*)` token; the one `outline: none` in the whole
stylesheet is paired with a real `:focus-visible` ring, not a bare
removal; and the one transition on a layout-triggering property
(`.body`'s `grid-template-columns`) is the single documented,
deliberate exception.
Each spins up a real `uvicorn` subprocess on a free port with its own
temp SQLite file (`tests/e2e/conftest.py`), so they exercise the actual
client JS against the actual relay -- not an in-process
`fastapi.testclient` double.

`.github/workflows/ci.yml` runs on every push/PR: `pytest` + `ruff
check` (fast job), the e2e suite (`playwright install chromium` then
`pytest -m e2e`), and a plain `docker build` -- three independent jobs,
the e2e one depending on the fast job passing first.

## Deployment

See **`docs/deployment.md`** for the full runbook (VPS + Caddy, Fly.io,
backups) and **`docs/configuration.md`** for every `CRDT_CAD_*`
environment variable. Summary:

**Docker (local dev)**: `docker compose up --build` -- built and
run-verified, including a full write -> restart -> data-still-there
check against the named volume.

**Docker + Caddy (production, single VPS)**: `docker-compose.prod.yml` +
a committed `Caddyfile` -- real HTTPS (Let's Encrypt for a real domain,
a local self-signed cert for `localhost`), verified end-to-end: `wss://`
through the proxy, the room-token auth flow, and the `/generate` rate
limiter correctly reading the real client IP via `X-Forwarded-For`
(`CRDT_CAD_TRUST_PROXY_HEADERS`) instead of Caddy's own. See
`docs/deployment.md`'s "VPS" section.

**Fly.io**: `fly.toml` committed (single machine, volume-mounted SQLite,
always-on for WebSockets). Checked for valid TOML and cross-referenced
against Fly's documented schema by hand, but **not live-deployed or
platform-validated** -- no Fly account/token in this environment. See
`docs/deployment.md`.

**Kubernetes**: manifests under `k8s/`, **validated against a real
cluster** (kind, Phase 18) -- both the default single-replica/SQLite
configuration and the horizontally-scaled Postgres+Redis configuration,
plus HPA and a TLS ingress. Read `k8s/README.md` before touching
`replicas` or `hpa.yaml`: the *default* configuration (SQLite, no Redis)
still keeps room state on one pod, so scaling past 1 replica in that
configuration would silently split users across pods that can't see
each other. `PostgresStore` + Redis pub/sub (see "Horizontal scaling
seam" above) make replicas > 1 legitimate once both are configured --
`k8s/README.md` has the exact steps and what was verified where.

**Backups**: `scripts/backup_sqlite.py` (SQLite mode, using the online
backup API -- safe against a live writer) and documented `pg_dump`/
`pg_restore` (Postgres mode) -- both with an automated restore test
(`tests/test_backup_sqlite.py`, `tests/test_backup_postgres.py`). See
`docs/deployment.md`'s "Backups" section.

**Graceful shutdown**: SIGTERM persists every room still holding
unpersisted ops before the process exits -- verified against a real
`docker stop` on a real container (not just a unit test): an edit sent
but never explicitly saved survives, and a fresh container reusing the
same volume picks the room back up intact.

**Metrics**: `/metrics` is real `prometheus_client` output (connection
counts, ops relayed, geometry rejections, merge-apply latency), so a
Grafana dashboard built against it is portable to a real deployment
unchanged.

## Responses to the architecture critique

A detailed external review raised three specific concerns. Taking each
on its merits rather than either rubber-stamping or dismissing it:

**1. "Thin-client bottleneck / the offline UX is an illusion since
every edit needs a server round-trip."** This is inaccurate as stated,
and it's checkable directly in the code: `RelayConnection` in
`common.js` mints every `OpId` locally and calls `applyOp()` to render
optimistically *before* anything is sent anywhere (see `addVertex`,
`finishFace`, etc. in `mesh3d.js` -- the op is applied locally, then
`sendOps()` is called). Going "offline" doesn't disable editing; it
queues emitted ops in an outbox and keeps working normally, which is
exactly what the Time-Travel Merge feature demonstrates. The one real,
narrower gap this claim did have a point about -- the outbox was
**in-memory only**, so a hard refresh or closed tab while offline lost
queued-but-unsent ops -- is now fixed: `persistOutbox()`/
`loadPersistedOutbox()` in `common.js` durably queue it in IndexedDB,
keyed per room+actor, without duplicating any CRDT logic client-side (a
Pyodide-hosted engine was considered and rejected for exactly that
reason -- see the Roadmap entry below). The fix took one genuinely
subtle wrong turn worth recording: the first version *also* persisted
the frontier (vector clock) across a reload, on the theory that
restoring it would let the reconnect flow through the normal
Time-Travel Merge `delta` path. That's backwards -- a `delta` reply
only contains ops the server thinks the client's *local state* is
missing, which is a safe assumption when a dropped WebSocket left
memory intact, but not after a hard reload wipes `state` to nothing.
Restoring a stale frontier made the server send a correctly-near-empty
delta, leaving the client's own view empty despite the server having
everything. Fixed by never claiming a `known_frontier` on a connection's
first connect (always request a full, correct `snapshot`) and replaying
the recovered outbox locally on top of it instead -- caught and fixed
before shipping, via the same live-browser verification discipline this
README keeps asking of every feature.

**2. "Tombstone accumulation / no garbage collection."** Real gap,
now fixed at the safe level: `RGA.compact(safe_vc)` (`crdt/rga.py`)
drops the *stored value* of any tombstone whose delete every replica
has already causally observed, while permanently keeping the tombstone
node's `id`/`origin`/`deleted_by` metadata. That distinction is
load-bearing, not a half-measure: an RGA node must remain resolvable as
an anchor forever, because a replica that's been offline for a long
time can still arrive with an insert whose `origin` points at that
node -- if the node were fully removed, that insert would have nothing
to anchor to, and different replicas compacting at different times
would make that failure mode replica-dependent (a real convergence
break, not just a memory optimization gone wrong). Value-only
compaction is safe with no coordination protocol; full node removal
is not, without a distributed causal-stability protocol (every replica
provably has the delete) that this project doesn't implement. Tested
in `tests/test_rga.py`: compaction removes stable tombstone values
without disturbing ordering or anchoring, and a fresh replica catching
up via `ops_since` after compaction still converges correctly.

**3. "Extrusion Nightmare" -- concurrent non-commutative edits (a face
boundary edit racing an extrude of that same face) can diverge.**
Correct that `MeshCRDT` merges each component (vertices/edges/face
index/per-face RGA) independently and correctly *on its own terms* --
and that was never going to change, since rejecting a CRDT merge
outright breaks convergence. What's now built is the "Validation Fork"
in the more limited, honest form that's actually compatible with a
CRDT: `crdt_cad.geometry.mesh_validity.check_mesh_validity` runs
*after* a merge that touched face topology (or deleted a vertex --
see `_touches_mesh_topology` in `app.py`), using `trimesh` to check for
(a) degenerate faces (zero-area/collinear), (b) non-manifold edges
(shared by more than 2 faces), (c) inconsistent winding between
adjacent faces, and (d) a face boundary referencing a vertex with no
live position -- exactly the "Extrusion Nightmare" shape, where one
replica extrudes a face while another concurrently deletes one of that
face's original boundary vertices. Deliberately *not* checked:
watertightness -- empirically, `trimesh.Trimesh.is_watertight` is
`False` for any incomplete WIP mesh (including a single valid
triangle), so using it as a warning trigger would be constant noise on
normal in-progress editing, not a signal. Any problems found are
broadcast to the room as `{"type": "validity_warning", "faces": [...],
"problems": [...]}` (see the Sync protocol section) -- a **warning, never
a gate**: the merge already happened and can't be undone without
breaking convergence, so the client just highlights the affected
face(s) with a red outline and a dismissible banner, and it's up to a
human to fix or delete them. Verified against a real concurrent merge
between two `MeshCRDT` replicas (`test_the_extrusion_nightmare_end_to_end_via_real_concurrent_merge`
in `tests/test_mesh_validity.py`), against the live WS broadcast wiring
(`tests/test_mesh_validity_integration.py`), and live in two real
browser tabs via Playwright (extrude in tab A, delete a shared boundary
vertex in tab B -- both tabs correctly show the outline and banner,
naming the exact face and problem, with no console errors). The
suggested DAG-based B-Rep replacement for the RGA-based face-boundary
representation remains genuinely **not implemented** -- a bigger,
structurally different data model than a warning-pass justifies
building this round -- and is called out in Roadmap below.

## Roadmap / what's not built yet

1. ~~**STEP/IGES export**~~ -- **STEP done** (Phase 9), re-evaluating the
   old "conda-only in practice" blocker: `build123d` (which pulls in
   `cadquery-ocp-novtk`, a real PyPI wheel) turned out to install and
   work in this environment after all -- confirmed by actually building
   and exporting a real STEP file, not assumed. See "STEP export" in the
   Import/export section above for the faceted-B-Rep scope and what a
   non-planar face falls back to. IGES remains unbuilt (not asked for by
   the brief's re-evaluation, and `build123d`/OCP's IGES support wasn't
   investigated).
2. ~~**Shared room-state broker for true horizontal scaling**~~ --
   **Done.** `PostgresStore` (shared persistence) and Redis pub/sub
   (`Room.broadcast()` now reaches clients connected to *other*
   processes) are both real, opt-in, and live-verified with two actual
   server processes *and* against real Kubernetes pods on a kind cluster
   (Phase 18 -- see "Horizontal scaling seam" in the Persistence section
   above and `k8s/README.md`). What's still open: a managed-cloud cluster
   (EKS/GKE/AKS) was never tried, only kind; a Kafka-style event log was
   not built (Redis pub/sub satisfies the same requirement the brief
   poses it as an alternative for); and the HPA's custom-metric path
   (scaling on `crdt_cad_active_connections` via a Prometheus Adapter,
   rather than CPU) remains future work.
3. ~~**Interactive constraint-assignment sketch UI**~~ -- **Done**
   (Phase 9). A new **Constrain** tool in the 2D demo: click two points
   (any paths, same or different) and apply Coincident, Parallel,
   Perpendicular, or Fixed Distance -- the existing, already-tested
   `/api/solve` endpoint does the actual solving; the client just moves
   the affected points via ordinary delete+reinsert `path_geom` ops
   (RGA values are immutable once inserted, so a "moved" point is a new
   node id under the hood -- see `movePathPoint` in `sketch.js`), synced
   to everyone the normal way. Parallel/perpendicular relate each
   selected point's *segment* (inferred from its live neighbor), not
   just the bare point. Live-verified: a Coincident pair converging to
   the same position and a Parallel pair's direction vectors reaching a
   zero cross product, both via the server's own JSON export (not just
   a visual effect), and synced to a second tab in the same room. One
   accepted trade-off: moving a point this way orphans any curve segment
   (Phase 8) attached to its old node id, reverting that segment to a
   straight line -- documented in `movePathPoint`'s docstring, not
   silently ignored.
4. **Pyodide/WASM client-side engine** -- the brief allows this as an
   enhancement. Deliberately not done: it would mean running the same
   package twice, and a thin JS renderer that reuses the *tested* server
   logic as the single source of truth was the more honest choice than
   an integration that's hard to verify reliably in this environment.
   A narrower version of this concern -- the in-memory offline outbox
   not surviving a hard refresh -- **is now fixed** (persisted to
   IndexedDB, see "Responses to the architecture critique" claim 1 above)
   without duplicating the engine.
5. ~~**Cross-component mesh validity ("Validation Fork")**~~ -- **Done.**
   A manifoldness/winding/degeneracy check now runs over the *merged*
   result after any op that could create or reveal this class of
   problem, and broadcasts a `validity_warning` (never a rejection --
   see "Responses to the architecture critique" claim 3 above for the
   full design and how it was verified). The one thing this does
   *not* do, and was never going to: reject or roll back the merge
   itself -- that's fundamentally incompatible with CRDT convergence,
   which is exactly why the DAG-based B-Rep item below remains open.
6. **DAG-based B-Rep face representation** -- a structurally different
   replacement for the RGA-based face-boundary loop, suggested as a way
   to make non-commutative topology edits (extrude, boundary split)
   merge more predictably. Not attempted here: too large a data-model
   change to make safely without a much larger dedicated verification
   pass.
7. ~~**Real ML mesh generation (TripoSR/Hunyuan3D/Meshy)**~~ -- **Built,
   but not verified** (Phase 9). `crdt_cad.ai.meshy_adapter` calls
   Meshy's hosted text-to-3D API when `MESHY_API_KEY` is set, parses the
   result with `trimesh`, and injects it through the same
   `generate_mesh_ops`/`commit_ops_batched` path the procedural pipeline
   already uses. No Meshy API key was available in this environment, so
   the actual live-API request/response handling is implemented against
   my best understanding of Meshy's documented API, **not confirmed
   against a real call** -- exactly the caveat the brief asks for in
   this situation. What *is* verified: the key-unset path (unchanged,
   procedural), every failure mode gracefully falling back to
   procedural instead of raising (mocked HTTP layer), and mesh-format
   parsing against a real GLB file built by `trimesh` itself. See
   `crdt_cad.ai.meshy_adapter`'s module docstring for the full honest
   accounting of what's real vs. assumed.

## Project layout

```
src/crdt_cad/
  crdt/
    clock.py, lww.py, rga.py, mesh.py, document.py, serialize.py
  geometry/
    constraints.py   Sketch / Constraint / numba-jitted Gauss-Newton solver
    validity.py      zero-length / self-intersection checks, the pre-commit gate
    mesh_validity.py trimesh-based post-merge mesh checks (the "Validation Fork")
  export/
    svg_io.py, dxf_io.py, stl_export.py, step_export.py (optional build123d extra)
  ai/
    house_spec.py       HouseSpec (bounded pydantic model the pipeline builds from)
    interpreter.py      Claude Fable 5 prompt interpretation + regex heuristic fallback
    procedural_house.py deterministic, watertight-by-construction mesh builder
    generator.py         prompt -> spec -> mesh -> batched CRDT MeshOps (ai_generator_bot)
    mesh_repair.py       optional pymeshlab print-prep (non-manifold repair, Poisson)
    meshy_adapter.py     optional hosted text-to-3D API tier (MESHY_API_KEY; mock-tested)
  persistence/
    store.py         DocumentStore interface, SQLiteStore, PostgresStore, InMemoryStore
  server/
    app.py            FastAPI WebSocket relay + REST (export/import/solve/AI-generate)
    security.py        opt-in room tokens, CORS lockdown, rate limiting, resource ceilings
    pubsub.py          optional Redis fan-out for multi-process rooms (CRDT_CAD_REDIS_URL)
    metrics.py         prometheus_client metric definitions
tests/                 pytest + Hypothesis, one file per module, conftest.py isolates storage
tests/e2e/             committed Playwright browser suite (opt-in via `-m e2e`)
scripts/
  load_test.py         N rooms x M WS clients load/soak driver (see docs/deployment.md)
  backup_sqlite.py     online-backup-API SQLite backups (safe against a live writer)
  k8s_smoke_test.py    post-deploy WebSocket round-trip check (used by CI's kind job)
demo/static/
  common.js            relay client, P2PManager, actor identity, shared UI helpers
  home.html/home.js         workspace home page (room list, rename, history/restore)
  index.html/sketch.js      2D sketch demo
  mesh3d.html/mesh3d.js     3D mesh demo (Three.js via CDN, incl. AI Generate panel)
  tokens.css/styles.css/icons.svg  design tokens, themed styles, icon sprite (Part 3)
docs/
  deployment.md        VPS/Caddy runbook, Fly.io, backups, monitoring, load-test findings
  configuration.md     every CRDT_CAD_* env var, exhaustively
  design-system.md     Part 3 design-token rationale
Dockerfile, docker-compose.yml            local dev stack
docker-compose.prod.yml, Caddyfile        production TLS stack
docker-compose.monitoring.yml, monitoring/  optional Prometheus + Grafana
fly.toml               Fly.io config (provided, not live-deployed)
k8s/                   kind-validated manifests + README.md (base/ + dev/ overlay)
.github/workflows/     ci.yml (pytest/ruff/e2e/Docker/kind smoke), release.yml (GHCR on v* tags)
```

## License

[MIT](LICENSE)
