"""Optional hosted ML mesh generation via Meshy AI's text-to-3D API,
gated by ``MESHY_API_KEY`` -- Phase 9's stretch item.

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
an unexpected JSON shape -- both are caught by
:func:`generate_mesh_via_meshy`'s broad `except Exception`, logged, and
treated exactly like "not configured": fall back to the deterministic
procedural pipeline (see ``crdt_cad.ai.generator.generate_mesh_ops``).
That fallback -- and the mesh-dict conversion from a well-formed GLB
via a mocked HTTP layer -- *is* verified; see
``tests/test_meshy_adapter.py``.

Needs ``requests`` (the ``meshy`` extra, lazily imported here the same
way ``pymeshlab``/``anthropic`` are elsewhere in this project) and
``trimesh`` (already a core dependency, used by
``crdt_cad.geometry.mesh_validity``) to parse whatever mesh format Meshy
returns -- GLB is requested; trimesh handles it (and OBJ/STL/etc, should
the API return one of those instead) without any hand-rolled parsing.
"""

from __future__ import annotations

import io
import logging
import os
import time

from crdt_cad.ai.procedural_house import GeneratedMesh

logger = logging.getLogger("crdt_cad.ai.meshy")

MESHY_API_BASE = "https://api.meshy.ai"
_POLL_INTERVAL_SECONDS = 5.0
_MAX_POLL_SECONDS = 300.0


def meshy_api_key() -> str | None:
    return os.environ.get("MESHY_API_KEY") or None


def generate_mesh_via_meshy(prompt: str, *, api_key: str | None = None) -> GeneratedMesh | None:
    """Returns a `GeneratedMesh` built from Meshy's text-to-3D API, or
    `None` if `MESHY_API_KEY` isn't set (or passed explicitly) or if
    anything at all about the call fails. Callers (`generate_mesh_ops`)
    treat `None` as "fall back to the procedural pipeline" -- this never
    raises out to a caller, by design, given the live API path here is
    unverified (see module docstring)."""
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
    return resp.json()["result"]


def _poll_until_done(task_id: str, key: str, requests_module) -> str:
    deadline = time.monotonic() + _MAX_POLL_SECONDS
    while time.monotonic() < deadline:
        resp = requests_module.get(
            f"{MESHY_API_BASE}/openapi/v2/text-to-3d/{task_id}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "SUCCEEDED":
            return data["model_urls"]["glb"]
        if status in ("FAILED", "CANCELED"):
            raise RuntimeError(f"Meshy task {task_id} ended with status {status!r}")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Meshy task {task_id} did not complete within {_MAX_POLL_SECONDS}s")


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
