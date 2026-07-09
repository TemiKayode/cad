"""Sandboxed geometry DSL (Phase G3, Part 5): open vocabulary for
prompts no registry generator matches, without loosening the "the LLM
never emits raw geometry" rule -- the model emits a small **JSON
program**, never Python, never vertices. This module is the entire
interpreter: a closed set of primitive/transform/combinator node types,
walked by pure Python functions with no ``eval``/``exec``, no imports
driven by program content, no filesystem/network access, and no
recursion beyond the program's own (depth- and count-capped) tree --
sandboxed by construction, not by a sandbox process.

**The closed grammar** (a node is a JSON object with an ``"op"`` key):

- Primitives: ``box`` (size), ``prism`` (regular N-gon extrusion:
  sides/radius/height), ``cylinder`` (radius/height/segments --
  "cylinder-approx", an N-gon approximation of a circle, same as every
  other generator in this package), ``extrude`` (an arbitrary polygon
  footprint extruded by height). Every primitive is centered in X/Z and
  based at Y=0, the same "stands on the local origin, gets moved into
  place afterward" convention the rest of this codebase already uses
  (see ``scene_layout.py``'s ground-plane snapping). Built via the
  *existing, already-tested* primitive helpers in ``mesh_builder.py``
  (``add_box``/``add_cylinder``/``add_extruded_polygon``) rather than
  reimplementing triangulation -- this module only adds the JSON
  grammar and sandboxing around them, not new geometry code.
- Transforms (each wraps exactly one ``child``): ``translate``,
  ``rotate`` (about the origin, one of the three cardinal axes),
  ``scale`` (non-uniform, per-axis).
- Combinators: ``union``/``difference`` (real CSG booleans, the same
  ``trimesh``/``manifold3d`` engine ``wall_opening.py`` already uses for
  door/window cuts -- proven, not new), ``group`` (a cheap, non-boolean
  concatenation for disjoint parts that don't need a true topological
  union, e.g. several separate primitives), ``repeat`` (a bounded loop:
  ``count`` translated copies of one child, merged as a group).

**Hard caps, checked before *and* during execution** (a validation pass
first, so a malformed program fails fast with a specific message before
any geometry work; then live budget checks during execution, since
``repeat`` expansion and boolean output size can only be known by
actually running the program): max textual node count, max tree depth,
max repeat count, max *expanded* node count (after repeat unrolling),
max vertices/faces (tighter than ``validation.py``'s general ceiling,
since a DSL program is one open-vocabulary object, not a whole scene),
max wall-clock execution time, and a max bounding box per node (catches
a runaway dimension immediately, not just at the end).

Every failure raises :class:`DSLValidationError` or
:class:`DSLBudgetExceededError` (both subclass :class:`DSLError`) with a
specific, actionable message -- this is exactly the text
``generator.py``'s repair loop feeds back to the model on retry.
"""

from __future__ import annotations

import math
import time

import numpy as np

from pydantic import BaseModel, Field

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box, add_cylinder, add_extruded_polygon, from_trimesh, to_trimesh
from crdt_cad.ai.mesh_types import GeneratedMesh

MAX_DSL_TEXTUAL_NODES = 48
MAX_DSL_TREE_DEPTH = 16
MAX_DSL_REPEAT_COUNT = 24
MAX_DSL_EXPANDED_NODES = 400
MAX_DSL_VERTICES = 20_000
MAX_DSL_FACES = 20_000
MAX_DSL_EXECUTION_SECONDS = 5.0
MAX_DSL_BOUNDING_BOX_M = 50.0

_ALLOWED_OPS = frozenset({
    "box", "prism", "cylinder", "extrude",
    "translate", "rotate", "scale",
    "union", "difference", "group", "repeat",
})


class DSLProgramSpec(BaseModel):
    """Wraps a raw DSL program dict as a pydantic model so it fits the
    same ``(generator_name, spec, source)`` contract every other
    ``interpret_prompt`` result uses (see ``scene.SceneSpec`` for the
    precedent). ``root``'s own recursive node structure is validated by
    :func:`execute_dsl_program`, not by pydantic -- a hand-typed
    recursive pydantic model would just duplicate ``_validate_node``
    with a second, harder-to-keep-in-sync set of rules."""

    root: dict = Field(default_factory=dict)
    material: str = ""


# Hand-written JSON schema (not auto-derived from a pydantic model --
# the grammar is recursive, which a flat `root: dict` field can't
# express) presented to the LLM as the "dsl" tool's input schema. Kept
# next to the grammar it describes so the two can't silently drift.
DSL_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["root"],
    "properties": {
        "material": {"type": "string", "description": "One material name applied to the whole result, e.g. 'wood', 'stone', 'metal'. Optional."},
        "root": {"$ref": "#/$defs/node"},
    },
    "$defs": {
        "node": {
            "description": "A DSL node: exactly one of the shapes below, chosen by its 'op' field.",
            "anyOf": [
                {"$ref": "#/$defs/box"}, {"$ref": "#/$defs/prism"}, {"$ref": "#/$defs/cylinder"},
                {"$ref": "#/$defs/extrude"}, {"$ref": "#/$defs/translate"}, {"$ref": "#/$defs/rotate"},
                {"$ref": "#/$defs/scale"}, {"$ref": "#/$defs/union"}, {"$ref": "#/$defs/difference"},
                {"$ref": "#/$defs/group"}, {"$ref": "#/$defs/repeat"},
            ],
        },
        "box": {
            "type": "object", "required": ["op", "size"],
            "properties": {"op": {"const": "box"}, "size": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "[width, height, depth] in metres, each > 0"}},
        },
        "prism": {
            "type": "object", "required": ["op", "sides", "radius", "height"],
            "properties": {
                "op": {"const": "prism"},
                "sides": {"type": "integer", "minimum": 3, "maximum": 12, "description": "regular N-gon side count"},
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "cylinder": {
            "type": "object", "required": ["op", "radius", "height"],
            "properties": {
                "op": {"const": "cylinder"},
                "radius": {"type": "number", "exclusiveMinimum": 0},
                "height": {"type": "number", "exclusiveMinimum": 0},
                "segments": {"type": "integer", "minimum": 3, "maximum": 48, "description": "circle approximation quality, default 16"},
            },
        },
        "extrude": {
            "type": "object", "required": ["op", "polygon", "height"],
            "properties": {
                "op": {"const": "extrude"},
                "polygon": {"type": "array", "minItems": 3, "maxItems": 32, "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}, "description": "closed footprint [[x, z], ...]"},
                "height": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "translate": {
            "type": "object", "required": ["op", "offset", "child"],
            "properties": {"op": {"const": "translate"}, "offset": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}, "child": {"$ref": "#/$defs/node"}},
        },
        "rotate": {
            "type": "object", "required": ["op", "axis", "degrees", "child"],
            "properties": {"op": {"const": "rotate"}, "axis": {"enum": ["x", "y", "z"]}, "degrees": {"type": "number", "minimum": -720, "maximum": 720}, "child": {"$ref": "#/$defs/node"}},
        },
        "scale": {
            "type": "object", "required": ["op", "factors", "child"],
            "properties": {"op": {"const": "scale"}, "factors": {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0}, "minItems": 3, "maxItems": 3}, "child": {"$ref": "#/$defs/node"}},
        },
        "union": {
            "type": "object", "required": ["op", "children"],
            "properties": {"op": {"const": "union"}, "children": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"$ref": "#/$defs/node"}}},
        },
        "difference": {
            "type": "object", "required": ["op", "children"],
            "properties": {"op": {"const": "difference"}, "children": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"$ref": "#/$defs/node"}, "description": "first child minus every following child"}},
        },
        "group": {
            "type": "object", "required": ["op", "children"],
            "properties": {"op": {"const": "group"}, "children": {"type": "array", "minItems": 1, "maxItems": 16, "items": {"$ref": "#/$defs/node"}}},
        },
        "repeat": {
            "type": "object", "required": ["op", "count", "offset", "child"],
            "properties": {"op": {"const": "repeat"}, "count": {"type": "integer", "minimum": 1, "maximum": MAX_DSL_REPEAT_COUNT}, "offset": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}, "child": {"$ref": "#/$defs/node"}},
        },
    },
}


class DSLError(Exception):
    """Base for every DSL validation/execution failure. Always carries a
    specific, actionable message -- callers (``generator.py``'s repair
    loop) feed ``str(exc)`` straight back to the model."""


class DSLValidationError(DSLError):
    """The program (or a subtree of it) is structurally malformed --
    wrong types, missing fields, an unknown op, out-of-range values.
    Raised entirely by the pre-execution validation pass, before any
    geometry is built."""


class DSLBudgetExceededError(DSLError):
    """The program is structurally valid but exceeds a hard cap -- too
    many (expanded) nodes, too many vertices/faces, too large a
    bounding box, or too much wall-clock time. Distinguished from
    :class:`DSLValidationError` so a caller could in principle react
    differently (e.g. suggest simplifying vs. suggest fixing a typo),
    though ``generator.py`` currently treats both the same way: feed
    the message back for one repair attempt, then fall back."""


def execute_dsl_program(program: dict) -> GeneratedMesh:
    """The single pure entrypoint: ``program`` (already-parsed JSON) in,
    a :class:`GeneratedMesh` out. Raises :class:`DSLError` on any
    problem -- never returns a partial/broken mesh. One material for
    the whole result (``program.get("material", "")``), the same
    "there's no meaningful per-part correspondence to preserve across a
    boolean op" convention ``mesh_builder.from_trimesh`` already uses
    for CSG results."""
    if not isinstance(program, dict):
        raise DSLValidationError("program must be a JSON object")
    material = program.get("material", "")
    if not isinstance(material, str):
        raise DSLValidationError("program.material must be a string")
    root = program.get("root")
    if not isinstance(root, dict):
        raise DSLValidationError("program must have a 'root' node (a JSON object)")

    _validate_node(root, depth=0, counter=[0])

    budget = _Budget()
    deadline = time.monotonic() + MAX_DSL_EXECUTION_SECONDS
    tri = _execute_node(root, budget, deadline)

    if len(tri.vertices) == 0 or len(tri.faces) == 0:
        raise DSLValidationError("program produced an empty mesh")

    return from_trimesh(tri, material)


# -- pre-execution validation (structural, fails fast, never builds geometry) ------


def _validate_node(node, depth: int, counter: list) -> None:
    if depth > MAX_DSL_TREE_DEPTH:
        raise DSLValidationError(f"tree depth exceeds the {MAX_DSL_TREE_DEPTH} limit")
    if not isinstance(node, dict):
        raise DSLValidationError("every node must be a JSON object")
    op = node.get("op")
    if op not in _ALLOWED_OPS:
        raise DSLValidationError(f"unknown op {op!r} -- allowed: {sorted(_ALLOWED_OPS)}")

    counter[0] += 1
    if counter[0] > MAX_DSL_TEXTUAL_NODES:
        raise DSLValidationError(f"program has too many nodes ({counter[0]} > {MAX_DSL_TEXTUAL_NODES})")

    if op == "box":
        _require_number_triple(node, "size", positive=True, max_component=MAX_DSL_BOUNDING_BOX_M)
    elif op == "prism":
        _require_int_range(node, "sides", 3, 12)
        _require_positive_number(node, "radius", MAX_DSL_BOUNDING_BOX_M)
        _require_positive_number(node, "height", MAX_DSL_BOUNDING_BOX_M)
    elif op == "cylinder":
        _require_positive_number(node, "radius", MAX_DSL_BOUNDING_BOX_M)
        _require_positive_number(node, "height", MAX_DSL_BOUNDING_BOX_M)
        if "segments" in node:
            _require_int_range(node, "segments", 3, 48)
    elif op == "extrude":
        polygon = node.get("polygon")
        if not isinstance(polygon, list) or not (3 <= len(polygon) <= 32):
            raise DSLValidationError("extrude.polygon must be a list of 3-32 [x, z] points")
        for pt in polygon:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2 and all(_is_number(c) for c in pt)):
                raise DSLValidationError("extrude.polygon points must each be [x, z]")
        _require_positive_number(node, "height", MAX_DSL_BOUNDING_BOX_M)
    elif op == "translate":
        _require_number_triple(node, "offset", positive=False, max_component=100.0)
        _validate_node(_require_child(node), depth + 1, counter)
    elif op == "rotate":
        if node.get("axis") not in ("x", "y", "z"):
            raise DSLValidationError("rotate.axis must be 'x', 'y', or 'z'")
        degrees = node.get("degrees")
        if not _is_number(degrees) or not (-720 <= degrees <= 720):
            raise DSLValidationError("rotate.degrees must be a number in [-720, 720]")
        _validate_node(_require_child(node), depth + 1, counter)
    elif op == "scale":
        _require_number_triple(node, "factors", positive=True, max_component=20.0)
        _validate_node(_require_child(node), depth + 1, counter)
    elif op in ("union", "difference"):
        children = node.get("children")
        if not isinstance(children, list) or not (2 <= len(children) <= 8):
            raise DSLValidationError(f"{op}.children must be a list of 2-8 nodes")
        for c in children:
            _validate_node(c, depth + 1, counter)
    elif op == "group":
        children = node.get("children")
        if not isinstance(children, list) or not (1 <= len(children) <= 16):
            raise DSLValidationError("group.children must be a list of 1-16 nodes")
        for c in children:
            _validate_node(c, depth + 1, counter)
    elif op == "repeat":
        count = node.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not (1 <= count <= MAX_DSL_REPEAT_COUNT):
            raise DSLValidationError(f"repeat.count must be an integer in [1, {MAX_DSL_REPEAT_COUNT}]")
        _require_number_triple(node, "offset", positive=False, max_component=100.0)
        _validate_node(_require_child(node), depth + 1, counter)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _require_child(node: dict) -> dict:
    child = node.get("child")
    if not isinstance(child, dict):
        raise DSLValidationError(f"{node['op']}.child must be a node object")
    return child


def _require_number_triple(node: dict, key: str, *, positive: bool, max_component: float) -> None:
    values = node.get(key)
    if not (isinstance(values, list) and len(values) == 3 and all(_is_number(v) for v in values)):
        raise DSLValidationError(f"{node['op']}.{key} must be a list of 3 numbers")
    for v in values:
        if positive and v <= 0:
            raise DSLValidationError(f"{node['op']}.{key} components must be positive")
        if abs(v) > max_component:
            raise DSLValidationError(f"{node['op']}.{key} component {v} exceeds the {max_component} limit")


def _require_positive_number(node: dict, key: str, max_value: float) -> None:
    v = node.get(key)
    if not _is_number(v) or v <= 0:
        raise DSLValidationError(f"{node['op']}.{key} must be a positive number")
    if v > max_value:
        raise DSLValidationError(f"{node['op']}.{key}={v} exceeds the {max_value} limit")


def _require_int_range(node: dict, key: str, lo: int, hi: int) -> None:
    v = node.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        raise DSLValidationError(f"{node['op']}.{key} must be an integer in [{lo}, {hi}]")


# -- execution (geometry construction + live budget checks) ------------------------


class _Budget:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0

    def count_node(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_DSL_EXPANDED_NODES:
            raise DSLBudgetExceededError(
                f"expanded node budget exceeded ({self.nodes} > {MAX_DSL_EXPANDED_NODES}) -- "
                "likely a repeat loop multiplying out too far"
            )


def _execute_node(node: dict, budget: _Budget, deadline: float):
    if time.monotonic() > deadline:
        raise DSLBudgetExceededError(f"execution exceeded the {MAX_DSL_EXECUTION_SECONDS}s time budget")
    budget.count_node()
    op = node["op"]

    try:
        if op == "box":
            mesh = _primitive_box(node)
        elif op == "prism":
            mesh = _primitive_prism(node)
        elif op == "cylinder":
            mesh = _primitive_cylinder(node)
        elif op == "extrude":
            mesh = _primitive_extrude(node)
        elif op == "translate":
            mesh = _execute_node(node["child"], budget, deadline).copy()
            mesh.apply_translation(tuple(float(c) for c in node["offset"]))
        elif op == "rotate":
            mesh = _execute_node(node["child"], budget, deadline).copy()
            mesh.apply_transform(_rotation_matrix(node["axis"], float(node["degrees"])))
        elif op == "scale":
            mesh = _execute_node(node["child"], budget, deadline).copy()
            mesh.apply_transform(_scale_matrix(node["factors"]))
        elif op == "group":
            mesh = _concatenate([_execute_node(c, budget, deadline) for c in node["children"]])
        elif op == "repeat":
            dx, dy, dz = (float(c) for c in node["offset"])
            copies = []
            for i in range(node["count"]):
                copy = _execute_node(node["child"], budget, deadline).copy()
                copy.apply_translation((dx * i, dy * i, dz * i))
                copies.append(copy)
            mesh = _concatenate(copies)
        elif op == "union":
            mesh = _boolean_fold([_execute_node(c, budget, deadline) for c in node["children"]], "union")
        else:  # "difference"
            mesh = _boolean_fold([_execute_node(c, budget, deadline) for c in node["children"]], "difference")
    except DSLError:
        raise
    except Exception as exc:  # sandboxing promise: any internal failure surfaces as a typed DSL error
        raise DSLValidationError(f"'{op}' failed: {exc}") from exc

    if len(mesh.vertices) > MAX_DSL_VERTICES:
        raise DSLBudgetExceededError(f"vertex budget exceeded ({len(mesh.vertices)} > {MAX_DSL_VERTICES})")
    if len(mesh.faces) > MAX_DSL_FACES:
        raise DSLBudgetExceededError(f"face budget exceeded ({len(mesh.faces)} > {MAX_DSL_FACES})")
    if len(mesh.vertices):
        extent = mesh.bounds[1] - mesh.bounds[0]
        if any(e > MAX_DSL_BOUNDING_BOX_M for e in extent):
            raise DSLBudgetExceededError(
                f"'{op}' bounding box {tuple(round(float(e), 2) for e in extent)} "
                f"exceeds the {MAX_DSL_BOUNDING_BOX_M}m-per-axis limit"
            )
    return mesh


def _primitive_box(node: dict):
    w, h, d = (float(c) for c in node["size"])
    b = MeshBuilder()
    add_box(b, (-w / 2.0, 0.0, -d / 2.0), (w, h, d))
    return to_trimesh(b.mesh)


def _primitive_prism(node: dict):
    points = _regular_polygon_points(int(node["sides"]), float(node["radius"]))
    b = MeshBuilder()
    add_extruded_polygon(b, points, 0.0, float(node["height"]))
    return to_trimesh(b.mesh)


def _primitive_cylinder(node: dict):
    b = MeshBuilder()
    add_cylinder(
        b, (0.0, 0.0, 0.0), float(node["radius"]), float(node["height"]),
        segments=int(node.get("segments", 16)),
    )
    return to_trimesh(b.mesh)


def _primitive_extrude(node: dict):
    points = [(float(p[0]), float(p[1])) for p in node["polygon"]]
    b = MeshBuilder()
    add_extruded_polygon(b, points, 0.0, float(node["height"]))
    return to_trimesh(b.mesh)


def _regular_polygon_points(sides: int, radius: float) -> list[tuple[float, float]]:
    return [
        (radius * math.cos(2 * math.pi * i / sides), radius * math.sin(2 * math.pi * i / sides))
        for i in range(sides)
    ]


def _rotation_matrix(axis: str, degrees: float) -> np.ndarray:
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    mat = np.eye(4)
    if axis == "x":
        mat[1, 1], mat[1, 2] = c, -s
        mat[2, 1], mat[2, 2] = s, c
    elif axis == "y":
        mat[0, 0], mat[0, 2] = c, s
        mat[2, 0], mat[2, 2] = -s, c
    else:  # "z"
        mat[0, 0], mat[0, 1] = c, -s
        mat[1, 0], mat[1, 1] = s, c
    return mat


def _scale_matrix(factors) -> np.ndarray:
    sx, sy, sz = (float(f) for f in factors)
    mat = np.eye(4)
    mat[0, 0], mat[1, 1], mat[2, 2] = sx, sy, sz
    return mat


def _concatenate(meshes: list):
    import trimesh

    return trimesh.util.concatenate(meshes)


def _boolean_fold(meshes: list, op: str):
    result = meshes[0]
    for m in meshes[1:]:
        result = getattr(result, op)(m)
    return result
