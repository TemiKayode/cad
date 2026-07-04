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
