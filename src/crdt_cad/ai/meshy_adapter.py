"""Optional hosted ML mesh generation via Meshy AI's text-to-3D API,
gated by ``MESHY_API_KEY`` -- Phase 9's stretch item, matured in Phase
G7 (Part 5) into a real async job flow with progress streaming and a
mesh-budget pipeline.

*** NOT VERIFIED AGAINST THE LIVE API ***
No Meshy API key was available in the environment this was written in.
The request/response handling below (create a task, poll for
completion, download the resulting GLB, parse it with ``trimesh``) is
implemented against my best understanding of Meshy's documented
text-to-3D API, not confirmed against a real call -- per the brief's own
guidance for exactly this situation ("must not be claimed as verified
unless actually exercised against the live API"), this is built and
degrades safely, but is not claimed to work. If Meshy's actual API
shape differs from what's assumed here (endpoint paths, auth header,
response field names, poll semantics), the most likely failure modes
are an ``HTTPError`` from a non-2xx response or a ``KeyError`` reading
an unexpected JSON shape -- both surface as a typed :class:`MeshyError`
subclass, caught by both entry points' broad ``except Exception``,
logged, and treated exactly like "not configured": fall back to the
deterministic procedural pipeline. That fallback -- and the mesh-dict
conversion from a well-formed GLB via a mocked HTTP layer, and the
decimation pipeline against a *real* trimesh-built mesh -- *is*
verified; see ``tests/test_meshy_adapter.py``.

Two entry points:

- :func:`generate_mesh_via_meshy` -- the original, fully synchronous
  version (submit/poll/download all block, ``time.sleep`` between
  polls). Kept unchanged for backward compatibility with any direct,
  non-endpoint caller (e.g. ``generate_ops_from_interpretation`` when
  invoked outside a live room, as it already is in
  ``tests/test_generator.py``) -- callers with no room to broadcast
  progress to have no reason to prefer the async version.
- :func:`generate_mesh_via_meshy_async` -- Phase G7's matured path.
  Each individual HTTP call still runs in a worker thread
  (``asyncio.to_thread``, so no new async HTTP dependency is needed --
  ``requests`` stays the only one), but the *wait* between polls and
  the progress notifications happen on the event loop, so a caller
  with access to a live room (the ``/generate`` endpoint) can broadcast
  real "queued" / "in progress NN%" / "downloading" / "decimating"
  messages while a potentially minutes-long generation is in flight,
  not leave the room silent until it's done.

Needs ``requests`` and ``fast_simplification`` (both the ``meshy``
extra, lazily imported here the same way ``pymeshlab``/``anthropic``
are elsewhere in this project) and ``trimesh`` (already a core
dependency) to parse and, if needed, decimate whatever mesh format
Meshy returns.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from crdt_cad.ai.procedural_house import GeneratedMesh

logger = logging.getLogger("crdt_cad.ai.meshy")

MESHY_API_BASE = "https://api.meshy.ai"
_POLL_INTERVAL_SECONDS = 5.0
_MAX_POLL_SECONDS = 300.0

ProgressCallback = Callable[[dict], Awaitable[None]]


class MeshyError(Exception):
    """Base for every typed Meshy-adapter failure. Both entry points
    catch this (and any other exception) broadly and return ``None``
    rather than raising -- the typed hierarchy exists for tests and
    logging to distinguish failure modes, not to change the "never
    raises to the generation pipeline" contract."""


class MeshyTaskFailedError(MeshyError):
    def __init__(self, task_id: str, status: str) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__(f"Meshy task {task_id} ended with status {status!r}")


class MeshyTimeoutError(MeshyError):
    def __init__(self, task_id: str, elapsed_seconds: float) -> None:
        self.task_id = task_id
        self.elapsed_seconds = elapsed_seconds
        super().__init__(f"Meshy task {task_id} did not complete within {elapsed_seconds:.0f}s")


class MeshyResponseShapeError(MeshyError):
    """The response parsed as JSON but didn't have the field(s) this
    module expects -- the concrete signal that Meshy's real API shape
    differs from what's assumed here (see module docstring)."""


class MeshyBudgetExceededError(MeshyError):
    """A mesh could not be decimated down to the configured face budget
    -- Phase G7's "refuse clearly" requirement: this is surfaced to the
    caller as a real failure (falls back to the procedural pipeline,
    same as any other Meshy failure), never a silently-oversized mesh
    injected into the room."""

    def __init__(self, original_faces: int, target_faces: int, reached_faces: int) -> None:
        self.original_faces = original_faces
        self.target_faces = target_faces
        self.reached_faces = reached_faces
        super().__init__(
            f"could not decimate {original_faces} faces down to the {target_faces}-face budget "
            f"(reached {reached_faces})"
        )


def meshy_api_key() -> str | None:
    return os.environ.get("MESHY_API_KEY") or None


def meshy_face_budget() -> int:
    """Phase G7 mesh-budget pipeline: the configurable face ceiling an
    imported diffusion mesh gets decimated to *before* CRDT injection
    -- a hosted diffusion model can easily return tens of thousands of
    triangles, more than this project's collaborative-editing UI (a
    per-face property panel, a face list the user can click through)
    is built for."""
    return int(os.environ.get("CRDT_CAD_MESHY_FACE_BUDGET", "4000"))


# -- synchronous entry point (unchanged, backward-compatible) ----------------------


def generate_mesh_via_meshy(prompt: str, *, api_key: str | None = None) -> GeneratedMesh | None:
    """Returns a `GeneratedMesh` built from Meshy's text-to-3D API, or
    `None` if `MESHY_API_KEY` isn't set (or passed explicitly) or if
    anything at all about the call fails. Callers (`generate_mesh_ops`)
    treat `None` as "fall back to the procedural pipeline" -- this never
    raises out to a caller, by design, given the live API path here is
    unverified (see module docstring). Does not apply the Phase G7
    budget pipeline itself -- see :func:`generate_mesh_via_meshy_async`
    for the matured path that does."""
    key = api_key or meshy_api_key()
    if not key:
        return None
    try:
        import requests

        task_id = _create_task(prompt, key, requests)
        model_url = _poll_until_done(task_id, key, requests)
        return _mesh_from_model_url(model_url, requests)
    except ImportError:
        logger.warning("MESHY_API_KEY is set but `requests` isn't installed -- pip install crdt-cad[meshy]")
        return None
    except Exception:
        logger.exception("Meshy generation failed -- falling back to the procedural pipeline")
        return None


def _create_task(prompt: str, key: str, requests_module) -> str:
    resp = requests_module.post(
        f"{MESHY_API_BASE}/openapi/v2/text-to-3d",
        headers={"Authorization": f"Bearer {key}"},
        json={"mode": "preview", "prompt": prompt, "art_style": "realistic"},
        timeout=30,
    )
    resp.raise_for_status()
    try:
        return resp.json()["result"]
    except KeyError as exc:
        raise MeshyResponseShapeError(f"task-creation response missing 'result': {exc}") from exc


def _poll_once(task_id: str, key: str, requests_module) -> dict:
    resp = requests_module.get(
        f"{MESHY_API_BASE}/openapi/v2/text-to-3d/{task_id}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _poll_until_done(task_id: str, key: str, requests_module) -> str:
    deadline = time.monotonic() + _MAX_POLL_SECONDS
    while time.monotonic() < deadline:
        data = _poll_once(task_id, key, requests_module)
        status = data.get("status")
        if status == "SUCCEEDED":
            try:
                return data["model_urls"]["glb"]
            except KeyError as exc:
                raise MeshyResponseShapeError(f"SUCCEEDED response missing model_urls.glb: {exc}") from exc
        if status in ("FAILED", "CANCELED"):
            raise MeshyTaskFailedError(task_id, status)
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise MeshyTimeoutError(task_id, _MAX_POLL_SECONDS)


def _mesh_from_model_url(url: str, requests_module) -> GeneratedMesh:
    resp = requests_module.get(url, timeout=60)
    resp.raise_for_status()
    return mesh_bytes_to_generated_mesh(resp.content, file_type="glb")


def mesh_bytes_to_generated_mesh(data: bytes, *, file_type: str = "glb") -> GeneratedMesh:
    """Converts a mesh file (GLB by default -- whatever `trimesh.load`
    supports) into the plain vertex/face dict shape the rest of this
    project's CRDT injection path expects. Split out from
    `_mesh_from_model_url` so tests can exercise the parsing logic
    directly against a well-formed file, without needing (or faking) an
    HTTP layer at all.
    """
    import trimesh

    loaded = trimesh.load(io.BytesIO(data), file_type=file_type)
    mesh = loaded.to_geometry() if hasattr(loaded, "to_geometry") else loaded

    out = GeneratedMesh()
    vertex_ids = [f"v{i}" for i in range(len(mesh.vertices))]
    for vid, v in zip(vertex_ids, mesh.vertices):
        out.vertices[vid] = (float(v[0]), float(v[1]), float(v[2]))
    for i, tri in enumerate(mesh.faces):
        out.faces[f"f{i}"] = [vertex_ids[tri[0]], vertex_ids[tri[1]], vertex_ids[tri[2]]]
    return out


# -- Phase G7: mesh-budget pipeline (decimation before CRDT injection) -------------


def decimate_to_budget(mesh: GeneratedMesh, max_faces: int) -> tuple[GeneratedMesh, bool, int]:
    """Simplifies `mesh` down to at most `max_faces` triangles via real
    quadric-error decimation (`trimesh`'s `fast_simplification`
    backend) if it exceeds the budget. Returns ``(result_mesh,
    was_decimated, original_triangle_count)`` so the caller can state
    the tradeoff plainly ("simplified 48,000 -> 4,000 faces") rather
    than silently swapping geometry. Raises
    :class:`MeshyBudgetExceededError` -- never silently injects an
    over-budget mesh -- if decimation can't reach a reasonable margin
    over the target (some slack is allowed: quadric decimation doesn't
    always land on the exact requested count)."""
    from crdt_cad.ai.mesh_builder import from_trimesh, to_trimesh

    original_count = mesh.triangle_count()
    if original_count <= max_faces:
        return mesh, False, original_count

    tri = to_trimesh(mesh)
    try:
        simplified = tri.simplify_quadric_decimation(face_count=max_faces)
    except Exception as exc:
        raise MeshyBudgetExceededError(original_count, max_faces, original_count) from exc

    reached = len(simplified.faces)
    if reached > max_faces * 1.5:
        raise MeshyBudgetExceededError(original_count, max_faces, reached)

    material = next(iter(mesh.face_materials.values()), "")
    return from_trimesh(simplified, material), True, original_count


# -- Phase G7: async job flow with progress streaming ------------------------------


async def generate_mesh_via_meshy_async(
    prompt: str,
    *,
    api_key: str | None = None,
    on_progress: Optional[ProgressCallback] = None,
    face_budget: Optional[int] = None,
) -> GeneratedMesh | None:
    """The matured Phase G7 path: submit -> poll -> stream progress ->
    import -> decimate to budget. Each HTTP call runs in a worker
    thread (`asyncio.to_thread`); the polling *wait* and every progress
    notification happen on the event loop, so a caller with a live room
    (see the `/generate` endpoint) can broadcast real status while a
    potentially minutes-long generation is in flight. `on_progress`,
    if given, is awaited with a small dict payload at each stage
    (`{"stage": "queued" | "in_progress" | "downloading" |
    "decimating" | "done" | "failed", ...}`).

    Returns `None` on any failure (never raises) -- the same contract
    as the synchronous `generate_mesh_via_meshy`, so callers can treat
    both identically ("fall back to the procedural pipeline").
    """
    key = api_key or meshy_api_key()
    if not key:
        return None

    async def notify(payload: dict) -> None:
        if on_progress is not None:
            await on_progress(payload)

    try:
        import requests
    except ImportError:
        logger.warning("MESHY_API_KEY is set but `requests` isn't installed -- pip install crdt-cad[meshy]")
        return None

    try:
        task_id = await asyncio.to_thread(_create_task, prompt, key, requests)
        await notify({"stage": "queued", "task_id": task_id})

        model_url = await _poll_until_done_async(task_id, key, requests, notify)

        await notify({"stage": "downloading"})
        mesh = await asyncio.to_thread(_mesh_from_model_url, model_url, requests)

        budget = face_budget if face_budget is not None else meshy_face_budget()
        result_mesh, was_decimated, original_count = await asyncio.to_thread(decimate_to_budget, mesh, budget)
        if was_decimated:
            await notify({
                "stage": "decimating",
                "original_faces": original_count,
                "target_faces": budget,
                "result_faces": result_mesh.triangle_count(),
            })

        await notify({"stage": "done"})
        return result_mesh
    except MeshyError as exc:
        logger.warning("Meshy generation failed (%s) -- falling back to the procedural pipeline", exc)
        await notify({"stage": "failed", "error": str(exc)})
        return None
    except Exception:
        logger.exception("Meshy generation failed unexpectedly -- falling back to the procedural pipeline")
        await notify({"stage": "failed", "error": "unexpected error"})
        return None


async def _poll_until_done_async(task_id: str, key: str, requests_module, notify: Callable[[dict], Awaitable[None]]) -> str:
    deadline = time.monotonic() + _MAX_POLL_SECONDS
    while time.monotonic() < deadline:
        data = await asyncio.to_thread(_poll_once, task_id, key, requests_module)
        status = data.get("status")
        await notify({"stage": "in_progress", "status": status, "progress": data.get("progress")})
        if status == "SUCCEEDED":
            try:
                return data["model_urls"]["glb"]
            except KeyError as exc:
                raise MeshyResponseShapeError(f"SUCCEEDED response missing model_urls.glb: {exc}") from exc
        if status in ("FAILED", "CANCELED"):
            raise MeshyTaskFailedError(task_id, status)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise MeshyTimeoutError(task_id, _MAX_POLL_SECONDS)
