"""Tests for the generator registry (Phase G1) -- the dispatch layer
every generator module registers itself into at import time."""

import pytest
from pydantic import BaseModel

from crdt_cad.ai import REGISTRY  # noqa: F401 -- import triggers registration
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import GeneratorEntry, dispatch_by_keyword, get_generator, register, tool_catalog


def test_every_expected_generator_is_registered():
    expected = {
        "house", "table", "chair", "shelf", "stairs", "column", "arch",
        "door", "window", "fence", "box", "cylinder", "cone", "torus",
    }
    assert expected <= set(REGISTRY.keys())


def test_get_generator_returns_the_registered_entry():
    entry = get_generator("table")
    assert entry.name == "table"
    assert callable(entry.build)


def test_get_generator_raises_a_clear_error_for_an_unknown_name():
    with pytest.raises(KeyError, match="no generator named 'nonexistent'"):
        get_generator("nonexistent")


def test_registering_a_duplicate_name_raises():
    """"table" is already registered by generators/furniture.py at
    import time -- registering it again (rather than mutating the
    shared, process-wide REGISTRY as a side effect of this test) must
    raise, not silently overwrite."""
    class _DummySpec(BaseModel):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register(GeneratorEntry(name="table", description="dup", spec_model=_DummySpec, build=lambda s: GeneratedMesh()))


def test_dispatch_by_keyword_matches_a_known_generator():
    entry = dispatch_by_keyword("I want a wooden chair please")
    assert entry is not None
    assert entry.name == "chair"


def test_dispatch_by_keyword_returns_none_for_no_match():
    assert dispatch_by_keyword("xyzzy plugh qwerty") is None


def test_tool_catalog_has_one_entry_per_generator_with_a_real_json_schema():
    catalog = tool_catalog()
    names = {t["name"] for t in catalog}
    assert names == set(REGISTRY.keys())
    for tool in catalog:
        assert "properties" in tool["input_schema"]
        assert tool["description"]
