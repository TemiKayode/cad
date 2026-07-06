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
import socket
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crdt_cad.ai.generator import DEFAULT_ACTOR_ID, generate_mesh_ops
from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.document import DocOp, DrawingDocument, bake_path_transform
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp
from crdt_cad.export.dxf_io import drawing_from_dxf_bytes, drawing_to_dxf_bytes
from crdt_cad.export.step_export import mesh_to_step_bytes
from crdt_cad.export.stl_export import mesh_to_stl
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string
from crdt_cad.geometry.constraints import Constraint, Sketch
from crdt_cad.geometry.mesh_validity import check_mesh_validity
from crdt_cad.geometry.validity import GeometryError, validate_new_point
from crdt_cad.persistence.store import DocumentStore, PostgresStore, SQLiteStore
from crdt_cad.server import metrics
from crdt_cad.server import pubsub
from crdt_cad.server import security

logger = logging.getLogger("crdt_cad.server")
logging.basicConfig(level=logging.INFO)

SNAPSHOT_INTERVAL_SECONDS = float(os.environ.get("CRDT_CAD_SNAPSHOT_INTERVAL_SECONDS", "30"))
VERSION_CHECKPOINT_INTERVAL_SECONDS = float(os.environ.get("CRDT_CAD_VERSION_CHECKPOINT_INTERVAL_SECONDS", "300"))
GENERATION_TIMEOUT_SECONDS = float(os.environ.get("CRDT_CAD_GENERATION_TIMEOUT_SECONDS", "60"))
GENERATION_OPS_BATCH_SIZE = int(os.environ.get("CRDT_CAD_GENERATION_BATCH_SIZE", "150"))
REPO_ROOT = Path(__file__).resolve().parents[3]
# REPO_ROOT only makes sense for an editable/dev install where this file
# lives at its original source path. A regular `pip install .` (e.g. the
# Docker image) copies the package into site-packages, where parents[3]
# points nowhere useful -- CRDT_CAD_STATIC_DIR lets a real deployment say
# explicitly where the demo assets were placed (the Dockerfile sets it).
DEMO_STATIC_DIR = Path(os.environ.get("CRDT_CAD_STATIC_DIR", str(REPO_ROOT / "demo" / "static")))
DB_PATH = os.environ.get("CRDT_CAD_DB_PATH", str(REPO_ROOT / "data" / "crdt_cad.db"))
DATABASE_URL = os.environ.get("CRDT_CAD_DATABASE_URL")

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

        self.clients: dict[str, WebSocket] = {}
        # Phase 17: each connected actor's role ("editor" or "viewer"),
        # keyed the same as `clients` -- populated in `_serve_room` from
        # the "hello" token, consulted in `_handle_message` to reject an
        # `ops` message from a read-only viewer connection.
        self.client_roles: dict[str, str] = {}
        self._snapshot_task: asyncio.Task | None = None
        self._redis_task: asyncio.Task | None = None
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
        for the default thread pool to drain). Persistence is fast
        (SQLite/in-memory), so awaiting it adds negligible latency."""
        await asyncio.to_thread(self.persist)

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
                    await self.persist_async()
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

app = FastAPI(title="crdt-cad collaboration server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=security.cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
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


def require_room_access(kind: str):
    """A FastAPI dependency gating one REST endpoint's ``{room_id}`` behind
    a valid room token -- a no-op (always passes) when
    ``CRDT_CAD_SECRET`` isn't configured, matching every other auth check
    in this module. FastAPI binds the returned callable's ``room_id``
    parameter from the route's own path parameter automatically. Accepts
    *either* role (editor or viewer) -- read-only endpoints (export,
    thumbnail, version history) use this; endpoints that mutate the room
    use :func:`require_editor_access` instead."""

    async def _dep(room_id: str, request: Request) -> None:
        if not security.verify_room_token(_extract_token(request), kind, room_id):
            raise HTTPException(status_code=401, detail="missing or invalid room token")

    return _dep


def require_editor_access(kind: str):
    """Like :func:`require_room_access`, but additionally refuses a
    verified **viewer**-role token (403) -- for REST endpoints that
    mutate a room (import, generate, rename, restore, minting further
    share links), so a read-only share link recipient can't bypass the
    WS-level ops rejection (Phase 17) just by calling these directly."""

    async def _dep(room_id: str, request: Request) -> None:
        token = _extract_token(request)
        if not security.verify_room_token(token, kind, room_id):
            raise HTTPException(status_code=401, detail="missing or invalid room token")
        if security.token_role(token, kind, room_id) == "viewer":
            raise HTTPException(status_code=403, detail="editor access required -- this token is read-only")

    return _dep


# ---------------------------------------------------------------------------
# Workspace: rooms as projects (Phase 17)
# ---------------------------------------------------------------------------


class RoomSummary(BaseModel):
    kind: str  # "drawing" | "mesh"
    room_id: str
    display_name: Optional[str] = None
    updated_at: float


@app.get("/api/workspace/rooms", response_model=list[RoomSummary])
async def list_workspace_rooms() -> list[RoomSummary]:
    """Backs the home page's room list. Deliberately ungated by any room
    token -- like the pre-existing `/api/rooms`/`/api/mesh-rooms`, a room
    id/kind/last-modified time isn't itself scoped to one room the way
    its *contents* are, so there's nothing here for `require_room_access`
    to check against. When auth is enabled, this still means every
    room's existence and last-modified time is visible workspace-wide;
    its content, thumbnail, and rename action remain individually
    token-gated (see below) -- documented in the README as an accepted,
    honest scope boundary, not a silent gap."""
    rows = [RoomSummary(kind=kind, **row) for kind in ("drawing", "mesh") for row in store.list_rooms_detailed(kind)]
    rows.sort(key=lambda r: r.updated_at, reverse=True)
    return rows


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


async def _create_share_link(kind: str, room_id: str, role: str) -> ShareLinkResponse:
    if not security.auth_enabled():
        raise HTTPException(
            status_code=400,
            detail="read-only share links need CRDT_CAD_SECRET configured on this server",
        )
    if role not in ("viewer", "editor"):
        raise HTTPException(status_code=400, detail="role must be 'viewer' or 'editor'")
    return ShareLinkResponse(token=security.mint_room_token(kind, room_id, role=role), role=role)


@app.post(
    "/api/rooms/{room_id}/share-link",
    response_model=ShareLinkResponse,
    dependencies=[Depends(require_editor_access("drawing"))],
)
async def create_drawing_share_link(room_id: str, req: ShareLinkRequest) -> ShareLinkResponse:
    return await _create_share_link("drawing", room_id, req.role)


@app.post(
    "/api/mesh/{room_id}/share-link",
    response_model=ShareLinkResponse,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def create_mesh_share_link(room_id: str, req: ShareLinkRequest) -> ShareLinkResponse:
    return await _create_share_link("mesh", room_id, req.role)


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


# ---------------------------------------------------------------------------
# AI text-to-3D generation (3D mesh rooms)
# ---------------------------------------------------------------------------


class GenerateMeshRequest(BaseModel):
    prompt: str


class GenerateMeshResult(BaseModel):
    actor: str
    interpretation_source: str  # "llm" | "heuristic"
    mesh_source: str  # "meshy" | "procedural"
    spec: dict
    vertex_count: int
    face_count: int
    triangle_count: int
    op_count: int
    batches: int


@app.post(
    "/api/mesh/{room_id}/generate",
    response_model=GenerateMeshResult,
    dependencies=[Depends(require_editor_access("mesh"))],
)
async def generate_mesh(room_id: str, req: GenerateMeshRequest, request: Request) -> GenerateMeshResult:
    """Text -> 3D: interprets ``req.prompt`` into a :class:`HouseSpec`
    (Claude Fable 5 if available, a regex heuristic otherwise), builds a
    deterministic procedural mesh from it, mints a chronological batch of
    CRDT ops for it under the ``ai_generator_bot`` actor identity, and
    commits them into the room in bounded-size chunks.

    The heavy work (LLM call + geometry construction) runs in a worker
    thread via ``asyncio.to_thread`` so it never blocks this room's (or
    any other room's) WebSocket loop, and is wrapped in a timeout so a
    hung LLM call can't tie up a thread-pool slot forever. Rate-limited
    per client IP (``CRDT_CAD_GENERATE_PER_MINUTE``, default 6/min) since
    each call burns real LLM spend and/or CPU time -- unlike the other
    endpoints in this module, this limit applies unconditionally, even
    when room auth is off, because the resource cost is real regardless
    of whether the deployment cares about access control.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")

    client_ip = request.client.host if request.client else "unknown"
    if not security.generate_rate_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="generation rate limit exceeded -- try again shortly")

    room = await mesh_room_manager.get_or_create(room_id)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(generate_mesh_ops, req.prompt),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"mesh generation exceeded the {GENERATION_TIMEOUT_SECONDS:.0f}s timeout",
        ) from exc
    except Exception as exc:
        logger.exception("room %s: mesh generation failed for prompt %r", room_id, req.prompt)
        raise HTTPException(status_code=422, detail=f"could not generate mesh: {exc}") from exc

    if not result.ops:
        raise HTTPException(status_code=422, detail="generation produced an empty mesh (malformed geometry)")

    batches = await room.commit_ops_batched(result.ops, actor=DEFAULT_ACTOR_ID, batch_size=GENERATION_OPS_BATCH_SIZE)
    logger.info(
        "room mesh/%s: generated %d ops (%d vertices, %d faces) from prompt %r via %s/%s, sent in %d batches",
        room_id, len(result.ops), result.vertex_count, result.face_count, req.prompt,
        result.interpretation_source, result.mesh_source, batches,
    )

    return GenerateMeshResult(
        actor=DEFAULT_ACTOR_ID,
        interpretation_source=result.interpretation_source,
        mesh_source=result.mesh_source,
        spec=result.spec.model_dump(),
        vertex_count=result.vertex_count,
        face_count=result.face_count,
        triangle_count=result.triangle_count,
        op_count=len(result.ops),
        batches=batches,
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

    if not security.verify_room_token(hello.get("token"), manager.kind, room_id):
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED)
        return

    actor = str(hello["actor"])
    role = security.token_role(hello.get("token"), manager.kind, room_id) or "editor"
    room.clients[actor] = websocket
    room.client_roles[actor] = role
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

    if room.client_roles.get(actor) == "viewer":
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
        await room.persist_async()
        await _check_and_broadcast_mesh_validity(room, touched_topology)
