"""Scene composition (Phase G2): a list of (generator spec, layout
intent) pairs -- "a table with four chairs around it" -- resolved into
final world positions by a deterministic layout solver
(``scene_layout.py``), never by the LLM. Claude (or the heuristic
fallback) only ever picks *which* generators and *how many*, and states
the relationship in plain terms ("around", "on top of", "row of four");
turning that into actual (x, y, z) coordinates -- ground-plane snapping,
non-overlapping placement, correct stacking for "on" -- is ordinary
deterministic code, the same "the LLM never emits geometry" rule every
other generator in this package already follows.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from crdt_cad.ai.mesh_builder import MeshBuilder
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.registry import REGISTRY, get_generator

Relation = Literal["none", "around", "on_top_of", "row", "beside"]


class SceneObjectSpec(BaseModel):
    generator: str
    # Raw field values for `generator`'s own spec model -- re-validated
    # against that model (not just accepted as an opaque dict) in
    # `expand_scene`, so a malformed nested spec fails with the same
    # clear pydantic error a standalone generation would.
    spec: dict = Field(default_factory=dict)
    relation: Relation = "none"
    # Index into SceneSpec.objects of the object this one relates to --
    # must reference an *earlier* entry (no forward references, no
    # cycles by construction) when relation != "none".
    target_index: Optional[int] = None
    count: int = Field(ge=1, le=12, default=1)
    spacing_m: float = Field(gt=0.05, le=10.0, default=1.0)

    @model_validator(mode="after")
    def _generator_is_known(self) -> "SceneObjectSpec":
        if self.generator not in REGISTRY:
            raise ValueError(f"unknown generator {self.generator!r} -- known: {sorted(REGISTRY)}")
        return self


class SceneSpec(BaseModel):
    objects: list[SceneObjectSpec] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _targets_reference_earlier_objects_only(self) -> "SceneSpec":
        # "row" is the one relation that doesn't need an anchor -- a
        # standalone "row of four chairs" is meaningful on its own,
        # placed along the shared layout cursor. Every other non-"none"
        # relation is inherently relative to an earlier object.
        for i, obj in enumerate(self.objects):
            if obj.relation in ("none", "row"):
                continue
            if obj.target_index is None:
                raise ValueError(f"object {i} has relation={obj.relation!r} but no target_index")
            if not (0 <= obj.target_index < i):
                raise ValueError(
                    f"object {i}'s target_index={obj.target_index} must reference an earlier object (0..{i - 1})"
                )
        return self


class ExpandedObject:
    """One concrete placed object -- a `count > 1` SceneObjectSpec
    expands into this many, one per copy, all sharing the same relation/
    target so the layout solver can distribute them together (e.g. four
    chairs "around" the same table)."""

    __slots__ = ("generator", "spec", "mesh", "relation", "target_index", "spacing_m", "copy_index", "copy_count")

    def __init__(self, generator: str, spec: BaseModel, mesh: GeneratedMesh, relation: Relation,
                 target_index: Optional[int], spacing_m: float, copy_index: int, copy_count: int) -> None:
        self.generator = generator
        self.spec = spec
        self.mesh = mesh
        self.relation = relation
        self.target_index = target_index
        self.spacing_m = spacing_m
        self.copy_index = copy_index
        self.copy_count = copy_count


def expand_scene(scene: SceneSpec) -> list[ExpandedObject]:
    """Builds every object's own mesh (generator dispatch, `count`
    expansion) but does **not** position anything -- positioning is
    `scene_layout.solve_layout`'s job, kept as a separate deterministic
    step so the two concerns (what to build vs. where to put it) stay
    independently testable."""
    expanded: list[ExpandedObject] = []
    # target_index refers to *original* SceneObjectSpec list positions;
    # remap to the first expanded copy of that object (the natural
    # "anchor" when a target itself had count > 1).
    first_copy_index_of: dict[int, int] = {}

    for original_index, obj in enumerate(scene.objects):
        entry = get_generator(obj.generator)
        spec_instance = entry.spec_model(**obj.spec)
        first_copy_index_of[original_index] = len(expanded)
        remapped_target = (
            first_copy_index_of[obj.target_index] if obj.target_index is not None else None
        )
        for copy_index in range(obj.count):
            mesh = entry.build(spec_instance)
            expanded.append(ExpandedObject(
                generator=obj.generator, spec=spec_instance, mesh=mesh,
                relation=obj.relation, target_index=remapped_target, spacing_m=obj.spacing_m,
                copy_index=copy_index, copy_count=obj.count,
            ))
    return expanded


def merge_placed_objects(
    objects: list[ExpandedObject], translations: list[tuple[float, float, float]]
) -> tuple[GeneratedMesh, list[tuple[list[str], list[str]]]]:
    """Merges every already-positioned object into one combined mesh with
    globally-unique ids (every generator's own ``build`` starts its ids
    back at v1/f1, so a shared ``MeshBuilder`` is what makes multi-object
    scenes collision-free). Returns ``(mesh, per_object_ids)`` where
    ``per_object_ids[i]`` is ``(vertex_ids, face_ids)`` -- the ids in
    `mesh` that belong to `objects[i]`, in the order they were minted.

    This one mapping serves both of Phase G2's remaining needs: the
    caller (``generator.py``) uses the face ids to tag each face with a
    ``set_face_prop(face_id, "scene_object", ...)`` CRDT op (provenance
    ahead of Phase G4's fuller generation-id system -- ``MeshCRDT``'s
    ``face_props`` is a genuine per-face map, unlike `GeneratedMesh`'s
    own single-string-per-face ``face_materials``, so it already
    supports "material"/"color"/"scene_object" on the same face at
    once), and groups the ops themselves so the server can commit each
    object as its own batch -- a scene building visibly object by
    object, extending the Phase D7 staged-build UI pattern."""
    b = MeshBuilder()
    per_object_ids: list[tuple[list[str], list[str]]] = []
    for obj, (dx, dy, dz) in zip(objects, translations):
        vertex_ids = {old: b.vertex((pos[0] + dx, pos[1] + dy, pos[2] + dz)) for old, pos in obj.mesh.vertices.items()}
        face_ids: list[str] = []
        for face_id, loop in obj.mesh.faces.items():
            material = obj.mesh.face_materials.get(face_id, "")
            face_ids.append(b.face([vertex_ids[v] for v in loop], material))
        per_object_ids.append((list(vertex_ids.values()), face_ids))
    return b.mesh, per_object_ids
