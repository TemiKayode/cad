"""Natural-language -> ``(generator_name, spec)`` interpretation
(Phase G1: "Interpretation becomes dispatch").

Two paths, always tried in this order:

1. **LLM** (``_llm_interpret``): calls Claude (``claude-fable-5``) via the
   ``anthropic`` SDK with the generator registry presented as a **tool
   catalog** (one tool per generator, each with its own spec's JSON
   schema) -- not one giant union schema, so adding a new generator to
   the registry never means hand-widening a shared schema here. Claude
   picks exactly one generator and fills its spec's fields; deterministic
   code in that generator's ``build`` function computes every vertex, the
   same "LLM never emits geometry" rule Part 5's brief states as
   non-negotiable.
2. **Heuristic** (``_heuristic_interpret``): keyword dispatch across the
   registry (``registry.dispatch_by_keyword``), defaulting to the house
   generator if nothing matches -- the one archetype this pipeline had
   before Phase G1. For the house generator specifically, the heuristic
   also does real field extraction (bedrooms/floors/material/style,
   unchanged from before); every other generator gets its spec's
   defaults without an API key -- a real, working object, just with
   "reduced vocabulary" (rule 2), never a broken/silent failure.

``interpret_prompt`` always tries the LLM path first and falls back on
*any* exception -- missing credentials, network failure, a safety
refusal, a rate limit, a malformed response. The failure mode of a
generation feature should be "slightly less clever," never "broken."
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.registry import dispatch_by_keyword, get_generator, tool_catalog
from crdt_cad.ai.scene import SceneObjectSpec, SceneSpec

logger = logging.getLogger("crdt_cad.ai.interpreter")

_SYSTEM_PROMPT = (
    "You turn a short design/architecture description into a call to exactly "
    "one of the provided generator tools, filling in its parameters as best "
    "matches the description. Infer reasonable defaults for anything not "
    "mentioned rather than leaving a field out. If the description doesn't "
    "clearly match any specific generator (furniture, architectural element, "
    "primitive shape), use the 'house' generator as the default -- it is the "
    "most general fallback for an architectural request. If the description "
    "asks for *multiple objects arranged relative to each other* (e.g. 'a "
    "table with four chairs around it', 'a row of three shelves', 'a lamp on "
    "the table'), use the 'scene' tool instead of a single-object generator "
    "-- list each object with a plain-language relation ('around', "
    "'on_top_of', 'row', 'beside', or 'none') and, for 'around'/'on_top_of'/"
    "'beside', a target_index referencing an earlier object in the same list."
)

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "a couple of": 2, "couple of": 2, "a few": 3, "few": 3,
}

_COUNT_PATTERN = r"(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"


def _parse_count(token: str) -> int:
    token = token.strip().lower()
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _keyword_generator_name(word: str) -> str | None:
    """Maps a plain noun (already singularized by the caller's regex,
    e.g. "chair" out of "chairs") to a registry generator name via the
    same keyword table `dispatch_by_keyword` uses, so scene parsing
    never hardcodes its own separate noun list."""
    entry = dispatch_by_keyword(word)
    return entry.name if entry else None


_SCENE_TOOL = {
    "name": "scene",
    "description": (
        "Compose a scene of multiple objects arranged relative to each "
        "other -- a table with chairs around it, a lamp on a shelf, a row "
        "of columns. Pick generator names from the registry's own tools "
        "(table, chair, shelf, box, house, ...); the layout solver (not "
        "you) turns relations into actual coordinates."
    ),
    "input_schema": SceneSpec.model_json_schema(),
}


def _heuristic_interpret(prompt: str) -> tuple[str, BaseModel]:
    scene = _heuristic_scene_interpret(prompt)
    if scene is not None:
        return "scene", scene
    entry = dispatch_by_keyword(prompt) or get_generator("house")
    if entry.name == "house":
        return "house", _heuristic_house_spec(prompt)
    return entry.name, entry.spec_model()


def _heuristic_scene_interpret(prompt: str) -> SceneSpec | None:
    """Simple counted-arrangement patterns for the no-API-key path --
    "reduced vocabulary" (only these plain-English shapes), never a
    silent failure: a prompt that doesn't match any pattern here just
    falls through to single-object dispatch, same as any other prompt
    the heuristic can't fully parse."""
    lowered = prompt.lower()

    # "a table with four chairs around it"
    m = re.search(r"(?:a|an|the)\s+(\w+)\s+with\s+" + _COUNT_PATTERN + r"\s+(\w+)\s+around\s+it", lowered)
    if m:
        target_name = _keyword_generator_name(m.group(1))
        obj_name = _keyword_generator_name(m.group(3))
        if target_name and obj_name:
            count = min(_parse_count(m.group(2)), 12)
            return SceneSpec(objects=[
                SceneObjectSpec(generator=target_name),
                SceneObjectSpec(generator=obj_name, relation="around", target_index=0, count=count),
            ])

    # "four chairs around a/the table"
    m = re.search(_COUNT_PATTERN + r"\s+(\w+)\s+around\s+(?:the\s+|a\s+|an\s+)?(\w+)", lowered)
    if m:
        obj_name = _keyword_generator_name(m.group(2))
        target_name = _keyword_generator_name(m.group(3))
        if obj_name and target_name:
            count = min(_parse_count(m.group(1)), 12)
            return SceneSpec(objects=[
                SceneObjectSpec(generator=target_name),
                SceneObjectSpec(generator=obj_name, relation="around", target_index=0, count=count),
            ])

    # "a lamp on the table" / "a box on top of a table"
    m = re.search(r"(?:a|an|the)\s+(\w+)\s+on(?:\s+top\s+of)?\s+(?:the\s+|a\s+|an\s+)?(\w+)", lowered)
    if m:
        obj_name = _keyword_generator_name(m.group(1))
        target_name = _keyword_generator_name(m.group(2))
        if obj_name and target_name and obj_name != target_name:
            return SceneSpec(objects=[
                SceneObjectSpec(generator=target_name),
                SceneObjectSpec(generator=obj_name, relation="on_top_of", target_index=0),
            ])

    # "a row of three shelves"
    m = re.search(r"row\s+of\s+" + _COUNT_PATTERN + r"\s+(\w+)", lowered)
    if m:
        obj_name = _keyword_generator_name(m.group(2))
        if obj_name:
            count = min(_parse_count(m.group(1)), 12)
            return SceneSpec(objects=[SceneObjectSpec(generator=obj_name, relation="row", count=count)])

    return None


def _heuristic_house_spec(prompt: str) -> HouseSpec:
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

    garage = "garage" in lowered
    roof_type = "flat"
    if "gable" in lowered:
        roof_type = "gable"
    elif "hip roof" in lowered or "hipped roof" in lowered:
        roof_type = "hip"

    floor_area_sq_m = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:square\s*(?:meter|metre)s?|sq\.?\s*m\b|m2\b|m²)", lowered)
    if m:
        floor_area_sq_m = max(2.0, min(2000.0, float(m.group(1))))

    return HouseSpec(
        bedrooms=bedrooms, floors=floors, floor_material=floor_material, style=style,
        garage=garage, roof_type=roof_type, floor_area_sq_m=floor_area_sq_m,
    )


def _llm_interpret(prompt: str) -> tuple[str, BaseModel]:
    """Raises on any failure; ``interpret_prompt`` catches broadly and
    falls back to the heuristic dispatcher. Imports ``anthropic`` lazily
    so this module (and the heuristic path) never require the SDK or
    credentials to be present."""
    import anthropic

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / an `ant auth login` profile from the environment
    tools = tool_catalog() + [_SCENE_TOOL]
    response = client.beta.messages.create(
        model="claude-fable-5",
        max_tokens=1024,
        betas=["server-side-fallback-2026-06-01"],
        fallbacks=[{"model": "claude-opus-4-8"}],
        system=_SYSTEM_PROMPT,
        tools=tools,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {getattr(response, 'stop_details', None)}")
    tool_use = next(block for block in response.content if block.type == "tool_use")
    if tool_use.name == "scene":
        return "scene", SceneSpec(**tool_use.input)
    entry = get_generator(tool_use.name)
    return entry.name, entry.spec_model(**tool_use.input)


def interpret_prompt(prompt: str) -> tuple[str, BaseModel, str]:
    """Returns ``(generator_name, spec, source)`` where ``source`` is
    ``"llm"`` or ``"heuristic"`` so callers (and tests) can tell which
    path ran."""
    try:
        name, spec = _llm_interpret(prompt)
        return name, spec, "llm"
    except Exception as exc:
        logger.info("LLM prompt interpretation unavailable (%s); using the heuristic dispatcher", exc)
        name, spec = _heuristic_interpret(prompt)
        return name, spec, "heuristic"
