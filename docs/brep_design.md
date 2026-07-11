# A real B-Rep kernel for crdt-cad: design notes

The README's "Non-commutative mesh edits" section already names the
honest limitation this document expands on: `MeshCRDT` is a flat bag
of vertices, edges, and per-face vertex-loops (an "indexed face set"),
not a genuine topological Boundary Representation (B-Rep) the way a
real CAD kernel (Parasolid, ACIS, OpenCascade, the one `build123d`
wraps for this project's own STEP export) represents a solid. A
post-merge validity check (`geometry/mesh_validity.py`) catches the
consequences — degenerate faces, non-manifold edges, inconsistent
winding, dangling references — and broadcasts a warning rather than
rejecting the merge, because a CRDT merge can't be rejected without
breaking convergence. That's a real, working answer to "what happens
when two people's concurrent edits conflict," not a stopgap — but it's
a *reactive* answer. A B-Rep data model would make a whole class of
those conflicts structurally impossible, or at least make them merge
predictably instead of needing after-the-fact detection. This
document is that redesign sketched out, and an honest accounting of
why it isn't what got built.

## What "indexed face set" actually means here, precisely

Today (`crdt_cad.crdt.mesh.MeshCRDT`):

- `vertices`: `LWWMap[VertexId, Position]` — a flat, unordered bag.
- `edges`: `LWWElementSet[EdgeId]` (canonicalized `(v1, v2)` pairs) —
  existence only, no notion of which faces an edge borders.
- `faces`: `face_index: LWWElementSet[FaceId]` (existence) +
  `face_rga: dict[FaceId, RGA[VertexId]]` (the face's own boundary
  loop, itself a CRDT so concurrent inserts/deletes into the *same*
  loop merge sensibly) + `face_props: dict[FaceId, LWWMap]` (color,
  material, `scene_object`, ...).

Nothing here encodes **adjacency** as first-class data: "which two
faces share this edge," "is this edge on the boundary of exactly one
solid or is it interior," "which faces bound this vertex, in what
cyclic order" are all *derived*, recomputed by scanning every face's
loop for a matching vertex/edge pair whenever something needs to know
them (`mesh_validity.py` does exactly this scan). A real B-Rep kernel
stores that adjacency directly — a half-edge or winged-edge structure
where every edge record *points at* its bordering faces and its
next/prev edges around each face, so "walk the boundary of this face"
or "find every face touching this edge" is a pointer-chase, not a scan,
and — more importantly for a CRDT — a structural invariant the data
type itself can maintain rather than something checked after the fact.

## Why "just add adjacency pointers" doesn't work as a CRDT

The tempting fix — add `left_face`/`right_face` fields to `edges`, add
`next_edge`/`prev_edge` per face-corner — fails for the same reason a
plain linked list is a bad CRDT: a pointer is a *reference to a specific
op's result*, and two replicas concurrently splicing the same
neighborhood produce pointer graphs that don't converge to the same
topology just because each individual field merges via LWW. RGA solves
this for a linear sequence (a path's point order, a face's own boundary
loop) by anchoring every insert to a specific existing element's stable
id rather than an index — the *sequence* converges because insertion
position is defined relationally, not positionally. A half-edge mesh's
adjacency is a **graph**, not a sequence: extending RGA's anchor trick
from "next point in this one loop" to "next edge around this vertex,
which face is on which side" means every edge, not just every face,
needs its own CRDT-native ordering primitive, and the *set of faces
touching a given edge* needs a CRDT that tolerates a temporarily
inconsistent state (edge briefly has 0, 1, or 3+ adjacent faces mid-
merge, before every concurrent op has arrived) without corrupting the
whole structure — genuinely open design work, not a known recipe to
port over.

## Sketch of a plausible design (not built, not committed to)

If this were built, the shape that seems most promising:

- **`HalfEdgeCRDT`** wrapping today's `vertices`/`edges`/`faces` with
  a fourth component, `half_edges: LWWMap[HalfEdgeId, dict]` — payload
  `{face_id, vertex_id, twin_id, next_id}`. Each field is independently
  LWW (same "concurrent edits to different fields never clobber" shape
  every existing prop bag already has), so setting `twin_id` (pairing
  two half-edges across a shared edge) and setting `next_id` (this
  face's own boundary order) can happen concurrently from different
  actors without conflict.
- **Convergence, not correctness, is what CRDT machinery guarantees.**
  A half-edge whose `twin_id` points at a half-edge that itself points
  its `twin_id` elsewhere (two different replicas each pairing the same
  edge with a *different* partner) converges to *some* deterministic
  state (last-writer-wins per field, like everything else here) — but
  that state can still be topologically invalid. This is the same
  ceiling `mesh_validity.py` already lives with today, just moved to a
  richer data model: **a validity checker doesn't go away**, it gets
  cheaper and more precise (walking a half-edge cycle instead of
  scanning every face's loop for shared vertices), and probably catches
  a strictly larger class of problems earlier, but "warning not gate"
  stays the right answer for the reason it already is: a CRDT merge
  still can't be rejected without breaking convergence, no matter how
  rich the topology model gets.
- **Migration is the expensive part, not the new type.** Every
  exporter (SVG has no 3D equivalent, but STL/STEP/glTF/3MF/PDF-via-2D
  all read `vertex_positions()`/`face_loops()`), the mesh validity
  checker, the AI generation pipeline's mesh-builder, the extrude tool,
  and `mesh3d.js`'s entire rendering/hit-testing/selection layer all
  currently code directly against "flat vertex dict + per-face loop
  list." A `HalfEdgeCRDT` would need to either replace `MeshCRDT`
  outright (a breaking change to every persisted room's snapshot format,
  needing a real migration path) or grow *alongside* it as a derived,
  reconstructible index (cheaper to ship, but then it's a cache, not a
  source of truth, and caches that can silently drift out of sync with
  the thing they're caching are their own bug class).

## Why this wasn't built this pass

Three honest reasons, not one:

1. **Scope.** This is a genuine core-data-model rewrite touching every
   layer of the 3D stack (CRDT, server validity, every exporter, the
   entire 3D renderer/selection/hit-test layer) — comparable in size to
   Part 6's whole accounts/permissions build, not a single feature.
2. **The CRDT research problem is open, not just unimplemented.**
   "Extend RGA's relational-anchor trick from a linear sequence to a
   graph's adjacency structure, in a way that's provably convergent
   under arbitrary concurrent splices" doesn't have a known, ported-in
   recipe the way (for example) STEP export's "OpenCascade wheel exists
   now, re-evaluate the old conda-only assumption" did — see
   `docs/ifc_dwg_assessment.md` for the same "confirmed, not assumed"
   standard applied to a different format-support question. This one
   is closer to research than integration.
3. **The payoff is real but narrow.** It would make *concurrent
   editors of the same local neighborhood* converge more predictably
   and would make topology-aware tools (real B-Rep booleans with exact
   surface intersection, fillets that follow edge adjacency, shells)
   possible to build well. It would not change what a *single* editor
   (or non-concurrent edits to different parts of the mesh — the
   overwhelming majority of real usage) can already do today.

## What Part 7 C6 builds instead

Given the above, C6's actual scope is bounded to what's tractable
*without* a topology rewrite: real mesh **boolean operations**
(union/subtract/intersect) via `trimesh`'s boolean API (backed by
`manifold3d`, already a core dependency — the same library this
project's own door/window CSG-cut generators already use, see
`src/crdt_cad/ai/generators/`), operating on the *current* flat
vertex/face-loop representation — a boolean's result is computed as an
ordinary triangle mesh and minted as fresh CRDT ops the same way STEP
import already does (Part 7 C4's `mesh_from_step_bytes` established
this exact "server computes real geometry off the event loop, mints
fresh vertex/face ops, commits via `Room.commit_ops_batched`" pattern),
plus a small, explicitly **flag-gated** parametric-primitive prototype
(see the README's own C6 section for what that covers) as a limited,
honest demonstration of what a parameter-preserving feature *could*
look like without pretending it's the general B-Rep solution above.
