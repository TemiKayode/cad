#!/usr/bin/env python
"""Smoke test for a real Kubernetes deployment (Phase 18.4) -- run against
a `kubectl port-forward`'d crdt-cad Service (or any reachable base URL) to
confirm the deployment actually works end-to-end, not just that the pod
reports Ready: a real WebSocket handshake, an accepted op, an explicit
save, and a fresh reconnect proving that op persisted.

Used by CI's k8s-smoke job (see .github/workflows/ci.yml) against a kind
cluster, and doubles as a manual post-deploy check against any real
cluster: `kubectl port-forward svc/crdt-cad 8080:80 &` then

    python scripts/k8s_smoke_test.py http://127.0.0.1:8080
"""
import asyncio
import json
import sys
import time
import urllib.request
import uuid

import websockets

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument


def _check_health(base_url: str) -> None:
    with urllib.request.urlopen(f"{base_url}/health", timeout=5) as resp:
        body = json.loads(resp.read())
    assert body.get("status") == "ok", f"/health did not report ok: {body}"
    print(f"PASS: /health ok ({body})")


async def _check_websocket_roundtrip(base_url: str) -> None:
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    room = f"k8s-smoke-{uuid.uuid4().hex[:8]}"
    layer_name = f"smoke-layer-{uuid.uuid4().hex[:8]}"

    doc = DrawingDocument(LamportClock(actor="smoke-client"))
    _, ops = doc.add_layer(layer_name)
    ops_wire = [op.to_dict() for op in ops]

    async with websockets.connect(f"{ws_base}/ws/{room}") as ws:
        await ws.send(json.dumps({"type": "hello", "actor": "smoke-client", "known_frontier": None}))
        snapshot = json.loads(await ws.recv())
        assert snapshot["type"] == "snapshot", f"expected snapshot, got: {snapshot}"
        print(f"PASS: WebSocket handshake ok, room {room!r} started empty")

        await ws.send(json.dumps({"type": "ops", "ops": ops_wire, "from": "smoke-client"}))
        await ws.send(json.dumps({"type": "save"}))
        while True:
            reply = json.loads(await ws.recv())
            if reply.get("type") == "rejected":
                raise AssertionError(f"server rejected the smoke-test op: {reply}")
            if reply.get("type") == "saved":
                break
        print("PASS: op accepted and explicitly saved")

    # Fresh connection -- proves the op is durable, not just held in the
    # first connection's in-memory state.
    async with websockets.connect(f"{ws_base}/ws/{room}") as ws2:
        await ws2.send(json.dumps({"type": "hello", "actor": "smoke-client-2", "known_frontier": None}))
        snapshot2 = json.loads(await ws2.recv())
        assert layer_name in json.dumps(snapshot2["doc"]), (
            f"reconnect didn't see the saved layer -- persistence is broken: {snapshot2}"
        )
        print("PASS: fresh reconnect sees the saved op -- persistence round-trip confirmed")


def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    print(f"running k8s smoke test against {base_url} ...")
    start = time.monotonic()
    try:
        _check_health(base_url)
        asyncio.run(_check_websocket_roundtrip(base_url))
    except Exception as exc:  # noqa: BLE001 -- top-level smoke test, want a clean failure message
        print(f"FAIL: {exc}")
        return 1
    print(f"all smoke checks passed in {time.monotonic() - start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
