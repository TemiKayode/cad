# Professional CAD brief: modify tools, drawing output, interop, mobile, and scale

## Context

This is **Part 7 of the improvement plan** for `crdt-cad`: the features
that make engineers *stay* after the first ten minutes, plus the reach
work (mobile/touch, PWA, interop, large documents) that "used worldwide"
actually requires. Read `README.md` first; the 2D document model
(`src/crdt_cad/crdt/document.py`), the shape-primitive and transform
patterns from Phases 11–12, the curve support from Phase 8, and the
export layer (`src/crdt_cad/export/`) are the foundations everything
here builds on.

All Part 1 working rules apply (tests + e2e + README per phase, one
commit per phase, no unverifiable claims, no Claude co-author trailer).
Part-7-specific rules:

1. **CRDT semantics first.** Every new edit operation must be specified
   as ops before it's implemented: what happens when two people trim the
   same segment concurrently must have a *designed* answer (usually:
   both ops apply, LWW/RGA semantics decide, result is convergent even
   if surprising — document each case in the phase's tests).
2. **The long-bet phases (C6) ship staged value, not a moonshot.**
   Anything that can't be verified in this environment is a design doc
   plus a flag-gated prototype, stated honestly — never a claimed
   feature.

## Phase C1 — Modify operations (the missing engineer's toolkit)

2D, all CRDT-safe, all undoable, all with e2e coverage:

- **Trim / Extend**: pick a cutting edge, click the segment portion to
  remove/extend to intersection. Implemented as delete+insert of RGA
  path points (the `movePathPoint` idiom).
- **Offset**: parallel copy of a path at typed distance (closed and
  open paths; polyline offset via the existing geometry kernel — new
  `geometry/offset.py` with its own unit tests including the
  self-intersecting-result cases, which must fail gracefully).
- **Fillet / Chamfer**: corner→arc (needs C2's arcs; sequence C2 before
  or land fillet-as-line-chamfer first and upgrade) with typed radius.
- **Mirror**: across a picked axis (2 points), as new paths.
- **Arrays**: rectangular (rows × cols × spacing) and polar (count ×
  center) duplication of a selection.
- Each tool joins the command palette, keyboard map, and the "?"
  overlay (Part 3 conventions).

## Phase C2 — Arcs and splines as first-class citizens

- Arc segments in the document model, following exactly the Phase 8
  curve pattern (a per-anchor `path_prop` payload — index-independent,
  concurrent-edit-safe). Arc tool (3-point and center-start-end).
- SVG import/export of `A` commands; DXF `ARC`/`CIRCLE` entities map to
  real arcs (today they flatten); DXF `SPLINE` import (flatten to the
  existing cubic-Bezier representation, stated precision).
- The validity gate and measurement tools learn arcs (length/area of
  arc-containing paths).

## Phase C3 — Sheets, title blocks, and print output

The actual *deliverable* of drafting work:

- A **sheet** entity per document: paper size (A4–A0, letter), scale,
  a viewport rectangle mapping model space onto the sheet.
- **Title block**: editable fields (project, author — account-aware if
  Part 6 landed, date, scale, revision) rendered from a template; the
  drawing border.
- **PDF export** of a sheet (server-side; pick the lightest solid
  library route and verify the output opens in two real viewers), plus
  print-ready SVG. Dimensions/text render at correct paper scale.
- Multiple sheets per document; the sheet list lives in the document
  CRDT (LWW per-sheet prop bags — concurrent sheet edits merge
  field-wise).
- **High-resolution raster export (4K and custom).** Today's PNG export
  is `canvas.toBlob()` at *screen* resolution (Phase 15) — fine for a
  quick share, not for print or marketing renders. Add a resolution
  picker (1×/2×/4K 3840×2160/custom, capped) to both editors: 2D
  re-renders the scene to an offscreen canvas at the target size (the
  render path is already resolution-independent world-space); 3D
  renders one frame to a sized `WebGLRenderTarget`/offscreen canvas and
  reads it back. Both renderers are already DPR-aware on-screen
  (`sketch.js` scales by `devicePixelRatio`; `mesh3d.js` calls
  `renderer.setPixelRatio`), so this is an export-path change only.
  Verify actual output dimensions and a pixel-sample in e2e; document
  the GPU-memory cap reasoning. While here: extend the D8-style
  viewport audit to 3840×2160 (it currently stops at 1920) and fix
  anything that breaks at 4K window sizes.

## Phase C4 — Interop: meet the world's formats

- **glTF export** (3D) via trimesh — cheap, high-leverage (three.js,
  Blender, game engines). Verified by re-importing with trimesh and in
  the three.js viewer.
- **3MF export** (3D printing's modern format) — trimesh supports it;
  verify in at least one real slicer (PrusaSlicer/Cura, headless check
  acceptable).
- **STEP import** (subset, honestly scoped): `build123d` is already an
  optional extra for export; import brings faceted geometry in
  (tessellate B-Rep → MeshCRDT) with a stated triangle budget and the
  Part 5 mesh-budget simplification path if it exists by then.
- **IFC and DWG: assessment documents, not builds.** Write
  `docs/interop.md` recording the honest evaluation: IFC via
  IfcOpenShell (install feasibility, what subset would mean), DWG via
  ODA licensing realities (likely: document "import DXF instead" as
  the supported answer). No half-built importers.

## Phase C5 — Components: reusable blocks with instances

Groups (Phase 15) made selections; this makes *libraries*:

- A **component definition** (captured from a selection) + **instances**
  that reference the definition with a transform. Edit-definition →
  all instances update. CRDT design: definitions are per-document
  components (RGA/LWW composition like paths); an instance is a small
  prop bag referencing the definition id — concurrent definition-edit
  vs instance-move must merge cleanly (test it).
- Component panel: document-local library, insert/replace, "detach"
  (bake to plain geometry). Cross-document/library sharing is roadmap
  (needs Part 6 orgs for ownership) — say so.

## Phase C6 — The 3D long bet, staged honestly

- **Stage 1 (build now): mesh booleans.** Union/subtract/intersect of
  selected watertight solids via trimesh's boolean backends —
  server-side op (like `/api/solve`), result re-injected as batched
  CRDT ops replacing the inputs, with the Validation Fork checking the
  result. Verify backend availability in this environment first; if
  trimesh's boolean engine (manifold3d etc.) installs from pip, this
  is real; if not, document and stop — don't ship flaky booleans.
- **Stage 2 (design + prototype): B-Rep/parametric direction.** A
  design document (`docs/brep-design.md`) for the DAG-based B-Rep /
  feature-tree representation: data model as CRDT (feature list as
  RGA, parameters as LWW), rebuild semantics, merge behavior for
  concurrent feature edits, migration story. Plus a flag-gated
  prototype: a *linear feature list* (sketch → extrude with editable
  distance) that regenerates the mesh on parameter change — the
  smallest real parametric loop. Clearly labeled experimental; no
  README claim beyond exactly what the prototype does.
- Revolve/loft/sweep: roadmap items behind Stage 2, not attempted.

## Phase C7 — Mobile/touch and PWA

- **Touch editing**: pointer-events unification across both demos;
  pinch-zoom + two-finger pan (2D and 3D orbit); long-press = context
  menu / right-click equivalent; touch-sized hit targets for handles
  and panels (44px minimum); on-screen numeric input working with
  virtual keyboards. Playwright touch emulation e2e for the core
  flows (draw, select, move, orbit, extrude).
- **PWA**: manifest + service worker (app-shell caching so the editor
  *loads* offline; the CRDT outbox already handles offline *edits*),
  install prompt, offline indicator integration with the existing
  status cluster. The service worker must never cache API/WS responses
  stale — cache-first for static, network-only for data; version-bump
  invalidation tested (the no-build-step constraint makes this easy to
  get wrong silently).

## Phase C8 — Large-document scale

- **Benchmark first**: extend `scripts/load_test.py` (or a sibling
  `scripts/doc_scale_test.py`) to build documents at 10k / 100k / 1M
  ops and measure: server memory, snapshot size/time, client load
  time, render frame time. Record honest numbers in
  `docs/deployment.md` like Phase 19.5 did.
- **Budgets**: per-document op/size ceilings (env-tunable, rejected
  with a clear message like the other ceilings) so one runaway
  document can't take down a shared server.
- **Render scalability**: 2D — viewport culling and path
  simplification when zoomed out (client-local, no CRDT change); 3D —
  merged-geometry rendering path when face count crosses a threshold.
  Measured before/after with the D8 methodology.
- **Wire scalability**: snapshot compression (MessagePack already;
  add permessage-deflate or gzip for the snapshot payload if
  measurement says it matters — measure first, don't assume).
- What this phase deliberately does *not* do: partial/lazy document
  loading (a protocol change; design-doc it in `docs/brep-design.md`'s
  sibling `docs/scale-design.md` if the benchmarks prove it necessary,
  with the measured numbers as justification).

## Definition of done

Phases C1–C8 committed one per phase, suite + e2e green throughout,
README status table and Roadmap updated with the same honesty discipline
(assessment-only items — DWG/IFC, SAML-class gaps, Stage-2 B-Rep —
labeled as exactly what they are). Sequence flexibly with Parts 5–6 but
respect the stated dependencies (C1 fillet needs C2 arcs; C5 library
sharing and C3 title-block identity fields reference Part 6 if present).
