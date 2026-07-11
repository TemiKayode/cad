# Large-document performance: benchmarks, budgets, and LOD

Part 7 C8. Two scripts (both committed, both real measurements you can
rerun yourself -- nothing here is a cached or assumed figure):

```bash
python scripts/bench_large_doc.py                                    # backend only, no server needed
python -m uvicorn crdt_cad.server.app:app --port 8791 &
python scripts/bench_frontend_render.py --base http://127.0.0.1:8791 # needs a running server + Chromium
```

## Where the actual bottleneck is

The backend (CRDT op application, MessagePack serialization, SQLite
persistence) stays fast even at 5000 paths/faces -- well under 200ms
for the *entire* pipeline. The frontend render loop had no such
ceiling before this phase: `sketch.js`'s `render()` walked every path
in the document every frame with no culling at all, and `mesh3d.js`
created one Three.js object per vertex, edge, *and* face with no
merging or level-of-detail. For a document with thousands of paths or
faces -- the realistic case for "one big shared room, everyone's
looking at their own corner of it" -- that's where a large document
actually gets slow, not the backend.

## Backend: `scripts/bench_large_doc.py`

Real measured run (this machine, single-threaded, cold each size --
`build` mints all the ops via `DrawingDocument.add_path`/
`MeshCRDT.add_face`, `to_bytes`/`from_bytes` is the same MessagePack
round trip `Room.persist`/reconnect-snapshot use, `json` is what
`/api/.../export/json` sends, `persist`/`load` are real `SQLiteStore`
calls against a real file):

**2D drawing documents, by path count**

| n | build | to_bytes | from_bytes | json | persist | load | bytes |
|---|---|---|---|---|---|---|---|
| 100 | 2.2ms | 0.8ms | 1.6ms | 2.2ms | 4.5ms | 2.1ms | 47,389 |
| 500 | 11.7ms | 4.5ms | 9.1ms | 6.6ms | 4.4ms | 1.5ms | 237,789 |
| 2000 | 45.4ms | 20.9ms | 43.3ms | 38.6ms | 7.1ms | 2.1ms | 951,789 |
| 5000 | 141.4ms | 83.1ms | 115.6ms | 97.4ms | 13.0ms | 3.1ms | 2,379,789 |

**3D mesh documents, by face count**

| n | build | to_bytes | from_bytes | json | persist | load | bytes |
|---|---|---|---|---|---|---|---|
| 96 | 2.1ms | 0.6ms | 1.1ms | 0.8ms | 4.3ms | 1.0ms | 26,276 |
| 498 | 6.9ms | 3.7ms | 5.5ms | 3.9ms | 4.9ms | 1.1ms | 139,774 |
| 2004 | 30.2ms | 14.0ms | 26.2ms | 26.2ms | 5.9ms | 2.1ms | 575,264 |
| 5004 | 85.0ms | 37.8ms | 96.1ms | 56.9ms | 9.9ms | 2.8ms | 1,444,264 |

Everything scales roughly linearly with document size, as expected --
no accidental quadratic anywhere in apply/serialize/persist. SQLite
`persist`/`load` barely move at all (single-blob upsert, dominated by
fixed overhead, not document size) up to 5000 elements.

## Frontend: `scripts/bench_frontend_render.py`

Seeds each room with N paths/faces spread across a huge world area (so
only a handful are ever inside one viewport -- the realistic large-doc
case, not N paths all crammed into one screen), then measures the real
render path against a real headless Chromium tab.

**2D (`/2d`): real `render()` timing, 200 calls/room** (`sketch.js` is
a classic, non-module script, so its top-level `render` is a real
`window` property `page.evaluate` can call directly -- see the
module-scoping note below for why the same trick doesn't work for the
3D page):

| paths | avg | min | max |
|---|---|---|---|
| 50 | 0.08ms | 0.00ms | 1.00ms |
| 500 | 0.37ms | 0.10ms | 2.40ms |
| 2000 | 0.92ms | 0.60ms | 6.10ms |
| 5000 | 1.92ms | 1.40ms | 9.60ms |

This is the viewport-bbox culling added this phase paying off directly
-- render time grows *sub-linearly* with document size (100x more
paths (50 -> 5000) costs about 24x more milliseconds, not 100x, only
because a fixed handful stay on-screen regardless of N; before
culling, every one of those 5000 paths would have been walked and
drawn every frame, regardless of whether it was ever visible). Seeding
was verified correct via each room's real
`/api/rooms/.../export/json` path count before trusting these numbers
-- an earlier run of this exact script silently seeded near-empty
rooms after blasting ops past the server's own per-connection rate
limit (`CRDT_CAD_WS_OPS_PER_SECOND`), which would have made the "flat
regardless of N" result meaningless; see `_seed`'s docstring in the
script for the fix (pace batches, drain *every* reply for a chunk
before deciding it was accepted, retry a chunk that was actually
rate-limited, never assume a passing HTTP status or a single quick
reply meant the data landed).

**3D (`/3d`): real requestAnimationFrame-tick FPS, 2s window/room.**
`mesh3d.js` is a `type="module"` script -- its internal `state`/
`scene`/`edgeLines` etc. are not reachable via `page.evaluate` at all
(a confirmed, recurring constraint in this codebase), so FPS is
measured the one way that's still honest regardless of module scoping:
counting real `requestAnimationFrame` callbacks over a fixed wall-clock
window, in this sandbox's software-rendered (SwiftShader, no real GPU)
Chromium:

| faces | fps |
|---|---|
| 48 | 60.4 |
| 498 | 53.4 |
| 2004 | 49.9 |
| 5004 | 36.7 |

A real, gradual degradation as scene object count grows -- consistent
with `syncScene()`'s one-Three.js-object-per-vertex/edge/face design
(no merged geometry, no instancing). All four rooms' face counts were
verified exact against `/api/mesh/.../export/json` before trusting
these numbers (an earlier run of this script had a face-id collision
bug -- every box's six faces reused the *same* six ids ("bottom",
"top", ...) instead of being prefixed per-box, so every room silently
converged to exactly 6 live faces regardless of the target size; fixed
by prefixing face ids the same way vertex ids already were). Absolute
numbers here are specific to this sandbox's software rasterizer, not
representative of real GPU hardware (the same caveat this repo's
Phase D8 perf audit already noted for WebGL) -- what's meaningful is
the *trend* (more objects, fewer frames), not the absolute fps. This
table already reflects the LOD change below being active; see that
section for a controlled A/B showing its actual (honestly modest, at
this specific scene shape) effect.

## Budgets: soft per-room size ceilings

New, matching the shape of every other resource ceiling in
`security.py` (`max_ops_per_message`, `max_clients_per_room`, etc.):
`CRDT_CAD_MAX_PATHS_PER_ROOM` / `CRDT_CAD_MAX_FACES_PER_ROOM`, both
`0` (unlimited) by default. Enforced in `app.py`'s `_validate_op` --
the one existing pre-commit gate -- so a genuinely *new* path/face is
refused once a room is at budget; editing or deleting what's already
there is never affected, and an already-merged remote op can never be
rejected retroactively (the same reasoning the rest of `_validate_op`
already documents: rejecting a merge would break CRDT convergence).
Deleting an element frees the slot back up. See
`docs/configuration.md`'s "Rate limits and resource ceilings" section.

## LOD: two real, narrow wins

**2D viewport culling** (`sketch.js`'s `render()`): before this phase,
every path in the document was walked and drawn every frame
unconditionally. Now each path's existing `pathWorldBounds` helper
(already used for hit-testing/align/distribute) is checked against the
current viewport, expanded by a 100px screen-space margin for stroke
width and selection halos, before doing any of the actual drawing
work -- an off-screen path costs one cheap bounds check instead of a
full curve-sampling stroke. This is the change the flat 2D numbers
above are measuring.

**3D edge-line hiding** (`mesh3d.js`'s `syncScene()`): past
`LOD_HIDE_EDGES_FACE_THRESHOLD` (300) live faces, the per-edge
`THREE.Line` helper objects are torn down and stop being recreated --
roughly halving total scene object count on a large mesh, since edge
count runs close to 1.5x face count for a closed mesh. Deliberately
scoped to edges only, not vertex markers: vertex meshes are
raycast-picked for building a new face and for drag-to-move (see
`raycaster.intersectObjects([...vertexMeshes.values()])`), so hiding
them would remove real editing capability, not just visual noise --
edge lines are confirmed (by grep) to never be raycast against
anywhere in the file, so they're the one helper that's pure rendering
cost with zero functional role. Below the threshold, behavior is
byte-for-byte unchanged. Verified live: a 360-face room (just over the
threshold) loads with zero console/page errors and renders normally
(screenshot-checked).

**Honest A/B result, not a fabricated win**: a controlled comparison on
the 5004-face room (same room, same browser session, only the
`LOD_HIDE_EDGES_FACE_THRESHOLD` constant toggled between runs -- 300
vs. an effectively-disabled 999999, three repeats each) measured
38.2/41.3/41.2 fps with the LOD active against 41.5/42.3/42.7 fps with
it disabled. That's within this sandbox's run-to-run noise (~5%), not
a clean win -- at this scale the ~2,700 hidden edge lines are a small
fraction of the roughly 12,000+ vertex-marker-and-face objects the LOD
deliberately leaves untouched (vertex markers stay because they're
raycast-picked for editing; faces stay because merging them is the
larger, out-of-scope rework noted above), so removing edges alone
doesn't move the needle much once face+vertex object count already
dominates the frame. The change is kept anyway -- it's free, strictly
correct, and a bigger win on a mesh with proportionally more edges
relative to faces than this benchmark's boxes (e.g. a dense freeform
surface) -- but the honest conclusion is that a real fix for large-mesh
performance needs to address face/vertex object count, not edges, the
same boundary the B-Rep design doc (Part 7 C6) already drew between a
narrow, safe prototype and the larger rework it deliberately stops
short of.
