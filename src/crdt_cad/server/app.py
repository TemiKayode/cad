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

    {"type": "snapshot", "doc": {...}, "frontier": {...}}          # new client
    {"type": "delta", "ops": [...], "frontier": {...}}             # reconnect

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

The server also re-broadcasts a full ``snapshot`` to every client in a
room on a fixed interval, so a late joiner or a client that missed
something for any reason resyncs without a special request.

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
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crdt_cad.ai.generator import DEFAULT_ACTOR_ID, generate_mesh_ops
from crdt_cad.crdt.clock import LamportClock, VectorClock
from crdt_cad.crdt.document import DocOp, DrawingDocument
from crdt_cad.crdt.mesh import MeshCRDT, MeshOp
from crdt_cad.export.dxf_io import drawing_from_dxf_bytes, drawing_to_dxf_bytes
from crdt_cad.export.stl_export import mesh_to_stl
from crdt_cad.export.svg_io import drawing_from_svg_string, drawing_to_svg_string
from crdt_cad.geometry.constraints import Constraint, Sketch
from crdt_cad.geometry.validity import GeometryError, validate_new_point
from crdt_cad.persistence.store import DocumentStore, SQLiteStore
from crdt_cad.server import metrics

logger = logging.getLogger("crdt_cad.server")
logging.basicConfig(level=logging.INFO)

SNAPSHOT_INTERVAL_SECONDS = 30
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

store: DocumentStore = SQLiteStore(DB_PATH)


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
    ) -> None:
        self.room_id = room_id
        self.kind = kind
        self.doc_class = doc_class
        self.op_from_dict = op_from_dict
        self.store = store
        self.clock = LamportClock(actor=f"__server__:{kind}:{room_id}")

        persisted = store.load(kind, room_id)
        if persisted:
            self.doc = doc_class.from_bytes(self.clock, persisted)
            logger.info("room %s/%s: hydrated from persisted snapshot (%d bytes)", kind, room_id, len(persisted))
        else:
            self.doc = doc_class(self.clock)

        self.clients: dict[str, WebSocket] = {}
        self._snapshot_task: asyncio.Task | None = None
        self._dirty_since_snapshot = False

    def mark_dirty(self) -> None:
        self._dirty_since_snapshot = True

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
                # heal, and a snapshot's payload size scales with the whole
                # document, not with what changed -- worth skipping.
                if self.clients and self._dirty_since_snapshot:
                    logger.info(
                        "room %s/%s: broadcasting periodic snapshot to %d clients",
                        self.kind, self.room_id, len(self.clients),
                    )
                    await self.broadcast(self.snapshot_message())
                    self._dirty_since_snapshot = False
        except asyncio.CancelledError:
            pass

    def snapshot_message(self) -> dict:
        return {"type": "snapshot", "doc": self.doc.to_dict(), "frontier": self.doc.frontier().to_dict()}

    async def broadcast(self, message: dict, exclude: str | None = None) -> None:
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
        for i in range(0, len(ops), max(1, batch_size)):
            chunk = ops[i : i + batch_size]
            for op in chunk:
                self.doc.apply(op)
            await self.broadcast({"type": "ops", "ops": [op.to_dict() for op in chunk], "from": actor})
            self.mark_dirty()
            batches += 1
            await asyncio.sleep(0)
        if ops:
            await self.persist_async()
        return batches


class RoomManager:
    def __init__(
        self,
        kind: str,
        doc_class,
        op_from_dict: Callable[[dict], object],
        store: DocumentStore,
    ) -> None:
        self.kind = kind
        self.doc_class = doc_class
        self.op_from_dict = op_from_dict
        self.store = store
        self.rooms: dict[str, Room] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, room_id: str) -> Room:
        async with self._lock:
            room = self.rooms.get(room_id)
            if room is None:
                room = Room(room_id, self.kind, self.doc_class, self.op_from_dict, self.store)
                self.rooms[room_id] = room
            return room

    def connection_count(self) -> int:
        return sum(len(r.clients) for r in self.rooms.values())


drawing_room_manager = RoomManager("drawing", DrawingDocument, DocOp.from_dict, store)
mesh_room_manager = RoomManager("mesh", MeshCRDT, MeshOp.from_dict, store)
room_manager = drawing_room_manager  # backwards-compatible alias

app = FastAPI(title="crdt-cad collaboration server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
async def index() -> FileResponse:
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
# Export / import (2D drawing rooms)
# ---------------------------------------------------------------------------


def _attachment(content: str | bytes, media_type: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/rooms/{room_id}/export/json")
async def export_drawing_json(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    return _attachment(json.dumps(room.doc.to_dict(), indent=2), "application/json", f"{room_id}.json")


@app.get("/api/rooms/{room_id}/export/svg")
async def export_drawing_svg(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    svg = drawing_to_svg_string(room.doc.path_list())
    return _attachment(svg, "image/svg+xml", f"{room_id}.svg")


@app.get("/api/rooms/{room_id}/export/dxf")
async def export_drawing_dxf(room_id: str) -> Response:
    room = await drawing_room_manager.get_or_create(room_id)
    data = drawing_to_dxf_bytes(room.doc.path_list())
    return _attachment(data, "application/dxf", f"{room_id}.dxf")


class ImportResult(BaseModel):
    layer_id: str
    path_count: int


async def _import_paths(room_id: str, paths: list[list[tuple[float, float]]], layer_name: str) -> ImportResult:
    room = await drawing_room_manager.get_or_create(room_id)
    layer_id, ops = room.doc.add_layer(layer_name)
    count = 0
    for pts in paths:
        if len(pts) < 2:
            continue
        _, path_ops = room.doc.add_path(layer_id, [tuple(p) for p in pts])
        ops.extend(path_ops)
        count += 1
    if ops:
        await room.broadcast({"type": "ops", "ops": [op.to_dict() for op in ops], "from": "__import__"})
        room.mark_dirty()
        await room.persist_async()
    return ImportResult(layer_id=layer_id, path_count=count)


@app.post("/api/rooms/{room_id}/import/svg", response_model=ImportResult)
async def import_drawing_svg(room_id: str, request: Request) -> ImportResult:
    body = await request.body()
    try:
        paths = drawing_from_svg_string(body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse SVG: {exc}") from exc
    return await _import_paths(room_id, paths, "Imported SVG")


@app.post("/api/rooms/{room_id}/import/dxf", response_model=ImportResult)
async def import_drawing_dxf(room_id: str, request: Request) -> ImportResult:
    body = await request.body()
    try:
        paths = drawing_from_dxf_bytes(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse DXF: {exc}") from exc
    return await _import_paths(room_id, paths, "Imported DXF")


# ---------------------------------------------------------------------------
# Export (3D mesh rooms)
# ---------------------------------------------------------------------------


@app.get("/api/mesh/{room_id}/export/json")
async def export_mesh_json(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    return _attachment(json.dumps(room.doc.to_dict(), indent=2), "application/json", f"{room_id}.json")


@app.get("/api/mesh/{room_id}/export/stl")
async def export_mesh_stl(room_id: str) -> Response:
    room = await mesh_room_manager.get_or_create(room_id)
    stl = mesh_to_stl(room.doc.vertex_positions(), room.doc.face_loops(), name=room_id.replace(" ", "_") or "mesh")
    return _attachment(stl, "model/stl", f"{room_id}.stl")


# ---------------------------------------------------------------------------
# AI text-to-3D generation (3D mesh rooms)
# ---------------------------------------------------------------------------


class GenerateMeshRequest(BaseModel):
    prompt: str


class GenerateMeshResult(BaseModel):
    actor: str
    interpretation_source: str  # "llm" | "heuristic"
    spec: dict
    vertex_count: int
    face_count: int
    triangle_count: int
    op_count: int
    batches: int


@app.post("/api/mesh/{room_id}/generate", response_model=GenerateMeshResult)
async def generate_mesh(room_id: str, req: GenerateMeshRequest) -> GenerateMeshResult:
    """Text -> 3D: interprets ``req.prompt`` into a :class:`HouseSpec`
    (Claude Fable 5 if available, a regex heuristic otherwise), builds a
    deterministic procedural mesh from it, mints a chronological batch of
    CRDT ops for it under the ``ai_generator_bot`` actor identity, and
    commits them into the room in bounded-size chunks.

    The heavy work (LLM call + geometry construction) runs in a worker
    thread via ``asyncio.to_thread`` so it never blocks this room's (or
    any other room's) WebSocket loop, and is wrapped in a timeout so a
    hung LLM call can't tie up a thread-pool slot forever.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt must not be empty")

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
        "room mesh/%s: generated %d ops (%d vertices, %d faces) from prompt %r via %s, sent in %d batches",
        room_id, len(result.ops), result.vertex_count, result.face_count, req.prompt, result.interpretation_source, batches,
    )

    return GenerateMeshResult(
        actor=DEFAULT_ACTOR_ID,
        interpretation_source=result.interpretation_source,
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


async def _serve_room(websocket: WebSocket, room_id: str, manager: RoomManager) -> None:
    await websocket.accept()
    room = await manager.get_or_create(room_id)

    try:
        hello = await websocket.receive_json()
    except WebSocketDisconnect:
        return

    if hello.get("type") != "hello" or not hello.get("actor"):
        await websocket.close(code=4400)
        return

    actor = str(hello["actor"])
    room.clients[actor] = websocket
    room.start_snapshot_loop()
    metrics.connections_total.inc()
    logger.info("room %s/%s: actor %s connected (%d clients)", room.kind, room_id, actor, len(room.clients))

    known_frontier = hello.get("known_frontier")
    if known_frontier:
        vc = VectorClock.from_dict(known_frontier)
        delta = room.doc.ops_since(vc)
        await websocket.send_json(
            {
                "type": "delta",
                "ops": [op.to_dict() for op in delta],
                "frontier": room.doc.frontier().to_dict(),
            }
        )
        logger.info("room %s/%s: sent %d-op reconnect delta to %s", room.kind, room_id, len(delta), actor)
    else:
        await websocket.send_json(room.snapshot_message())

    try:
        while True:
            message = await websocket.receive_json()
            await _handle_message(room, actor, message)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("room %s/%s: error handling messages from %s", room.kind, room_id, actor)
    finally:
        room.clients.pop(actor, None)
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
    """Pre-commit geometry validity gate for 2D path point inserts.

    Returns a rejection reason string if ``op`` should be refused, else
    None. Only ``path_geom`` inserts on drawing rooms are gated -- see
    the "Roadmap" section of the README for why mesh-side validity
    (manifoldness etc.) isn't gated here yet. Zero-length segments are
    always rejected; self-intersection is only enforced for paths
    created with the strict "Polygon" tool (freehand pen strokes
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


async def _handle_message(room: Room, actor: str, message: dict) -> None:
    msg_type = message.get("type")

    if msg_type == "signal":
        target_ws = room.clients.get(message.get("to"))
        if target_ws is not None:
            await target_ws.send_json({"type": "signal", "from": actor, "data": message.get("data")})
        return

    if msg_type == "save":
        await room.persist_async()
        ws = room.clients.get(actor)
        if ws is not None:
            await ws.send_json({"type": "saved", "at": time.time()})
        return

    if msg_type != "ops":
        return

    ops_wire = message.get("ops", [])
    start = time.perf_counter()
    accepted_wire = []
    for op_dict in ops_wire:
        op = room.op_from_dict(op_dict)
        reason = _validate_op(room, op)
        if reason is not None:
            metrics.geometry_rejections_total.inc()
            ws = room.clients.get(actor)
            if ws is not None:
                await ws.send_json({"type": "rejected", "reason": reason, "op": op_dict})
            continue
        room.doc.apply(op)
        accepted_wire.append(op_dict)
    metrics.merge_latency_seconds.observe(time.perf_counter() - start)
    metrics.ops_relayed_total.inc(len(accepted_wire))

    if accepted_wire:
        await room.broadcast({"type": "ops", "ops": accepted_wire, "from": actor}, exclude=actor)
        room.mark_dirty()
        await room.persist_async()
