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
protocol work, and how to run the two live demos (2D sketch, 3D mesh).
It also says plainly what's still missing and why, rather than papering
over gaps.

<p align="center">
  <img src="docs/screenshots/2d_sketch_demo.png" width="32%" alt="2D sketch demo: three freehand strokes, layers, and the path selection panel">
  <img src="docs/screenshots/3d_mesh_ai_generated_house.png" width="32%" alt="3D mesh demo: a 4-bedroom house built by the AI text-to-3D generator, wood floor material rendered from a CRDT face_prop">
  <img src="docs/screenshots/time_travel_merge.png" width="32%" alt="Time-Travel Merge panel showing two branches diffed before an automatic conflict-free merge">
</p>

## Status at a glance

| Area | Status |
|---|---|
| CRDT core (vector clocks, Lamport `OpId`, LWW-Register/Map/Set, RGA) | **Done**, Hypothesis-fuzzed for convergence |
| Tombstone value-compaction (bounded RGA memory growth) | **Done** -- see "Responses to the architecture critique" below |
| Mesh CRDT (vertices/edges/face boundaries/per-face properties) + presence | **Done**, composed from the primitives above |
| Mesh undo/redo (incl. bundled extrude, Ctrl+Z/Ctrl+Y in the 3D demo) | **Done** -- inverted ops, not snapshots, same pattern as 2D |
| AI text-to-3D generation (`src/crdt_cad/ai/`) | **Done** -- Claude Fable 5 prompt interpretation (heuristic fallback, no API key required) + deterministic procedural geometry + optional `pymeshlab` print-repair, injected as batched CRDT ops -- see below for exact scope |
| `DrawingDocument` (layers, paths, props, comments, presence, undo/redo) | **Done** |
| Geometry kernel: constraint solver (coincident/tangent/perpendicular/parallel/fixed-distance), numpy+numba | **Done**, own test suite incl. an independent Pythagorean-triple correctness check |
| Geometry validity gate (reject zero-length / self-intersecting) | **Done**, server-side pre-commit gate; demoed live via the strict Polygon tool |
| WebSocket relay server (rooms, snapshots, delta resync) | **Done**, FastAPI/asyncio |
| Bounded periodic self-heal traffic (frontier ping, not a full snapshot) | **Done** -- O(actor count), not O(document size), see below |
| Security hardening (opt-in room tokens, CORS lockdown, rate limits, resource ceilings) | **Done** -- off/wide-open by default, see below |
| Durable persistence (SQLite snapshots, survives restart) | **Done** |
| Save / download (JSON, SVG, DXF for 2D; JSON, STL for 3D) | **Done** |
| Import (SVG, DXF reference geometry) | **Done** (straight-segment subset -- see below) |
| WebRTC P2P direct sync, WS-relayed signaling, relay fallback | **Done**, verified with a real two-tab RTCPeerConnection/DataChannel |
| "Time-Travel Merge" branch-preview UI | **Done** |
| Offline outbox durability (survives a hard refresh/closed tab) | **Done** -- IndexedDB, no JS CRDT engine added, see below |
| Prometheus metrics (`prometheus_client`) | **Done** |
| CI (GitHub Actions: pytest/ruff, e2e, Docker build) | **Done** -- `.github/workflows/ci.yml` |
| Committed browser e2e suite (`tests/e2e/`, Playwright) | **Done**, opt-in via `-m e2e`, 4 tests |
| Docker image + Compose stack | **Done**, built and run-verified, persistence-across-restart verified |
| Kubernetes manifests | Written, **not validated against a live cluster** (none was available) -- see `k8s/README.md` for the important caveat on replica count |
| STEP/IGES import/export (`pythonOCC`) | **Not built** -- no usable PyPI wheel (conda-only in practice); see below |
| True horizontal scaling of room state (multi-pod) | **Not built** -- room state is in-process per pod today; needs a shared broker (Redis/Kafka) -- see `k8s/README.md` |
| Pyodide/WASM client-side engine | **Not built** -- deliberate; see rationale below |

## Quickstart

### Local (Python venv)

```bash
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"      # Windows; use .venv/bin/pip on macOS/Linux

./.venv/Scripts/python -m pytest tests/ -v   # 203 tests, ~8s

./.venv/Scripts/python -m uvicorn crdt_cad.server.app:app --reload
```

### Docker

```bash
docker compose up --build
```

Either way, open two browser tabs:

- `http://127.0.0.1:8000/` -- 2D sketch demo
- `http://127.0.0.1:8000/3d` -- 3D mesh demo

Both tabs on the same `?room=` name see each other's edits live (over a
direct WebRTC data channel when negotiation succeeds, and always over
the WebSocket relay too). Click **"Go offline"** in one tab, keep
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
constraints, get back solved positions), ready for a future interactive
"select two entities, apply a constraint" sketch tool; that UI itself
isn't built yet (see Roadmap).

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

`DocumentStore` is a three-method interface (`save`/`load`/`list_rooms`)
implemented today by `SQLiteStore` -- one row per `(room kind, room id)`
holding the latest MessagePack snapshot. Rooms hydrate from their last
snapshot when a server (re)starts, and every accepted ops batch triggers
an (awaited, not fire-and-forget -- see the note in `Room.persist_async`
about why that distinction mattered) persist. A client can also force
one via the **Save** button (`{"type": "save"}` -> `{"type": "saved", ...}`).

The brief asks for "PostgreSQL (JSONB) or an append-only event log";
SQLite was chosen for zero required infrastructure to run this locally.
Swapping in Postgres is implementing the same `DocumentStore` interface
against `asyncpg`/JSONB -- nothing above that layer changes.

## Import / export (`src/crdt_cad/export/`)

- **SVG**: export always; import supports `<line>`, `<polyline>`,
  `<polygon>`, and `<path>` using only `M`/`L` commands (absolute or
  relative) -- the common "reference geometry" case. Curves
  (`C`/`S`/`Q`/`T`/`A`) aren't parsed; today's document model only has
  polylines, not Bezier/arc primitives, to hold them.
- **DXF**: export/import via `ezdxf` (`LWPOLYLINE`/`LINE`/`POLYLINE`).
- **STL**: ASCII export for the 3D mesh, fan-triangulated per face (same
  technique the Three.js renderer uses client-side -- correct for the
  convex/simple polygons the Face tool and Extrude produce).
- **STEP/IGES: not built.** Both need a real B-Rep kernel
  (`pythonOCC`), which has no usable PyPI wheel in practice (it's a
  conda-forge package, and pulling in a conda environment just for this
  felt like the wrong tradeoff for a pip-based project) -- and
  `MeshCRDT` has no B-Rep representation yet for a STEP writer to
  target regardless. The export plugin structure (`crdt_cad.export`) is
  set up so adding one later is additive.

All of this is reachable from the UI: **Save** (explicit durable
snapshot), **.json/.svg/.dxf/.stl** download buttons, and an **Import
SVG/DXF** file picker that broadcasts the imported paths to everyone
currently in the room.

## AI text-to-3D generation (`src/crdt_cad/ai/`)

Type a prompt like *"create a 4 bedroom house with a wooden floor"*
into the **AI Generate** box on the `/3d` demo, and a real, watertight,
collaboratively-editable house mesh appears in the scene for everyone
in the room -- built and synced through the exact same CRDT machinery
as a hand-placed vertex.

**Scope, stated honestly up front:** this does *not* wrap TripoSR,
Hunyuan3D, Meshy, or any other multi-GB GPU mesh-diffusion model. This
project runs in a sandboxed, CPU-only environment with no way to
responsibly download, run, or verify a several-gigabyte GPU checkpoint
-- claiming that integration while unable to test it would be exactly
the kind of unverifiable, faked feature this README explicitly tries
not to ship. What's built instead is a genuinely working split of the
same problem into its two real sub-problems:

1. **Language understanding** -- turning "a 4 bedroom house with a
   wooden floor" into bounded structured parameters (`bedrooms=4`,
   `floor_material="wood"`, ...) is squarely an LLM-shaped problem, and
   is where **Claude Fable 5** is actually used
   (`crdt_cad/ai/interpreter.py`, `_llm_interpret`): `output_config.format`
   constrains the response to a JSON schema, the model card's
   server-side refusal-fallback (`betas=["server-side-fallback-2026-06-01"]`
   + `fallbacks=[{"model": "claude-opus-4-8"}]`) is enabled by default,
   and `interpret_prompt()` falls back to a pure-regex heuristic parser
   (`_heuristic_interpret`) on *any* failure -- missing credentials, a
   network error, a safety refusal, a malformed response. That fallback
   isn't a rare degraded corner case: it's what actually runs in any
   environment without `ANTHROPIC_API_KEY` configured (including this
   one), so the feature is fully functional and fully tested with zero
   external credentials.
2. **3D construction** -- this is *not* asked of the model.
   `procedural_house.py` deterministically builds an actual mesh
   (floor slab, roof slab, exterior walls, interior partitions sized to
   the bedroom count) from the interpreted spec: every vertex, edge,
   and face loop is computed geometry, not a hallucinated guess, and is
   watertight and axis-aligned-planar by construction (see
   `tests/test_procedural_house.py`, which checks exactly that -- no
   duplicate vertices, no zero-length edges, every face genuinely
   planar).

`generator.py` orchestrates prompt -> spec -> mesh -> a **chronological
batch of CRDT ops**, minted under a dedicated `ai_generator_bot` actor
identity via its own `LamportClock`, built against a throwaway
`MeshCRDT` scratch instance so every `OpId` is correctly ordered before
any of it touches a live room:

- every vertex becomes a `vertices` (`LWWMap`) write,
- every face becomes a `face_index` add plus an ordered `faces` (`RGA`)
  insert per boundary vertex,
- the floor face(s) get `face_prop` writes for `material` and a
  matching `color`, which `mesh3d.js` reads to render the AI-tagged
  floor in its actual material color instead of the default per-face
  hash palette.

**The batching answer, concretely:** `POST /api/mesh/{room_id}/generate`
(`app.py`) runs `generate_mesh_ops` in a worker thread via
`asyncio.to_thread` (under an `asyncio.wait_for` timeout, so a hung LLM
call can't tie up a thread-pool slot forever), then hands the resulting
op list to `Room.commit_ops_batched()` -- which applies+broadcasts them
in fixed-size chunks (150 ops by default,
`CRDT_CAD_GENERATION_BATCH_SIZE`), `await`ing `asyncio.sleep(0)` between
each chunk so one large generation never monopolizes the event loop or
arrives at clients as a single giant WebSocket frame; clients instead
watch the house build itself in visible stages. A malformed-geometry or
empty result returns `422`; a timeout returns `504`; both are covered
in `tests/test_generation_endpoint.py`.

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

## The two demos

Both are plain HTML/CSS/vanilla-JS (`demo/static/`) -- no build step, no
npm project. The 3D demo additionally loads Three.js + OrbitControls
from a CDN via an import map (the only external runtime dependency
either frontend has).

**2D sketch (`/`)**: pen tool, select tool, a strict **Polygon** tool
that demonstrates the geometry validity gate, per-layer visibility,
undo/redo, live multi-user cursors, comments, **Save**/**.json/.svg/.dxf
download**/**Import SVG or DXF**/**Share** (copies an invite link), and
an offline toggle that closes both the WebSocket and any P2P
connection.

**3D mesh (`/3d`)**: click the ground grid to place vertices, click 3+
vertices in order (then the first one again, or "Finish") to build a
face, drag a vertex to move it (or type exact X/Y/Z into the vertex
list), select a face to **recolor it, tag its material, extrude it
into a prism, or delete it**, **Undo/Redo** buttons and Ctrl+Z/Ctrl+Y
(or Ctrl+Shift+Z) for every one of those actions -- extrude included,
as one bundled undo step -- the same **Save**/download(**.json/.stl**)/**Share**/
offline toggle set, plus an **AI Generate** box -- describe a house in
plain English and a real procedurally-built mesh streams into the scene
as CRDT ops, exactly like any other collaborator's edit. Every one of
these -- including a face's color and material -- is a `face_prop`
`LWWMap` write, so recoloring a face you didn't create merges the same
conflict-free way a vertex move does.

## Testing

```bash
./.venv/Scripts/python -m pytest tests/ -v
```

203 tests: unit tests per CRDT type and geometry module, serialization
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
back a collaborator's simultaneous, unrelated vertex move.

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
undo); and the Docker image built, run, and checked for
persistence-across-container-restart. Real bugs were caught this way
that no unit test had covered (a fire-and-forget background task that
hung the process at exit, "offline" not tearing down an already-open
P2P channel, and a URL-token re-prompt loop where a proven-bad token
never actually left the address bar) -- all fixed and specifically
regression-tested.

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

### Committed e2e suite + CI

All of the ad-hoc Playwright verification above was, for most of this
project's life, exactly that -- ad-hoc, run by hand, never committed.
`tests/e2e/` (6 tests, opt-in via `pytest -m e2e`, excluded from a plain
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
and the offline-outbox-survives-a-hard-refresh behavior
(`test_offline_durability_e2e.py`) -- go offline, draw, `page.reload()`,
confirm the edit is both visible locally and actually landed
server-side, plus a check that a room nobody ever went offline in shows
no recovery toast (persistence is additive, not a new default).
Each spins up a real `uvicorn` subprocess on a free port with its own
temp SQLite file (`tests/e2e/conftest.py`), so they exercise the actual
client JS against the actual relay -- not an in-process
`fastapi.testclient` double.

`.github/workflows/ci.yml` runs on every push/PR: `pytest` + `ruff
check` (fast job), the e2e suite (`playwright install chromium` then
`pytest -m e2e`), and a plain `docker build` -- three independent jobs,
the e2e one depending on the fast job passing first.

## Deployment

**Docker**: `docker compose up --build` -- built and run-verified,
including a full write -> restart -> data-still-there check against the
named volume.

**Kubernetes**: manifests exist under `k8s/` (`kubectl kustomize k8s/`
builds cleanly) but were **not applied to a live cluster** -- none was
reachable in the environment they were written in. Read `k8s/README.md`
before touching `replicas` or `hpa.yaml`: room state and the SQLite file
both currently live on one pod, so scaling past 1 replica today would
silently split users across pods that can't see each other, not
actually scale anything.

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
Acknowledged plainly: this is real and **not fixed**. `MeshCRDT`
merges each component (vertices/edges/face index/per-face RGA)
independently and correctly on its own terms, but nothing today
validates the *cross-component* semantic result -- a manifoldness or
winding check that would catch "this extrude and this boundary edit
together produced something inconsistent" the way `validity.py` already
does for 2D self-intersection. The suggested "Validation Fork" (run the
constraint/geometry solver as an independent pass over the merged
result, rejecting the merge -- not the individual ops -- if it fails)
is a reasonable direction and is **not implemented**; it would need a
manifoldness/winding checker for 3D face loops that doesn't exist yet
(2D's `validity.py` has no 3D equivalent), and a protocol for what a
"rejected merge" even means for a CRDT that's supposed to always
converge. Same honest status for the suggested DAG-based B-Rep
replacement for the RGA-based face-boundary representation: a bigger,
structurally different data model, not a change made lightly this late
without a much larger test/verification pass than fits this pass. Both
are called out explicitly in Roadmap below rather than silently
left out.

## Roadmap / what's not built yet

1. **STEP/IGES export** -- blocked on a B-Rep representation in
   `MeshCRDT` and a usable `pythonOCC` install path; see the Import/export
   section above.
2. **Shared room-state broker for true horizontal scaling** -- Redis
   pub/sub or the Kafka event log the brief mentions, so `Room.broadcast()`
   reaches clients connected to *other* pods. Today's persistence layer
   (SQLite snapshots) is fine as-is or swappable to Postgres
   independently of this.
3. **Interactive constraint-assignment sketch UI** -- the solver and its
   `/api/solve` endpoint are real and tested; a UI for "select two
   existing path endpoints, click 'make parallel'" on top of the
   freehand/polygon tools isn't built yet.
4. **Pyodide/WASM client-side engine** -- the brief allows this as an
   enhancement. Deliberately not done: it would mean running the same
   package twice, and a thin JS renderer that reuses the *tested* server
   logic as the single source of truth was the more honest choice than
   an integration that's hard to verify reliably in this environment.
   A narrower version of this concern -- the in-memory offline outbox
   not surviving a hard refresh -- **is now fixed** (persisted to
   IndexedDB, see "Responses to the architecture critique" claim 1 above)
   without duplicating the engine.
5. **Cross-component mesh validity ("Validation Fork")** -- a
   manifoldness/winding check over the *merged* result of concurrent
   mesh edits (e.g. a face-boundary edit racing an extrude of that
   face), the 3D analogue of `validity.py`'s 2D self-intersection gate.
   Not built; see "Responses to the architecture critique" above for
   the concrete failure mode this leaves open.
6. **DAG-based B-Rep face representation** -- a structurally different
   replacement for the RGA-based face-boundary loop, suggested as a way
   to make non-commutative topology edits (extrude, boundary split)
   merge more predictably. Not attempted here: too large a data-model
   change to make safely without a much larger dedicated verification
   pass.
7. **Real ML mesh generation (TripoSR/Hunyuan3D/Meshy)** -- deliberately
   out of scope for this environment; see the AI text-to-3D generation
   section above for exactly what's built instead (Claude Fable 5 +
   deterministic procedural geometry) and why.

## Project layout

```
src/crdt_cad/
  crdt/
    clock.py, lww.py, rga.py, mesh.py, document.py, serialize.py
  geometry/
    constraints.py   Sketch / Constraint / numba-jitted Gauss-Newton solver
    validity.py      zero-length / self-intersection checks, the pre-commit gate
  export/
    svg_io.py, dxf_io.py, stl_export.py
  ai/
    house_spec.py       HouseSpec (bounded pydantic model the pipeline builds from)
    interpreter.py      Claude Fable 5 prompt interpretation + regex heuristic fallback
    procedural_house.py deterministic, watertight-by-construction mesh builder
    generator.py         prompt -> spec -> mesh -> batched CRDT MeshOps (ai_generator_bot)
    mesh_repair.py       optional pymeshlab print-prep (non-manifold repair, Poisson)
  persistence/
    store.py         DocumentStore interface, SQLiteStore, InMemoryStore (tests)
  server/
    app.py            FastAPI WebSocket relay + REST (export/import/solve/AI-generate)
    security.py        opt-in room tokens, CORS lockdown, rate limiting, resource ceilings
    metrics.py         prometheus_client metric definitions
tests/                 pytest + Hypothesis, one file per module, conftest.py isolates storage
demo/static/
  common.js            relay client, P2PManager, actor identity, shared UI helpers
  index.html/sketch.js      2D sketch demo
  mesh3d.html/mesh3d.js     3D mesh demo (Three.js via CDN, incl. AI Generate panel)
  styles.css                shared dark theme
Dockerfile, docker-compose.yml
k8s/                   manifests + README.md explaining the replica-count caveat
```

## License

[MIT](LICENSE)
