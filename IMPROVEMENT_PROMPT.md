# Improvement brief: take crdt-cad from verified prototype to production-grade

This is **Part 1 of a five-part plan**: it hardens the platform — security,
CI, durability, scaling, correctness (phases 1–9 below). **Part 2 lives in
`FEATURE_IMPROVEMENT_PROMPT.md`** (phases 10–17): the product features that
make the tool genuinely usable by engineers, designers, and casual users.
**Part 3 lives in `UI_UX_PROMPT.md`** (phases D1–D8): a world-class design
system and interaction feel. **Part 4 lives in `DEPLOYMENT_PROMPT.md`**
(phases 18–19): live Kubernetes validation of the Phase 7 scaling work,
plus the production deployment/ops story (TLS, backups, monitoring,
published image). **Part 5 lives in `AI_GENERATION_PROMPT.md`** (phases
G1–G7): the AI text-to-3D pipeline taken from one honest archetype to a
world-class standard — generator registry, scene composition, sandboxed
geometry DSL, iterative editing, and a measured eval harness.

**Status (2026-07-09): Parts 1–3 are fully implemented and committed**
(see git log — phases 1–17 and D1–D8). Part 4 is nearly done: Phase 18 is
committed; Phase 19's work (19.1–19.6) exists in the working tree but is
not yet fully verified/committed. Part 5 is not started. An agent picking
this up should audit `git log` and `git status` first and work only on
what remains, keeping the phase-by-phase commit discipline.

## Context

You are working on `crdt-cad`, a real-time collaborative CAD engine in this
repository. Read `README.md` first — it is the authoritative, honest record of
what is built, what is missing, and why certain things were deliberately not
built. Do not contradict its scoping decisions without strong reason.

Architecture in one paragraph: pure-Python CRDTs (`src/crdt_cad/crdt/` —
LWW-Register/Map/Set, RGA, vector clocks, Lamport OpIds) form the merge layer;
a FastAPI/asyncio WebSocket relay (`src/crdt_cad/server/app.py`) is the single
authoritative merge point with a geometry validity pre-commit gate; SQLite
holds MessagePack snapshots (`src/crdt_cad/persistence/`); two vanilla-JS
demos (`demo/static/` — 2D sketch, 3D mesh with Three.js via CDN, no build
step) are deliberately thin renderers that mint OpIds locally but never
implement merge logic; WebRTC P2P is a latency layer on top of the relay; an
AI text-to-3D pipeline (`src/crdt_cad/ai/`) uses Claude for prompt→spec
interpretation (with a fully-tested regex fallback that runs when no
`ANTHROPIC_API_KEY` is set) and deterministic procedural geometry for mesh
construction.

## Non-negotiable working rules

1. **Never ship an unverifiable feature.** This project's entire identity is
   that everything claimed is tested and demonstrated. If you cannot verify
   something in this environment, build the verifiable part, document the gap
   in README.md, and stop.
2. **The browser client must never implement CRDT conflict resolution.** It
   mints OpIds and renders server-confirmed state. Do not add a JS CRDT
   engine.
3. **Every feature gets: unit tests (pytest, matching the existing style in
   `tests/`), a README.md update, and — for anything user-facing — live
   browser verification.** Run the full suite (`./.venv/Scripts/python -m
   pytest tests/ -v`) after each phase; all existing tests must stay green.
4. **Keep the stack pip-only, zero-build-step frontend, CPU-only.** No conda,
   no npm project, no GPU model weights.
5. **Do not add a `Co-Authored-By: Claude` trailer to commits in this repo.**
6. Commit at the end of each phase with a clear message, so each phase is
   independently revertable. Before starting, commit the currently
   uncommitted working-tree changes (face color/material editor, LocalClock
   observe fix, no-cache static files) as their own commit first.

## The work, in priority order

### Phase 1 — Security hardening (biggest gap; README doesn't even mention it)

Currently: no auth, any client who guesses a `?room=` name has full write
access, `allow_origins=["*"]`, no rate limits, no payload caps, and
`POST /api/mesh/{room_id}/generate` is an unauthenticated LLM-spend/CPU-burn
endpoint.

- Add optional shared-secret room tokens: a signed token (`itsdangerous` or
  PyJWT) validated in the WebSocket `hello` handshake and on all REST
  endpoints that touch a room. Must be **opt-in via env var**
  (e.g. `CRDT_CAD_SECRET`) so the zero-config local demo experience is
  unchanged when unset. The Share button should embed the token in the invite
  link when auth is on.
- Lock CORS to a configurable origin list (env var, default `*` only when no
  secret is configured; when a secret is set, default to same-origin).
- Rate limiting with `slowapi` (or a small hand-rolled token bucket if the
  dependency fights the WebSocket path): per-connection ops/second cap on the
  WS relay, and a strict per-IP limit on `/generate`.
- Server-side resource ceilings, each an env-tunable constant: max WS frame
  size, max ops per message, max rooms per server, max clients per room, max
  total ops applied per room per minute. Exceeding a cap returns the existing
  `{"type": "rejected", ...}` shape or closes the connection with a clear
  code — never silent drops.
- Tests: token accept/reject paths, rate-limit trips, oversized-frame
  rejection, and that the no-secret default behaves exactly as today.

### Phase 2 — CI + committed end-to-end tests

Currently: 162 tests but no CI, and the extensive Playwright verification
described in README's Testing section was ad-hoc, never committed.

- GitHub Actions workflow: on push/PR run `pytest`, `ruff check`, and a
  Docker image build. Keep it under ~5 minutes.
- Create `tests/e2e/` using `pytest-playwright` against a live uvicorn spun up
  by a fixture. Port the scenarios README describes as manually verified:
  two tabs drawing concurrently and converging; offline → edit both sides →
  reconnect → Time-Travel Merge panel appears → merge converges; strict
  Polygon rejection round-trip; the two-actor LWW tie-break regression (fresh
  client joining a room pre-populated with high-counter ops — the
  `LocalClock.observe()` bug). Mark them so they can be skipped where
  browsers aren't installed (`-m e2e`), and run them in CI with
  `playwright install chromium`.

### Phase 3 — Offline outbox durability (README Roadmap item 4, narrowed)

Persist the client-side offline `outbox` (in `demo/static/common.js`) to
IndexedDB, keyed by room + actor, so a hard refresh or closed tab while
offline no longer loses queued ops. On reconnect, flush persisted ops through
the existing Time-Travel Merge path. This must NOT add a JS CRDT — it is
purely queue durability. Verify with a real browser: go offline, draw, hard
refresh, reconnect, confirm the edits arrive and merge. Update the README
sections ("Responses to the architecture critique" claim 1, and Roadmap) that
call this out as the known gap.

### Phase 4 — Undo/redo for the 3D mesh demo

`DrawingDocument` (`src/crdt_cad/crdt/document.py`) already implements
undo/redo as inverted ops (never snapshots — see
`test_undo_does_not_clobber_concurrent_remote_edit`). Port that exact pattern
to `MeshCRDT`: vertex add/move/delete, face create/delete, extrude (undo =
delete the created geometry), and face_prop writes (undo = write back prior
value with a fresh OpId). Wire Undo/Redo buttons + Ctrl+Z/Ctrl+Y into
`mesh3d.js` the same way `sketch.js` does. Test the concurrent-safety
property: undoing my extrude must not roll back your simultaneous vertex
move.

### Phase 5 — Replace the 30s full-snapshot rebroadcast

`app.py` currently rebroadcasts the entire document to every client every
30s — O(doc size × clients) even when nothing changed. Replace with a
frontier check: every 30s broadcast only the room's current `VectorClock`
(tiny). Each client compares against its own known frontier and, only on
mismatch, requests a delta via the existing `ops_since` path. Keep a full
snapshot as the response of last resort. Add a server test that a quiescent
room generates no snapshot traffic, and that a client with a stale frontier
self-heals.

### Phase 6 — Cross-component mesh validity ("Validation Fork", Roadmap item 5)

The known unresolved limitation: concurrent non-commutative mesh edits (face
boundary edit racing an extrude) can merge into semantically inconsistent
geometry. Build the 3D analogue of `geometry/validity.py`:

- Add `trimesh` (pip-installable) and implement a post-merge validity pass
  over a room's `MeshCRDT`: watertightness, winding consistency, degenerate
  faces, non-manifold edges.
- Since a CRDT merge cannot be "rejected" without breaking convergence,
  surface problems instead of blocking them: after applying a delta that
  touched faces, run the check; on failure, broadcast a new
  `{"type": "validity_warning", "faces": [...], "problems": [...]}` message
  and have `mesh3d.js` highlight the offending faces (e.g. red outline) with
  a dismissible banner. Document in README why this is a warning, not a gate.
- Read README's "Responses to the architecture critique" section (claim 3)
  first and update it to reflect what is now built vs. still open (the
  DAG-based B-Rep remains out of scope).

### Phase 7 — Horizontal scaling seam (Roadmap item 2)

- Implement `PostgresStore` (`asyncpg`, JSONB or BYTEA snapshot column)
  behind the existing three-method `DocumentStore` interface, selected by
  `CRDT_CAD_DATABASE_URL`. Unit-test it with a mocked/skipped-if-unavailable
  pattern so CI doesn't need Postgres.
- Add optional Redis pub/sub fan-out for `Room.broadcast()` (env
  `CRDT_CAD_REDIS_URL`): ops applied on one process publish to
  `room:{kind}:{id}`; other processes subscribe and relay to their local
  clients. When unset, behavior is exactly today's single-process path.
- Update `k8s/README.md` and the manifests: with both env vars set, replicas
  > 1 becomes legitimate; document what was and wasn't live-verified.

### Phase 8 — Curve support (unblocks real SVG import)

The document model is polyline-only; SVG import silently drops
`C/S/Q/T/A` commands. Add a per-path `curve` property (or per-segment flag)
supporting quadratic/cubic Beziers, render them in `sketch.js` via
`quadraticCurveTo`/`bezierCurveTo`, extend `export/svg_io.py` to parse curve
commands (use `svgpathtools` or hand-parse — the importer already handles
M/L), and export them back out. DXF export may flatten curves to polylines —
say so in the README. Keep the validity gate polyline-only for now and
document that.

### Phase 9 — Stretch (do only if all above are done and verified)

- **Interactive constraint UI** (Roadmap item 3): the solver and
  `POST /api/solve` already exist and are tested. Add to the 2D demo: select
  two path endpoints → apply coincident/parallel/perpendicular/fixed-distance
  → server solves → points move as CRDT ops for everyone.
- **STEP export**: re-evaluate `build123d`/`cadquery-ocp` pip wheels (the
  README's "pythonOCC is conda-only" blocker is dated). Only attempt if a
  wheel actually installs in this environment; a faceted-B-Rep STEP writer
  fed from `MeshCRDT` triangles is acceptable scope. Otherwise leave the
  README note as-is.
- **Hosted ML mesh-gen adapter**: an optional `MESHY_API_KEY`-gated adapter
  in `ai/` that calls a hosted text-to-3D API and injects the result through
  the existing `commit_ops_batched` path. Must degrade to the current
  procedural pipeline when unset, and must not be claimed as verified unless
  actually exercised against the live API.

## Definition of done

- All phases 1–8 implemented, tested, browser-verified, committed
  phase-by-phase.
- Full pytest suite green; e2e suite green locally.
- README.md updated per phase: the "Status at a glance" table, the critique
  responses, and the Roadmap must accurately reflect the new reality — items
  that are now done move out of "not built", with the same plain-spoken
  honesty about anything that remains.
