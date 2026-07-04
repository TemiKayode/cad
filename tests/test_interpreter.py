import json
import sys
import types

import pytest

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.interpreter import _heuristic_interpret, interpret_prompt


# -- heuristic parser ------------------------------------------------------------


def test_heuristic_extracts_bedroom_count():
    spec = _heuristic_interpret("create a 4 bedroom house with wooden floor")
    assert spec.bedrooms == 4


def test_heuristic_extracts_floor_material_wood():
    spec = _heuristic_interpret("a small home with a wooden floor")
    assert spec.floor_material == "wood"


def test_heuristic_extracts_multiple_stories():
    spec = _heuristic_interpret("a 3 story house with 5 bedrooms")
    assert spec.floors == 3
    assert spec.bedrooms == 5


def test_heuristic_two_story_keyword_without_a_number():
    spec = _heuristic_interpret("a two-story cottage")
    assert spec.floors == 2


def test_heuristic_defaults_when_nothing_specified():
    spec = _heuristic_interpret("a house")
    assert spec == HouseSpec()


def test_heuristic_clamps_bedroom_count_to_valid_range():
    spec = _heuristic_interpret("a 99 bedroom mansion")
    assert spec.bedrooms == 12  # HouseSpec's max


def test_heuristic_detects_style_keyword():
    spec = _heuristic_interpret("a rustic farmhouse with a tiled floor")
    assert spec.floor_material == "tile"
    assert spec.style in ("rustic", "farmhouse")


# -- interpret_prompt routing / fallback behavior --------------------------------


def test_interpret_prompt_falls_back_to_heuristic_when_llm_raises(monkeypatch):
    def boom(prompt):
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr("crdt_cad.ai.interpreter._llm_interpret", boom)
    spec, source = interpret_prompt("a 2 bedroom house with a marble floor")
    assert source == "heuristic"
    assert spec.bedrooms == 2
    assert spec.floor_material == "marble"


def test_interpret_prompt_uses_llm_result_when_available(monkeypatch):
    expected = HouseSpec(bedrooms=6, floors=2, floor_material="wood", style="modern")

    def fake_llm(prompt):
        return expected

    monkeypatch.setattr("crdt_cad.ai.interpreter._llm_interpret", fake_llm)
    spec, source = interpret_prompt("anything")
    assert source == "llm"
    assert spec == expected


# -- _llm_interpret's own response-parsing logic, via a fake anthropic client ------


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, payload, stop_reason="end_turn"):
        self.content = [_FakeTextBlock(json.dumps(payload))]
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


def test_llm_interpret_parses_a_well_formed_response(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    payload = {"bedrooms": 4, "floors": 1, "floor_material": "wood", "wall_height_m": 2.7, "style": "modern"}
    fake_anthropic_module["client"] = _FakeAnthropicClient(_FakeResponse(payload))

    spec = _llm_interpret("create a 4 bedroom house with wooden floor")
    assert spec == HouseSpec(**payload)


def test_llm_interpret_raises_on_refusal(fake_anthropic_module):
    from crdt_cad.ai.interpreter import _llm_interpret

    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse({"bedrooms": 1, "floors": 1, "floor_material": "concrete", "wall_height_m": 2.7, "style": "modern"}, stop_reason="refusal")
    )
    with pytest.raises(RuntimeError):
        _llm_interpret("anything")


def test_interpret_prompt_falls_back_end_to_end_on_refusal(fake_anthropic_module):
    fake_anthropic_module["client"] = _FakeAnthropicClient(
        _FakeResponse({}, stop_reason="refusal")
    )
    spec, source = interpret_prompt("a 3 bedroom house with a wood floor")
    assert source == "heuristic"
    assert spec.bedrooms == 3
    assert spec.floor_material == "wood"
