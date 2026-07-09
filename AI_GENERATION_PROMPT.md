# AI generation brief: take text-to-3D from one honest archetype to a world-class standard

## Context

This is **Part 5 of the improvement plan** for `crdt-cad`. Read `README.md`
(especially the "AI text-to-3D generation" section) and the existing
pipeline first: `src/crdt_cad/ai/` — `interpreter.py` (Claude Fable 5
prompt→spec with JSON-schema-constrained output and a fully-tested regex
fallback that runs with no API key), `house_spec.py` (the *entire* current
vocabulary: one archetype, five fields), `procedural_house.py`
(deterministic watertight construction), `generator.py` (spec → batched
CRDT ops under the `ai_generator_bot` actor), `mesh_repair.py`,
`meshy_adapter.py` (Phase 9 stretch stub, `MESHY_API_KEY`-gated), plus
`Room.commit_ops_batched` in `server/app.py` and the staged-build art
direction from Phase D7.

Every working rule from Part 1 (`IMPROVEMENT_PROMPT.md`) applies. Three
AI-specific rules are load-bearing and non-negotiable:

1. **The LLM never emits raw geometry.** It emits *specifications* or
   *programs*; deterministic code computes every vertex. Anything the LLM
   influences passes validation (watertight/manifold/bounds via the
   existing `trimesh` machinery) before touching a room, and a validation
   failure is a visible, typed error — never a silently-injected broken
   mesh.
2. **The no-API-key path stays first-class.** Every feature below must
   work (possibly with reduced vocabulary) via the heuristic fallback and
   be fully tested that way, because that is what CI and most local
   installs actually run. LLM-path tests use mocked responses; live-LLM
   verification happens only if `ANTHROPIC_API_KEY` is actually present.
3. **No GPU model weights.** The hosted-API tier (G7) is the only route to
   diffusion meshes, and it stays opt-in. (Use the `claude-fable-5` model
   id, the `anthropic` SDK's structured-output support, and prompt caching
   for the system/schema prompt — check docs for current parameter names
   rather than assuming.)

## What "world class" means here — the acceptance bar

A stranger types prompts for five minutes and every one of these holds:
common objects beyond houses generate correctly; multi-object prompts
produce sensibly laid-out scenes; an unrecognized shape either generates
via the DSL path or fails with a clear message (never garbage); a
follow-up prompt edits the previous result; every generation shows a
verifiable report card; and the team can point at an eval score trending
upward in CI rather than at anecdotes. **"Shows success" means measured
success: evals in CI, validation reports in the UI, metrics in Grafana.**

## Phase G1 — Generator library: from one archetype to a vocabulary

- Refactor toward a registry: each generator = (pydantic spec with bounded
  fields, deterministic builder returning watertight geometry, tests for
  its invariants — the pattern `procedural_house.py` already sets).
- Add at minimum: **table, chair, shelf/bookcase, stairs, column, arch,
  door + window as wall openings (real CSG cuts via trimesh boolean,
  validated), fence/railing, simple gable/hip/flat roof options for the
  house, box/cylinder/cone/torus parametrics with dimensions**.
- Enrich `HouseSpec`: stories with distinct footprints, roof type, garage,
  window/door placement, per-element materials, overall dimensions with
  units ("a 30 square meter cabin" must be honored).
- Interpretation becomes dispatch: Claude (and the heuristic fallback, for
  a useful keyword subset) picks the generator and fills its spec —
  present the registry to the LLM as a tool/schema catalog, not one giant
  union schema.
- Every generator's output goes through the same invariant tests
  (planarity, no duplicate vertices, watertightness) the house already
  has. Every generator demo'd live in the 3D room before it's claimed.

## Phase G2 — Scene composition

- A `SceneSpec`: list of (generator spec, transform) plus layout intent
  ("around", "on top of", "row of four"). Claude fills it; a
  deterministic layout solver resolves it — ground-plane snapping,
  AABB non-overlap resolution, "on" relationships stack correctly.
  The solver, not the LLM, owns final coordinates.
- Heuristic fallback handles simple counted arrangements ("four chairs
  around a table") for at least the furniture set.
- Each scene object gets its own provenance tag (see G4) so it is
  individually selectable/deletable afterwards.
- Batched injection already exists; a scene builds visibly object by
  object (extending the Phase D7 staging).

## Phase G3 — Open vocabulary via a sandboxed geometry DSL (the step-change)

- Define a tiny, closed DSL (JSON program, not Python): primitives
  (box, prism, cylinder-approx, extruded polygon), transforms
  (translate/rotate/scale), combinators (union, difference — trimesh
  booleans), loops with bounded counts. Hard caps: max nodes, max
  vertices/faces, max execution time, max bounding box. No I/O, no
  recursion, no arbitrary code — the interpreter is a small pure
  function over the JSON tree, unit-tested exhaustively including every
  cap.
- For prompts no registry generator matches, Claude writes a DSL program.
  Execute → validate (watertight, manifold, budgets) → on failure, retry
  up to N times feeding the *specific* validation error back to the model
  → on final failure, fall back to the closest registry archetype or a
  clear typed error to the user. Log every attempt outcome (feeds G5/G6).
- The heuristic fallback does not attempt DSL synthesis — without an API
  key this path degrades to the registry, stated plainly in the UI and
  README.
- Tests: mocked-LLM programs (valid, invalid-then-repaired, budget-
  exceeding, malicious-shaped inputs), plus golden DSL programs checked
  into the eval set.

## Phase G4 — Iterative refinement, provenance, one-unit undo

- **Provenance**: every generation gets an id; its ops are minted under
  `ai_generator_bot` with the generation id recorded (face/vertex prop or
  op metadata — choose what survives merge cleanly and document why).
  UI: "select everything from this generation", show its prompt.
- **Spec persistence**: store each generation's final spec/scene/DSL
  program in room state (an `LWWMap` keyed by generation id).
- **Follow-up edits**: a prompt entered while a generation is selected
  (or "make the roof steeper" referencing the last one) goes to Claude
  as *edit this spec*; the pipeline regenerates and applies the delta as
  ordinary CRDT ops — collaborators see the edit like any other. The
  heuristic fallback covers simple parameter edits ("taller", "wider",
  "5 bedrooms instead").
- **One-unit undo**: undoing an AI generation removes the whole
  generation (inverted ops over its op set), not one vertex at a time —
  extend the Phase 4 mesh-undo machinery with grouped entries.

## Phase G5 — Success visibility (the user-facing half of "shows success")

- **Report card per generation**, broadcast to the room and rendered in
  the AI panel: watertight ✓/✗, manifold ✓/✗, face planarity, vertex/face
  counts vs budget, dimensions, which path produced it (registry /
  DSL / hosted / heuristic-fallback), interpretation summary chips
  ("understood: 4 bedrooms · wood floor · gable roof") shown *before*
  geometry lands, elapsed time. Honest by construction: the card renders
  whatever validation actually returned, including ✗.
- **Metrics**: counters for generations by outcome
  (success/fallback/repair-retries/failure) and path, plus a latency
  histogram — added to `/metrics`, a new Grafana dashboard row, and one
  alert rule (failure ratio abnormal).
- **Cancel + cost guardrails**: a cancel button that actually cancels the
  in-flight task; per-room and per-IP generation budgets already exist —
  surface remaining budget in the UI instead of surprising users with 429s.

## Phase G6 — Eval harness (the engineering half of "shows success")

This is what makes the standard *world class* rather than asserted:

- `evals/` with a golden prompt set (≥60 prompts: every registry
  generator, scenes, dimensioned prompts, ambiguous prompts, adversarial/
  out-of-scope prompts, non-English samples). Each case: expected
  generator/spec assertions + geometry invariants on the output.
- Runs in CI on the heuristic + mocked-LLM paths on every push (fast,
  deterministic). Score reported as a single number + per-category
  breakdown; a regression fails CI.
- A separate, manually-triggered (or API-key-gated scheduled) live-LLM
  eval run measuring: schema-valid response rate, dispatch accuracy,
  DSL first-try validity rate, repair-loop recovery rate, p50/p95
  latency. Results written to a versioned `evals/RESULTS.md` with date
  and model id — honest history, not marketing.
- README gets an "AI quality" subsection quoting the latest scores and
  linking the harness, replacing adjectives with numbers.

## Phase G7 — Hosted ML tier, matured (optional, still honest)

- Rework `meshy_adapter.py` into a real async job flow: submit → poll →
  stream progress messages to the room → import. Timeouts, typed errors,
  and tests with a mocked API throughout.
- **Mesh budget pipeline**: imported diffusion meshes get simplified
  (trimesh decimation) to a configurable face budget *before* CRDT
  injection, with the tradeoff stated in the UI ("simplified 48k → 4k
  faces for collaborative editing"). Refuse (clearly) anything that
  can't fit the budget.
- Same provenance/report-card/eval treatment as every other path. Only
  claim live verification if a real API key was actually exercised;
  otherwise the README says "mock-tested, not live-verified" — same
  honesty rule as Fly.io in Part 4.

## Definition of done

- G1–G6 implemented, tested, browser-verified, committed phase-by-phase
  (G7 optional but its honesty rules bind whoever attempts it).
- Full suite + e2e green; eval harness wired into CI and passing with a
  recorded baseline score.
- README's AI section rewritten to describe the registry/scene/DSL
  architecture, the measured eval results, and — in the same plain voice
  the project already uses — exactly which paths exist without an API
  key and what remains out of scope (GPU diffusion locally).
- `project` memory and the prompt-brief index updated to include Part 5.
