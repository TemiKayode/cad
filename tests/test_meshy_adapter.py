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
pytest.importorskip("fast_simplification")

from crdt_cad.ai import meshy_adapter  # noqa: E402
from crdt_cad.ai.meshy_adapter import (  # noqa: E402
    MeshyBudgetExceededError,
    MeshyResponseShapeError,
    MeshyTaskFailedError,
    MeshyTimeoutError,
    decimate_to_budget,
    generate_mesh_via_meshy,
    generate_mesh_via_meshy_async,
    mesh_bytes_to_generated_mesh,
)
from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, from_trimesh  # noqa: E402


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


# -- Phase G7: typed error hierarchy ------------------------------------------------


def test_typed_errors_carry_their_own_context():
    task_failed = MeshyTaskFailedError("task-1", "FAILED")
    assert task_failed.task_id == "task-1" and task_failed.status == "FAILED"
    assert "FAILED" in str(task_failed)

    timeout = MeshyTimeoutError("task-2", 300.0)
    assert timeout.task_id == "task-2"
    assert "300" in str(timeout)

    shape_error = MeshyResponseShapeError("missing field 'x'")
    assert "missing field" in str(shape_error)

    budget = MeshyBudgetExceededError(original_faces=48000, target_faces=4000, reached_faces=9000)
    assert budget.original_faces == 48000 and budget.target_faces == 4000 and budget.reached_faces == 9000
    assert "48000" in str(budget) and "4000" in str(budget)


def test_task_failed_status_raises_the_typed_error_internally(monkeypatch):
    """The public entry point still returns None (unchanged contract),
    but the internal poll helper raises the specific typed error -- this
    is what the docstring's "typed errors" claim is actually about."""
    from crdt_cad.ai.meshy_adapter import _poll_until_done

    fake_requests = _FakeRequestsModule([_FakeResponse(json_data={"status": "FAILED"})])
    with pytest.raises(MeshyTaskFailedError):
        _poll_until_done("task-x", "key", fake_requests)


def test_missing_model_url_raises_response_shape_error(monkeypatch):
    from crdt_cad.ai.meshy_adapter import _poll_until_done

    fake_requests = _FakeRequestsModule([_FakeResponse(json_data={"status": "SUCCEEDED"})])  # no model_urls
    with pytest.raises(MeshyResponseShapeError):
        _poll_until_done("task-x", "key", fake_requests)


# -- Phase G7: mesh-budget pipeline (decimation) -------------------------------------


def _high_poly_mesh(subdivisions=4):
    sphere = trimesh.creation.icosphere(subdivisions=subdivisions)
    return from_trimesh(sphere, "metal")


def test_decimate_to_budget_leaves_an_under_budget_mesh_untouched():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "wood")
    result, was_decimated, original = decimate_to_budget(b.mesh, max_faces=500)
    assert was_decimated is False
    assert result is b.mesh
    assert original == b.mesh.triangle_count()


def test_decimate_to_budget_simplifies_an_over_budget_mesh():
    mesh = _high_poly_mesh()
    original_count = mesh.triangle_count()
    assert original_count > 1000
    result, was_decimated, original = decimate_to_budget(mesh, max_faces=200)
    assert was_decimated is True
    assert original == original_count
    assert result.triangle_count() <= 200 * 1.5
    assert result.triangle_count() < original_count


def test_decimate_to_budget_result_is_a_real_valid_mesh():
    from crdt_cad.ai.validation import validate_generated_mesh

    mesh = _high_poly_mesh()
    result, _, _ = decimate_to_budget(mesh, max_faces=300)
    report = validate_generated_mesh(result)
    assert report.ok, report.errors


def test_decimate_to_budget_refuses_clearly_when_decimation_cannot_reach_the_target(monkeypatch):
    """Phase G7's "refuse clearly" requirement: never silently inject an
    over-budget mesh. Simulates decimation landing far short of the
    target by monkeypatching trimesh's own method."""
    mesh = _high_poly_mesh()

    class _StubSimplified:
        faces = list(range(9000))  # way over any reasonable target

    def _stub_decimate(self, face_count):
        return _StubSimplified()

    monkeypatch.setattr(trimesh.Trimesh, "simplify_quadric_decimation", _stub_decimate)
    with pytest.raises(MeshyBudgetExceededError) as excinfo:
        decimate_to_budget(mesh, max_faces=200)
    assert excinfo.value.target_faces == 200
    assert excinfo.value.reached_faces == 9000


# -- Phase G7: async job flow with progress streaming --------------------------------


class _AsyncFakeResponse(_FakeResponse):
    pass


def _install_fake_requests(monkeypatch, responses):
    fake_requests = _FakeRequestsModule(responses)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    return fake_requests


async def test_async_meshy_returns_none_without_an_api_key(monkeypatch):
    monkeypatch.delenv("MESHY_API_KEY", raising=False)
    result = await generate_mesh_via_meshy_async("a chair")
    assert result is None


async def test_async_meshy_full_flow_notifies_progress_in_order(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    glb_bytes = _box_glb_bytes()
    _install_fake_requests(monkeypatch, [
        _FakeResponse(json_data={"result": "task-123"}),
        _FakeResponse(json_data={"status": "IN_PROGRESS", "progress": 40}),
        _FakeResponse(json_data={"status": "SUCCEEDED", "model_urls": {"glb": "https://example/model.glb"}}),
        _FakeResponse(content=glb_bytes),
    ])

    events = []

    async def on_progress(payload):
        events.append(payload)

    mesh = await generate_mesh_via_meshy_async("a small wooden chair", on_progress=on_progress, face_budget=10_000)
    assert mesh is not None
    assert len(mesh.vertices) >= 8

    stages = [e["stage"] for e in events]
    assert stages == ["queued", "in_progress", "in_progress", "downloading", "done"]


async def test_async_meshy_applies_the_face_budget_and_notifies_decimation(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    sphere = trimesh.creation.icosphere(subdivisions=4)
    glb_bytes = sphere.export(file_type="glb")
    _install_fake_requests(monkeypatch, [
        _FakeResponse(json_data={"result": "task-123"}),
        _FakeResponse(json_data={"status": "SUCCEEDED", "model_urls": {"glb": "https://example/model.glb"}}),
        _FakeResponse(content=glb_bytes),
    ])

    events = []

    async def on_progress(payload):
        events.append(payload)

    mesh = await generate_mesh_via_meshy_async("a decorative sphere", on_progress=on_progress, face_budget=100)
    assert mesh is not None
    assert mesh.triangle_count() <= 150

    decimating_events = [e for e in events if e["stage"] == "decimating"]
    assert len(decimating_events) == 1
    assert decimating_events[0]["target_faces"] == 100
    assert decimating_events[0]["original_faces"] > 100


async def test_async_meshy_task_failure_notifies_failed_stage_and_returns_none(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    _install_fake_requests(monkeypatch, [
        _FakeResponse(json_data={"result": "task-123"}),
        _FakeResponse(json_data={"status": "FAILED"}),
    ])

    events = []

    async def on_progress(payload):
        events.append(payload)

    mesh = await generate_mesh_via_meshy_async("anything", on_progress=on_progress)
    assert mesh is None
    assert events[-1]["stage"] == "failed"


async def test_async_meshy_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    monkeypatch.setattr(meshy_adapter, "_MAX_POLL_SECONDS", 0.0)
    monkeypatch.setattr(meshy_adapter, "_POLL_INTERVAL_SECONDS", 0.0)
    _install_fake_requests(monkeypatch, [_FakeResponse(json_data={"result": "task-123"})])
    result = await generate_mesh_via_meshy_async("anything")
    assert result is None


async def test_async_meshy_works_without_a_progress_callback():
    """`on_progress` is optional -- must not raise if omitted."""
    result = await generate_mesh_via_meshy_async("anything", api_key=None)
    assert result is None  # no key resolved, returns cleanly with no callback at all
