import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from crdt_cad.ai.generator import GenerationResult
from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.validation import ValidationReport
from crdt_cad.server import app as app_module
from crdt_cad.server.app import app

_EMPTY_VALIDATION = ValidationReport(
    ok=True, watertight=True, manifold=True, within_bounds=True,
    vertex_count=0, face_count=0, triangle_count=0, bounding_box=(0.0, 0.0, 0.0),
)

# `isolated_store` fixture (autouse) lives in tests/conftest.py and applies here too.


def _client() -> TestClient:
    return TestClient(app)


def test_generate_endpoint_populates_the_room():
    client = _client()
    resp = client.post(
        "/api/mesh/genroom/generate",
        json={"prompt": "create a 4 bedroom house with a wooden floor"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["actor"] == "ai_generator_bot"
    assert body["interpretation_source"] == "heuristic"
    assert body["spec"]["bedrooms"] == 4
    assert body["spec"]["floor_material"] == "wood"
    assert body["vertex_count"] > 0
    assert body["face_count"] > 0
    assert body["op_count"] > 0
    assert body["batches"] >= 1

    export = client.get("/api/mesh/genroom/export/json").json()
    assert len(export["face_index"]["entries"]) == body["face_count"]


def test_generate_endpoint_rejects_empty_prompt():
    client = _client()
    resp = client.post("/api/mesh/genroom2/generate", json={"prompt": "   "})
    assert resp.status_code == 400


def test_generate_endpoint_broadcasts_ops_to_connected_clients():
    client = _client()
    with client.websocket_connect("/ws/mesh/genroom3") as ws:
        ws.send_json({"type": "hello", "actor": "watcher"})
        ws.receive_json()  # initial snapshot

        resp = client.post(
            "/api/mesh/genroom3/generate",
            json={"prompt": "a 1 bedroom house"},
        )
        assert resp.status_code == 200
        body = resp.json()

        # Phase G5: "understood: ..." chips arrive first, broadcast to the
        # whole room before any geometry lands.
        interpreting = ws.receive_json()
        assert interpreting["type"] == "generation_interpreting"
        assert interpreting["chips"]

        seen_vertex_ops = 0
        for _ in range(body["batches"]):
            msg = ws.receive_json()
            assert msg["type"] == "ops"
            assert msg["from"] == "ai_generator_bot"
            seen_vertex_ops += sum(1 for op in msg["ops"] if op["target"] == "vertex")
        assert seen_vertex_ops == body["vertex_count"]

        # ... and the full report card arrives last, also room-wide -- a
        # validity_warning may legitimately interleave first (the house
        # generator isn't held to strict winding consistency, see
        # validation.py), so scan forward rather than assume it's next.
        report_card = None
        for _ in range(3):
            msg = ws.receive_json()
            if msg["type"] == "report_card":
                report_card = msg
                break
        assert report_card is not None
        assert report_card["generation_id"] == body["generation_id"]


def test_generate_endpoint_batches_large_meshes(monkeypatch):
    """Forces a tiny batch size so a modest house still spans multiple
    batches, proving the relay never sends one giant frame for a large
    generated mesh."""
    monkeypatch.setattr(app_module, "GENERATION_OPS_BATCH_SIZE", 5)
    client = _client()
    resp = client.post(
        "/api/mesh/genroom4/generate",
        json={"prompt": "a 4 bedroom house"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["batches"] > 1
    assert body["batches"] == -(-body["op_count"] // 5)  # ceil division


def test_generate_endpoint_scene_prompt_populates_the_room_object_by_object():
    """Phase G2: each scene object is forced into its own batch
    (Room.commit_ops_grouped_batched), regardless of the default batch
    size, so `batches` is at least the object count."""
    client = _client()
    resp = client.post(
        "/api/mesh/genroom-scene/generate",
        json={"prompt": "a table with four chairs around it"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["generator"] == "scene"
    assert body["spec"]["objects"][0]["generator"] == "table"
    assert body["vertex_count"] > 0
    assert body["face_count"] > 0
    assert body["batches"] >= 5  # 1 table + 4 chairs, one batch boundary each

    export = client.get("/api/mesh/genroom-scene/export/json").json()
    assert len(export["face_index"]["entries"]) == body["face_count"]


def test_generate_endpoint_scene_batches_break_at_object_boundaries(monkeypatch):
    """Even with a batch size larger than any single object's own op
    count, every object still gets its own batch -- proving the boundary
    is forced by object membership, not incidentally by size."""
    monkeypatch.setattr(app_module, "GENERATION_OPS_BATCH_SIZE", 100_000)
    client = _client()
    with client.websocket_connect("/ws/mesh/genroom-scene2") as ws:
        ws.send_json({"type": "hello", "actor": "watcher"})
        ws.receive_json()  # initial snapshot

        resp = client.post(
            "/api/mesh/genroom-scene2/generate",
            json={"prompt": "a table with four chairs around it"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["batches"] == 5

        assert ws.receive_json()["type"] == "generation_interpreting"

        seen_scene_objects = set()
        for _ in range(body["batches"]):
            msg = ws.receive_json()
            assert msg["type"] == "ops"
            for op in msg["ops"]:
                if op["target"] == "face_prop" and op["payload"]["k"] == "scene_object":
                    seen_scene_objects.add(op["payload"]["v"])
        assert seen_scene_objects == {"0", "1", "2", "3", "4"}


def test_generate_endpoint_dsl_prompt_populates_the_room(monkeypatch):
    """End-to-end through real HTTP: a mocked LLM response routes to the
    'dsl' tool, generator.py executes the program, and the response's
    `spec` (a DSLProgramSpec) serializes cleanly through the same
    `spec: dict` response field every other path uses."""

    class _FakeToolUse:
        def __init__(self, name, input_):
            self.type = "tool_use"
            self.name = name
            self.input = input_

    class _FakeResponse:
        def __init__(self, name, input_):
            self.content = [_FakeToolUse(name, input_)]
            self.stop_reason = "tool_use"
            self.stop_details = None

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse("dsl", {"root": {"op": "cylinder", "radius": 0.3, "height": 1.2}, "material": "metal"})

    class _FakeBeta:
        messages = _FakeMessages()

    class _FakeClient:
        beta = _FakeBeta()

    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = lambda *a, **kw: _FakeClient()
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    client = _client()
    resp = client.post("/api/mesh/genroom-dsl/generate", json={"prompt": "a weird custom bracket shape"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["generator"] == "dsl"
    assert body["interpretation_source"] == "llm"
    assert body["spec"]["root"]["op"] == "cylinder"
    assert body["vertex_count"] > 0
    assert body["watertight"] is True

    export = client.get("/api/mesh/genroom-dsl/export/json").json()
    assert len(export["face_index"]["entries"]) == body["face_count"]


def test_generate_endpoint_response_includes_generation_id():
    client = _client()
    resp = client.post("/api/mesh/genroom-provenance/generate", json={"prompt": "a wooden chair"})
    body = resp.json()
    assert body["generation_id"]


def test_generate_endpoint_edit_of_regenerates_under_the_same_generation_id():
    client = _client()
    first = client.post("/api/mesh/genroom-edit/generate", json={"prompt": "a wooden table"}).json()

    edited = client.post(
        "/api/mesh/genroom-edit/generate",
        json={"prompt": "make it taller", "edit_of": first["generation_id"]},
    ).json()
    assert edited["generation_id"] == first["generation_id"]
    assert edited["generator"] == "table"
    assert edited["spec"]["height_m"] > first["spec"]["height_m"]

    # the room now has exactly one generation's worth of live geometry --
    # the old faces/vertices were removed, not left behind as duplicates
    export = client.get("/api/mesh/genroom-edit/export/json").json()
    live_faces = [e for e in export["face_index"]["entries"] if not e.get("d")]
    assert len(live_faces) == edited["face_count"]


def test_generate_endpoint_edit_of_unknown_generation_returns_422():
    client = _client()
    resp = client.post(
        "/api/mesh/genroom-edit2/generate", json={"prompt": "taller", "edit_of": "nonexistent_gen_id"},
    )
    assert resp.status_code == 422
    assert "nonexistent_gen_id" in resp.json()["detail"]


def test_generate_endpoint_edit_of_a_scene_generation_returns_422():
    client = _client()
    first = client.post(
        "/api/mesh/genroom-edit3/generate", json={"prompt": "a table with four chairs around it"},
    ).json()
    resp = client.post(
        "/api/mesh/genroom-edit3/generate",
        json={"prompt": "make the table bigger", "edit_of": first["generation_id"]},
    )
    assert resp.status_code == 422
    assert "scene" in resp.json()["detail"].lower()


def test_generate_endpoint_edit_does_not_disturb_other_generations_in_the_room():
    client = _client()
    table = client.post("/api/mesh/genroom-edit4/generate", json={"prompt": "a wooden table"}).json()
    chair = client.post("/api/mesh/genroom-edit4/generate", json={"prompt": "a wooden chair"}).json()

    client.post(
        "/api/mesh/genroom-edit4/generate",
        json={"prompt": "make it taller", "edit_of": table["generation_id"]},
    )

    export = client.get("/api/mesh/genroom-edit4/export/json").json()
    live_faces = [e for e in export["face_index"]["entries"] if not e.get("d")]
    assert len(live_faces) == table["face_count"] + chair["face_count"]


def test_generate_endpoint_returns_422_on_generation_failure(monkeypatch):
    def boom(prompt, generator_name, spec, source, *, actor_id="ai_generator_bot", **kwargs):
        raise ValueError("simulated malformed geometry")

    monkeypatch.setattr(app_module, "generate_ops_from_interpretation", boom)
    client = _client()
    resp = client.post("/api/mesh/genroom5/generate", json={"prompt": "anything"})
    assert resp.status_code == 422
    assert "simulated malformed geometry" in resp.json()["detail"]


def test_generate_endpoint_returns_504_on_timeout(monkeypatch):
    def slow(prompt, generator_name, spec, source, *, actor_id="ai_generator_bot", **kwargs):
        time.sleep(0.3)
        return GenerationResult(
            ops=[], generator_name="house", spec=HouseSpec(), interpretation_source="heuristic", mesh_source="procedural",
            vertex_count=0, face_count=0, triangle_count=0, validation=_EMPTY_VALIDATION,
        )

    monkeypatch.setattr(app_module, "generate_ops_from_interpretation", slow)
    monkeypatch.setattr(app_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
    client = _client()
    resp = client.post("/api/mesh/genroom6/generate", json={"prompt": "anything"})
    assert resp.status_code == 504


def test_generate_endpoint_returns_422_on_empty_mesh(monkeypatch):
    def empty(prompt, generator_name, spec, source, *, actor_id="ai_generator_bot", **kwargs):
        return GenerationResult(
            ops=[], generator_name="house", spec=HouseSpec(), interpretation_source="heuristic", mesh_source="procedural",
            vertex_count=0, face_count=0, triangle_count=0, validation=_EMPTY_VALIDATION,
        )

    monkeypatch.setattr(app_module, "generate_ops_from_interpretation", empty)
    client = _client()
    resp = client.post("/api/mesh/genroom7/generate", json={"prompt": "anything"})
    assert resp.status_code == 422
    assert "empty mesh" in resp.json()["detail"]


# -- Phase G5: report card fields, metrics, budget ---------------------------------


def test_generate_endpoint_response_includes_report_card_fields():
    client = _client()
    resp = client.post("/api/mesh/genroom-reportcard/generate", json={"prompt": "a wooden table"})
    body = resp.json()
    assert body["path"] == "registry"
    assert body["outcome"] == "success"
    assert body["planar"] is True
    assert body["non_planar_face_count"] == 0
    assert body["within_bounds"] is True
    assert len(body["bounding_box"]) == 3
    assert body["elapsed_seconds"] > 0.0
    assert body["interpretation_chips"]  # non-empty


def test_generate_endpoint_scene_path_label_is_scene():
    client = _client()
    resp = client.post("/api/mesh/genroom-reportcard2/generate", json={"prompt": "a table with four chairs around it"})
    assert resp.json()["path"] == "scene"


def test_generate_endpoint_edit_path_label_is_edit():
    client = _client()
    first = client.post("/api/mesh/genroom-reportcard3/generate", json={"prompt": "a wooden table"}).json()
    edited = client.post(
        "/api/mesh/genroom-reportcard3/generate",
        json={"prompt": "make it taller", "edit_of": first["generation_id"]},
    ).json()
    assert edited["path"] == "edit"


def test_generate_endpoint_increments_success_metric():
    before = app_module.metrics.generations_total.labels(outcome="success", path="registry")._value.get()
    client = _client()
    client.post("/api/mesh/genroom-metrics1/generate", json={"prompt": "a wooden chair"})
    after = app_module.metrics.generations_total.labels(outcome="success", path="registry")._value.get()
    assert after == before + 1


def test_generate_endpoint_increments_latency_histogram():
    before = app_module.metrics.generation_latency_seconds._sum.get()
    client = _client()
    client.post("/api/mesh/genroom-metrics2/generate", json={"prompt": "a wooden chair"})
    after = app_module.metrics.generation_latency_seconds._sum.get()
    assert after > before


def test_generate_endpoint_increments_failure_metric_on_validation_error(monkeypatch):
    def boom(prompt, generator_name, spec, source, *, actor_id="ai_generator_bot", **kwargs):
        raise ValueError("simulated failure")

    monkeypatch.setattr(app_module, "generate_ops_from_interpretation", boom)
    # the failure happens *after* interpretation but the endpoint only
    # commits to a path label once the whole pipeline succeeds -- "unknown"
    # is the honest label for a build-phase failure, not a guess at what
    # the path would have been.
    before = app_module.metrics.generations_total.labels(outcome="failure", path="unknown")._value.get()
    client = _client()
    resp = client.post("/api/mesh/genroom-metrics3/generate", json={"prompt": "a wooden chair"})
    assert resp.status_code == 422
    after = app_module.metrics.generations_total.labels(outcome="failure", path="unknown")._value.get()
    assert after == before + 1


def test_generate_budget_endpoint_reflects_remaining_capacity():
    client = _client()
    first = client.get("/api/mesh/genroom-budget/generate/budget").json()
    assert first["remaining"] == first["capacity"]  # unused IP starts full
    assert first["per_minute"] > 0

    client.post("/api/mesh/genroom-budget/generate", json={"prompt": "a wooden table"})
    second = client.get("/api/mesh/genroom-budget/generate/budget").json()
    # a real POST /generate takes real wall-clock time, during which the
    # bucket keeps continuously refilling -- allow generous slack rather
    # than assert an exact "spent exactly 1 token" delta.
    assert second["remaining"] < first["remaining"]


def test_generate_budget_endpoint_does_not_itself_spend_budget():
    client = _client()
    a = client.get("/api/mesh/genroom-budget2/generate/budget").json()
    b = client.get("/api/mesh/genroom-budget2/generate/budget").json()
    assert a["remaining"] == pytest.approx(b["remaining"], abs=0.01)


# -- Phase G7: matured Meshy path -- progress broadcast, budget, fallback ----------


def _fake_box_mesh():
    from crdt_cad.ai.mesh_builder import MeshBuilder, add_box

    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "metal")
    return b.mesh


def test_generate_endpoint_uses_meshy_and_broadcasts_progress(monkeypatch):
    async def fake_meshy_async(prompt, *, api_key=None, on_progress=None, face_budget=None):
        if on_progress:
            await on_progress({"stage": "queued", "task_id": "fake-1"})
            await on_progress({"stage": "in_progress", "status": "SUCCEEDED", "progress": 100})
            await on_progress({"stage": "downloading"})
            await on_progress({"stage": "done"})
        return _fake_box_mesh()

    monkeypatch.setattr(app_module, "generate_mesh_via_meshy_async", fake_meshy_async)
    monkeypatch.setattr(app_module, "meshy_api_key", lambda: "fake-key")

    client = _client()
    with client.websocket_connect("/ws/mesh/genroom-meshy1") as ws:
        ws.send_json({"type": "hello", "actor": "watcher"})
        ws.receive_json()

        resp = client.post("/api/mesh/genroom-meshy1/generate", json={"prompt": "a wooden table"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mesh_source"] == "meshy"
        assert body["path"] == "meshy"

        message_types = []
        for _ in range(10):
            msg = ws.receive_json()
            message_types.append(msg["type"])
            if msg["type"] == "report_card":
                break
        assert message_types[0] == "generation_interpreting"
        assert message_types.count("meshy_progress") == 4
        assert "ops" in message_types
        assert message_types[-1] == "report_card"


def test_generate_endpoint_meshy_progress_messages_carry_the_real_stage_payload(monkeypatch):
    async def fake_meshy_async(prompt, *, api_key=None, on_progress=None, face_budget=None):
        if on_progress:
            await on_progress({"stage": "decimating", "original_faces": 48000, "target_faces": 4000, "result_faces": 4000})
        return _fake_box_mesh()

    monkeypatch.setattr(app_module, "generate_mesh_via_meshy_async", fake_meshy_async)
    monkeypatch.setattr(app_module, "meshy_api_key", lambda: "fake-key")

    client = _client()
    with client.websocket_connect("/ws/mesh/genroom-meshy2") as ws:
        ws.send_json({"type": "hello", "actor": "watcher"})
        ws.receive_json()
        client.post("/api/mesh/genroom-meshy2/generate", json={"prompt": "a wooden table"})

        ws.receive_json()  # generation_interpreting
        progress = ws.receive_json()
        assert progress["type"] == "meshy_progress"
        assert progress["stage"] == "decimating"
        assert progress["original_faces"] == 48000
        assert progress["target_faces"] == 4000


def test_generate_endpoint_falls_back_to_procedural_when_meshy_returns_none(monkeypatch):
    async def fake_meshy_async(prompt, *, api_key=None, on_progress=None, face_budget=None):
        if on_progress:
            await on_progress({"stage": "failed", "error": "simulated Meshy failure"})
        return None

    monkeypatch.setattr(app_module, "generate_mesh_via_meshy_async", fake_meshy_async)
    monkeypatch.setattr(app_module, "meshy_api_key", lambda: "fake-key")

    client = _client()
    resp = client.post("/api/mesh/genroom-meshy3/generate", json={"prompt": "a wooden table"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mesh_source"] == "procedural"
    assert body["path"] == "registry"
    assert body["vertex_count"] > 0


def test_generate_endpoint_does_not_attempt_meshy_for_scenes(monkeypatch):
    """A scene has no single "the hosted mesh" to substitute in for --
    the Meshy path must not even be attempted."""
    def boom(prompt, **kwargs):
        raise AssertionError("must not attempt Meshy for a scene generation")

    monkeypatch.setattr(app_module, "generate_mesh_via_meshy_async", boom)
    monkeypatch.setattr(app_module, "meshy_api_key", lambda: "fake-key")

    client = _client()
    resp = client.post("/api/mesh/genroom-meshy4/generate", json={"prompt": "a table with four chairs around it"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "scene"
