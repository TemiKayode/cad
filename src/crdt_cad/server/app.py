"""FastAPI/asyncio WebSocket relay + REST API: one pub/sub room per
document, durable snapshots, import/export, and the constraint solver.

Two independent room kinds share one relay implementation:

- ``/ws/{room_id}``      -- 2D sketch rooms, backed by :class:`DrawingDocument`.
- ``/ws/mesh/{room_id}`` -- 3D mesh rooms, backed by :class:`MeshCRDT`.

Both documents expose the same four-method surface (``apply``,
``ops_since``, ``frontier``, ``to_dict``/``to_bytes``), so one generic
:class:`Room` / :class:`RoomManager` pair serves either kind.

WebSocket protocol (JSON frames)
------------------------------------
Client -> server, first frame after connecting::

    {"type": "hello", "actor": "<actor id>", "known_frontier": {...} | null}

Server -> client, in reply to ``hello``::

    {"type": "snapshot", "doc": {...}, "frontier": {...}, "role": "editor"}  # new client
    {"type": "delta", "ops": [...], "frontier": {...}, "role": "editor"}     # reconnect

``role`` (Phase 17) is ``"editor"`` (full read/write, and the only role
that ever existed before this) or ``"viewer"`` (read-only -- see
``crdt_cad.server.security.token_role``): a viewer still receives every
snapshot/delta/ops/frontier message normally, but any ``"ops"`` message
*it* submits is refused (see the ``"rejected"`` reply below). Always
``"editor"`` when room auth isn't configured at all.

Either direction, at any time afterwards::

    {"type": "ops", "ops": [Op, ...], "from": "<actor id>"}        # CRDT ops
    {"type": "signal", "to": "<actor id>", "data": {...}}          # WebRTC
        signaling relay -- SDP offer/answer/ICE candidates, forwarded
        verbatim to one specific peer so two browsers can negotiate a
        direct P2P data channel; the server never inspects ``data``.
    {"type": "save"}                                                # client ->
        server only: force an immediate durable snapshot; server replies
        {"type": "saved", "at": <unix time>} to the requester.
    {"type": "rejected", "reason": "...", "op": {...}}              # server ->
        client only: a submitted op failed the geometry validity gate
        (see ``_validate_op``) and was not applied or relayed.
    {"type": "frontier", "frontier": {...}}                         # server ->
        client only: a lightweight periodic ping (see below) -- not a
        request, just the room's current VectorClock.
    {"type": "resync", "known_frontier": {...} | null}              # client ->
        server only: "catch me up" after noticing a "frontier" ping ahead
        of what this client has recorded. Server replies with a "delta"
        (known_frontier given) or a full "snapshot" (null -- the response
        of last resort, meaning this client never got an initial one).

The server also broadcasts a lightweight ``frontier`` ping to every
client in a room on a fixed interval (only when something changed since
the last one), so a late joiner or a client that missed something for
any reason resyncs without a special request -- but unlike a full
``snapshot``, this periodic ping is O(actor count), not O(document
size), regardless of how large the room's document has grown.

This module is a relay, not an authority in the OT sense: it never
rewrites or reorders client ops (aside from the pre-commit validity
gate, which *rejects*, never *modifies*). Convergence is entirely the
CRDT's responsibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crdt_cad.ai.generator import (
    DEFAULT_ACTOR_ID,
    EditNotSupportedError,
    check_edit_supported,
    generate_edit_ops_from_interpretation,
    generate_ops_from_interpretation,
    generation_geometry,
    interpretation_chips,
)
from crdt_cad.ai.interpreter import interpret_edit, interpret_prompt
from crdt_cad.ai.meshy_adapter import generate_mesh_via_meshy_async, meshy_api_key
from crdt_cad.ai.validation import GenerationValidationError
from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.document import DocOp, DrawingDocument, bake_path_transform
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp
from crdt_cad.crdt.mesh import new_id as mesh_new_id
from crdt_cad.export.dxf_io import drawing_from_dxf_bytes, drawing_to_dxf_bytes
from crdt_cad.export.mesh_interop import mesh_to_3mf_bytes, mesh_to_glb_bytes
from crdt_cad.export.pdf_io import sheet_to_pdf_bytes
from crdt_cad.export.step_export import mesh_from_step_bytes, mesh_to_step_bytes
from crdt_cad.export.stl_export import mesh_to_stl
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string
from crdt_cad.geometry.constraints import Constraint, Sketch
from crdt_cad.geometry.mesh_validity import check_mesh_validity
from crdt_cad.geometry.modify import OffsetError, offset_path
from crdt_cad.geometry.validity import GeometryError, validate_new_point
from crdt_cad.persistence.store import DocumentStore, PostgresStore, SQLiteStore
from crdt_cad.server import auth
from crdt_cad.server import billing
from crdt_cad.server import metrics
from crdt_cad.server import pubsub
from crdt_cad.server import security

logger = logging.getLogger("crdt_cad.server")
logging.basicConfig(level=logging.INFO)

SNAPSHOT_INTERVAL_SECONDS = float(os.environ.get("CRDT_CAD_SNAPSHOT_INTERVAL_SECONDS", "30"))
VERSION_CHECKPOINT_INTERVAL_SECONDS = float(os.environ.get("CRDT_CAD_VERSION_CHECKPOINT_INTERVAL_SECONDS", "300"))
GENERATION_TIMEOUT_SECONDS = float(os.environ.get("CRDT_CAD_GENERATION_TIMEOUT_SECONDS", "60"))
GENERATION_OPS_BATCH_SIZE = int(os.environ.get("CRDT_CAD_GENERATION_BATCH_SIZE", "150"))
# Floor on how often live-edit traffic may trigger a durable persist per
# room (Phase 19.5 -- found by scripts/load_test.py, not hypothetically):
# persisting is a full-document `to_bytes()` + store write, so doing it
# per accepted ops *message* made per-message cost grow with document
# size -- at 50 clients x 5 ops/s the event loop drowned in snapshot
# serialization (12s mean op latency, keepalive timeouts). Debouncing to
# at most one persist per interval (plus a trailing flush, so the last
# edit is never left unpersisted for more than the interval) bounds that
# cost independently of op rate. `0` restores persist-per-message.
# Explicit saves, imports, AI generation, and graceful shutdown still
# persist unconditionally -- see Room.persist_debounced.
PERSIST_MIN_INTERVAL_SECONDS = float(os.environ.get("CRDT_CAD_PERSIST_MIN_INTERVAL_SECONDS", "0.5"))
REPO_ROOT = Path(__file__).resolve().parents[3]
# REPO_ROOT only makes sense for an editable/dev install where this file
# lives at its original source path. A regular `pip install .` (e.g. the
# Docker image) copies the package into site-packages, where parents[3]
# points nowhere useful -- CRDT_CAD_STATIC_DIR lets a real deployment say
# explicitly where the demo assets were placed (the Dockerfile sets it).
DEMO_STATIC_DIR = Path(os.environ.get("CRDT_CAD_STATIC_DIR", str(REPO_ROOT / "demo" / "static")))
DB_PATH = os.environ.get("CRDT_CAD_DB_PATH", str(REPO_ROOT / "data" / "crdt_cad.db"))
DATABASE_URL = os.environ.get("CRDT_CAD_DATABASE_URL")
# Part 6 P4: per-signed-in-user quotas, accounts mode only -- 0 (the
# default) means unlimited, so a deployment that never sets these stays
# exactly as unbounded as before this phase existed.
QUOTA_GENERATIONS_PER_DAY = int(os.environ.get("CRDT_CAD_QUOTA_GENERATIONS_PER_DAY", "0"))
QUOTA_SHARE_LINKS_PER_DAY = int(os.environ.get("CRDT_CAD_QUOTA_SHARE_LINKS_PER_DAY", "0"))
QUOTA_OWNED_DOCUMENTS = int(os.environ.get("CRDT_CAD_QUOTA_OWNED_DOCUMENTS", "0"))

# CRDT_CAD_DATABASE_URL opts into Postgres -- the one persistence backend
# that lets more than one server *process* share room state (see
# PostgresStore's docstring). Unset (the default), every room lives in
# the per-process SQLite file at DB_PATH, same as before this existed.
store: DocumentStore = PostgresStore(DATABASE_URL) if DATABASE_URL else SQLiteStore(DB_PATH)

# CRDT_CAD_REDIS_URL opts into cross-process broadcast fan-out (see
# pubsub.py) -- the other half of true horizontal scaling alongside
# PostgresStore above. Unset (the default), this is None and
# Room.broadcast never touches Redis at all.
redis_client = pubsub.create_redis_client()


class Room:
    """One collaboratively-edited document (of whichever kind) + its clients.

    Hydrates from the durable store on first access if a snapshot for
    this room id already exists (e.g. the server restarted), so rooms
    survive restarts transparently.
    """

    def __init__(
        self,
        room_id: str,
        kind: str,
        doc_class,
        op_from_dict: Callable[[dict], object],
        store: DocumentStore,
        redis_client=None,
        _origin: str | None = None,
    ) -> None:
        self.room_id = room_id
        self.kind = kind
        self.doc_class = doc_class
        self.op_from_dict = op_from_dict
        self.store = store
        self.redis_client = redis_client
        # Real usage always defaults to the process-wide pubsub.PROCESS_ID.
        # The override exists solely so a single-process test can
        # construct two Room instances that behave, for fan-out purposes,
        # like they belong to two different server processes sharing one
        # Redis -- see tests/test_redis_fanout.py.
        self._origin = _origin or pubsub.PROCESS_ID
        self.clock = LamportClock(actor=f"__server__:{kind}:{room_id}")

        persisted = store.load(kind, room_id)
        if persisted:
            self.doc = doc_class.from_bytes(self.clock, persisted)
            logger.info("room %s/%s: hydrated from persisted snapshot (%d bytes)", kind, room_id, len(persisted))
        else:
            self.doc = doc_class(self.clock)
        # Part 6 P2: true only for the very first Room object ever
        # constructed for this room id in this server's lifetime (a
        # RoomManager caches instances, so later opens of the same room
        # reuse this same object and never see True again) *and* there
        # was no prior snapshot -- i.e. this room is genuinely brand new,
        # not a pre-existing room accounts mode was merely turned on for
        # later. `_serve_room` uses this to claim ownership for whichever
        # signed-in user opens it first; a room opened anonymously, or
        # already claimed, is never auto-claimed.
        self.was_freshly_created = persisted is None

        self.clients: dict[str, WebSocket] = {}
        # Phase 17: each connected actor's role ("editor" or "viewer"),
        # keyed the same as `clients` -- populated in `_serve_room` from
        # the "hello" token, consulted in `_handle_message` to reject an
        # `ops` message from a read-only viewer connection.
        self.client_roles: dict[str, str] = {}
        # Part 6 P5: the signed-in user (or None) behind each connected
        # actor, keyed the same as `clients`/`client_roles` -- lets
        # `_handle_message` attribute a comment's @mentions and this
        # room's activity-log entries to a real account without
        # re-resolving the session cookie on every message (auth is
        # only ever checked once, at "hello" time, same as `role` is).
        self.client_users: dict[str, Optional[dict]] = {}
        self._snapshot_task: asyncio.Task | None = None
        self._redis_task: asyncio.Task | None = None
        self._deferred_persist: asyncio.Task | None = None
        self._last_persist = 0.0  # monotonic; 0.0 => first persist is always immediate
        self._dirty_since_snapshot = False
        self._dirty_since_version = False
        self._last_version_checkpoint = time.monotonic()
        self.ops_rate_limiter = security.new_room_ops_bucket()

    def mark_dirty(self) -> None:
        self._dirty_since_snapshot = True
        self._dirty_since_version = True

    def persist(self) -> None:
        self.store.save(self.kind, self.room_id, self.doc.to_bytes())

    async def persist_async(self) -> None:
        """Persist off the event loop thread, awaited inline rather than
        fire-and-forget: a stray ``asyncio.create_task`` per ops batch
        would leave an unbounded, untracked number of background tasks
        running (real resource-leak risk in any long-lived deployment,
        and it made the test suite hang at interpreter shutdown waiting
        for the default thread pool to drain). One persist is cheap; what
        is *not* cheap is one per accepted message under load, which is
        why live-edit callers go through :meth:`persist_debounced`."""
        await asyncio.to_thread(self.persist)
        self._last_persist = time.monotonic()

    async def persist_debounced(self) -> None:
        """Rate-bounded persist for live-edit traffic (see
        PERSIST_MIN_INTERVAL_SECONDS for why): persists immediately if the
        last persist is at least the interval old, otherwise schedules a
        single trailing flush for when the interval elapses -- at most one
        deferred task per room ever exists (not one per message, which is
        the unbounded-task trap persist_async's docstring warns about),
        and the trailing flush guarantees the newest state still lands
        durably within one interval of the last edit. Callers where a
        *user* expects durability right now -- explicit save, import, AI
        generation, graceful shutdown -- call persist_async directly and
        are not debounced."""
        if PERSIST_MIN_INTERVAL_SECONDS <= 0:
            await self.persist_async()
            return
        if self._deferred_persist is not None and not self._deferred_persist.done():
            return  # a trailing flush is already scheduled; it will cover this edit
        elapsed = time.monotonic() - self._last_persist
        if elapsed >= PERSIST_MIN_INTERVAL_SECONDS:
            await self.persist_async()
        else:
            self._deferred_persist = asyncio.create_task(
                self._persist_after(PERSIST_MIN_INTERVAL_SECONDS - elapsed)
            )

    async def _persist_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.persist_async()

    async def flush_deferred_persist(self) -> None:
        """Cancels any scheduled trailing flush (the caller is about to
        persist unconditionally, so the deferred one would be redundant
        work at best and a post-shutdown task at worst)."""
        task = self._deferred_persist
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def checkpoint_version(self) -> None:
        """Phase 17 (version history): appends an immutable checkpoint
        snapshot -- distinct from `persist()`'s single overwritten "latest"
        row -- pruned to `security.max_versions_per_room()`. Deliberately
        *not* called from every `persist()` (which fires after nearly
        every accepted ops batch, i.e. on every drag tick): that would
        make "version history" indistinguishable from "every keystroke,"
        defeating the point. Instead this is called at a much coarser
        cadence -- see `_snapshot_loop` (periodic, every
        `VERSION_CHECKPOINT_INTERVAL_SECONDS`, only when something
        actually changed) and the explicit `"save"` message handler
        (an intentional user checkpoint, regardless of the timer)."""
        self.store.save_version(self.kind, self.room_id, self.doc.to_bytes(), keep=security.max_versions_per_room())

    async def checkpoint_version_async(self) -> None:
        await asyncio.to_thread(self.checkpoint_version)
        self._dirty_since_version = False
        self._last_version_checkpoint = time.monotonic()

    def start_snapshot_loop(self) -> None:
        if self._snapshot_task is None or self._snapshot_task.done():
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())

    async def _snapshot_loop(self) -> None:
        try:
            while self.clients:
                await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
                # Skip the broadcast if nothing changed since the last one --
                # a late joiner already gets a fresh snapshot on connect, so
                # this loop exists purely as a self-healing backstop for
                # missed/reordered live ops. An idle room has nothing to
                # heal, so there's nothing to send either way.
                if self.clients and self._dirty_since_snapshot:
                    logger.info(
                        "room %s/%s: broadcasting periodic frontier to %d clients",
                        self.kind, self.room_id, len(self.clients),
                    )
                    # A full snapshot's payload scales with the whole
                    # document regardless of what changed -- for a
                    # self-healing backstop that fires on a timer for every
                    # room, that's O(doc size x clients) of traffic even
                    # when nothing was actually missed. Broadcasting just
                    # the current VectorClock is tiny and lets each client
                    # compare against its own known frontier, requesting a
                    # real delta (via the existing ops_since/"resync" path)
                    # only on an actual mismatch. A full snapshot remains
                    # the response of last resort -- see _handle_message's
                    # "resync" handling for when a client asks for one
                    # outright (no known frontier yet).
                    await self.broadcast({"type": "frontier", "frontier": self.doc.frontier().to_dict()})
                    self._dirty_since_snapshot = False
                if (
                    self.clients
                    and self._dirty_since_version
                    and (time.monotonic() - self._last_version_checkpoint) >= VERSION_CHECKPOINT_INTERVAL_SECONDS
                ):
                    await self.checkpoint_version_async()
        except asyncio.CancelledError:
            pass

    def snapshot_message(self, role: str = "editor") -> dict:
        """`role` (Phase 17) tells *this* connecting client whether it's a
        full editor or a read-only viewer -- see the "role" field in the
        WS protocol docstring above. Defaults to "editor" so every
        internal call site that doesn't care about a specific actor (e.g.
        tests constructing a snapshot directly) keeps today's behavior."""
        return {"type": "snapshot", "doc": self.doc.to_dict(), "frontier": self.doc.frontier().to_dict(), "role": role}

    def _redis_channel(self) -> str:
        return f"room:{self.kind}:{self.room_id}"

    async def broadcast(self, message: dict, exclude: str | None = None) -> None:
        """Delivers `message` to every client on *this* process, and --
        if Redis fan-out is configured -- publishes it so every other
        process subscribed to this room's channel relays it to their own
        local clients too. `exclude` only makes sense locally (a given
        actor's WebSocket lives on exactly one process at a time), so it
        isn't part of the published envelope."""
        await self._deliver_local(message, exclude)
        if self.redis_client is not None:
            envelope = json.dumps({"origin": self._origin, "message": message})
            try:
                await self.redis_client.publish(self._redis_channel(), envelope)
            except Exception:
                logger.exception("room %s/%s: failed to publish to redis", self.kind, self.room_id)

    async def _deliver_local(self, message: dict, exclude: str | None = None) -> None:
        dead = []
        for actor, ws in list(self.clients.items()):
            if actor == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(actor)
        for actor in dead:
            self.clients.pop(actor, None)

    def start_redis_relay_loop(self) -> None:
        if self.redis_client is None:
            return
        if self._redis_task is None or self._redis_task.done():
            self._redis_task = asyncio.create_task(self._redis_relay_loop())

    async def _redis_relay_loop(self) -> None:
        """Subscribes to this room's Redis channel and relays anything
        published by *other* processes to this process's local clients.
        Messages this same process published are recognized via
        `pubsub.PROCESS_ID` and skipped -- `broadcast` already delivered
        them locally, so relaying them again would double-deliver every
        locally-originated op to this process's own clients. Lives only
        as long as the room has local clients, same as `_snapshot_loop`,
        so an idle room's Redis subscription doesn't linger forever.

        Critically, an incoming `"ops"` message isn't *just* forwarded to
        local WebSocket clients -- it's also applied to *this* process's
        own `self.doc`. Skipping that would leave this process's
        server-side document silently stale: any new client that joins
        this process afterwards would get a snapshot missing the other
        process's edits, and this process's own next periodic/explicit
        persist would write that stale state back to the shared store,
        clobbering the newer data the other process already saved. This
        exact gap was caught live (not by a unit test) via a real
        two-process verification run -- see the Redis fan-out section
        in the README."""
        conn = self.redis_client.pubsub()
        channel = self._redis_channel()
        await conn.subscribe(channel)
        try:
            while self.clients:
                raw = await conn.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if raw is None:
                    continue
                try:
                    envelope = json.loads(raw["data"])
                except (TypeError, ValueError):
                    continue
                if envelope.get("origin") == self._origin:
                    continue
                message = envelope["message"]
                if message.get("type") == "ops":
                    touched_topology = False
                    for op_dict in message["ops"]:
                        op = self.op_from_dict(op_dict)
                        self.doc.apply(op)
                        touched_topology = touched_topology or _touches_mesh_topology(op)
                    self.mark_dirty()
                    await self.persist_debounced()
                    await _check_and_broadcast_mesh_validity(self, touched_topology)
                await self._deliver_local(message)
        except asyncio.CancelledError:
            pass
        finally:
            await conn.unsubscribe(channel)
            await conn.aclose()

    async def commit_ops_batched(self, ops: list, actor: str, batch_size: int = 150) -> int:
        """Applies and broadcasts a (potentially large) list of
        already-minted ops in fixed-size chunks, yielding the event loop
        between each one.

        This exists for bulk-insertion sources -- an AI-generated mesh can
        easily be a few thousand ops -- so they arrive at clients as a
        stream of bounded-size WebSocket frames rather than one giant
        message that blocks the relay while it's assembled/sent, and so
        applying them doesn't monopolize the event loop in a single tick
        alongside every other room's traffic. Persists once at the end,
        not per batch: a mid-generation crash just leaves the durable
        snapshot stale (recovered from the next periodic snapshot or the
        in-memory doc, which already has every op applied), not corrupt.
        Returns the number of batches sent.
        """
        batches = 0
        touched_topology = False
        for i in range(0, len(ops), max(1, batch_size)):
            chunk = ops[i : i + batch_size]
            for op in chunk:
                self.doc.apply(op)
                touched_topology = touched_topology or _touches_mesh_topology(op)
            await self.broadcast({"type": "ops", "ops": [op.to_dict() for op in chunk], "from": actor})
            self.mark_dirty()
            batches += 1
            await asyncio.sleep(0)
        if ops:
            await self.persist_async()
            await _check_and_broadcast_mesh_validity(self, touched_topology)
        return batches

    async def commit_ops_grouped_batched(self, object_groups: list[list], actor: str, batch_size: int = 150) -> int:
        """Like :meth:`commit_ops_batched`, but forces a batch boundary
        between every group in `object_groups` (in addition to the usual
        size-based chunking within a group) -- Phase G2's per-object
        staging for scene generation, so a "table with four chairs"
        arrives as one batch per object regardless of how small each
        object's own op count is, while still persisting only once at
        the end rather than once per object."""
        batches = 0
        touched_topology = False
        for group in object_groups:
            for i in range(0, len(group), max(1, batch_size)):
                chunk = group[i : i + batch_size]
                for op in chunk:
                    self.doc.apply(op)
                    touched_topology = touched_topology or _touches_mesh_topology(op)
                await self.broadcast({"type": "ops", "ops": [op.to_dict() for op in chunk], "from": actor})
                self.mark_dirty()
                batches += 1
                await asyncio.sleep(0)
        if any(object_groups):
            await self.persist_async()
            await _check_and_broadcast_mesh_validity(self, touched_topology)
        return batches


class RoomManager:
    def __init__(
        self,
        kind: str,
        doc_class,
        op_from_dict: Callable[[dict], object],
        store: DocumentStore,
        redis_client=None,
    ) -> None:
        self.kind = kind
        self.doc_class = doc_class
        self.op_from_dict = op_from_dict
        self.store = store
        self.redis_client = redis_client
        self.rooms: dict[str, Room] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, room_id: str) -> Room:
        async with self._lock:
            room = self.rooms.get(room_id)
            if room is None:
                if len(self.rooms) >= security.max_rooms_per_server():
                    raise security.RoomLimitExceeded(
                        f"server room limit reached ({security.max_rooms_per_server()})"
                    )
                room = Room(room_id, self.kind, self.doc_class, self.op_from_dict, self.store, self.redis_client)
                self.rooms[room_id] = room
            return room

    def connection_count(self) -> int:
        return sum(len(r.clients) for r in self.rooms.values())


drawing_room_manager = RoomManager("drawing", DrawingDocument, DocOp.from_dict, store, redis_client)
mesh_room_manager = RoomManager("mesh", MeshCRDT, MeshOp.from_dict, store, redis_client)
room_manager = drawing_room_manager  # backwards-compatible alias

# WebSocket close code for a clean, server-initiated shutdown -- the
# standard "Going Away" code (RFC 6455), not one of the private-use
# WS_CLOSE_* codes below (those all mean "don't bother reconnecting");
# common.js's reconnect logic already treats every code except
# WS_CLOSE_UNAUTHORIZED as "reconnect", so no client change was needed.
WS_CLOSE_GOING_AWAY = 1001


async def _graceful_shutdown() -> None:
    """Runs on SIGTERM (via the `lifespan` shutdown phase below) -- what
    makes `kubectl rollout restart`/a `docker stop`/a VM reboot safe. The
    persist is the part that actually matters and is guaranteed: verified
    against a real `docker stop` on a real container (not just a unit
    test) -- an edit sent but never explicitly saved survives the
    container being SIGTERM'd and a fresh container reusing the same
    volume picking the room back up.

    The `ws.close(code=...)` calls below are best-effort, not the primary
    guarantee: in that same real-container test, uvicorn's own WebSocket
    shutdown handling closed the connection with code 1012 ("Service
    Restart") *before* this hook's own close() call took effect --
    whichever side sends the first CLOSE frame wins, and uvicorn's own
    shutdown path won that race. 1012 already isn't
    WS_CLOSE_UNAUTHORIZED, so common.js's reconnect logic still retries
    correctly either way; this hook's own close() is a backstop for
    whatever ASGI server/version combination *doesn't* close things
    itself, not something to rely on for the close code a client
    actually observes today."""
    for manager in (drawing_room_manager, mesh_room_manager):
        for room in list(manager.rooms.values()):
            await room.flush_deferred_persist()
            await room.persist_async()
            for ws in list(room.clients.values()):
                try:
                    await ws.close(code=WS_CLOSE_GOING_AWAY)
                except Exception:
                    pass  # already disconnecting/disconnected -- nothing to clean up


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await _graceful_shutdown()


app = FastAPI(title="crdt-cad collaboration server", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=security.cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Part 6 P1: user accounts -- entirely inert in the default tokens mode
# (the router still mounts so /api/auth/me can answer {"mode": "tokens"},
# but no schema is created and every sign-in route 404s). The signed-
# cookie SessionMiddleware exists solely to hold OAuth's state/nonce
# between the redirect out and the callback back (authlib requires it);
# it is not the user session, which is server-side (see auth.py).
app.include_router(auth.router)
if auth.accounts_enabled():
    _auth_secret = os.environ.get("CRDT_CAD_SECRET")
    if _auth_secret:
        from starlette.middleware.sessions import SessionMiddleware

        app.add_middleware(SessionMiddleware, secret_key=_auth_secret, same_site="lax")
    else:
        logger.warning(
            "CRDT_CAD_AUTH_MODE=accounts but CRDT_CAD_SECRET is unset -- "
            "sign-in routes will answer 503 until it is configured"
        )

# Kubernetes sets HOSTNAME to the pod name automatically; falls back to
# socket.gethostname() so this is still meaningful outside a cluster.
# Exists so multi-replica fan-out (Phase 18.2's Mode B) can be verified by
# reading which pod actually answered a given request, rather than trusting
# the Service's load-balancing blindly.
POD_NAME = os.environ.get("HOSTNAME") or socket.gethostname()


@app.middleware("http")
async def _add_served_by_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = POD_NAME
    return response


class _NoCacheStaticFiles(StaticFiles):
    """Forces browsers to always revalidate (via the ETag/Last-Modified
    conditional GET Starlette already sets) instead of trusting a local
    heuristic cache for these files. Without this, editing demo JS/CSS
    during active development can silently keep serving an old version
    to an already-open tab even after a plain refresh -- there's no
    build step or cache-busted filename to force a fetch otherwise."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if DEMO_STATIC_DIR.exists():
    app.mount("/static", _NoCacheStaticFiles(directory=str(DEMO_STATIC_DIR)), name="static")


@app.get("/")
async def home() -> FileResponse:
    """Phase 17: the workspace home page -- lists existing rooms (both
    kinds) via `DocumentStore.list_rooms_detailed`. The 2D demo itself
    moved to `/2d` to make room for this; `/3d` is unchanged."""
    return FileResponse(str(DEMO_STATIC_DIR / "home.html"))


@app.get("/2d")
async def index_2d() -> FileResponse:
    return FileResponse(str(DEMO_STATIC_DIR / "index.html"))


@app.get("/3d")
async def index_3d() -> FileResponse:
    return FileResponse(str(DEMO_STATIC_DIR / "mesh3d.html"))


@app.get("/admin")
async def index_admin() -> FileResponse:
    """Static shell for the operator admin panel (Part 6 P4) -- gated
    client-side by rendering nothing useful until /api/auth/me reports
    is_platform_admin; every actual admin action goes through the
    /api/admin/* routes, which enforce require_platform_admin() again
    server-side regardless of what this page shows."""
    return FileResponse(str(DEMO_STATIC_DIR / "admin.html"))


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(str(DEMO_STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "drawing_rooms": len(drawing_room_manager.rooms),
        "mesh_rooms": len(mesh_room_manager.rooms),
        "connections": drawing_room_manager.connection_count() + mesh_room_manager.connection_count(),
        "served_by": POD_NAME,
    }


@app.get("/metrics")
async def prometheus_metrics() -> Response:
    metrics.rooms_gauge.set(len(drawing_room_manager.rooms) + len(mesh_room_manager.rooms))
    metrics.active_connections.set(
        drawing_room_manager.connection_count() + mesh_room_manager.connection_count()
    )
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)


@app.get("/api/rooms")
async def list_drawing_rooms() -> dict:
    return {"rooms": store.list_rooms("drawing")}


@app.get("/api/mesh-rooms")
async def list_mesh_rooms() -> dict:
    return {"rooms": store.list_rooms("mesh")}


# ---------------------------------------------------------------------------
# Optional shared-secret room auth (opt-in via CRDT_CAD_SECRET; see
# crdt_cad.server.security for the full design rationale)
# ---------------------------------------------------------------------------


class AuthRequiredResponse(BaseModel):
    required: bool


@app.get("/api/auth/required", response_model=AuthRequiredResponse)
async def auth_required() -> AuthRequiredResponse:
    return AuthRequiredResponse(required=security.auth_enabled())


class TokenRequest(BaseModel):
    secret: str
    kind: str
    room_id: str


class TokenResponse(BaseModel):
    token: str


@app.post("/api/auth/token", response_model=TokenResponse)
async def issue_token(req: TokenRequest) -> TokenResponse:
    if not security.auth_enabled():
        raise HTTPException(status_code=400, detail="authentication is not enabled on this server")
    if req.kind not in ("drawing", "mesh"):
        raise HTTPException(status_code=400, detail="kind must be 'drawing' or 'mesh'")
    if not security.secret_matches(req.secret):
        raise HTTPException(status_code=403, detail="incorrect secret")
    return TokenResponse(token=security.mint_room_token(req.kind, req.room_id))


def _extract_token(request: Request) -> Optional[str]:
    token = request.query_params.get("token")
    if token:
        return token
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return None


_ROLE_RANK = {"viewer": 0, "commenter": 1, "editor": 2, "owner": 3}


def _better_role(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Whichever of two roles grants more access; None only if both are."""
    candidates = [r for r in (a, b) if r is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda r: _ROLE_RANK.get(r, -1))


def _account_role_for_room(kind: str, room_id: str, user: Optional[dict]) -> Optional[str]:
    """The role Part 6 P2/P3 ownership/grants/org-membership grants for
    this room, purely from account identity -- independent of any room
    token. None when accounts mode is off, or the room has no owner yet
    (every room predating this phase, or one no signed-in user has ever
    opened -- see ``Room.was_freshly_created``), since there's no
    account-based permission to check in either case. A room *with* an
    owner and visibility ``"public"`` is open to everyone, signed in or
    not, the same way an unowned room always has been.

    Precedence, most specific first: the personal owner; an explicit
    per-room grant (Part 6 P2 -- always wins even on an org-owned room,
    since it's the more specific instruction); active org membership on
    an org-owned room (Part 6 P3 -- an org admin manages it like an
    owner, an ordinary member gets editor, this is what makes "only
    members of the same team can see this" real); finally the room's
    own visibility default."""
    if not auth.accounts_enabled():
        return None
    ownership = auth.get_account_store().get_room_ownership(kind, room_id)
    if ownership is None:
        return None
    if user is not None:
        if user["user_id"] == ownership["owner_user_id"]:
            return "owner"
        grant = auth.get_account_store().get_room_grant(kind, room_id, user["user_id"])
        if grant is not None:
            return grant
        org_id = ownership.get("owner_org_id")
        if org_id is not None:
            membership = auth.get_account_store().get_org_membership(org_id, user["user_id"])
            if membership is not None and membership["status"] == "active":
                return "owner" if membership["role"] == "admin" else "editor"
    return "editor" if ownership["visibility"] == "public" else None


def _effective_role(kind: str, room_id: str, token: Optional[str], user: Optional[dict]) -> Optional[str]:
    """The more permissive of the token-based role (the pre-P2 system,
    entirely unchanged) and the account-based role (Part 6 P2) -- either
    alone can grant access, so an already-distributed share-link token
    keeps working even on a room that's since been made private, and a
    signed-in owner/grantee gets in with no token at all. When accounts
    mode is off, or a room has never been claimed by any account, this
    reduces to exactly the token-only behavior every deployment already
    has today."""
    return _better_role(security.token_role(token, kind, room_id), _account_role_for_room(kind, room_id, user))


def require_room_access(kind: str):
    """A FastAPI dependency gating one REST endpoint's ``{room_id}`` behind
    a valid room token *or* (Part 6 P2) account-based permission -- a
    no-op (always passes) when neither ``CRDT_CAD_SECRET`` nor accounts
    mode is configured, matching every other auth check in this module.
    FastAPI binds the returned callable's ``room_id`` parameter from the
    route's own path parameter automatically. Accepts *any* role --
    read-only endpoints (export, thumbnail, version history) use this;
    endpoints that mutate the room use :func:`require_editor_access`
    instead."""

    async def _dep(room_id: str, request: Request) -> None:
        token = _extract_token(request)
        user = auth.current_user(request)
        if _effective_role(kind, room_id, token, user) is None:
            raise HTTPException(status_code=401, detail="missing or invalid room token")

    return _dep


def require_editor_access(kind: str):
    """Like :func:`require_room_access`, but additionally refuses a
    **viewer** or **commenter** role (403) -- for REST endpoints that
    mutate a room (import, generate, rename, restore, minting further
    share links), so a read-only share link recipient or comment-only
    grantee can't bypass the WS-level ops rejection (Phase 17 / Part 6
    P2) just by calling these directly."""

    async def _dep(room_id: str, request: Request) -> None:
        token = _extract_token(request)
        user = auth.current_user(request)
        role = _effective_role(kind, room_id, token, user)
        if role is None:
            raise HTTPException(status_code=401, detail="missing or invalid room token")
        if role in ("viewer", "commenter"):
            raise HTTPException(status_code=403, detail="editor access required -- this token is read-only")

    return _dep


def require_owner_access(kind: str):
    """Part 6 P2: only a room's owner may change its visibility or manage
    per-user grants -- editor access isn't enough. 404s (not 403) when
    accounts mode is off or the room has no owner at all, since there is
    nothing to manage in either case, not merely something forbidden.
    Part 6 P3: once a room is org-owned, any active *admin* of that org
    manages it exactly like the owner would -- the whole point of
    transferring a document to an org is that its sharing no longer
    depends on one specific person still being around."""

    async def _dep(room_id: str, request: Request) -> None:
        if not auth.accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
        ownership = auth.get_account_store().get_room_ownership(kind, room_id)
        if ownership is None:
            raise HTTPException(status_code=404, detail="this room has no owner to manage")
        user = auth.current_user(request)
        if user is not None:
            if user["user_id"] == ownership["owner_user_id"]:
                return
            org_id = ownership.get("owner_org_id")
            if org_id is not None:
                membership = auth.get_account_store().get_org_membership(org_id, user["user_id"])
                if membership is not None and membership["status"] == "active" and membership["role"] == "admin":
                    return
        raise HTTPException(
            status_code=403, detail="only this room's owner (or an admin of its organization) can manage sharing"
        )

    return _dep


# ---------------------------------------------------------------------------
# Workspace: rooms as projects (Phase 17)
# ---------------------------------------------------------------------------


class RoomSummary(BaseModel):
    kind: str  # "drawing" | "mesh"
    room_id: str
    display_name: Optional[str] = None
    updated_at: float
    # Part 6 P2, all None for a room no signed-in user has ever claimed
    # (every room predating this phase, or one only ever opened
    # anonymously) -- those keep exactly today's always-listed behavior.
    visibility: Optional[str] = None  # "private" | "link" | "public"
    owner_display_name: Optional[str] = None
    your_role: Optional[str] = None
    org_name: Optional[str] = None  # Part 6 P3, set only once a room's been transferred to an org


@app.get("/api/workspace/rooms", response_model=list[RoomSummary])
async def list_workspace_rooms(request: Request) -> list[RoomSummary]:
    """Backs the home page's room list. A room with no owner is listed
    for everyone, ungated by any room token -- like the pre-existing
    `/api/rooms`/`/api/mesh-rooms`, a room id/kind/last-modified time
    isn't itself scoped to one room the way its *contents* are, so
    there's nothing here for `require_room_access` to check against;
    its content, thumbnail, and rename action remain individually
    token-gated (see below) -- documented in the README as an accepted,
    honest scope boundary, not a silent gap. A room WITH an owner (Part
    6 P2) is different: `visibility="private"`/`"link"` rooms are
    dropped from the list entirely for anyone who isn't the owner or an
    explicit grantee -- existence itself, not just content, is what
    "private" means once a room has an account attached to it."""
    user = auth.current_user(request)
    accounts_on = auth.accounts_enabled()
    rows: list[RoomSummary] = []
    for kind in ("drawing", "mesh"):
        for row in store.list_rooms_detailed(kind):
            summary = RoomSummary(kind=kind, **row)
            if accounts_on:
                ownership = auth.get_account_store().get_room_ownership(kind, summary.room_id)
                if ownership is not None:
                    role = _account_role_for_room(kind, summary.room_id, user)
                    if role is None:
                        continue
                    summary.visibility = ownership["visibility"]
                    summary.your_role = role
                    org_id = ownership.get("owner_org_id")
                    if org_id is not None:
                        org = auth.get_account_store().get_org(org_id)
                        summary.org_name = org["name"] if org else None
                    else:
                        owner = auth.get_account_store().get_user(ownership["owner_user_id"])
                        summary.owner_display_name = owner["display_name"] if owner else None
            rows.append(summary)
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows


class VisibilityRequest(BaseModel):
    visibility: str  # "private" | "link" | "public"


class GrantRequest(BaseModel):
    email: str
    role: str  # "editor" | "commenter" | "viewer"


_VALID_VISIBILITIES = {"private", "link", "public"}
_VALID_GRANT_ROLES = {"editor", "commenter", "viewer"}


def _enforce_daily_quota(user: Optional[dict], kind: str, limit: int, label: str) -> None:
    """Soft per-user daily cap (Part 6 P4). A no-op for token-only rooms
    (``user`` is None) and whenever the deployment leaves ``limit`` at
    its default of 0 -- quotas are opt-in, never a surprise regression
    for an existing accounts-mode deployment that never configured one.
    """
    if user is None or limit <= 0:
        return
    day = date.today().isoformat()
    current = auth.get_account_store().get_quota_usage(user["user_id"], kind, day)
    if current >= limit:
        raise HTTPException(
            status_code=429, detail=f"daily {label} quota exceeded ({limit}/day) -- resets tomorrow"
        )
    auth.get_account_store().increment_quota_usage(user["user_id"], kind, day)


async def _get_sharing(kind: str, room_id: str) -> dict:
    ownership = auth.get_account_store().get_room_ownership(kind, room_id)
    if ownership is None:
        raise HTTPException(status_code=404, detail="this room has no owner")
    owner = auth.get_account_store().get_user(ownership["owner_user_id"])
    return {
        "visibility": ownership["visibility"],
        "owner": (
            {"user_id": owner["user_id"], "email": owner["email"], "display_name": owner["display_name"]}
            if owner else None
        ),
        "grants": auth.get_account_store().list_room_grants(kind, room_id),
    }


async def _set_visibility(kind: str, room_id: str, visibility: str) -> dict:
    if visibility not in _VALID_VISIBILITIES:
        raise HTTPException(status_code=400, detail=f"visibility must be one of {sorted(_VALID_VISIBILITIES)}")
    auth.get_account_store().set_room_visibility(kind, room_id, visibility)
    return {"ok": True, "visibility": visibility}


async def _grant_room(kind: str, room_id: str, req: GrantRequest) -> dict:
    if req.role not in _VALID_GRANT_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(_VALID_GRANT_ROLES)}")
    user = auth.get_account_store().create_or_get_user(req.email)
    auth.get_account_store().set_room_grant(kind, room_id, user["user_id"], req.role)
    return {"ok": True, "user_id": user["user_id"], "email": user["email"], "role": req.role}


async def _revoke_grant(kind: str, room_id: str, user_id: str) -> dict:
    auth.get_account_store().revoke_room_grant(kind, room_id, user_id)
    return {"ok": True}


@app.get("/api/rooms/{room_id}/sharing", dependencies=[Depends(require_owner_access("drawing"))])
async def get_drawing_sharing(room_id: str) -> dict:
    return await _get_sharing("drawing", room_id)


@app.post("/api/rooms/{room_id}/visibility", dependencies=[Depends(require_owner_access("drawing"))])
async def set_drawing_visibility(room_id: str, req: VisibilityRequest) -> dict:
    return await _set_visibility("drawing", room_id, req.visibility)


@app.post("/api/rooms/{room_id}/grant", dependencies=[Depends(require_owner_access("drawing"))])
async def grant_drawing_room(room_id: str, req: GrantRequest) -> dict:
    return await _grant_room("drawing", room_id, req)


@app.delete("/api/rooms/{room_id}/grant/{user_id}", dependencies=[Depends(require_owner_access("drawing"))])
async def revoke_drawing_grant(room_id: str, user_id: str) -> dict:
    return await _revoke_grant("drawing", room_id, user_id)


@app.get("/api/mesh/{room_id}/sharing", dependencies=[Depends(require_owner_access("mesh"))])
async def get_mesh_sharing(room_id: str) -> dict:
    return await _get_sharing("mesh", room_id)


@app.post("/api/mesh/{room_id}/visibility", dependencies=[Depends(require_owner_access("mesh"))])
async def set_mesh_visibility(room_id: str, req: VisibilityRequest) -> dict:
    return await _set_visibility("mesh", room_id, req.visibility)


@app.post("/api/mesh/{room_id}/grant", dependencies=[Depends(require_owner_access("mesh"))])
async def grant_mesh_room(room_id: str, req: GrantRequest) -> dict:
    return await _grant_room("mesh", room_id, req)


@app.delete("/api/mesh/{room_id}/grant/{user_id}", dependencies=[Depends(require_owner_access("mesh"))])
async def revoke_mesh_grant(room_id: str, user_id: str) -> dict:
    return await _revoke_grant("mesh", room_id, user_id)


# ---------------------------------------------------------------------------
# Organizations and teams (Part 6, Phase P3)
# ---------------------------------------------------------------------------


def require_org_membership():
    """Any active member (admin or plain member) may view an org's own
    detail page -- read access only, matching require_room_access's
    "any role gets in" shape."""

    async def _dep(org_id: str, request: Request) -> None:
        if not auth.accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
        user = auth.current_user(request)
        membership = auth.get_account_store().get_org_membership(org_id, user["user_id"]) if user else None
        if membership is None or membership["status"] != "active":
            raise HTTPException(status_code=403, detail="not a member of this organization")

    return _dep


def require_org_admin():
    """Membership management, invites, and per-org defaults are
    admin-only -- an ordinary member can see the roster but not change
    it, matching the brief's admin/member split."""

    async def _dep(org_id: str, request: Request) -> None:
        if not auth.accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
        user = auth.current_user(request)
        membership = auth.get_account_store().get_org_membership(org_id, user["user_id"]) if user else None
        if membership is None or membership["status"] != "active" or membership["role"] != "admin":
            raise HTTPException(status_code=403, detail="organization admin access required")

    return _dep


class CreateOrgRequest(BaseModel):
    name: str


class OrgSummary(BaseModel):
    org_id: str
    name: str
    role: str


@app.post("/api/orgs")
async def create_org(req: CreateOrgRequest, request: Request) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    return auth.get_account_store().create_org(name, user["user_id"])


@app.get("/api/orgs", response_model=list[OrgSummary])
async def list_my_orgs(request: Request) -> list[OrgSummary]:
    if not auth.accounts_enabled():
        return []
    user = auth.current_user(request)
    if user is None:
        return []
    return [OrgSummary(**o) for o in auth.get_account_store().list_orgs_for_user(user["user_id"])]


@app.get("/api/orgs/{org_id}", dependencies=[Depends(require_org_membership())])
async def get_org_detail(org_id: str) -> dict:
    org = auth.get_account_store().get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return {**org, "members": auth.get_account_store().list_org_members(org_id)}


class InviteOrgMemberRequest(BaseModel):
    email: str
    role: str = "member"  # "admin" | "member"


@app.post("/api/orgs/{org_id}/invite", dependencies=[Depends(require_org_admin())])
async def invite_org_member(org_id: str, req: InviteOrgMemberRequest) -> dict:
    if req.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
    store = auth.get_account_store()
    # Part 6 P6: a free-plan seat cap, only when this deployment has
    # actually configured billing -- a deployment that never sets
    # CRDT_CAD_STRIPE_SECRET_KEY keeps P3's original unlimited org
    # membership, byte-for-byte. Re-inviting an *existing* active
    # member (e.g. just to change their role) never counts as growing
    # the roster, so that's exempted from the cap.
    if billing.billing_enabled():
        org = store.get_org(org_id)
        limit = billing.seat_limit_for_plan(org["billing_plan"]) if org else None
        if limit is not None:
            invitee = store.get_user_by_email(req.email)
            membership = store.get_org_membership(org_id, invitee["user_id"]) if invitee else None
            already_active = membership is not None and membership["status"] == "active"
            active_count = sum(1 for m in store.list_org_members(org_id) if m["status"] == "active")
            if active_count >= limit and not already_active:
                raise HTTPException(
                    status_code=402,
                    detail=f"this organization's free plan is limited to {limit} members -- upgrade to add more",
                )
    user, status = store.invite_org_member(org_id, req.email, req.role)
    return {"user_id": user["user_id"], "email": user["email"], "role": req.role, "status": status}


class SetOrgMemberRoleRequest(BaseModel):
    role: str  # "admin" | "member"


@app.post("/api/orgs/{org_id}/members/{user_id}/role", dependencies=[Depends(require_org_admin())])
async def set_org_member_role(org_id: str, user_id: str, req: SetOrgMemberRoleRequest) -> dict:
    if req.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
    if req.role == "member":
        membership = auth.get_account_store().get_org_membership(org_id, user_id)
        if (
            membership and membership["role"] == "admin"
            and auth.get_account_store().count_org_admins(org_id) <= 1
        ):
            raise HTTPException(status_code=400, detail="cannot demote the organization's last admin")
    if not auth.get_account_store().set_org_member_role(org_id, user_id, req.role):
        raise HTTPException(status_code=404, detail="member not found")
    return {"ok": True}


@app.delete("/api/orgs/{org_id}/members/{user_id}", dependencies=[Depends(require_org_admin())])
async def remove_org_member(org_id: str, user_id: str) -> dict:
    membership = auth.get_account_store().get_org_membership(org_id, user_id)
    if membership and membership["role"] == "admin" and auth.get_account_store().count_org_admins(org_id) <= 1:
        raise HTTPException(status_code=400, detail="cannot remove the organization's last admin")
    auth.get_account_store().remove_org_member(org_id, user_id)
    return {"ok": True}


@app.post("/api/orgs/{org_id}/leave")
async def leave_org(org_id: str, request: Request) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    membership = auth.get_account_store().get_org_membership(org_id, user["user_id"])
    if membership is None:
        raise HTTPException(status_code=404, detail="not a member of this organization")
    if membership["role"] == "admin" and auth.get_account_store().count_org_admins(org_id) <= 1:
        raise HTTPException(
            status_code=400, detail="cannot leave -- you are the last admin; promote someone else first"
        )
    auth.get_account_store().remove_org_member(org_id, user["user_id"])
    return {"ok": True}


class OrgDefaultsRequest(BaseModel):
    default_visibility: Optional[str] = None
    allowed_share_link_roles: Optional[list[str]] = None


@app.post("/api/orgs/{org_id}/defaults", dependencies=[Depends(require_org_admin())])
async def set_org_defaults(org_id: str, req: OrgDefaultsRequest) -> dict:
    if req.default_visibility is not None and req.default_visibility not in _VALID_VISIBILITIES:
        raise HTTPException(
            status_code=400, detail=f"default_visibility must be one of {sorted(_VALID_VISIBILITIES)}"
        )
    if req.allowed_share_link_roles is not None and not set(req.allowed_share_link_roles) <= {"viewer", "editor"}:
        raise HTTPException(status_code=400, detail="allowed_share_link_roles must be a subset of ['viewer', 'editor']")
    auth.get_account_store().set_org_defaults(org_id, req.default_visibility, req.allowed_share_link_roles)
    return {"ok": True}


class OrgSSORequest(BaseModel):
    issuer: str
    client_id: str
    client_secret: str
    domain: str


@app.get("/api/orgs/{org_id}/sso", dependencies=[Depends(require_org_admin())])
async def get_org_sso(org_id: str) -> dict:
    org = auth.get_account_store().get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    # The client secret is write-only from the API's point of view -- an
    # admin can tell it's configured, never read it back.
    return {
        "configured": bool(org.get("sso_issuer") and org.get("sso_client_id") and org.get("sso_client_secret")),
        "issuer": org.get("sso_issuer"),
        "domain": org.get("sso_domain"),
        "start_url": f"/api/auth/sso/{org_id}/start" if org.get("sso_issuer") else None,
    }


@app.post("/api/orgs/{org_id}/sso", dependencies=[Depends(require_org_admin())])
async def set_org_sso(org_id: str, req: OrgSSORequest) -> dict:
    issuer = req.issuer.strip()
    client_id = req.client_id.strip()
    client_secret = req.client_secret.strip()
    domain = req.domain.strip().lower()
    if not (issuer.startswith("https://") or issuer.startswith("http://")):
        raise HTTPException(status_code=400, detail="issuer must be a URL")
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required")
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", domain):
        raise HTTPException(status_code=400, detail="domain must look like a bare domain, e.g. example.com")
    existing = auth.get_account_store().get_org_by_sso_domain(domain)
    if existing is not None and existing["org_id"] != org_id:
        raise HTTPException(status_code=409, detail="another organization has already claimed this domain")
    auth.get_account_store().set_org_sso(org_id, issuer, client_id, client_secret, domain)
    auth.forget_org_oidc_client(org_id)
    return {"ok": True}


@app.delete("/api/orgs/{org_id}/sso", dependencies=[Depends(require_org_admin())])
async def clear_org_sso(org_id: str) -> dict:
    auth.get_account_store().set_org_sso(org_id, None, None, None, None)
    auth.forget_org_oidc_client(org_id)
    return {"ok": True}


# -- billing (Part 6 P6) -------------------------------------------------------


@app.get("/api/orgs/{org_id}/billing", dependencies=[Depends(require_org_membership())])
async def get_org_billing(org_id: str) -> dict:
    org = auth.get_account_store().get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return {
        "billing_enabled": billing.billing_enabled(),
        "plan": org["billing_plan"],
        "status": org["billing_status"],
        "seat_limit": billing.seat_limit_for_plan(org["billing_plan"]) if billing.billing_enabled() else None,
        "has_customer": bool(org.get("billing_customer_id")),
    }


@app.post("/api/orgs/{org_id}/billing/checkout", dependencies=[Depends(require_org_admin())])
async def start_org_checkout(org_id: str, request: Request) -> dict:
    if not billing.billing_enabled():
        raise HTTPException(status_code=404, detail="billing is not configured on this server")
    store = auth.get_account_store()
    org = store.get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    base = str(request.base_url).rstrip("/")
    try:
        url = billing.create_checkout_session(
            store, org, success_url=f"{base}/?billing=success", cancel_url=f"{base}/?billing=cancelled",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"checkout_url": url}


@app.post("/api/orgs/{org_id}/billing/portal", dependencies=[Depends(require_org_admin())])
async def start_org_billing_portal(org_id: str, request: Request) -> dict:
    if not billing.billing_enabled():
        raise HTTPException(status_code=404, detail="billing is not configured on this server")
    org = auth.get_account_store().get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    base = str(request.base_url).rstrip("/")
    try:
        url = billing.create_portal_session(org, return_url=f"{base}/")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"portal_url": url}


@app.post("/api/billing/webhook")
async def stripe_webhook(request: Request) -> dict:
    """Stripe calls this directly -- no session/room auth applies here
    at all; the webhook signature (verified against
    CRDT_CAD_STRIPE_WEBHOOK_SECRET) is the entire trust boundary, which
    is why this reads the raw body instead of a parsed Pydantic model
    (signature verification needs the exact bytes Stripe signed)."""
    if not billing.billing_enabled():
        raise HTTPException(status_code=404, detail="billing is not configured on this server")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = billing.verify_webhook(payload, sig_header)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid webhook signature: {exc}") from exc
    billing.handle_webhook_event(auth.get_account_store(), event)
    return {"ok": True}


# -- GDPR data export / account deletion (Part 6 P7) ---------------------------


@app.get("/api/account/export")
async def export_account_data(request: Request) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    store = auth.get_account_store()
    return {
        "exported_at": time.time(),
        "profile": {k: v for k, v in user.items() if k != "disabled"},
        "owned_rooms": store.list_owned_rooms(user["user_id"]),
        "granted_rooms": store.list_granted_rooms(user["user_id"]),
        "organizations": store.list_orgs_for_user(user["user_id"]),
        "notifications": store.list_notifications(user["user_id"]),
    }


@app.post("/api/account/delete")
async def delete_account(request: Request, response: Response) -> dict:
    """The 'right to erasure' -- see `AccountStore.delete_user_account`'s
    docstring for exactly what this does and doesn't remove (a room they
    personally owned is released, not destroyed; their past comments/
    edits in room history are untouched, since those only ever held a
    display-name snapshot, never a live reference to this account)."""
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    store = auth.get_account_store()
    for org in store.list_orgs_for_user(user["user_id"]):
        if org["role"] == "admin" and store.count_org_admins(org["org_id"]) <= 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"you are the last admin of '{org['name']}' -- promote someone else "
                    "or delete the organization first"
                ),
            )
    store.delete_user_account(user["user_id"])
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


# -- abuse reports (Part 6 P7) --------------------------------------------------


class ReportRequest(BaseModel):
    reason: str
    details: str = ""


async def _create_report(kind: str, room_id: str, request: Request, req: ReportRequest) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required")
    user = auth.current_user(request)
    report = auth.get_account_store().create_abuse_report(
        user["user_id"] if user else None, kind, room_id, req.reason.strip(), req.details.strip(),
    )
    return {"ok": True, "report_id": report["report_id"]}


@app.post("/api/rooms/{room_id}/report", dependencies=[Depends(require_room_access("drawing"))])
async def report_drawing_room(room_id: str, req: ReportRequest, request: Request) -> dict:
    return await _create_report("drawing", room_id, request, req)


@app.post("/api/mesh/{room_id}/report", dependencies=[Depends(require_room_access("mesh"))])
async def report_mesh_room(room_id: str, req: ReportRequest, request: Request) -> dict:
    return await _create_report("mesh", room_id, request, req)


async def _transfer_room_to_org(kind: str, room_id: str, org_id: str, user: Optional[dict]) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    membership = auth.get_account_store().get_org_membership(org_id, user["user_id"])
    if membership is None or membership["status"] != "active" or membership["role"] != "admin":
        raise HTTPException(status_code=403, detail="only an admin of the target organization can accept a transfer")
    org = auth.get_account_store().get_org(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    if not auth.get_account_store().transfer_room_to_org(kind, room_id, org_id):
        raise HTTPException(status_code=404, detail="this room has no owner to transfer")
    auth.get_account_store().set_room_visibility(kind, room_id, org["default_visibility"])
    return {"ok": True, "owner_org_id": org_id, "visibility": org["default_visibility"]}


class TransferRequest(BaseModel):
    org_id: str


@app.post("/api/rooms/{room_id}/transfer", dependencies=[Depends(require_owner_access("drawing"))])
async def transfer_drawing_room(room_id: str, req: TransferRequest, request: Request) -> dict:
    return await _transfer_room_to_org("drawing", room_id, req.org_id, auth.current_user(request))


@app.post("/api/mesh/{room_id}/transfer", dependencies=[Depends(require_owner_access("mesh"))])
async def transfer_mesh_room(room_id: str, req: TransferRequest, request: Request) -> dict:
    return await _transfer_room_to_org("mesh", room_id, req.org_id, auth.current_user(request))


# -- operator admin panel (Part 6 P4) ------------------------------------------
#
# Gated by CRDT_CAD_ADMIN_EMAILS (see auth.platform_admin_emails), not a
# database flag -- an operator sets the env var, signs in with that
# address, and gets in. No bootstrap chicken-and-egg where the first
# admin has to be granted by an admin who doesn't exist yet.


def require_platform_admin():
    async def _dep(request: Request) -> None:
        if not auth.accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
        if not auth.is_platform_admin(auth.current_user(request)):
            raise HTTPException(status_code=403, detail="platform admin access required")

    return _dep


@app.get("/api/admin/users", dependencies=[Depends(require_platform_admin())])
async def admin_list_users() -> list[dict]:
    return auth.get_account_store().list_all_users()


class AdminSetDisabledRequest(BaseModel):
    disabled: bool


@app.post("/api/admin/users/{user_id}/disabled", dependencies=[Depends(require_platform_admin())])
async def admin_set_user_disabled(user_id: str, req: AdminSetDisabledRequest) -> dict:
    if not auth.get_account_store().set_user_disabled(user_id, req.disabled):
        raise HTTPException(status_code=404, detail="user not found")
    return {"ok": True}


@app.get("/api/admin/orgs", dependencies=[Depends(require_platform_admin())])
async def admin_list_orgs() -> list[dict]:
    return auth.get_account_store().list_all_orgs()


@app.get("/api/admin/rooms", dependencies=[Depends(require_platform_admin())])
async def admin_list_rooms() -> list[dict]:
    return auth.get_account_store().list_all_owned_rooms()


class AdminClaimRoomRequest(BaseModel):
    kind: str  # "drawing" | "mesh"
    room_id: str
    owner_user_id: str


@app.post("/api/admin/rooms/claim", dependencies=[Depends(require_platform_admin())])
async def admin_claim_room(req: AdminClaimRoomRequest) -> dict:
    if req.kind not in ("drawing", "mesh"):
        raise HTTPException(status_code=400, detail="kind must be 'drawing' or 'mesh'")
    if auth.get_account_store().get_user(req.owner_user_id) is None:
        raise HTTPException(status_code=404, detail="owner_user_id does not exist")
    if not auth.get_account_store().admin_claim_room(req.kind, req.room_id, req.owner_user_id):
        raise HTTPException(status_code=409, detail="this room already has an owner")
    return {"ok": True}


@app.delete("/api/admin/rooms/{kind}/{room_id}", dependencies=[Depends(require_platform_admin())])
async def admin_delete_room(kind: str, room_id: str) -> dict:
    if kind not in ("drawing", "mesh"):
        raise HTTPException(status_code=400, detail="kind must be 'drawing' or 'mesh'")
    manager = drawing_room_manager if kind == "drawing" else mesh_room_manager
    room = manager.rooms.pop(room_id, None)
    if room is not None:
        for ws in list(room.clients.values()):
            try:
                await ws.close(code=WS_CLOSE_GOING_AWAY)
            except Exception:
                pass  # already disconnecting -- nothing to clean up
    store.delete(kind, room_id)
    auth.get_account_store().delete_room_ownership(kind, room_id)
    return {"ok": True}


@app.get("/api/admin/reports", dependencies=[Depends(require_platform_admin())])
async def admin_list_reports(status: Optional[str] = None) -> list[dict]:
    return auth.get_account_store().list_abuse_reports(status=status)


class ResolveReportRequest(BaseModel):
    status: str  # "resolved" | "dismissed"


@app.post("/api/admin/reports/{report_id}/resolve", dependencies=[Depends(require_platform_admin())])
async def admin_resolve_report(report_id: str, req: ResolveReportRequest) -> dict:
    if req.status not in ("resolved", "dismissed"):
        raise HTTPException(status_code=400, detail="status must be 'resolved' or 'dismissed'")
    if not auth.get_account_store().resolve_abuse_report(report_id, req.status):
        raise HTTPException(status_code=404, detail="report not found")
    return {"ok": True}


# -- notifications & per-room activity feed (Part 6 P5) ------------------------


@app.get("/api/notifications")
async def list_notifications(request: Request, unread_only: bool = False) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    store = auth.get_account_store()
    return {
        "notifications": store.list_notifications(user["user_id"], unread_only=unread_only),
        "unread_count": store.count_unread_notifications(user["user_id"]),
    }


@app.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, request: Request) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    if not auth.get_account_store().mark_notification_read(notification_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="notification not found")
    return {"ok": True}


@app.post("/api/notifications/read-all")
async def mark_all_notifications_read(request: Request) -> dict:
    if not auth.accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts mode is not enabled on this server")
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    count = auth.get_account_store().mark_all_notifications_read(user["user_id"])
    return {"ok": True, "marked_read": count}


async def _room_activity(kind: str, room_id: str) -> dict:
    if not auth.accounts_enabled():
        return {"activity": []}  # feature is accounts-only -- see _handle_comment_ops's docstring
    return {"activity": auth.get_account_store().list_activity(kind, room_id)}


@app.get("/api/rooms/{room_id}/activity", dependencies=[Depends(require_room_access("drawing"))])
async def drawing_room_activity(room_id: str) -> dict:
    return await _room_activity("drawing", room_id)


@app.get("/api/mesh/{room_id}/activity", dependencies=[Depends(require_room_access("mesh"))])
async def mesh_room_activity(room_id: str) -> dict:
    return await _room_activity("mesh", room_id)


class RenameRequest(BaseModel):
    display_name: str


async def _rename_room(kind: str, room_id: str, display_name: str) -> None:
    if not store.set_display_name(kind, room_id, display_name.strip()):
        raise HTTPException(status_code=404, detail="room not found")


@app.post("/api/rooms/{room_id}/rename", dependencies=[Depends(require_editor_access("drawing"))])
async def rename_drawing_room(room_id: str, req: RenameRequest) -> dict:
    await _rename_room("drawing", room_id, req.display_name)
    return {"ok": True}


@app.post("/api/mesh/{room_id}/rename", dependencies=[Depends(require_editor_access("mesh"))])
async def rename_mesh_room(room_id: str, req: RenameRequest) -> dict:
    await _rename_room("mesh", room_id, req.display_name)
    return {"ok": True}


@app.get("/api/rooms/{room_id}/thumbnail.svg", dependencies=[Depends(require_room_access("drawing"))])
async def drawing_thumbnail(room_id: str) -> Response:
    """2D rooms get a real server-rendered thumbnail -- exactly the same
    SVG the export button produces, just displayed small by the home
    page's own CSS, not a separate rendering path to keep in sync. 3D
    rooms deliberately get a static placeholder icon instead (see
    home.js) -- the brief explicitly allows this, and a real 3D preview
    would need either an offscreen Three.js render (a second renderer to
    maintain) or a client-captured-on-save screenshot (a new upload path)
    for comparatively little payoff for a home-page icon."""
    room = await drawing_room_manager.get_or_create(room_id)
    units = room.doc.settings_dict().get("units", "px")
    paths = [bake_path_transform(p) for p in room.doc.path_list()]
    layer_order = [layer["id"] for layer in room.doc.layer_list()]
    svg = drawing_to_svg_string(paths, units=units, dimensions=room.doc.dimension_list(), layer_order=layer_order)
    return Response(content=svg, media_type="image/svg+xml")


# -- version history ---------------------------------------------------------


class VersionSummary(BaseModel):
    version_id: int
    created_at: float


class RestoreResult(BaseModel):
    new_room_id: str


async def _list_versions(kind: str, room_id: str) -> list[VersionSummary]:
    return [VersionSummary(**v) for v in store.list_versions(kind, room_id)]


async def _restore_version(kind: str, room_id: str, version_id: int) -> RestoreResult:
    """Forks `version_id` into a brand-new room rather than overwriting
    `room_id` in place -- the brief's own reasoning, restated here: a
    live room's causal history (its CRDT ops/frontier) can't be rewound
    without breaking convergence for anyone still connected to it or
    reconnecting later, so "restore" instead means "create a new room
    whose *starting* snapshot is that old version" -- always safe, since
    it never touches `room_id`'s own persisted state or in-memory Room at
    all. An "advanced restore in place" via generated inverse ops (the
    brief's optional stretch) was not attempted -- see the README for why
    (there's no general way to invert an arbitrary historical diff back
    through RGA/LWW's normal op path for every op kind this document
    supports, so it would need its own bespoke, unverified merge logic --
    exactly the kind of unverifiable feature this project avoids shipping)."""
    data = store.load_version(kind, room_id, version_id)
    if data is None:
        raise HTTPException(status_code=404, detail="version not found")
    new_room_id = f"{room_id}-restored-{uuid.uuid4().hex[:8]}"
    store.save(kind, new_room_id, data)
    return RestoreResult(new_room_id=new_room_id)


@app.get("/api/rooms/{room_id}/versions", response_model=list[VersionSummary], dependencies=[Depends(require_room_access("drawing"))])
async def list_drawing_versions(room_id: str) -> list[VersionSummary]:
    return await _list_versions("drawing", room_id)


@app.get("/api/mesh/{room_id}/versions", response_model=list[VersionSummary], dependencies=[Depends(require_room_access("mesh"))])
async def list_mesh_versions(room_id: str) -> list[VersionSummary]:
    return await _list_versions("mesh", room_id)


@app.post(
    "/api/rooms/{room_id}/versions/{version_id}/restore",
    response_model=RestoreResult,
    dependencies=[Depends(require_editor_access("drawing"))],
)
async def restore_drawing_version(room_id: str, version_id: int) -> RestoreResult:
    return await _restore_version("drawing", room_id, version_id)


@app.post(
    "/api/mesh/{room_id}/versions/{version_id}/restore",
    response_model=RestoreResult,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def restore_mesh_version(room_id: str, version_id: int) -> RestoreResult:
    return await _restore_version("mesh", room_id, version_id)


# -- read-only share links ----------------------------------------------------


class ShareLinkRequest(BaseModel):
    role: str = "viewer"  # "viewer" | "editor"


class ShareLinkResponse(BaseModel):
    token: str
    role: str


async def _create_share_link(kind: str, room_id: str, role: str, request: Request) -> ShareLinkResponse:
    if not security.auth_enabled():
        raise HTTPException(
            status_code=400,
            detail="read-only share links need CRDT_CAD_SECRET configured on this server",
        )
    if role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="role must be 'viewer' or 'editor'")
    _enforce_daily_quota(auth.current_user(request), "share_link", QUOTA_SHARE_LINKS_PER_DAY, "share link")
    # Part 6 P3: an org can restrict which roles a share link may carry
    # for one of its own documents (e.g. "no editor links, viewer only")
    # -- checked here so it applies no matter which room this link is
    # for, without touching the (accounts-agnostic) token-minting code
    # in security.py itself.
    if auth.accounts_enabled():
        ownership = auth.get_account_store().get_room_ownership(kind, room_id)
        org_id = ownership.get("owner_org_id") if ownership else None
        if org_id is not None:
            org = auth.get_account_store().get_org(org_id)
            if org is not None and role not in org["allowed_share_link_roles"]:
                raise HTTPException(
                    status_code=403,
                    detail=f"this document's organization only allows share links with role(s): "
                    f"{', '.join(org['allowed_share_link_roles'])}",
                )
    return ShareLinkResponse(token=security.mint_room_token(kind, room_id, role=role), role=role)


@app.post(
    "/api/rooms/{room_id}/share-link",
    response_model=ShareLinkResponse,
    dependencies=[Depends(require_editor_access("drawing"))],
)
async def create_drawing_share_link(room_id: str, req: ShareLinkRequest, request: Request) -> ShareLinkResponse:
    return await _create_share_link("drawing", room_id, req.role, request)


@app.post(
    "/api/mesh/{room_id}/share-link",
    response_model=ShareLinkResponse,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def create_mesh_share_link(room_id: str, req: ShareLinkRequest, request: Request) -> ShareLinkResponse:
    return await _create_share_link("mesh", room_id, req.role, request)


# ---------------------------------------------------------------------------
# Export / import (2D drawing rooms)
# ---------------------------------------------------------------------------


def _attachment(content: str | bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/rooms/{room_id}/export/json", dependencies=[Depends(require_room_access("drawing"))])
async def export_drawing_json(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    return _attachment(json.dumps(room.doc.to_dict(), indent=2), "application/json", f"{room_id}.json")


@app.get("/api/rooms/{room_id}/export/svg", dependencies=[Depends(require_room_access("drawing"))])
async def export_drawing_svg(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    units = room.doc.settings_dict().get("units", "px")
    paths = [bake_path_transform(p) for p in room.doc.path_list()]
    layer_order = [layer["id"] for layer in room.doc.layer_list()]
    svg = drawing_to_svg_string(paths, units=units, dimensions=room.doc.dimension_list(), layer_order=layer_order)
    return _attachment(svg, "image/svg+xml", f"{room_id}.svg")


@app.get("/api/rooms/{room_id}/export/dxf", dependencies=[Depends(require_room_access("drawing"))])
async def export_drawing_dxf(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    units = room.doc.settings_dict().get("units", "px")
    paths = [bake_path_transform(p) for p in room.doc.path_list()]
    layer_order = [layer["id"] for layer in room.doc.layer_list()]
    data = drawing_to_dxf_bytes(paths, units=units, dimensions=room.doc.dimension_list(), layer_order=layer_order)
    return _attachment(data, "application/dxf", f"{room_id}.dxf")


@app.get("/api/rooms/{room_id}/export/pdf", dependencies=[Depends(require_room_access("drawing"))])
async def export_drawing_pdf(room_id: str, sheet_id: str | None = None) -> Response:
    """Renders one sheet (Part 7 C3 -- page setup + title block, see
    `DrawingDocument.sheets`/`sheet_props`) to PDF. `sheet_id` picks
    which sheet when a room has more than one; omitted (or a stale id
    a concurrent delete removed) falls back to the first sheet in
    creation order, same "don't error on a race, degrade to something
    reasonable" choice the rest of this room's export endpoints make."""
    room = await drawing_room_manager.get_or_create(room_id)
    sheets = room.doc.sheet_list()
    if not sheets:
        raise HTTPException(status_code=404, detail="this room has no sheets yet -- create one first")
    sheet = next((s for s in sheets if s["id"] == sheet_id), sheets[0])
    paths = [bake_path_transform(p) for p in room.doc.path_list()]
    layer_order = [layer["id"] for layer in room.doc.layer_list()]
    data = sheet_to_pdf_bytes(paths, sheet, dimensions=room.doc.dimension_list(), layer_order=layer_order)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", sheet.get("name") or "sheet")
    return _attachment(data, "application/pdf", f"{room_id}-{safe_name}.pdf")


class ImportResult(BaseModel):
    layer_id: str
    path_count: int


async def _import_paths(room_id: str, paths: list[dict], layer_name: str) -> ImportResult:
    """`paths` items are `{"points": [...], "curves": {...}}` -- `curves`
    (Phase 8) maps a point index to a curve payload, passed straight
    through to `add_path` so an imported SVG's Bezier segments survive as
    real curves, not a flattened polyline. DXF import has no curve
    concept of its own (see dxf_io.py), so its caller below just wraps
    each plain point list with an empty `curves`."""
    room = await drawing_room_manager.get_or_create(room_id)
    layer_id, ops = room.doc.add_layer(layer_name)
    count = 0
    for p in paths:
        pts = p["points"]
        if len(pts) < 2:
            continue
        _, path_ops = room.doc.add_path(layer_id, [tuple(pt) for pt in pts], curves=p.get("curves"))
        ops.extend(path_ops)
        count += 1
    if ops:
        await room.broadcast({"type": "ops", "ops": [op.to_dict() for op in ops], "from": "__import__"})
        room.mark_dirty()
        await room.persist_async()
    return ImportResult(layer_id=layer_id, path_count=count)


@app.post("/api/rooms/{room_id}/import/svg", response_model=ImportResult, dependencies=[Depends(require_editor_access("drawing"))])
async def import_drawing_svg(room_id: str, request: Request) -> ImportResult:
    body = await request.body()
    try:
        paths = drawing_from_svg_string(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse SVG: {exc}") from exc
    return await _import_paths(room_id, paths, "Imported SVG")


@app.post("/api/rooms/{room_id}/import/dxf", response_model=ImportResult, dependencies=[Depends(require_editor_access("drawing"))])
async def import_drawing_dxf(room_id: str, request: Request) -> ImportResult:
    body = await request.body()
    try:
        paths = drawing_from_dxf_bytes(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse DXF: {exc}") from exc
    return await _import_paths(room_id, [{"points": pts, "curves": {}} for pts in paths], "Imported DXF")


# ---------------------------------------------------------------------------
# Export (3D mesh rooms)
# ---------------------------------------------------------------------------


@app.get("/api/mesh/{room_id}/export/json", dependencies=[Depends(require_room_access("mesh"))])
async def export_mesh_json(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    return _attachment(json.dumps(room.doc.to_dict(), indent=2), "application/json", f"{room_id}.json")


@app.get("/api/mesh/{room_id}/export/stl", dependencies=[Depends(require_room_access("mesh"))])
async def export_mesh_stl(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    stl = mesh_to_stl(room.doc.vertex_positions(), room.doc.face_loops(), name=room_id.replace(" ", "_") or "mesh")
    return _attachment(stl, "model/stl", f"{room_id}.stl")


@app.get("/api/mesh/{room_id}/export/step", dependencies=[Depends(require_room_access("mesh"))])
async def export_mesh_step(room_id: str) -> Response:
    """`build123d` (the `step` extra) is a heavy, optional dependency --
    see step_export.py's module docstring for why this re-evaluates the
    README's older "pythonOCC is conda-only" note. Runs off the event
    loop (real OpenCascade geometry construction, not free) via
    asyncio.to_thread, same as the mesh validity check."""
    room = await mesh_room_manager.get_or_create(room_id)
    try:
        data = await asyncio.to_thread(mesh_to_step_bytes, room.doc.vertex_positions(), room.doc.face_loops())
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail=f"STEP export needs the optional 'step' extra -- install with `pip install crdt-cad[step]`: {exc}",
        ) from exc
    if not data:
        raise HTTPException(status_code=400, detail="nothing to export -- no face has 3 or more live vertices")
    return _attachment(data, "application/step", f"{room_id}.step")


@app.get("/api/mesh/{room_id}/export/glb", dependencies=[Depends(require_room_access("mesh"))])
async def export_mesh_glb(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    data = mesh_to_glb_bytes(room.doc.vertex_positions(), room.doc.face_loops())
    if not data:
        raise HTTPException(status_code=400, detail="nothing to export -- no face has 3 or more live vertices")
    return _attachment(data, "model/gltf-binary", f"{room_id}.glb")


@app.get("/api/mesh/{room_id}/export/3mf", dependencies=[Depends(require_room_access("mesh"))])
async def export_mesh_3mf(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    data = mesh_to_3mf_bytes(room.doc.vertex_positions(), room.doc.face_loops())
    if not data:
        raise HTTPException(status_code=400, detail="nothing to export -- no face has 3 or more live vertices")
    return _attachment(data, "model/3mf", f"{room_id}.3mf")


class ImportMeshResult(BaseModel):
    vertex_count: int
    face_count: int


@app.post(
    "/api/mesh/{room_id}/import/step",
    response_model=ImportMeshResult,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def import_mesh_step(room_id: str, request: Request) -> ImportMeshResult:
    """Part 7 C4 (STEP-import interop). Tessellation is real OpenCascade
    geometry work (like export_mesh_step above), so it runs off the
    event loop via asyncio.to_thread. Every imported vertex/face id is
    remapped to a fresh globally-unique one (`MeshCRDT.new_id`) before
    minting ops, the same "never reuse a caller-supplied id, mint your
    own" rule `_mint_ops_for_mesh` follows for AI-generated meshes --
    otherwise importing into a room that already has a "v0" would
    either collide or silently overwrite it."""
    body = await request.body()
    try:
        mesh = await asyncio.to_thread(mesh_from_step_bytes, body)
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail=f"STEP import needs the optional 'step' extra -- install with `pip install crdt-cad[step]`: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse STEP: {exc}") from exc

    room = await mesh_room_manager.get_or_create(room_id)
    clock = LamportClock(actor=DEFAULT_ACTOR_ID)
    scratch = MeshCRDT(clock)
    vertex_ids = {old: mesh_new_id("v") for old in mesh.vertices}
    ops: list[MeshOp] = [scratch.add_vertex(vertex_ids[old], pos) for old, pos in mesh.vertices.items()]
    for loop in mesh.faces.values():
        remapped = [vertex_ids[v] for v in loop]
        face_id = mesh_new_id("f")
        ops.extend(scratch.add_face(face_id, remapped))
        for i in range(len(remapped)):
            a, b = remapped[i], remapped[(i + 1) % len(remapped)]
            ops.append(scratch.add_edge(a, b))
    await room.commit_ops_batched(ops, actor=DEFAULT_ACTOR_ID)
    return ImportMeshResult(vertex_count=len(mesh.vertices), face_count=len(mesh.faces))


# ---------------------------------------------------------------------------
# AI text-to-3D generation (3D mesh rooms)
# ---------------------------------------------------------------------------


class GenerateMeshRequest(BaseModel):
    prompt: str
    # Phase G4 follow-up edits: set while a generation is selected in the
    # UI to send `prompt` to the pipeline as "edit this spec" instead of
    # a fresh dispatch -- see `generate_edit_ops_from_interpretation`.
    edit_of: Optional[str] = None


class GenerateMeshResult(BaseModel):
    actor: str
    generator: str  # which registry entry produced this (Phase G1: "house", "table", "chair", ...)
    interpretation_source: str  # "llm" | "heuristic"
    mesh_source: str  # "meshy" | "procedural"
    spec: dict
    vertex_count: int
    face_count: int
    triangle_count: int
    op_count: int
    batches: int
    watertight: bool
    manifold: bool
    generation_id: str  # Phase G4 provenance: select/edit this generation later by this id
    # Phase G5 report card fields -- honest by construction: these are
    # exactly what `ValidationReport` returned, never a rosier summary.
    planar: bool
    non_planar_face_count: int
    within_bounds: bool
    bounding_box: tuple[float, float, float]
    elapsed_seconds: float
    path: str  # "registry" | "scene" | "dsl" | "meshy" | "edit"
    outcome: str  # "success" | "fallback" | "repair_retry"
    interpretation_chips: list[str]


class GenerateBudgetResult(BaseModel):
    remaining: float
    capacity: float
    per_minute: float


@app.get(
    "/api/mesh/{room_id}/generate/budget",
    response_model=GenerateBudgetResult,
    dependencies=[Depends(require_room_access("mesh"))],
)
async def generate_budget(room_id: str, request: Request) -> GenerateBudgetResult:
    """Phase G5 cost guardrail: lets the UI show remaining generation
    budget *before* a surprise 429, not after. Per-IP, not per-room
    (`room_id` is only in the URL for consistency with every other mesh
    endpoint) -- a pure peek (`PerKeyRateLimiter.remaining`), never
    spends a token itself, safe to poll as often as the UI wants."""
    client_ip = security.client_ip(request)
    return GenerateBudgetResult(
        remaining=security.generate_rate_limiter.remaining(client_ip),
        capacity=security.generate_rate_limiter.capacity(),
        per_minute=security.generate_per_minute(),
    )


def _generation_outcome(result) -> str:
    if result.dsl_attempts:
        if result.dsl_attempts[-1]["outcome"] == "failed":
            return "fallback"
        if len(result.dsl_attempts) > 1:
            return "repair_retry"
    return "success"


async def _interpret_and_generate(room: "Room", req: "GenerateMeshRequest") -> tuple:
    """Runs interpretation, broadcasts "understood: ..." chips to the
    *whole room* (Phase G5: shown before geometry lands, not just to the
    requester), then runs the (slower) build phase. Both phases run in a
    worker thread (`asyncio.to_thread`) since both can do real network/
    CPU work; this coroutine itself does no blocking work of its own, so
    it's safe to run inside `_run_cancellable`. Returns
    ``(GenerationResult, path_label, outcome)``.
    """
    if req.edit_of:
        prior_record = room.doc.generation(req.edit_of)
        if prior_record is None:
            raise HTTPException(status_code=422, detail=f"no generation with id {req.edit_of!r} in this room")
        check_edit_supported(prior_record)
        old_face_ids, old_vertex_ids, old_edges = generation_geometry(
            room.doc.face_loops(), {fid: room.doc.face_props_dict(fid) for fid in room.doc.face_loops()}, req.edit_of,
        )
        start_counter = room.doc.frontier().get(DEFAULT_ACTOR_ID)
        generator_name, spec, source = await asyncio.to_thread(interpret_edit, req.prompt, prior_record)
        await room.broadcast({
            "type": "generation_interpreting",
            "chips": interpretation_chips(generator_name, spec),
            "generator": generator_name, "path": "edit", "interpretation_source": source,
        })
        result = await asyncio.to_thread(
            generate_edit_ops_from_interpretation, req.prompt, req.edit_of, generator_name, spec, source,
            old_face_ids, old_vertex_ids, old_edges, start_counter, actor_id=DEFAULT_ACTOR_ID,
        )
        return result, "edit", _generation_outcome(result)

    generator_name, spec, source = await asyncio.to_thread(interpret_prompt, req.prompt)
    await room.broadcast({
        "type": "generation_interpreting",
        "chips": interpretation_chips(generator_name, spec),
        "generator": generator_name, "path": generator_name, "interpretation_source": source,
    })

    # Phase G7: the matured async Meshy path -- submit/poll/decimate with
    # real progress broadcast to the room -- runs *before* the ordinary
    # build phase, only for a plain single-object dispatch (a scene or a
    # DSL program has no single "the hosted mesh" to substitute in for).
    # meshy_attempted=True either way tells generate_ops_from_interpretation
    # not to redundantly retry Meshy itself when this already tried
    # (successfully or not).
    meshy_mesh = None
    meshy_attempted = False
    if generator_name not in ("scene", "dsl") and meshy_api_key():
        meshy_attempted = True

        async def _on_meshy_progress(payload: dict) -> None:
            await room.broadcast({"type": "meshy_progress", **payload})

        meshy_mesh = await generate_mesh_via_meshy_async(req.prompt, on_progress=_on_meshy_progress)

    result = await asyncio.to_thread(
        generate_ops_from_interpretation, req.prompt, generator_name, spec, source, actor_id=DEFAULT_ACTOR_ID,
        meshy_mesh=meshy_mesh, meshy_attempted=meshy_attempted,
    )
    path = generator_name if generator_name in ("scene", "dsl") else ("meshy" if result.mesh_source == "meshy" else "registry")
    return result, path, _generation_outcome(result)


class GenerationCancelledError(Exception):
    """Raised by :func:`_run_cancellable` when the client disconnects
    (an aborted ``fetch`` -- Phase G5's cancel button) before the
    generation finished."""


async def _run_cancellable(coro, request: Request, *, poll_interval: float = 0.5):
    """Runs `coro` as a background task, polling `request.is_disconnected()`
    (Starlette's real ASGI-level disconnect signal, which fires when the
    client's connection actually drops -- e.g. an `AbortController`-driven
    fetch abort) and cancelling the task the moment that's observed.

    Honesty note, not glossed over: `asyncio.to_thread` (used inside
    `coro`) schedules work on a real OS thread, and Python cannot forcibly
    kill a running thread. Cancelling *this* task stops the server from
    committing/broadcasting a result and returns control to the caller
    immediately -- a thread-pool worker already mid-build keeps running in
    the background regardless, its result simply discarded when it
    finishes. That's still a real, meaningful cancellation from the
    user's point of view (no ops land in the room, no response is sent,
    the room is never touched by the cancelled attempt), just not a
    literal OS-level kill -- the same limitation every Python web
    framework built on a thread pool has.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            if await request.is_disconnected():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                raise GenerationCancelledError()
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=poll_interval)
            except asyncio.TimeoutError:
                continue
    finally:
        if not task.done():
            task.cancel()


@app.post(
    "/api/mesh/{room_id}/generate",
    response_model=GenerateMeshResult,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def generate_mesh(room_id: str, req: GenerateMeshRequest, request: Request) -> GenerateMeshResult:
    """Text -> 3D: interprets ``req.prompt`` (Claude Fable 5 if
    available, a regex heuristic otherwise), broadcasts "understood:
    ..." chips to the whole room (Phase G5, before geometry lands),
    builds a deterministic procedural mesh, mints a chronological batch
    of CRDT ops for it under the ``ai_generator_bot`` actor identity,
    commits them into the room in bounded-size chunks, and broadcasts a
    full report card.

    The heavy work runs in worker threads via ``asyncio.to_thread`` so
    it never blocks this room's (or any other room's) WebSocket loop,
    wrapped in both a timeout (a hung LLM call can't tie up a thread-pool
    slot forever) and a real cancellation path (Phase G5: an aborted
    client request actually stops the request, see `_run_cancellable`).
    Rate-limited per client IP (``CRDT_CAD_GENERATE_PER_MINUTE``, default
    6/min, remaining budget peekable via ``GET .../generate/budget``)
    since each call burns real LLM spend and/or CPU time -- unlike the
    other endpoints in this module, this limit applies unconditionally,
    even when room auth is off, because the resource cost is real
    regardless of whether the deployment cares about access control.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    client_ip = security.client_ip(request)
    if not security.generate_rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="generation rate limit exceeded -- try again shortly")
    _enforce_daily_quota(auth.current_user(request), "generation", QUOTA_GENERATIONS_PER_DAY, "AI generation")

    room = await mesh_room_manager.get_or_create(room_id)
    request_start = time.monotonic()
    path_for_metrics = "edit" if req.edit_of else "unknown"

    try:
        result, path, outcome = await asyncio.wait_for(
            _run_cancellable(_interpret_and_generate(room, req), request),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
        path_for_metrics = path
    except asyncio.TimeoutError as exc:
        metrics.generations_total.labels(outcome="failure", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        raise HTTPException(
            status_code=504,
            detail=f"mesh generation exceeded the {GENERATION_TIMEOUT_SECONDS:.0f}s timeout",
        ) from exc
    except GenerationCancelledError as exc:
        metrics.generations_total.labels(outcome="cancelled", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        raise HTTPException(status_code=499, detail="generation cancelled by client") from exc
    except EditNotSupportedError as exc:
        metrics.generations_total.labels(outcome="failure", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GenerationValidationError as exc:
        # Rule 1 (AI_GENERATION_PROMPT.md): a validation failure is a
        # visible, typed error -- never a silently-injected broken mesh.
        # The report's own fields (not just a joined string) are surfaced
        # so a client can render exactly what failed, not just "it failed".
        metrics.generations_total.labels(outcome="failure", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        logger.warning("room %s: generation failed validation for prompt %r: %s", room_id, req.prompt, exc.report.errors)
        raise HTTPException(
            status_code=422,
            detail={
                "message": "generated mesh failed pre-commit validation",
                "errors": exc.report.errors,
                "watertight": exc.report.watertight,
                "manifold": exc.report.manifold,
                "within_bounds": exc.report.within_bounds,
            },
        ) from exc
    except HTTPException:
        metrics.generations_total.labels(outcome="failure", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        raise
    except Exception as exc:
        metrics.generations_total.labels(outcome="failure", path=path_for_metrics).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        logger.exception("room %s: mesh generation failed for prompt %r", room_id, req.prompt)
        raise HTTPException(status_code=422, detail=f"could not generate mesh: {exc}") from exc

    if not result.ops:
        metrics.generations_total.labels(outcome="failure", path=path).inc()
        metrics.generation_latency_seconds.observe(time.monotonic() - request_start)
        raise HTTPException(status_code=422, detail="generation produced an empty mesh (malformed geometry)")

    if result.object_ops is not None:
        # Scene generation (Phase G2): force a batch boundary between
        # objects so a "table with four chairs around it" visibly builds
        # object by object rather than arriving as one big flush.
        batches = await room.commit_ops_grouped_batched(
            result.object_ops, actor=DEFAULT_ACTOR_ID, batch_size=GENERATION_OPS_BATCH_SIZE
        )
    else:
        batches = await room.commit_ops_batched(result.ops, actor=DEFAULT_ACTOR_ID, batch_size=GENERATION_OPS_BATCH_SIZE)

    elapsed = time.monotonic() - request_start
    metrics.generations_total.labels(outcome=outcome, path=path).inc()
    metrics.generation_latency_seconds.observe(elapsed)
    if auth.accounts_enabled():
        # Part 6 P5: the room activity feed's one non-comment trigger --
        # a generation is this app's most visible "something happened"
        # event, worth surfacing there even though it's not itself a
        # comment/mention. actor_user_id is whoever's REST session made
        # the call (None for an anonymous/token-only request).
        requester = auth.current_user(request)
        auth.get_account_store().log_activity(
            "mesh", room_id, requester["user_id"] if requester else None, "generation_completed",
            {
                "prompt": req.prompt, "generator": result.generator_name, "generation_id": result.generation_id,
                "author": requester["display_name"] if requester else "someone",
            },
        )
    logger.info(
        "room mesh/%s: generated %d ops (%d vertices, %d faces) from prompt %r via %s (%s/%s), sent in %d batches",
        room_id, len(result.ops), result.vertex_count, result.face_count, req.prompt,
        result.generator_name, result.interpretation_source, result.mesh_source, batches,
    )

    report_card = {
        "type": "report_card",
        "generation_id": result.generation_id,
        "generator": result.generator_name,
        "path": path,
        "outcome": outcome,
        "interpretation_source": result.interpretation_source,
        "mesh_source": result.mesh_source,
        "watertight": result.validation.watertight,
        "manifold": result.validation.manifold,
        "planar": result.validation.planar,
        "non_planar_face_count": result.validation.non_planar_face_count,
        "within_bounds": result.validation.within_bounds,
        "vertex_count": result.vertex_count,
        "face_count": result.face_count,
        "triangle_count": result.triangle_count,
        "bounding_box": result.validation.bounding_box,
        "elapsed_seconds": elapsed,
        "dsl_attempts": result.dsl_attempts,
        "errors": result.validation.errors,
    }
    await room.broadcast(report_card)

    return GenerateMeshResult(
        actor=DEFAULT_ACTOR_ID,
        generator=result.generator_name,
        interpretation_source=result.interpretation_source,
        mesh_source=result.mesh_source,
        spec=result.spec.model_dump(),
        vertex_count=result.vertex_count,
        face_count=result.face_count,
        triangle_count=result.triangle_count,
        op_count=len(result.ops),
        batches=batches,
        watertight=result.validation.watertight,
        manifold=result.validation.manifold,
        generation_id=result.generation_id,
        planar=result.validation.planar,
        non_planar_face_count=result.validation.non_planar_face_count,
        within_bounds=result.validation.within_bounds,
        bounding_box=result.validation.bounding_box,
        elapsed_seconds=elapsed,
        path=path,
        outcome=outcome,
        interpretation_chips=interpretation_chips(result.generator_name, result.spec),
    )


# ---------------------------------------------------------------------------
# Geometry constraint solver (REST, stateless)
# ---------------------------------------------------------------------------


class ConstraintIn(BaseModel):
    kind: str
    point_ids: list[Optional[str]]
    param: float = 0.0


class SolveRequest(BaseModel):
    points: dict[str, tuple[float, float]]
    constraints: list[ConstraintIn]
    max_iterations: int = 50
    tol: float = 1e-9


class SolveResponse(BaseModel):
    positions: dict[str, tuple[float, float]]
    converged: bool
    iterations: int
    residual_norm: float


@app.post("/api/solve", response_model=SolveResponse)
async def solve_endpoint(req: SolveRequest) -> SolveResponse:
    sketch = Sketch()
    for pid, (x, y) in req.points.items():
        sketch.add_point(pid, x, y)
    try:
        for c in req.constraints:
            sketch.add_constraint(Constraint(kind=c.kind, point_ids=tuple(c.point_ids), param=c.param))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await asyncio.to_thread(sketch.solve, req.max_iterations, req.tol)
    return SolveResponse(
        positions=result.positions,
        converged=result.converged,
        iterations=result.iterations,
        residual_norm=result.residual_norm,
    )


# ---------------------------------------------------------------------------
# Path offset (REST, stateless -- Part 7 C1, same "needs a real numerical
# library" reasoning as the solver above)
# ---------------------------------------------------------------------------


class OffsetRequest(BaseModel):
    points: list[tuple[float, float]]
    distance: float
    closed: bool = False


class OffsetResponse(BaseModel):
    points: list[tuple[float, float]]


@app.post("/api/geometry/offset", response_model=OffsetResponse)
async def offset_endpoint(req: OffsetRequest) -> OffsetResponse:
    try:
        result = await asyncio.to_thread(offset_path, req.points, req.distance, req.closed)
    except OffsetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OffsetResponse(points=result)


# ---------------------------------------------------------------------------
# WebSocket relay
# ---------------------------------------------------------------------------


# WebSocket close codes in the 4000-4999 private-use range (RFC 6455), chosen
# to loosely echo familiar HTTP statuses so they're self-explanatory in logs.
WS_CLOSE_BAD_HELLO = 4400
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_TOO_MANY_CLIENTS = 4429
WS_CLOSE_MESSAGE_TOO_LARGE = 4413
WS_CLOSE_SERVER_AT_CAPACITY = 4503


async def _receive_capped(websocket: WebSocket) -> Optional[dict]:
    """Receives one JSON WS frame, enforcing security.max_ws_message_bytes()
    on the raw text before any JSON parsing is attempted. Returns the
    parsed dict, ``{}`` for a frame this JSON-only protocol doesn't
    understand (binary, or malformed JSON -- silently ignored, same as
    before this existed), or ``None`` if the client disconnected. Raises
    ``security.MessageTooLarge`` for an oversized frame; the caller closes
    the connection with a clear code rather than trying to process it."""
    raw = await websocket.receive()
    if raw.get("type") == "websocket.disconnect":
        return None
    text = raw.get("text")
    if text is None:
        return {}
    if len(text.encode("utf-8")) > security.max_ws_message_bytes():
        raise security.MessageTooLarge()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def _serve_room(websocket: WebSocket, room_id: str, manager: RoomManager) -> None:
    await websocket.accept()

    try:
        room = await manager.get_or_create(room_id)
    except security.RoomLimitExceeded:
        await websocket.close(code=WS_CLOSE_SERVER_AT_CAPACITY)
        return

    if len(room.clients) >= security.max_clients_per_room():
        await websocket.close(code=WS_CLOSE_TOO_MANY_CLIENTS)
        return

    try:
        hello = await _receive_capped(websocket)
    except security.MessageTooLarge:
        await websocket.close(code=WS_CLOSE_MESSAGE_TOO_LARGE)
        return
    if hello is None:
        return

    if hello.get("type") != "hello" or not hello.get("actor"):
        await websocket.close(code=WS_CLOSE_BAD_HELLO)
        return

    user = auth.current_user(websocket)  # WebSocket exposes .cookies same as Request
    if room.was_freshly_created and user is not None:
        # Part 6 P2 claim-on-first-touch: a genuinely brand-new room
        # (no persisted snapshot existed when this Room object was
        # constructed) becomes owned by whichever signed-in user opens
        # it first. A pre-existing room -- including one created before
        # accounts mode was ever turned on -- is never retroactively
        # claimed this way; it stays ownerless-public until an admin
        # tool claims it (Part 6 P4, not yet built).
        # Part 6 P4: an opt-in cap on how many rooms one user can own.
        # Over quota, the claim is simply skipped -- the room stays
        # ownerless-public rather than the connection being refused, so
        # a quota never turns into a broken collaboration session.
        account_store = auth.get_account_store()
        if QUOTA_OWNED_DOCUMENTS <= 0 or len(account_store.list_owned_rooms(user["user_id"])) < QUOTA_OWNED_DOCUMENTS:
            account_store.claim_room(manager.kind, room_id, user["user_id"])
        room.was_freshly_created = False

    role = _effective_role(manager.kind, room_id, hello.get("token"), user)
    if role is None:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    actor = str(hello["actor"])
    room.clients[actor] = websocket
    room.client_roles[actor] = role
    room.client_users[actor] = user
    room.start_snapshot_loop()
    room.start_redis_relay_loop()
    metrics.connections_total.inc()
    logger.info(
        "room %s/%s: actor %s connected as %s (%d clients)", room.kind, room_id, actor, role, len(room.clients)
    )

    known_frontier = hello.get("known_frontier")
    if known_frontier:
        vc = VectorClock.from_dict(known_frontier)
        delta = room.doc.ops_since(vc)
        await websocket.send_json(
            {
                "type": "delta",
                "ops": [op.to_dict() for op in delta],
                "frontier": room.doc.frontier().to_dict(),
                "role": role,
            }
        )
        logger.info("room %s/%s: sent %d-op reconnect delta to %s", room.kind, room_id, len(delta), actor)
    else:
        await websocket.send_json(room.snapshot_message(role))

    ops_bucket = security.new_ws_ops_bucket()

    try:
        while True:
            try:
                message = await _receive_capped(websocket)
            except security.MessageTooLarge:
                await websocket.close(code=WS_CLOSE_MESSAGE_TOO_LARGE)
                return
            if message is None:
                break
            if message:
                await _handle_message(room, actor, message, ops_bucket)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("room %s/%s: error handling messages from %s", room.kind, room_id, actor)
    finally:
        room.clients.pop(actor, None)
        room.client_roles.pop(actor, None)
        room.client_users.pop(actor, None)
        metrics.active_connections.dec()
        logger.info(
            "room %s/%s: actor %s disconnected (%d clients left)",
            room.kind, room_id, actor, len(room.clients),
        )


@app.websocket("/ws/{room_id}")
async def ws_drawing_endpoint(websocket: WebSocket, room_id: str) -> None:
    await _serve_room(websocket, room_id, drawing_room_manager)


@app.websocket("/ws/mesh/{room_id}")
async def ws_mesh_endpoint(websocket: WebSocket, room_id: str) -> None:
    await _serve_room(websocket, room_id, mesh_room_manager)


def _validate_op(room: Room, op) -> Optional[str]:
    """Pre-commit geometry validity **gate** for 2D path point inserts --
    the only place an op can be refused before it's ever applied.

    Returns a rejection reason string if ``op`` should be refused, else
    None. Only ``path_geom`` inserts on drawing rooms are gated. Mesh
    rooms have no equivalent pre-commit gate -- a CRDT merge can't be
    rejected without breaking convergence, which is exactly why mesh
    cross-component consistency (manifoldness, winding, degenerate
    faces) is instead checked *after* merging and surfaced as a
    `validity_warning` (see `_check_and_broadcast_mesh_validity` and
    `crdt_cad.geometry.mesh_validity`), not enforced here. Zero-length
    segments are always rejected; self-intersection is only enforced for
    paths created with the strict "Polygon" tool (freehand pen strokes
    crossing themselves is normal and shouldn't be blocked).
    """
    if room.kind != "drawing" or not isinstance(op, DocOp):
        return None
    if op.target != "path_geom" or op.payload.get("t") != "ins":
        return None

    path_id = op.scope
    assert path_id is not None
    existing_points = room.doc.path_points(path_id)
    strict = bool(room.doc.path_props_dict(path_id).get("strict"))
    try:
        validate_new_point(existing_points, tuple(op.payload["v"]), check_self_intersection=strict)
    except GeometryError as exc:
        return str(exc)
    return None


_MESH_TOPOLOGY_TARGETS = {"face_index", "face_geom"}


def _touches_mesh_topology(op: object) -> bool:
    """True if `op` could create or reveal a cross-component mesh
    inconsistency: either a direct face-topology edit (face created/removed,
    boundary edited) or a vertex *deletion* -- the other half of the
    "Extrusion Nightmare", where a face boundary ends up referencing a
    vertex that no longer exists without any face_index/face_geom op ever
    being involved. Vertex *moves* are deliberately excluded: they fire on
    every ~80ms drag tick, so checking on every one would be wasteful, and
    unlike a deletion a move can't make a vertex stop existing -- only
    change its position.
    """
    target = getattr(op, "target", None)
    if target in _MESH_TOPOLOGY_TARGETS:
        return True
    payload = getattr(op, "payload", None)
    return target == "vertex" and isinstance(payload, dict) and bool(payload.get("d"))


async def _check_and_broadcast_mesh_validity(room: Room, touched_topology: bool) -> None:
    """Runs the cross-component mesh validity check (see
    `crdt_cad.geometry.mesh_validity`) after a delta that touched face
    topology has already been merged, and broadcasts a
    `{"type": "validity_warning", "faces": [...], "problems": [...]}`
    message to the whole room if it finds anything -- a warning, never a
    rejection, since the merge this runs after has already happened and
    cannot be undone without breaking convergence (see the "Extrusion
    Nightmare" discussion in README's "Responses to the architecture
    critique"). A no-op for drawing rooms, and skipped entirely when
    nothing in this batch could have created or revealed this class of
    problem -- see `_touches_mesh_topology` (pure vertex-position moves
    and face_prop edits are excluded, since a move fires on every ~80ms
    drag tick and can't remove a vertex's existence, only its position).
    """
    if room.kind != "mesh" or not touched_topology:
        return
    problems = await asyncio.to_thread(check_mesh_validity, room.doc.vertex_positions(), room.doc.face_loops())
    if problems:
        all_faces = sorted({face_id for p in problems for face_id in p["faces"]})
        logger.info("room mesh/%s: validity warning on %d face(s): %s", room.room_id, len(all_faces), problems)
        await room.broadcast({"type": "validity_warning", "faces": all_faces, "problems": problems})


_MENTION_RE = re.compile(r"@([\w.+-]+@[\w-]+\.[\w.-]+)")


async def _handle_comment_ops(room: Room, actor: str, accepted_wire: list[dict]) -> None:
    """Part 6 P5: for every accepted comment op, logs a room activity
    entry, and -- accounts mode only, since this needs a real identity
    to attribute and notify -- resolves any ``@email`` mentions in a
    newly-added comment's text to real accounts and creates a
    notification for each, skipping anyone who couldn't actually reach
    a *private* room (an unowned or public/link room is reachable by
    anyone anyway, so mentioning someone there is always notified).
    Adding/removing a comment itself works in tokens-only mode too,
    mirroring the pre-existing 2D comments feature -- only this
    attribution/notification layer is accounts-gated."""
    comment_ops = [op for op in accepted_wire if op.get("target") == "comment"]
    if not comment_ops:
        return
    accounts_on = auth.accounts_enabled()
    if not accounts_on:
        return
    store = auth.get_account_store()
    actor_user = room.client_users.get(actor)
    actor_user_id = actor_user["user_id"] if actor_user else None
    ownership = store.get_room_ownership(room.kind, room.room_id)

    for op_dict in comment_ops:
        payload = op_dict.get("payload") or {}
        deleted = bool(payload.get("d"))
        value = payload.get("v") or {}
        store.log_activity(
            room.kind, room.room_id, actor_user_id,
            "comment_removed" if deleted else "comment_added",
            {"comment_id": payload.get("k"), "text": value.get("text"), "author": value.get("author")},
        )
        if deleted:
            continue
        text = value.get("text") or ""
        for email in {m.lower() for m in _MENTION_RE.findall(text)}:
            mentioned = store.get_user_by_email(email)
            if mentioned is None or mentioned["user_id"] == actor_user_id:
                continue
            if ownership is not None and ownership["visibility"] == "private":
                if _account_role_for_room(room.kind, room.room_id, mentioned) is None:
                    continue  # a private room this mentioned user has no access to
            store.create_notification(
                mentioned["user_id"], "mention",
                {
                    "room_kind": room.kind, "room_id": room.room_id,
                    "comment_id": payload.get("k"), "text": text,
                    "from_user_id": actor_user_id,
                    "from_display_name": (actor_user or {}).get("display_name") or actor,
                },
            )


async def _handle_message(room: Room, actor: str, message: dict, ops_bucket: security.TokenBucket) -> None:
    msg_type = message.get("type")

    if msg_type == "signal":
        target_ws = room.clients.get(message.get("to"))
        if target_ws is not None:
            await target_ws.send_json({"type": "signal", "from": actor, "data": message.get("data")})
        return

    if msg_type == "save":
        await room.persist_async()
        # An explicit user-initiated save is exactly the kind of
        # intentional checkpoint version history should capture, so it
        # takes one immediately instead of waiting for the next periodic
        # tick -- see checkpoint_version's docstring for why persist()
        # itself doesn't do this on every call.
        await room.checkpoint_version_async()
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json({"type": "saved", "at": time.time()})
        return

    if msg_type == "resync":
        # A client that received a periodic "frontier" ping ahead of its
        # own recorded frontier asks for a catch-up this way, reusing the
        # exact reply shape (and client-side handling) a reconnect delta
        # already uses. No known frontier at all -> full snapshot, the
        # response of last resort.
        ws = room.clients.get(actor)
        if ws is None:
            return
        known_frontier = message.get("known_frontier")
        role = room.client_roles.get(actor, "editor")
        if not known_frontier:
            await ws.send_json(room.snapshot_message(role))
            return
        vc = VectorClock.from_dict(known_frontier)
        delta = room.doc.ops_since(vc)
        await ws.send_json(
            {
                "type": "delta",
                "ops": [op.to_dict() for op in delta],
                "frontier": room.doc.frontier().to_dict(),
                "role": role,
            }
        )
        return

    if msg_type != "ops":
        return

    role_for_actor = room.client_roles.get(actor)
    if role_for_actor == "viewer":
        # Phase 17 read-only share links: a viewer-role connection still
        # receives every snapshot/delta/ops broadcast normally (so it can
        # render the live document), but anything *it* submits as an edit
        # is refused here, server-side -- the UI also hides editing tools
        # and shows a "view only" badge (see mesh3d.js/sketch.js), but
        # that's a courtesy, not the actual enforcement boundary.
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json({"type": "rejected", "reason": "connected as a read-only viewer -- editing is disabled"})
        return

    ops_wire = message.get("ops", [])

    if role_for_actor == "commenter":
        # Part 6 P5: a commenter may add/remove comments but not touch
        # geometry -- filtered per-op (unlike "viewer" above, which
        # refuses the whole message) so a commenter's comment ops land
        # normally while any geometry op they send is individually
        # rejected, the same per-op shape already used below for a
        # malformed or geometry-invalid op.
        allowed_wire = []
        for op_dict in ops_wire:
            if op_dict.get("target") == "comment":
                allowed_wire.append(op_dict)
            else:
                ws = room.clients.get(actor)
                if ws is not None:
                    await ws.send_json(
                        {"type": "rejected", "reason": "commenter access -- only comments may be added", "op": op_dict}
                    )
        ops_wire = allowed_wire

    if len(ops_wire) > security.max_ops_per_message():
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json(
                {"type": "rejected", "reason": f"too many ops in one message (max {security.max_ops_per_message()})"}
            )
        return

    if not ops_bucket.allow(cost=len(ops_wire)):
        metrics.rate_limited_total.inc()
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json({"type": "rejected", "reason": "rate limit exceeded -- slow down"})
        return

    if not room.ops_rate_limiter.allow(cost=len(ops_wire)):
        metrics.rate_limited_total.inc()
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json({"type": "rejected", "reason": "room-wide rate limit exceeded -- try again shortly"})
        return

    start = time.perf_counter()
    accepted_wire = []
    touched_topology = False
    for op_dict in ops_wire:
        # A malformed op (missing/wrong-shaped fields in the envelope
        # itself, or in the sub-CRDT payload `apply()` only inspects
        # lazily) is a client bug or bad-faith payload, not a server
        # error -- reject it cleanly and keep the connection alive, the
        # same as a geometry-invalid op. Before this, an exception raised
        # anywhere in here propagated out of the whole receive loop,
        # silently dropping the connection with no reply the client
        # could react to (see test_malformed_op_is_rejected_cleanly...).
        try:
            op = room.op_from_dict(op_dict)
            reason = _validate_op(room, op)
        except Exception as exc:
            ws = room.clients.get(actor)
            if ws is not None:
                await ws.send_json({"type": "rejected", "reason": f"malformed op: {exc}", "op": op_dict})
            continue
        if reason is not None:
            metrics.geometry_rejections_total.inc()
            ws = room.clients.get(actor)
            if ws is not None:
                await ws.send_json({"type": "rejected", "reason": reason, "op": op_dict})
            continue
        try:
            room.doc.apply(op)
        except Exception as exc:
            ws = room.clients.get(actor)
            if ws is not None:
                await ws.send_json({"type": "rejected", "reason": f"malformed op: {exc}", "op": op_dict})
            continue
        accepted_wire.append(op_dict)
        touched_topology = touched_topology or _touches_mesh_topology(op)
    metrics.merge_latency_seconds.observe(time.perf_counter() - start)
    metrics.ops_relayed_total.inc(len(accepted_wire))

    if accepted_wire:
        await room.broadcast({"type": "ops", "ops": accepted_wire, "from": actor}, exclude=actor)
        room.mark_dirty()
        await room.persist_debounced()
        await _check_and_broadcast_mesh_validity(room, touched_topology)
        await _handle_comment_ops(room, actor, accepted_wire)
