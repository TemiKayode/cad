import pytest

from crdt_cad.ai.dsl import DSLProgramSpec
from crdt_cad.ai.generator import (
    DEFAULT_ACTOR_ID,
    EditNotSupportedError,
    generate_edit_ops,
    generate_mesh_ops,
    generation_geometry,
    interpretation_chips,
)
from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.interpreter import _heuristic_interpret
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


_VALID_BOX_PROGRAM = DSLProgramSpec(root={"op": "box", "size": [1.0, 1.0, 1.0]}, material="metal")
_OVERSIZED_BOX_PROGRAM = DSLProgramSpec(root={"op": "box", "size": [500.0, 1.0, 1.0]}, material="metal")


def test_generate_mesh_ops_dsl_succeeds_on_the_first_try(monkeypatch):
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _VALID_BOX_PROGRAM, "llm"))

    def boom(*a, **kw):
        raise AssertionError("repair should not be called when the first attempt succeeds")

    monkeypatch.setattr(generator_module, "llm_repair_dsl_program", boom)

    result = generate_mesh_ops("a weird bracket")
    assert result.generator_name == "dsl"
    assert result.dsl_attempts == [{"attempt": 0, "outcome": "ok", "error": None}]
    assert result.validation.ok
    assert result.vertex_count > 0


def test_generate_mesh_ops_dsl_repairs_after_one_failure(monkeypatch):
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _OVERSIZED_BOX_PROGRAM, "llm"))
    monkeypatch.setattr(
        generator_module, "llm_repair_dsl_program",
        lambda prompt, program, error: {"root": {"op": "box", "size": [1.0, 1.0, 1.0]}, "material": "metal"},
    )

    result = generate_mesh_ops("a weird bracket")
    assert result.generator_name == "dsl"
    assert [a["outcome"] for a in result.dsl_attempts] == ["failed", "ok"]
    assert "exceeds" in result.dsl_attempts[0]["error"]
    assert result.validation.ok


def test_generate_mesh_ops_dsl_falls_back_to_registry_after_exhausting_repairs(monkeypatch):
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _OVERSIZED_BOX_PROGRAM, "llm"))
    # every repair attempt returns another invalid program -- never recovers
    monkeypatch.setattr(
        generator_module, "llm_repair_dsl_program",
        lambda prompt, program, error: {"root": {"op": "box", "size": [999.0, 1.0, 1.0]}, "material": "metal"},
    )

    result = generate_mesh_ops("a weird chair-like bracket")
    assert result.generator_name != "dsl"
    assert len(result.dsl_attempts) == generator_module.MAX_DSL_REPAIR_ATTEMPTS + 1
    assert all(a["outcome"] == "failed" for a in result.dsl_attempts)
    assert result.validation.ok  # the fallback mesh itself is still a real, valid object
    assert result.vertex_count > 0


def test_generate_mesh_ops_dsl_fallback_prefers_a_keyword_match_over_the_house_default(monkeypatch):
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _OVERSIZED_BOX_PROGRAM, "llm"))
    monkeypatch.setattr(generator_module, "llm_repair_dsl_program", lambda prompt, program, error: dict(program))

    result = generate_mesh_ops("a strange chair with an odd backrest")
    assert result.generator_name == "chair"


def test_generate_mesh_ops_dsl_never_calls_repair_when_source_is_not_llm(monkeypatch):
    """Defensive guard: `_heuristic_interpret` never returns "dsl" (see
    test_interpreter.py), but if generator_name somehow ends up "dsl"
    with source != "llm", the repair loop must not attempt an LLM call
    (there may be no credentials at all on this path)."""
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _OVERSIZED_BOX_PROGRAM, "heuristic"))

    def boom(*a, **kw):
        raise AssertionError("must not call the LLM repair path when source != 'llm'")

    monkeypatch.setattr(generator_module, "llm_repair_dsl_program", boom)

    result = generate_mesh_ops("a weird bracket")
    assert result.generator_name != "dsl"
    assert len(result.dsl_attempts) == 1  # no repair attempts made


def test_dsl_generated_ops_apply_cleanly_to_a_fresh_room_document(monkeypatch):
    import crdt_cad.ai.generator as generator_module

    monkeypatch.setattr(generator_module, "interpret_prompt", lambda prompt: ("dsl", _VALID_BOX_PROGRAM, "llm"))
    result = generate_mesh_ops("a weird bracket")

    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)
    for op in result.ops:
        doc.apply(op)
    assert len(doc.vertex_positions()) == result.vertex_count
    assert len(doc.face_loops()) == result.face_count
    materials = {doc.face_props_dict(fid).get("material") for fid in doc.face_loops()}
    assert materials == {"metal"}


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


# -- Phase G4: provenance (generation_id), spec persistence, id-collision fix -----


def test_every_generation_path_tags_faces_with_a_generation_id_and_records_a_spec():
    for prompt in ("a wooden table", "a table with four chairs around it"):
        result = generate_mesh_ops(prompt)
        assert result.generation_id

        room_clock = LamportClock(actor="__server__:mesh:test-room")
        doc = MeshCRDT(room_clock)
        for op in result.ops:
            doc.apply(op)

        gen_ids_seen = {doc.face_props_dict(fid).get("generation_id") for fid in doc.face_loops()}
        assert gen_ids_seen == {result.generation_id}, f"{prompt}: {gen_ids_seen}"

        record = doc.generation(result.generation_id)
        assert record is not None
        assert record["prompt"] == prompt
        assert record["generator_name"] == result.generator_name
        assert record["spec"] == result.spec.model_dump()


def test_two_separate_generations_in_the_same_room_do_not_collide():
    """Regression test for a real bug found while building Phase G4's
    edit path: every generator's own `build()` restarts vertex/face ids
    at v1/f1, so two *separate* `generate_mesh_ops` calls landing in the
    same room used to silently overwrite each other's geometry (a
    second generation's "v1" would clobber the first's, since an LWWMap
    `set` on an existing key is a move, not a create) -- see
    `_fresh_ids`'s docstring in generator.py."""
    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)

    r1 = generate_mesh_ops("a wooden table")
    for op in r1.ops:
        doc.apply(op)
    assert len(doc.vertex_positions()) == r1.vertex_count
    assert len(doc.face_loops()) == r1.face_count

    r2 = generate_mesh_ops("a wooden chair")
    for op in r2.ops:
        doc.apply(op)

    assert len(doc.vertex_positions()) == r1.vertex_count + r2.vertex_count
    assert len(doc.face_loops()) == r1.face_count + r2.face_count
    gen_ids = {doc.face_props_dict(fid).get("generation_id") for fid in doc.face_loops()}
    assert gen_ids == {r1.generation_id, r2.generation_id}


def test_scene_generations_also_avoid_cross_generation_id_collisions():
    room_clock = LamportClock(actor="__server__:mesh:test-room")
    doc = MeshCRDT(room_clock)

    r1 = generate_mesh_ops("a table with four chairs around it")
    for op in r1.ops:
        doc.apply(op)
    r2 = generate_mesh_ops("a table with four chairs around it")
    for op in r2.ops:
        doc.apply(op)

    assert len(doc.vertex_positions()) == r1.vertex_count + r2.vertex_count
    assert len(doc.face_loops()) == r1.face_count + r2.face_count


# -- Phase G4: generation_geometry (pure lookup) -----------------------------------


def test_generation_geometry_finds_faces_vertices_edges_for_one_generation():
    face_loops = {"f1": ["v1", "v2", "v3"], "f2": ["v4", "v5", "v6"]}
    face_props = {"f1": {"generation_id": "gen_a"}, "f2": {"generation_id": "gen_b"}}
    faces, vertices, edges = generation_geometry(face_loops, face_props, "gen_a")
    assert faces == ["f1"]
    assert set(vertices) == {"v1", "v2", "v3"}
    assert set(edges) == {("v1", "v2"), ("v2", "v3"), ("v3", "v1")}


def test_generation_geometry_never_removes_a_vertex_shared_with_another_generation():
    """Safety check, not an assumption: a vertex referenced by a face
    OUTSIDE the target generation must never be marked for removal,
    even though AI generators never actually share vertices across
    generations in practice."""
    face_loops = {"f1": ["v1", "v2", "v3"], "f2": ["v1", "v4", "v5"]}
    face_props = {"f1": {"generation_id": "gen_a"}, "f2": {"generation_id": "gen_b"}}
    faces, vertices, edges = generation_geometry(face_loops, face_props, "gen_a")
    assert faces == ["f1"]
    assert "v1" not in vertices  # shared with f2 (gen_b) -- must not be removed
    assert set(vertices) == {"v2", "v3"}


def test_generation_geometry_returns_empty_for_unknown_generation_id():
    faces, vertices, edges = generation_geometry({"f1": ["v1", "v2", "v3"]}, {"f1": {"generation_id": "gen_a"}}, "gen_z")
    assert faces == [] and vertices == [] and edges == []


# -- Phase G4: generate_edit_ops ---------------------------------------------------


def _generate_and_apply(prompt: str, doc: MeshCRDT):
    result = generate_mesh_ops(prompt)
    for op in result.ops:
        doc.apply(op)
    return result


def _edit_args_for(doc: MeshCRDT, generation_id: str):
    face_props = {fid: doc.face_props_dict(fid) for fid in doc.face_loops()}
    faces, vertices, edges = generation_geometry(doc.face_loops(), face_props, generation_id)
    start_counter = doc.frontier().get(DEFAULT_ACTOR_ID)
    return faces, vertices, edges, start_counter


def test_generate_edit_ops_heuristic_taller():
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r1 = _generate_and_apply("a wooden table", doc)
    prior_record = doc.generation(r1.generation_id)

    faces, vertices, edges, start_counter = _edit_args_for(doc, r1.generation_id)
    r2 = generate_edit_ops("make it taller", r1.generation_id, prior_record, faces, vertices, edges, start_counter)
    for op in r2.ops:
        doc.apply(op)

    assert r2.generation_id == r1.generation_id
    assert r2.generator_name == "table"
    assert r2.spec.height_m > prior_record["spec"]["height_m"]
    assert len(doc.vertex_positions()) == r2.vertex_count
    assert len(doc.face_loops()) == r2.face_count
    gen_ids = {doc.face_props_dict(fid).get("generation_id") for fid in doc.face_loops()}
    assert gen_ids == {r1.generation_id}
    assert doc.generation(r1.generation_id)["prompt"] == "make it taller"


def test_generate_edit_ops_does_not_disturb_a_different_generation_in_the_same_room():
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r_table = _generate_and_apply("a wooden table", doc)
    r_chair = _generate_and_apply("a wooden chair", doc)

    prior_record = doc.generation(r_table.generation_id)
    faces, vertices, edges, start_counter = _edit_args_for(doc, r_table.generation_id)
    r_edit = generate_edit_ops("make it taller", r_table.generation_id, prior_record, faces, vertices, edges, start_counter)
    for op in r_edit.ops:
        doc.apply(op)

    # the chair's own geometry must be completely untouched
    chair_faces_now = {fid for fid in doc.face_loops() if doc.face_props_dict(fid).get("generation_id") == r_chair.generation_id}
    assert len(chair_faces_now) == r_chair.face_count
    assert len(doc.vertex_positions()) == r_edit.vertex_count + r_chair.vertex_count
    assert len(doc.face_loops()) == r_edit.face_count + r_chair.face_count


def test_generate_edit_ops_rejects_scene_generations():
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r1 = _generate_and_apply("a table with four chairs around it", doc)
    prior_record = doc.generation(r1.generation_id)
    faces, vertices, edges, start_counter = _edit_args_for(doc, r1.generation_id)

    with pytest.raises(EditNotSupportedError):
        generate_edit_ops("make the table bigger", r1.generation_id, prior_record, faces, vertices, edges, start_counter)


def test_generate_edit_ops_rejects_dsl_generations(monkeypatch):
    import crdt_cad.ai.generator as generator_module
    from crdt_cad.ai.dsl import DSLProgramSpec

    monkeypatch.setattr(
        generator_module, "interpret_prompt",
        lambda prompt: ("dsl", DSLProgramSpec(root={"op": "box", "size": [1.0, 1.0, 1.0]}), "llm"),
    )
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r1 = _generate_and_apply("a weird bracket", doc)
    prior_record = doc.generation(r1.generation_id)
    faces, vertices, edges, start_counter = _edit_args_for(doc, r1.generation_id)

    with pytest.raises(EditNotSupportedError):
        generate_edit_ops("make it bigger", r1.generation_id, prior_record, faces, vertices, edges, start_counter)


def test_generate_edit_ops_seeded_counter_actually_removes_old_geometry():
    """Without seeding the edit's clock from the room's own frontier, the
    removal ops would silently lose the LWW race against the original
    (higher-counter) creation ops -- this is exactly what `start_counter`
    exists to prevent. Regression guard: pass counter=0 (what an
    *unseeded* clock would start at) and confirm the old geometry
    survives, proving the seeded path is the one that actually works."""
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r1 = _generate_and_apply("a wooden table", doc)
    prior_record = doc.generation(r1.generation_id)
    faces, vertices, edges, _ = _edit_args_for(doc, r1.generation_id)

    r_broken = generate_edit_ops("make it taller", r1.generation_id, prior_record, faces, vertices, edges, start_counter=0)
    for op in r_broken.ops:
        doc.apply(op)
    # the old table's faces are still live -- the low-counter removal lost
    assert len(doc.face_loops()) > r_broken.face_count


# -- Phase G5: interpretation_chips + elapsed_seconds -------------------------------


def test_interpretation_chips_for_house():
    spec = HouseSpec(bedrooms=4, floors=1, floor_material="wood", style="modern", roof_type="gable")
    chips = interpretation_chips("house", spec)
    assert "4 bedroom(s)" in chips
    assert "wood floor" in chips
    assert "gable roof" in chips


def test_interpretation_chips_for_house_omits_flat_roof_and_no_garage():
    spec = HouseSpec(bedrooms=2, floors=1, floor_material="concrete", style="modern", roof_type="flat", garage=False)
    chips = interpretation_chips("house", spec)
    assert not any("roof" in c for c in chips)
    assert not any("garage" in c for c in chips)


def test_interpretation_chips_for_table_uses_dimension_fields():
    name, spec = _heuristic_interpret("a wooden table")
    chips = interpretation_chips(name, spec)
    assert any("width" in c for c in chips)
    assert any("m" in c for c in chips)


def test_interpretation_chips_for_scene_summarizes_object_counts():
    name, spec = _heuristic_interpret("a table with four chairs around it")
    assert name == "scene"
    chips = interpretation_chips(name, spec)
    assert "table" in chips
    assert "4x chair" in chips


def test_interpretation_chips_for_dsl_mentions_the_shape_and_material():
    spec = DSLProgramSpec(root={"op": "box", "size": [1.0, 1.0, 1.0]}, material="metal")
    chips = interpretation_chips("dsl", spec)
    assert any("box" in c for c in chips)
    assert "metal" in chips


def test_generate_mesh_ops_reports_a_nonzero_elapsed_time():
    result = generate_mesh_ops("a wooden table")
    assert result.elapsed_seconds > 0.0


def test_generate_edit_ops_reports_a_nonzero_elapsed_time():
    doc = MeshCRDT(LamportClock(actor="__server__:mesh:test-room"))
    r1 = _generate_and_apply("a wooden table", doc)
    prior_record = doc.generation(r1.generation_id)
    faces, vertices, edges, start_counter = _edit_args_for(doc, r1.generation_id)
    r2 = generate_edit_ops("make it taller", r1.generation_id, prior_record, faces, vertices, edges, start_counter)
    assert r2.elapsed_seconds > 0.0


# -- Phase G7: pre-fetched meshy_mesh / meshy_attempted (avoids a double Meshy call) --


def test_generate_ops_from_interpretation_uses_a_prefetched_meshy_mesh(monkeypatch):
    """When the caller (the endpoint) already ran the async Meshy path
    itself and passes the result in, this function must use it directly
    rather than trying Meshy again."""
    import crdt_cad.ai.generator as generator_module
    from crdt_cad.ai.generators.furniture import ChairSpec
    from crdt_cad.ai.procedural_house import GeneratedMesh

    fake_mesh = GeneratedMesh(
        vertices={"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)},
        faces={"f1": ["a", "b", "c"]},
    )

    def boom(prompt):
        raise AssertionError("must not call generate_mesh_via_meshy when a pre-fetched mesh was already provided")

    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    monkeypatch.setattr(generator_module, "generate_mesh_via_meshy", boom)

    result = generator_module.generate_ops_from_interpretation(
        "a small wooden chair", "chair", ChairSpec(), "heuristic",
        meshy_mesh=fake_mesh, meshy_attempted=True,
    )
    assert result.mesh_source == "meshy"
    assert result.vertex_count == 3
    assert result.face_count == 1


def test_generate_ops_from_interpretation_skips_meshy_retry_when_already_attempted_and_failed(monkeypatch):
    """meshy_attempted=True with meshy_mesh=None means "I already tried
    Meshy myself and it failed" -- this function must fall straight to
    the procedural generator, not spend a second (redundant, and in
    this test, forbidden) attempt."""
    import crdt_cad.ai.generator as generator_module
    from crdt_cad.ai.generators.furniture import ChairSpec

    def boom(prompt):
        raise AssertionError("must not retry Meshy when the caller already attempted and failed")

    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    monkeypatch.setattr(generator_module, "generate_mesh_via_meshy", boom)

    result = generator_module.generate_ops_from_interpretation(
        "a small wooden chair", "chair", ChairSpec(), "heuristic",
        meshy_mesh=None, meshy_attempted=True,
    )
    assert result.mesh_source == "procedural"
    assert result.vertex_count > 0


def test_generate_ops_from_interpretation_default_params_still_try_meshy_itself(monkeypatch):
    """Backward compatibility: a caller that doesn't pass meshy_mesh/
    meshy_attempted at all (every pre-G7 call site, including direct
    test/script callers) gets the original behavior unchanged."""
    import crdt_cad.ai.generator as generator_module
    from crdt_cad.ai.procedural_house import GeneratedMesh

    fake_mesh = GeneratedMesh(
        vertices={"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)},
        faces={"f1": ["a", "b", "c"]},
    )
    calls = []

    def fake_meshy(prompt):
        calls.append(prompt)
        return fake_mesh

    monkeypatch.setenv("MESHY_API_KEY", "fake-key")
    monkeypatch.setattr(generator_module, "generate_mesh_via_meshy", fake_meshy)

    from crdt_cad.ai.generators.furniture import ChairSpec

    result = generator_module.generate_ops_from_interpretation("a chair", "chair", ChairSpec(), "heuristic")
    assert calls == ["a chair"]
    assert result.mesh_source == "meshy"
