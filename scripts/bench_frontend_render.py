#!/usr/bin/env python
"""Part 7 C8 -- frontend render/FPS benchmarks against a real browser.

The backend benchmark (bench_large_doc.py) already shows CRDT apply and
serialization stay well under 200ms even at 5000 paths/faces -- the
real bottleneck for a large document is the frontend render loop, which
this script measures directly against a real running dev server and a
real headless Chromium tab (not an in-process function call), the same
two-stage "isolated math first, live UI second" verification this repo
uses everywhere else.

    python -m uvicorn crdt_cad.server.app:app --port 8791 &
    python scripts/bench_frontend_render.py --base http://127.0.0.1:8791

Seeds each room with N paths/faces spread across a huge world area (so
only a handful are ever inside any one viewport -- the realistic case
for a genuinely large document, as opposed to N paths all crammed into
one screen's worth of space) via a raw websockets connection, then:

- 2D (`/2d`): calls the page's own real `render()` function directly
  (sketch.js is a classic, non-module script, so its top-level
  functions are real `window` properties -- see this repo's own notes
  on why that's NOT true for mesh3d.js) 200 times and reports the
  average, min, max in milliseconds.
- 3D (`/3d`): mesh3d.js is a `type="module"` script, so its internals
  aren't reachable via page.evaluate at all (a recurring, confirmed
  constraint in this repo) -- FPS is instead measured honestly via a
  requestAnimationFrame tick counter over a fixed real-time window,
  which works regardless of module scoping since it only touches the
  DOM/window, never mesh3d.js's own state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets
from playwright.async_api import async_playwright

PATH_COUNTS = [50, 500, 2000, 5000]
FACE_COUNTS = [48, 498, 2004, 5004]
# A fresh, timestamp-suffixed room per run -- reusing a fixed room name
# across runs would let a previous run's ops (or a previous, buggier
# version of this script's seeding) linger and silently inflate/corrupt
# this run's counts.
RUN_ID = str(int(time.time()))


def _drawing_ops(n_paths: int) -> list[dict]:
    from crdt_cad.crdt.clock import LamportClock
    from crdt_cad.crdt.document import DrawingDocument

    doc = DrawingDocument(LamportClock(actor="bench-frontend"))
    layer_id, layer_ops = doc.add_layer("L")
    ops = list(layer_ops)
    for i in range(n_paths):
        # Spread across a 100,000 x 100,000 world area -- at the default
        # zoom/pan a viewport only ever spans a few thousand px, so the
        # overwhelming majority of these are off-screen, the realistic
        # "one big shared document, everyone's looking at their own
        # corner of it" case this benchmark is meant to represent.
        x, y = float((i * 977) % 100000), float((i * 613) % 100000)
        _, path_ops = doc.add_path(layer_id, [(x, y), (x + 5, y), (x + 5, y + 5), (x, y + 5), (x, y)])
        ops.extend(path_ops)
    return [op.to_dict() for op in ops]


def _mesh_ops(n_faces: int) -> list[dict]:
    from crdt_cad.crdt.clock import LamportClock
    from crdt_cad.crdt.mesh import MeshCRDT

    mesh = MeshCRDT(LamportClock(actor="bench-frontend"))
    n_boxes = max(1, n_faces // 6)
    ops = []
    for b in range(n_boxes):
        prefix = f"b{b}_"
        x0, y0, z0 = float(b % 50) * 3.0, float((b // 50) % 50) * 3.0, float(b // 2500) * 3.0
        x1, y1, z1 = x0 + 1.0, y0 + 1.0, z0 + 1.0
        verts = {
            f"{prefix}0": (x0, y0, z0), f"{prefix}1": (x1, y0, z0),
            f"{prefix}2": (x1, y1, z0), f"{prefix}3": (x0, y1, z0),
            f"{prefix}4": (x0, y0, z1), f"{prefix}5": (x1, y0, z1),
            f"{prefix}6": (x1, y1, z1), f"{prefix}7": (x0, y1, z1),
        }
        for vid, pos in verts.items():
            ops.append(mesh.add_vertex(vid, pos))
        faces = {
            f"{prefix}bottom": [f"{prefix}0", f"{prefix}3", f"{prefix}2", f"{prefix}1"],
            f"{prefix}top": [f"{prefix}4", f"{prefix}5", f"{prefix}6", f"{prefix}7"],
            f"{prefix}front": [f"{prefix}0", f"{prefix}1", f"{prefix}5", f"{prefix}4"],
            f"{prefix}back": [f"{prefix}3", f"{prefix}7", f"{prefix}6", f"{prefix}2"],
            f"{prefix}left": [f"{prefix}0", f"{prefix}4", f"{prefix}7", f"{prefix}3"],
            f"{prefix}right": [f"{prefix}1", f"{prefix}2", f"{prefix}6", f"{prefix}5"],
        }
        for fid, loop in faces.items():
            ops.extend(mesh.add_face(fid, loop))
    return [op.to_dict() for op in ops]


async def _seed(base_ws: str, path: str, ops: list[dict], batch: int = 150) -> None:
    """Seeds `ops` into a fresh room, respecting the server's own
    per-connection rate limit (CRDT_CAD_WS_OPS_PER_SECOND, default 200
    ops/sec with a 400-op burst) instead of blasting everything through
    at once -- an early version of this benchmark did exactly that and
    silently seeded a near-empty room (every op past the burst capacity
    came back `{"type": "rejected", "reason": "rate limit exceeded"}`,
    and every *later* batch then failed too, since it referenced RGA
    anchor ids the server never actually applied) -- a test-script
    pacing bug, not a product bug, caught by checking the room's real
    face count after seeding rather than trusting that "no exception
    was raised" meant seeding worked."""
    async with websockets.connect(f"{base_ws}{path}", max_size=None, ping_timeout=60) as ws:
        await ws.send(json.dumps({"type": "hello", "actor": "bench-seeder"}))
        await ws.recv()  # snapshot
        for i in range(0, len(ops), batch):
            chunk = ops[i : i + batch]
            while True:
                await ws.send(json.dumps({"type": "ops", "ops": chunk}))
                # Drain every reply the server sends for this chunk (not
                # just the first) -- a big chunk can trigger a slow
                # server-side validity check (asyncio.to_thread over a
                # large mesh) that delays a rate-limit rejection past a
                # short per-message timeout, which an earlier version of
                # this loop misread as "no reply -> accepted" and silently
                # dropped the chunk. A 1.5s idle gap (not total time) is
                # the actual "nothing more is coming" signal.
                rate_limited = False
                while True:
                    try:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.5))
                    except asyncio.TimeoutError:
                        break
                    if reply.get("type") == "rejected" and "rate limit" in reply.get("reason", ""):
                        rate_limited = True
                if rate_limited:
                    await asyncio.sleep(0.5)
                    continue  # same chunk was never applied -- resend it
                break
        await ws.send(json.dumps({"type": "save"}))
        await ws.recv()  # saved ack


async def bench_2d(base_http: str, base_ws: str) -> list[dict]:
    rows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for n in PATH_COUNTS:
            room = f"bench2d{n}_{RUN_ID}"
            await _seed(base_ws, f"/ws/{room}", _drawing_ops(n))
            page = await browser.new_page()
            # "networkidle" can hang indefinitely on this app: the live
            # WebSocket connection (reconnect pings, presence) keeps the
            # page's network activity from ever truly going idle. "load"
            # is what every other live-Playwright check in this repo
            # already uses for exactly this reason.
            await page.goto(f"{base_http}/2d?room={room}", wait_until="load")
            await page.wait_for_timeout(1000)
            timings = await page.evaluate(
                """() => {
                    const out = [];
                    for (let i = 0; i < 200; i++) {
                        const t0 = performance.now();
                        render();
                        out.push(performance.now() - t0);
                    }
                    return out;
                }"""
            )
            await page.close()
            rows.append({
                "n": n,
                "avg_ms": sum(timings) / len(timings),
                "min_ms": min(timings),
                "max_ms": max(timings),
            })
        await browser.close()
    return rows


async def bench_3d(base_http: str, base_ws: str, window_s: float = 2.0) -> list[dict]:
    rows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for n in FACE_COUNTS:
            room = f"bench3d{n}_{RUN_ID}"
            await _seed(base_ws, f"/ws/mesh/{room}", _mesh_ops(n))
            page = await browser.new_page()
            await page.goto(f"{base_http}/3d?room={room}", wait_until="load")
            await page.wait_for_timeout(1500)
            fps = await page.evaluate(
                """(windowMs) => new Promise((resolve) => {
                    let frames = 0;
                    const start = performance.now();
                    function tick() {
                        frames++;
                        if (performance.now() - start < windowMs) {
                            requestAnimationFrame(tick);
                        } else {
                            resolve(frames / ((performance.now() - start) / 1000));
                        }
                    }
                    requestAnimationFrame(tick);
                })""",
                window_s * 1000,
            )
            await page.close()
            rows.append({"n": n, "fps": fps})
        await browser.close()
    return rows


def _print_2d(rows: list[dict]) -> None:
    print("\n2D sketch: real render() timing (200 calls/room, milliseconds)")
    print(f"{'paths':>6} | {'avg':>7} | {'min':>7} | {'max':>7}")
    print("-" * 36)
    for r in rows:
        print(f"{r['n']:>6} | {r['avg_ms']:>5.2f}ms | {r['min_ms']:>5.2f}ms | {r['max_ms']:>5.2f}ms")


def _print_3d(rows: list[dict]) -> None:
    print("\n3D mesh: real rAF-tick FPS (2s window/room)")
    print(f"{'faces':>6} | {'fps':>7}")
    print("-" * 18)
    for r in rows:
        print(f"{r['n']:>6} | {r['fps']:>6.1f}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8791", help="running dev server base URL")
    args = parser.parse_args()
    base_ws = args.base.replace("http://", "ws://").replace("https://", "wss://")

    rows_2d = await bench_2d(args.base, base_ws)
    _print_2d(rows_2d)
    rows_3d = await bench_3d(args.base, base_ws)
    _print_3d(rows_3d)


if __name__ == "__main__":
    asyncio.run(main())
