"""Natural-language -> :class:`HouseSpec` interpretation.

Two paths, always tried in this order:

1. **LLM** (``_llm_interpret``): calls Claude (``claude-fable-5``) via the
   ``anthropic`` SDK with ``output_config.format`` constrained to the
   spec's JSON schema, plus the server-side refusal-fallback the model
   card recommends enabling by default for Fable 5. This is the "use
   Claude Fable where necessary" part of the pipeline -- turning a vague
   architectural description into bounded structured parameters is
   exactly the kind of judgment call an LLM is suited for (a regex can't
   tell that "a cozy little cottage" implies 1 bedroom while "a large
   family home" implies 4), whereas the actual 3D construction is
   handled by deterministic geometry code, not asked of the model.
2. **Heuristic** (``_heuristic_interpret``): pure regex/keyword
   extraction, stdlib only. This is not a rare degraded corner case --
   it's what actually runs in any environment without
   ``ANTHROPIC_API_KEY``/``ant auth login`` configured, so the pipeline
   is fully functional and testable without external credentials.

``interpret_prompt`` always tries the LLM path first and falls back on
*any* exception -- missing credentials, network failure, a safety
refusal, a rate limit, a malformed response. The failure mode of a
generation feature should be "slightly less clever," never "broken."
"""

from __future__ import annotations

import json
import logging
import re

from crdt_cad.ai.house_spec import HouseSpec

logger = logging.getLogger("crdt_cad.ai.interpreter")

_HOUSE_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "bedrooms": {"type": "integer", "minimum": 1, "maximum": 12},
        "floors": {"type": "integer", "minimum": 1, "maximum": 4},
        "floor_material": {"type": "string"},
        "wall_height_m": {"type": "number", "minimum": 1.5, "maximum": 6.0},
        "style": {"type": "string"},
    },
    "required": ["bedrooms", "floors", "floor_material", "wall_height_m", "style"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You turn a short architectural description into a bounded, structured "
    "specification for a simple procedural house generator that only builds "
    "box-based buildings (a rectangular grid of rooms, flat floors/roof, "
    "straight exterior and interior walls) -- not architectural styles the "
    "generator can't render, and never more than 12 bedrooms or 4 floors. "
    "Infer reasonable defaults for anything not mentioned: a typical home "
    "is 1 bedroom, 1 floor, wall height 2.7m, floor material 'concrete', "
    "style 'modern'."
)


def _heuristic_interpret(prompt: str) -> HouseSpec:
    lowered = prompt.lower()

    bedrooms = 1
    m = re.search(r"(\d+)\s*[- ]?\s*(?:bed\s*room|bedroom|br\b)", lowered)
    if m:
        bedrooms = max(1, min(12, int(m.group(1))))

    floors = 1
    m = re.search(r"(\d+)\s*[- ]?\s*(?:stor(?:y|ey|ies)|floor)", lowered)
    if m:
        floors = max(1, min(4, int(m.group(1))))
    elif re.search(r"\btwo[- ]stor(?:y|ey)\b|\bdouble[- ]stor(?:y|ey)\b", lowered):
        floors = 2

    floor_material = "concrete"
    for keyword, material in (
        ("wooden", "wood"),
        ("wood", "wood"),
        ("timber", "wood"),
        ("marble", "marble"),
        ("tiled", "tile"),
        ("tile", "tile"),
        ("carpet", "carpet"),
        ("concrete", "concrete"),
        ("stone", "stone"),
    ):
        if keyword in lowered:
            floor_material = material
            break

    style = "modern"
    for keyword in ("minimalist", "rustic", "colonial", "traditional", "victorian", "farmhouse", "modern"):
        if keyword in lowered:
            style = keyword
            break

    return HouseSpec(bedrooms=bedrooms, floors=floors, floor_material=floor_material, style=style)


def _llm_interpret(prompt: str) -> HouseSpec:
    """Raises on any failure; ``interpret_prompt`` catches broadly and
    falls back to the heuristic parser. Imports ``anthropic`` lazily so
    this module (and the heuristic path) never require the SDK or
    credentials to be present."""
    import anthropic

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / an `ant auth login` profile from the environment
    response = client.beta.messages.create(
        model="claude-fable-5",
        max_tokens=1024,
        betas=["server-side-fallback-2026-06-01"],
        fallbacks=[{"model": "claude-opus-4-8"}],
        system=_SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": _HOUSE_SPEC_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {getattr(response, 'stop_details', None)}")
    text = next(block.text for block in response.content if block.type == "text")
    return HouseSpec(**json.loads(text))


def interpret_prompt(prompt: str) -> tuple[HouseSpec, str]:
    """Returns ``(spec, source)`` where ``source`` is ``"llm"`` or
    ``"heuristic"`` so callers (and tests) can tell which path ran."""
    try:
        return _llm_interpret(prompt), "llm"
    except Exception as exc:
        logger.info("LLM prompt interpretation unavailable (%s); using the heuristic parser", exc)
        return _heuristic_interpret(prompt), "heuristic"
