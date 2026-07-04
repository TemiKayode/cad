"""Tests for the Redis pub/sub fan-out (Phase 7's other horizontal
scaling seam alongside PostgresStore) -- Room.broadcast/_redis_relay_loop
in app.py.

Per the brief ("optional Redis pub/sub fan-out"): this module skips
cleanly if `redis-py` isn't installed or no local Redis is reachable, so
CI never needs one. Point `CRDT_CAD_TEST_REDIS_URL` at a real Redis
(e.g. `docker run -p 56379:6379 redis:7-alpine`) to actually exercise it.

A single test process can't literally run two server *processes*, so
these tests construct two real `Room` instances sharing one Redis
connection and give them distinct `_origin` values (`Room`'s
constructor-only test seam -- production always defaults to the
process-wide `pubsub.PROCESS_ID`) to stand in for "two different server
processes". Everything else is real: real Redis, real pub/sub, real
asyncio tasks -- only the "which process published this" identity is
faked, since that's the one thing that's actually per-interpreter.
"""

import asyncio
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest

pytest.importorskip("redis")

from crdt_cad.crdt.document import DocOp, DrawingDocument  # noqa: E402
from crdt_cad.persistence.store import InMemoryStore  # noqa: E402
from crdt_cad.server.app import Room  # noqa: E402

TEST_REDIS_URL = os.environ.get("CRDT_CAD_TEST_REDIS_URL", "redis://localhost:56379/0")


def _quick_reachability_check(url: str) -> str | None:
    """Same rationale as test_postgres_store.py's version: a fast,
    synchronous TCP probe so an unreachable Redis skips the whole module
    in well under a second, instead of paying a slower per-test
    connection-failure cost on every plain `pytest tests/` run."""
    parsed = urlparse(url)
    host, port = parsed.hostname or "localhost", parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return None
    except OSError as exc:
        return f"no Redis reachable at {host}:{port} ({exc})"


_skip_reason = _quick_reachability_check(TEST_REDIS_URL)
if _skip_reason:
    pytest.skip(_skip_reason, allow_module_level=True)


def _room_id() -> str:
    return f"room-{uuid.uuid4().hex[:12]}"


class _FakeWebSocket:
    """Records every JSON message `Room._deliver_local` sends it --
    enough of `WebSocket`'s surface for these tests (`Room` only ever
    calls `send_json` on a client connection)."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


def _make_room(room_id: str, redis_client, origin: str) -> Room:
    return Room(room_id, "drawing", DrawingDocument, DocOp.from_dict, InMemoryStore(), redis_client, _origin=origin)


@pytest.fixture
def redis_client():
    import redis.asyncio as redis

    yield redis.from_url(TEST_REDIS_URL)


def _real_layer_ops_wire() -> tuple[str, list[dict]]:
    """A real, correctly-shaped `add_layer` op batch -- minted the same
    way every other test in this project builds ops, rather than a
    placeholder string, since `_redis_relay_loop` now actually applies
    incoming "ops" messages to the receiving room's own `self.doc` (see
    that regression note in app.py), not just forwards the raw message."""
    from crdt_cad.crdt.clock import LamportClock

    doc = DrawingDocument(LamportClock(actor="actor-on-process-1"))
    layer_id, ops = doc.add_layer("Layer 1")
    return layer_id, [op.to_dict() for op in ops]


@pytest.mark.asyncio
async def test_broadcast_from_one_room_relays_to_a_second_rooms_local_clients(redis_client):
    """The actual point of this feature: two Room instances (standing in
    for two server processes) for the *same* room_id/kind, sharing one
    Redis -- a client connected only to "process 2"'s Room still
    receives an op that was only ever applied/broadcast on "process 1"'s
    Room, *and* that op gets applied to process 2's own `self.doc` too
    (not just forwarded to its local clients) -- the exact gap a live
    two-process verification run caught: without this, a new client
    joining process 2 afterwards would get a stale snapshot, and
    process 2's own next persist would clobber process 1's newer data
    in the shared store."""
    room_id = _room_id()
    layer_id, ops_wire = _real_layer_ops_wire()
    room1 = _make_room(room_id, redis_client, origin="process-1")
    room2 = _make_room(room_id, redis_client, origin="process-2")

    ws2 = _FakeWebSocket()
    room2.clients["actor-on-process-2"] = ws2
    room2.start_redis_relay_loop()
    try:
        # Give the relay loop a moment to actually subscribe before
        # room1 publishes -- otherwise the message could be published
        # before room2's subscription is registered with Redis and
        # would simply never arrive (pub/sub has no backlog/replay).
        await asyncio.sleep(0.3)

        await room1.broadcast({"type": "ops", "ops": ops_wire, "from": "actor-on-process-1"})

        for _ in range(30):
            if ws2.sent:
                break
            await asyncio.sleep(0.1)
        assert ws2.sent == [{"type": "ops", "ops": ops_wire, "from": "actor-on-process-1"}]
        assert layer_id in room2.doc.layers.to_set()
    finally:
        room2.clients.clear()  # lets _redis_relay_loop's `while self.clients:` exit on its own
        if room2._redis_task:
            await asyncio.wait_for(room2._redis_task, timeout=3)


@pytest.mark.asyncio
async def test_a_rooms_own_publish_is_not_relayed_back_to_its_own_clients_twice(redis_client):
    """A Room's own `broadcast` already delivers locally -- if its Redis
    relay loop didn't recognize (and skip) its own publishes, every
    locally-originated op would double-deliver to that same process's
    clients."""
    room_id = _room_id()
    _layer_id, ops_wire = _real_layer_ops_wire()
    room1 = _make_room(room_id, redis_client, origin="process-1")

    ws1 = _FakeWebSocket()
    room1.clients["actor-on-process-1"] = ws1
    room1.start_redis_relay_loop()
    try:
        await asyncio.sleep(0.3)
        await room1.broadcast({"type": "ops", "ops": ops_wire, "from": "actor-on-process-1"})
        # Give the relay loop ample time to have (wrongly) redelivered
        # this if the origin check were broken, before asserting it didn't.
        await asyncio.sleep(1.0)
        assert ws1.sent == [{"type": "ops", "ops": ops_wire, "from": "actor-on-process-1"}]
    finally:
        room1.clients.clear()
        if room1._redis_task:
            await asyncio.wait_for(room1._redis_task, timeout=3)
