import sys
import types

import pytest

from crdt_cad.ai import REGISTRY  # noqa: F401 -- triggers registration
from crdt_cad.ai.dsl import DSLProgramSpec
from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.interpreter import (
    _heuristic_house_spec,
    _heuristic_interpret,
    _heuristic_scene_interpret,
    interpret_prompt,
    llm_repair_dsl_program,
)
from crdt_cad.ai.scene import SceneSpec


# -- heuristic house-spec field extraction ----------------------------------------


def test_heuristic_extracts_bedroom_count():
    spec = _heuristic_house_spec("create a 4 bedroom house with wooden floor")
    assert spec.bedrooms == 4


def test_heuristic_extracts_floor_material_wood():
    spec = _heuristic_house_spec("a small home with a wooden floor")
    assert spec.floor_material == "wood"


def test_heuristic_extracts_multiple_stories():
    spec = _heuristic_house_spec("a 3 story house with 5 bedrooms")
    assert spec.floors == 3
    assert spec.bedrooms == 5


def test_heuristic_two_story_keyword_without_a_number():
    spec = _heuristic_house_spec("a two-story cottage")
    assert spec.floors == 2


def test_heuristic_defaults_when_nothing_specified():
    spec = _heuristic_house_spec("a house")
    assert spec == HouseSpec()


def test_heuristic_clamps_bedroom_count_to_valid_range():
    spec = _heuristic_house_spec("a 99 bedroom mansion")
    assert spec.bedrooms == 12  # HouseSpec's max


def test_heuristic_detects_style_keyword():
    spec = _heuristic_house_spec("a rustic farmhouse with a tiled floor")
    assert spec.floor_material == "tile"
    assert spec.style in ("rustic", "farmhouse")


def test_heuristic_extracts_garage_and_roof_type():
    spec = _heuristic_house_spec("a house with a garage and a gable roof")
    assert spec.garage is True
    assert spec.roof_type == "gable"


def test_heuristic_extracts_floor_area():
    spec = _heuristic_house_spec("a 30 square meter cabin")
    assert spec.floor_area_sq_m == 30.0


# -- registry dispatch (heuristic keyword routing) ---------------------------------


def test_heuristic_interpret_dispatches_to_house_by_default():
    name, spec = _heuristic_interpret("a 4 bedroom house")
    assert name == "house"
    assert isinstance(spec, HouseSpec)


def test_heuristic_interpret_dispatches_to_table_by_keyword():
    name, spec = _heuristic_interpret("a wooden table")
    assert name == "table"


def test_heuristic_interpret_dispatches_to_chair_by_keyword():
    name, spec = _heuristic_interpret("a chair")
    assert name == "chair"


def test_heuristic_interpret_falls_back_to_house_for_unmatched_prompt():
    name, spec = _heuristic_interpret("xyzzy plugh")
    assert name == "house"


# -- heuristic scene parsing (Phase G2) --------------------------------------------


def test_heuristic_scene_interpret_parses_table_with_n_chairs_around_it():
    scene = _heuristic_scene_interpret("a table with four chairs around it")
    assert isinstance(scene, SceneSpec)
    assert [o.generator for o in scene.objects] == ["table", "chair"]
    assert scene.objects[1].relation == "around"
    assert scene.objects[1].target_index == 0
    assert scene.objects[1].count == 4


def test_heuristic_scene_interpret_parses_n_chairs_around_a_table():
    scene = _heuristic_scene_interpret("six chairs around a table")
    assert scene.objects[1].count == 6


def test_heuristic_scene_interpret_parses_digit_counts_and_word_counts():
    assert _heuristic_scene_interpret("3 chairs around a table").objects[1].count == 3
    assert _heuristic_scene_interpret("two chairs around a table").objects[1].count == 2


def test_heuristic_scene_interpret_clamps_count_to_twelve():
    scene = _heuristic_scene_interpret("a table with 99 chairs around it")
    assert scene.objects[1].count == 12


def test_heuristic_scene_interpret_parses_on_top_of():
    scene = _heuristic_scene_interpret("a box on top of a table")
    assert [o.generator for o in scene.objects] == ["table", "box"]
    assert scene.objects[1].relation == "on_top_of"
    assert scene.objects[1].target_index == 0


def test_heuristic_scene_interpret_parses_row_of_n():
    scene = _heuristic_scene_interpret("a row of three shelves")
    assert len(scene.objects) == 1
    assert scene.objects[0].generator == "shelf"
    assert scene.objects[0].relation == "row"
    assert scene.objects[0].count == 3


def test_heuristic_scene_interpret_returns_none_for_single_object_prompts():
    assert _heuristic_scene_interpret("a wooden chair") is None
    assert _heuristic_scene_interpret("a two-bedroom cottage") is None


def test_heuristic_scene_interpret_returns_none_when_nouns_are_unrecognized():
    # "gnome" and "toadstool" aren't registry keywords -- must not
    # silently produce a bogus scene.
    assert _heuristic_scene_interpret("four gnomes around a toadstool") is None


def test_heuristic_interpret_routes_scene_prompts_through_interpret_prompt():
    name, spec = _heuristic_interpret("a table with four chairs around it")
    assert name == "scene"
    assert isinstance(spec, SceneSpec)


def test_heuristic_interpret_never_attempts_dsl_synthesis():
    """Phase G3 rule: the heuristic (no-API-key) path does not attempt
    DSL synthesis -- an unmatched prompt degrades to the registry
    (house), never to a made-up geometry program."""
    for prompt in ("a weird twisty bracket thing", "an abstract sculptural form", "xyzzy plugh qux"):
        name, spec = _heuristic_interpret(prompt)
        assert name != "dsl"


def test_interpret_prompt_end_to_end_dsl_via_llm(fake_anthropic_module):
    dsl_payload = {"root": {"op": "cylinder", "radius": 0.3, "height": 1.0}, "material": "metal"}
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse("dsl", dsl_payload))
    name, spec, source = interpret_prompt("a strange custom bracket")
    assert name == "dsl"
    assert source == "llm"
    assert isinstance(spec, DSLProgramSpec)


def test_interpret_prompt_end_to_end_scene_via_heuristic_fallback(monkeypatch):
    def boom(prompt):
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr("crdt_cad.ai.interpreter._llm_interpret", boom)
    name, spec, source = interpret_prompt("a table with four chairs around it")
    assert name == "scene"
    assert source == "heuristic"
    assert isinstance(spec, SceneSpec)


# -- interpret_prompt routing / fallback behavior --------------------------------


def test_interpret_prompt_falls_back_to_heuristic_when_llm_raises(monkeypatch):
    def boom(prompt):
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr("crdt_cad.ai.interpreter._llm_interpret", boom)
    name, spec, source = interpret_prompt("a 2 bedroom house with a marble floor")
    assert source == "heuristic"
    assert name == "house"
    assert spec.bedrooms == 2
    assert spec.floor_material == "marble"


def test_interpret_prompt_uses_llm_result_when_available(monkeypatch):
    expected = HouseSpec(bedrooms=6, floors=2, floor_material="wood", style="modern")

    def fake_llm(prompt):
        return "house", expected

    monkeypatch.setattr("crdt_cad.ai.interpreter._llm_interpret", fake_llm)
    name, spec, source = interpret_prompt("anything")
    assert source == "llm"
    assert name == "house"
    assert spec == expected


# -- _llm_interpret's own response-parsing logic, via a fake anthropic client ------


class _FakeToolUseBlock:
    def __init__(self, name, input_):
        self.type = "tool_use"
        self.name = name
        self.input = input_


class _FakeResponse:
    def __init__(self, tool_name, tool_input, stop_reason="tool_use"):
        self.content = [_FakeToolUseBlock(tool_name, tool_input)]
        self.stop_reason = stop_reason
        self.stop_details = None


class _FakeMessages:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        self._last_kwargs = kwargs
        return self._response


class _FakeBeta:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


class _FakeAnthropicClient:
    def __init__(self, response):
        self.beta = _FakeBeta(response)


@pytest.fixture
def fake_anthropic_module(monkeypatch):
    """Installs a fake `anthropic` module in sys.modules so `_llm_interpret`'s
    lazy `import anthropic` picks it up without the real SDK/network."""
    module = types.ModuleType("anthropic")
    holder = {}

    def make_client(*args, **kwargs):
        return holder["client"]

    module.Anthropic = make_client
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return holder


def test_llm_interpret_parses_a_tool_use_response(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    payload = {"bedrooms": 4, "floors": 1, "floor_material": "wood", "wall_height_m": 2.7, "style": "modern"}
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse("house", payload))

    name, spec = _llm_interpret("create a 4 bedroom house with wooden floor")
    assert name == "house"
    assert spec == HouseSpec(**payload)


def test_llm_interpret_dispatches_to_a_non_house_generator(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse("table", {"width_m": 2.0, "depth_m": 1.0, "height_m": 0.75})
    )
    name, spec = _llm_interpret("a big dining table")
    assert name == "table"
    assert spec.width_m == 2.0


def test_llm_interpret_dispatches_to_the_scene_tool(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    scene_payload = {
        "objects": [
            {"generator": "table", "spec": {}},
            {"generator": "chair", "spec": {}, "relation": "around", "target_index": 0, "count": 4},
        ]
    }
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse("scene", scene_payload))
    name, spec = _llm_interpret("a table with four chairs around it")
    assert name == "scene"
    assert isinstance(spec, SceneSpec)
    assert len(spec.objects) == 2
    assert spec.objects[1].count == 4


def test_llm_interpret_dispatches_to_the_dsl_tool(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    dsl_payload = {"root": {"op": "box", "size": [1.0, 1.0, 1.0]}, "material": "metal"}
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse("dsl", dsl_payload))
    name, spec = _llm_interpret("a weird bracket shape no generator covers")
    assert name == "dsl"
    assert isinstance(spec, DSLProgramSpec)
    assert spec.root == {"op": "box", "size": [1.0, 1.0, 1.0]}
    assert spec.material == "metal"


def test_llm_repair_dsl_program_returns_the_repaired_program(fake_anthropic_module):
    repaired = {"root": {"op": "box", "size": [1.0, 1.0, 1.0]}, "material": ""}
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse("dsl", repaired))

    result = llm_repair_dsl_program(
        "a weird bracket", {"root": {"op": "box", "size": [500, 1, 1]}}, "box.size component 500 exceeds the 50.0 limit"
    )
    assert result == repaired


def test_llm_repair_dsl_program_forces_the_dsl_tool_and_reports_the_error(fake_anthropic_module):
    client = _FakeAnthropicClient(_FakeResponse("dsl", {"root": {"op": "box", "size": [1, 1, 1]}}))
    fake_anthropic_module["client"] = client

    llm_repair_dsl_program("a weird bracket", {"root": {"op": "box", "size": [500, 1, 1]}}, "size too large")

    sent = client.beta.messages._last_kwargs
    assert sent["tool_choice"] == {"type": "tool", "name": "dsl"}
    assert sent["tools"][0]["name"] == "dsl"
    # the specific error must actually reach the model, not just a generic retry nudge
    tool_result = sent["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "size too large" in tool_result["content"]
    assert tool_result["is_error"] is True


def test_llm_repair_dsl_program_raises_on_refusal(fake_anthropic_module):
    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse("dsl", {"root": {"op": "box", "size": [1, 1, 1]}}, stop_reason="refusal")
    )
    with pytest.raises(RuntimeError):
        llm_repair_dsl_program("a weird bracket", {"root": {"op": "box", "size": [500, 1, 1]}}, "too large")


def test_llm_interpret_raises_on_refusal(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse("house", {"bedrooms": 1, "floors": 1, "floor_material": "concrete", "wall_height_m": 2.7, "style": "modern"}, stop_reason="refusal")
    )
    with pytest.raises(RuntimeError):
        _llm_interpret("anything")


def test_interpret_prompt_falls_back_end_to_end_on_refusal(fake_anthropic_module):
    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse("house", {}, stop_reason="refusal")
    )
    name, spec, source = interpret_prompt("a 3 bedroom house with a wood floor")
    assert source == "heuristic"
    assert name == "house"
    assert spec.bedrooms == 3
    assert spec.floor_material == "wood"
