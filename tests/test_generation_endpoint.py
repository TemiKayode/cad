import sys
import time
import types

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

        seen_vertex_ops = 0
        for _ in range(body["batches"]):
            msg = ws.receive_json()
            assert msg["type"] == "ops"
            assert msg["from"] == "ai_generator_bot"
            seen_vertex_ops += sum(1 for op in msg["ops"] if op["target"] == "vertex")
        assert seen_vertex_ops == body["vertex_count"]


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
    def boom(prompt, *, actor_id="ai_generator_bot"):
        raise ValueError("simulated malformed geometry")

    monkeypatch.setattr(app_module, "generate_mesh_ops", boom)
    client = _client()
    resp = client.post("/api/mesh/genroom5/generate", json={"prompt": "anything"})
    assert resp.status_code == 422
    assert "simulated malformed geometry" in resp.json()["detail"]


def test_generate_endpoint_returns_504_on_timeout(monkeypatch):
    def slow(prompt, *, actor_id="ai_generator_bot"):
        time.sleep(0.3)
        return GenerationResult(
            ops=[], generator_name="house", spec=HouseSpec(), interpretation_source="heuristic", mesh_source="procedural",
            vertex_count=0, face_count=0, triangle_count=0, validation=_EMPTY_VALIDATION,
        )

    monkeypatch.setattr(app_module, "generate_mesh_ops", slow)
    monkeypatch.setattr(app_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
    client = _client()
    resp = client.post("/api/mesh/genroom6/generate", json={"prompt": "anything"})
    assert resp.status_code == 504


def test_generate_endpoint_returns_422_on_empty_mesh(monkeypatch):
    def empty(prompt, *, actor_id="ai_generator_bot"):
        return GenerationResult(
            ops=[], generator_name="house", spec=HouseSpec(), interpretation_source="heuristic", mesh_source="procedural",
            vertex_count=0, face_count=0, triangle_count=0, validation=_EMPTY_VALIDATION,
        )

    monkeypatch.setattr(app_module, "generate_mesh_ops", empty)
    client = _client()
    resp = client.post("/api/mesh/genroom7/generate", json={"prompt": "anything"})
    assert resp.status_code == 422
    assert "empty mesh" in resp.json()["detail"]
