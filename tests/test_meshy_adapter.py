"""Tests for the optional Meshy hosted-mesh-gen adapter (Phase 9 stretch
item). See crdt_cad.ai.meshy_adapter's module docstring: the actual
live-API request/response handling is NOT verified against a real Meshy
account (no API key was available while building this) -- what's
verified here is (a) the MESHY_API_KEY-unset and any-failure paths both
gracefully return None (never raise), and (b) mesh_bytes_to_generated_mesh
correctly parses a *real* mesh file (built and exported by trimesh
itself, not a hand-typed fixture) into the vertex/face dict shape the
rest of the CRDT injection pipeline expects. The orchestration test
mocks the HTTP layer to confirm _create_task -> _poll_until_done ->
_mesh_from_model_url are wired together correctly; it does not and
cannot confirm Meshy's real API actually looks like this.
"""

import sys

import pytest

trimesh = pytest.importorskip("trimesh")

from crdt_cad.ai import meshy_adapter  # noqa: E402
from crdt_cad.ai.meshy_adapter import generate_mesh_via_meshy, mesh_bytes_to_generated_mesh  # noqa: E402


def _box_glb_bytes() -> bytes:
    box = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    return box.export(file_type="glb")


def test_no_api_key_returns_none_without_any_network_call(monkeypatch):
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    assert generate_mesh_via_meshy("a chair") is None


def test_mesh_bytes_to_generated_mesh_parses_a_real_glb_box():
    """Uses trimesh to build and export a real box GLB (not a fixture
    typed by hand), then confirms mesh_bytes_to_generated_mesh parses it
    back into a sane vertex/face dict -- this is the one part of the
    Meshy pipeline that's genuinely, fully verified, since it's pure
    local mesh-format parsing with no network involved at all."""
    data = _box_glb_bytes()
    mesh = mesh_bytes_to_generated_mesh(data, file_type="glb")
    assert len(mesh.vertices) >= 8  # a box has 8 corners (possibly duplicated per-face by the exporter)
    assert len(mesh.faces) >= 12  # at least 12 triangles (2 per box face x 6 faces)
    for loop in mesh.faces.values():
        assert len(loop) == 3
        for vid in loop:
            assert vid in mesh.vertices


class _FakeResponse:
    def __init__(self, json_data=None, content=b"", status_ok=True):
        self._json = json_data
        self.content = content
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("simulated HTTP error")

    def json(self):
        return self._json


class _FakeRequestsModule:
    """Stands in for the real `requests` module -- generate_mesh_via_meshy
    does `import requests` lazily, so patching sys.modules["requests"]
    is what actually intercepts that import."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)


def test_full_flow_with_mocked_http_layer_produces_a_real_mesh(monkeypatch):
    """Mocks only the network boundary (task creation, one poll, model
    download) -- everything downstream (GLB parsing) is the real,
    verified code path from the test above."""
    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    glb_bytes = _box_glb_bytes()
    fake_requests = _FakeRequestsModule(
        [
            _FakeResponse(json_data={"result": "task-123"}),  # POST create task
            _FakeResponse(json_data={"status": "SUCCEEDED", "model_urls": {"glb": "https://example/model.glb"}}),  # GET poll
            _FakeResponse(content=glb_bytes),  # GET download
        ]
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    mesh = generate_mesh_via_meshy("a small wooden chair")
    assert mesh is not None
    assert len(mesh.vertices) >= 8
    assert len(mesh.faces) >= 12
    assert fake_requests.calls[0][0] == "POST"
    assert fake_requests.calls[1][0] == "GET"
    assert fake_requests.calls[2] == ("GET", "https://example/model.glb", {"timeout": 60})


def test_task_failed_status_gracefully_returns_none(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    fake_requests = _FakeRequestsModule(
        [
            _FakeResponse(json_data={"result": "task-123"}),
            _FakeResponse(json_data={"status": "FAILED"}),
        ]
    )
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert generate_mesh_via_meshy("anything") is None


def test_http_error_during_task_creation_gracefully_returns_none(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    fake_requests = _FakeRequestsModule([_FakeResponse(status_ok=False)])
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert generate_mesh_via_meshy("anything") is None


def test_malformed_json_response_gracefully_returns_none(monkeypatch):
    """A response missing the field this code expects (e.g. Meshy's real
    API shape differs from what's assumed here -- see module docstring)
    must degrade the same way any other failure does, not raise."""
    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    fake_requests = _FakeRequestsModule([_FakeResponse(json_data={"unexpected": "shape"})])
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert generate_mesh_via_meshy("anything") is None


def test_meshy_api_key_reads_env_var(monkeypatch):
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    assert meshy_adapter.meshy_api_key() is None
    monkeypatch.setenv("MESHY_API_KEY", "abc123")
    assert meshy_adapter.meshy_api_key() == "abc123"
