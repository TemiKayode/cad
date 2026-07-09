#!/usr/bin/env python
"""Load / soak test (Phase 19.5) -- drive N rooms x M concurrent WebSocket
clients, each drawing like a real user, against a running deployment, and
report throughput, fan-out delivery, op latency, and server memory growth.

    python scripts/load_test.py http://127.0.0.1:8000 \
        --rooms 5 --clients 4 --duration 60 --rate 5

Every client is a real protocol participant: it connects, sends `hello`,
receives the snapshot, creates its own layer + path with genuine
`DrawingDocument`-minted ops, then appends pen-stroke points at `--rate`
ops/sec. Latency is measured sender-to-receiver across clients in the
same room (all clients share this process's clock, so no clock-skew
problem): the sender records `time.monotonic()` per op id, each receiver
that sees the broadcast looks it up and records the delta.

Stays deliberately under the default per-connection rate limit
(CRDT_CAD_WS_OPS_PER_SECOND=200) and per-room ceiling
(CRDT_CAD_MAX_OPS_PER_ROOM_PER_MINUTE=20000) unless you push --rate /
--clients up on purpose; any `rejected` message the server sends is
counted and reported separately, so limit-trips show up as findings,
not silent losses.

Server memory is sampled from /metrics (`process_resident_memory_bytes`,
exported by prometheus_client's process collector inside the Linux
container) before and after the run -- growth that doesn't level off
across a soak run is the leak signal to investigate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
import uuid

import websockets

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument


def _op_key(op_dict: dict) -> str:
    """Stable identity for an op as it crosses the wire, independent of
    the op schema's field layout."""
    return json.dumps(op_dict, sort_keys=True)


class Stats:
    def __init__(self) -> None:
        self.sent = 0
        self.received = 0
        self.rejected = 0
        self.errors: list[str] = []
        self.latencies_ms: list[float] = []
        self.send_times: dict[str, float] = {}

    def note_sent(self, ops_wire: list[dict]) -> None:
        now = time.monotonic()
        for op in ops_wire:
            self.send_times[_op_key(op)] = now
        self.sent += len(ops_wire)

    def note_received(self, ops_wire: list[dict]) -> None:
        now = time.monotonic()
        self.received += len(ops_wire)
        for op in ops_wire:
            # pop, not get: each op is broadcast to clients-1 receivers, but
            # one latency sample per op is enough, and popping keeps the
            # map from growing for the whole run.
            sent_at = self.send_times.pop(_op_key(op), None)
            if sent_at is not None:
                self.latencies_ms.append((now - sent_at) * 1000.0)


async def _client(base_ws: str, room: str, idx: int, args: argparse.Namespace, stats: Stats, ready: asyncio.Barrier) -> None:
    actor = f"load-{room}-{idx}"
    doc = DrawingDocument(LamportClock(actor=actor))

    # ping_timeout is generous on purpose: under deliberate overload the
    # goal is to measure degraded latency, not have the client library
    # amputate the connection the moment a pong is 20s late.
    async with websockets.connect(f"{base_ws}/ws/{room}", max_size=None, ping_timeout=60) as ws:
        await ws.send(json.dumps({"type": "hello", "actor": actor, "known_frontier": None}))
        snapshot = json.loads(await ws.recv())
        assert snapshot["type"] == "snapshot", f"{actor}: expected snapshot, got {snapshot.get('type')}"

        layer_id, layer_ops = doc.add_layer(f"layer-{actor}")
        path_id, path_ops = doc.add_path(layer_id, [(float(idx * 100), 0.0)], color="#4dabf7")
        setup_wire = [op.to_dict() for op in [*layer_ops, *path_ops]]
        stats.note_sent(setup_wire)
        await ws.send(json.dumps({"type": "ops", "ops": setup_wire, "from": actor}))

        async def receiver() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "ops" and msg.get("from") != actor:
                    stats.note_received(msg["ops"])
                elif msg.get("type") == "rejected":
                    stats.rejected += 1

        recv_task = asyncio.create_task(receiver())
        await ready.wait()  # start every client's write phase together

        interval = 1.0 / args.rate
        deadline = time.monotonic() + args.duration
        x, y = float(idx * 100), 0.0
        try:
            while time.monotonic() < deadline:
                # Monotonic coordinates: every segment has nonzero length,
                # so the geometry validity gate never (correctly) rejects
                # these as degenerate.
                x += 3.0
                y += 2.0
                op = doc.append_point(path_id, (x, y))
                wire = [op.to_dict()]
                stats.note_sent(wire)
                await ws.send(json.dumps({"type": "ops", "ops": wire, "from": actor}))
                await asyncio.sleep(interval)
        finally:
            # Let in-flight broadcasts drain before tearing the socket down,
            # so end-of-run fan-out isn't undercounted.
            await asyncio.sleep(args.drain)
            recv_task.cancel()


def _scrape_metrics(base_url: str) -> dict[str, float]:
    wanted = ("process_resident_memory_bytes", "crdt_cad_active_connections", "crdt_cad_ops_relayed_total")
    out: dict[str, float] = {}
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=5) as resp:
            for line in resp.read().decode().splitlines():
                for name in wanted:
                    if line.startswith(name + " ") or line.startswith(name + "{"):
                        out[name] = float(line.rsplit(" ", 1)[1])
    except Exception as exc:  # noqa: BLE001 -- metrics are informative, not the test itself
        print(f"note: could not scrape /metrics ({exc}); memory numbers will be missing")
    return out


async def _run(args: argparse.Namespace) -> int:
    base_ws = args.base_url.replace("https://", "wss://").replace("http://", "ws://")
    run_id = uuid.uuid4().hex[:6]
    stats = Stats()

    before = _scrape_metrics(args.base_url)
    n_clients = args.rooms * args.clients
    ready = asyncio.Barrier(n_clients)
    started = time.monotonic()
    tasks = [
        asyncio.create_task(_client(base_ws, f"load-{run_id}-{r}", i, args, stats, ready))
        for r in range(args.rooms)
        for i in range(args.clients)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.monotonic() - started
    after = _scrape_metrics(args.base_url)

    for res in results:
        if isinstance(res, BaseException):
            stats.errors.append(repr(res))

    expected_fanout = stats.sent * (args.clients - 1)
    lat = stats.latencies_ms
    print(f"\n== load test: {args.rooms} rooms x {args.clients} clients, "
          f"{args.rate} ops/s/client, {args.duration}s (wall {elapsed:.1f}s) ==")
    print(f"ops sent            : {stats.sent} ({stats.sent / elapsed:.1f}/s aggregate)")
    print(f"fan-out received    : {stats.received} / {expected_fanout} expected "
          f"({100.0 * stats.received / expected_fanout if expected_fanout else 100.0:.1f}%)")
    print(f"server rejections   : {stats.rejected}")
    if lat:
        lat.sort()
        print(f"op latency (ms)     : mean {statistics.fmean(lat):.1f}  "
              f"p50 {lat[len(lat) // 2]:.1f}  "
              f"p95 {lat[int(len(lat) * 0.95)]:.1f}  max {lat[-1]:.1f}")
    if "process_resident_memory_bytes" in before and "process_resident_memory_bytes" in after:
        b, a = before["process_resident_memory_bytes"], after["process_resident_memory_bytes"]
        print(f"server RSS          : {b / 1e6:.1f} MB -> {a / 1e6:.1f} MB ({(a - b) / 1e6:+.1f} MB)")
    if "crdt_cad_ops_relayed_total" in before and "crdt_cad_ops_relayed_total" in after:
        print(f"ops relayed (server): +{after['crdt_cad_ops_relayed_total'] - before['crdt_cad_ops_relayed_total']:.0f}")
    if stats.errors:
        print(f"CLIENT ERRORS ({len(stats.errors)}):")
        for err in stats.errors[:10]:
            print(f"  {err}")
        return 1
    if expected_fanout and stats.received < expected_fanout * args.min_delivery:
        print(f"FAIL: fan-out delivery below --min-delivery={args.min_delivery:.0%}")
        return 1
    print("PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--rooms", type=int, default=5)
    parser.add_argument("--clients", type=int, default=4, help="clients per room")
    parser.add_argument("--duration", type=float, default=60.0, help="write-phase seconds")
    parser.add_argument("--rate", type=float, default=5.0, help="ops/sec per client")
    parser.add_argument("--drain", type=float, default=2.0, help="seconds to keep receiving after writes stop")
    parser.add_argument("--min-delivery", type=float, default=0.99, help="fail below this fan-out delivery fraction")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
