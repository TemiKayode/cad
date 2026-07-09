from crdt_cad.ai.generator import DEFAULT_ACTOR_ID, generate_mesh_ops
from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT


def test_generate_mesh_ops_uses_heuristic_without_credentials():
    result = generate_mesh_ops("create a 4 bedroom house with a wooden floor")
    assert result.interpretation_source == "heuristic"
    assert result.spec.bedrooms == 4
    assert result.spec.floor_material == "wood"
    assert result.vertex_count > 0
    assert result.face_count > 0
    assert result.ops


def test_generate_mesh_ops_reports_procedural_mesh_source_by_default():
    """MESHY_API_KEY is unset by the autouse fixture in conftest.py --
    this must be the unchanged, always-available path."""
    result = generate_mesh_ops("a small cabin")
    assert result.mesh_source == "procedural"


def test_generate_mesh_ops_uses_meshy_mesh_when_the_adapter_succeeds(monkeypatch):
    """Phase 9's Meshy adapter is unverified against the live API (see
    crdt_cad.ai.meshy_adapter's module docstring) -- this only confirms
    generate_mesh_ops's own wiring: when MESHY_API_KEY is set and the
    adapter returns a mesh, that mesh (not the procedural one) is what
    gets used and reported."""
    import crdt_cad.ai.generator as generator_module
    from crdt_cad.ai.procedural_house import GeneratedMesh

    fake_mesh = GeneratedMesh(
        vertices={"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)},
        faces={"f1": ["a", "b", "c"]},
    )
    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    monkeypatch.setattr(generator_module, "generate_mesh_via_meshy", lambda prompt: fake_mesh)

    result = generate_mesh_ops("a small wooden chair")
    assert result.mesh_source == "meshy"
    assert result.vertex_count == 3
    assert result.face_count == 1


def test_generate_mesh_ops_falls_back_to_procedural_when_meshy_returns_none(monkeypatch):
    """MESHY_API_KEY set, but the adapter itself degrades (unreachable,
    bad response, etc. -- see test_meshy_adapter.py) -- generate_mesh_ops
    must still produce a usable mesh via the procedural pipeline, not an
    empty result."""
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setenv("MESHY_API_KEY", "fake-key-for-testing")
    monkeypatch.setattr(generator_module, "generate_mesh_via_meshy", lambda prompt: None)

    result = generate_mesh_ops("a 2 bedroom cottage")
    assert result.mesh_source == "procedural"
    assert result.vertex_count > 0
    assert result.face_count > 0


def test_generated_ops_apply_cleanly_to_a_fresh_room_document():
    """The whole point of the CRDT mapping rule: these ops must be
    injectable into a live document (as a remote replica would receive
    them), not just internally consistent inside the generator."""
    result = generate_mesh_ops("a 2 bedroom cottage with a marble floor")

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)

    assert len(doc.vertex_positions()) == result.vertex_count
    assert len(doc.face_loops()) == result.face_count
    for face_id, loop in doc.face_loops().items():
        assert len(loop) >= 3
        for vertex_id in loop:
            assert vertex_id in doc.vertex_positions()


def test_generated_ops_carry_the_ai_generator_bot_actor_identity():
    result = generate_mesh_ops("a small modern house")
    vertex_op = next(op for op in result.ops if op.target == "vertex")
    counter, actor = vertex_op.payload["id"]  # wire format: [counter, actor]
    assert actor == DEFAULT_ACTOR_ID


def test_floor_faces_are_tagged_with_the_interpreted_material():
    result = generate_mesh_ops("a house with a tiled floor")
    assert result.spec.floor_material == "tile"

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)

    materials = {
        face_id: doc.face_props_dict(face_id).get("material")
        for face_id in doc.face_loops()
    }
    assert "tile" in materials.values()


def test_generation_ops_are_chronologically_increasing_per_actor():
    result = generate_mesh_ops("a 3 bedroom house")
    counters = [op.payload["id"][0] for op in result.ops if "id" in op.payload]
    assert counters == sorted(counters)
    assert len(set(counters)) == len(counters)  # every minted OpId is unique


def test_generate_mesh_ops_scene_produces_object_ops_grouped_by_object():
    """Phase G2: a scene prompt sets generator_name == "scene" and
    populates ``object_ops`` (one sub-list per placed object, summing to
    ``ops``) so the server can commit each object as its own batch."""
    result = generate_mesh_ops("a table with four chairs around it")
    assert result.generator_name == "scene"
    assert result.object_ops is not None
    assert len(result.object_ops) == 5  # 1 table + 4 chairs
    assert sum(len(group) for group in result.object_ops) == len(result.ops)
    assert result.validation.ok


def test_non_scene_generation_leaves_object_ops_none():
    result = generate_mesh_ops("a wooden chair")
    assert result.generator_name != "scene"
    assert result.object_ops is None


def test_scene_faces_are_tagged_with_their_scene_object_index():
    result = generate_mesh_ops("a table with four chairs around it")

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)

    scene_object_values = {
        doc.face_props_dict(face_id).get("scene_object")
        for face_id in doc.face_loops()
    }
    assert scene_object_values == {"0", "1", "2", "3", "4"}


def test_scene_ops_apply_cleanly_and_produce_one_watertight_merged_mesh():
    result = generate_mesh_ops("a row of three shelves")

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)

    assert len(doc.vertex_positions()) == result.vertex_count
    assert len(doc.face_loops()) == result.face_count
    assert result.validation.watertight
    assert result.validation.manifold


def test_ops_batch_reconstructs_the_same_mesh_as_build_house_mesh_directly():
    from crdt_cad.ai.house_spec import HouseSpec
    from crdt_cad.ai.procedural_house import build_house_mesh

    result = generate_mesh_ops("a 6 bedroom house with a concrete floor")
    expected = build_house_mesh(HouseSpec(bedrooms=6, floors=1, floor_material="concrete"))

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)

    assert len(doc.vertex_positions()) == len(expected.vertices)
    assert len(doc.face_loops()) == len(expected.faces)
