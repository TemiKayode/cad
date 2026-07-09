"""Generator registry (Phase G1, Part 5): the dispatch layer between an
interpreted prompt and a deterministic mesh builder.

Each entry is a **(bounded pydantic spec, deterministic builder,
description)** triple -- the same shape ``procedural_house.py`` already
used informally for the house generator, made explicit and pluggable so
Claude (and the heuristic fallback) can be handed a *catalog* of
generators to choose from instead of a single hardcoded target. The LLM
never emits geometry through this registry, only a generator name plus
field values for that generator's own bounded spec -- deterministic code
in ``build`` computes every vertex, same rule as everywhere else in this
project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from crdt_cad.ai.mesh_types import GeneratedMesh


@dataclass(frozen=True)
class GeneratorEntry:
    name: str
    description: str
    spec_model: type[BaseModel]
    build: Callable[[BaseModel], GeneratedMesh]
    # Lowercase keywords the heuristic (no-API-key) dispatcher matches
    # against a prompt to pick this generator -- deliberately simple
    # substring matching, not NLP, so it stays predictable/testable with
    # no model in the loop. Order in REGISTRY matters: first match wins.
    keywords: tuple[str, ...] = ()


REGISTRY: dict[str, GeneratorEntry] = {}


def register(entry: GeneratorEntry) -> GeneratorEntry:
    if entry.name in REGISTRY:
        raise ValueError(f"generator {entry.name!r} already registered")
    REGISTRY[entry.name] = entry
    return entry


def get_generator(name: str) -> GeneratorEntry:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"no generator named {name!r} -- known: {sorted(REGISTRY)}") from None


def dispatch_by_keyword(prompt: str) -> GeneratorEntry | None:
    """First-match keyword dispatch for the heuristic (no-API-key) path.
    Returns ``None`` if nothing matches -- callers fall back to the house
    generator, the one archetype that predates this registry and is
    still the safest default for an unrecognized architectural prompt."""
    lowered = prompt.lower()
    for entry in REGISTRY.values():
        if any(keyword in lowered for keyword in entry.keywords):
            return entry
    return None


def tool_catalog() -> list[dict]:
    """The registry presented to Claude as a tool/schema catalog (G1:
    "present the registry to the LLM as a tool/schema catalog, not one
    giant union schema") -- one tool per generator, each with its own
    spec's JSON schema as the input schema."""
    return [
        {
            "name": entry.name,
            "description": entry.description,
            "input_schema": entry.spec_model.model_json_schema(),
        }
        for entry in REGISTRY.values()
    ]
