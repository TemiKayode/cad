import time

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
