"""Tests for the pre-commit generation validation module (Phase G1,
rule 1: "a validation failure is a visible, typed error -- never a
silently-injected broken mesh")."""

import pytest

from crdt_cad.ai.mesh_builder import MeshBuilder, add_box
from crdt_cad.ai.mesh_types import GeneratedMesh
from crdt_cad.ai.validation import (
    GenerationValidationError,
    validate_generated_mesh,
    validate_or_raise,
)


def _watertight_box():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "wood")
    return b.mesh


def test_a_proper_watertight_box_passes():
    report = validate_generated_mesh(_watertight_box())
    assert report.ok
    assert report.watertight
    assert report.manifold
    assert report.within_bounds
    assert report.vertex_count == 8
    assert report.face_count == 6
    assert report.triangle_count == 12
    assert report.errors == []


def test_an_open_box_missing_one_face_fails_watertight_check():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "wood")
    # Drop one face to leave a real hole in the solid.
    last_face = list(b.mesh.faces)[-1]
    del b.mesh.faces[last_face]

    report = validate_generated_mesh(b.mesh)
    assert not report.ok
    assert not report.watertight
    assert any("watertight" in e for e in report.errors)


def test_require_watertight_false_allows_an_open_mesh():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "wood")
    last_face = list(b.mesh.faces)[-1]
    del b.mesh.faces[last_face]

    report = validate_generated_mesh(b.mesh, require_watertight=False)
    assert report.ok
    assert not report.watertight  # still honestly reported, just not treated as an error


def test_oversized_mesh_fails_the_bounding_box_check():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (500.0, 1.0, 1.0), "wood")
    report = validate_generated_mesh(b.mesh)
    assert not report.ok
    assert not report.within_bounds
    assert any("bounding box" in e for e in report.errors)


def test_too_many_vertices_fails_regardless_of_geometry():
    mesh = GeneratedMesh(vertices={f"v{i}": (0.0, 0.0, 0.0) for i in range(10)}, faces={})
    report = validate_generated_mesh(mesh, max_vertices=5)
    assert not report.ok
    assert any("vertices exceeds" in e for e in report.errors)


def test_empty_mesh_fails():
    report = validate_generated_mesh(GeneratedMesh())
    assert not report.ok
    assert "empty mesh" in report.errors


def test_degenerate_zero_area_triangle_fails():
    mesh = GeneratedMesh(
        vertices={"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (2.0, 0.0, 0.0)},  # collinear -> zero area
        faces={"f1": ["a", "b", "c"]},
    )
    report = validate_generated_mesh(mesh, require_watertight=False)
    assert not report.ok
    assert any("degenerate" in e for e in report.errors)


def test_validate_or_raise_raises_generation_validation_error_with_the_report():
    b = MeshBuilder()
    add_box(b, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), "wood")
    del b.mesh.faces[list(b.mesh.faces)[-1]]

    with pytest.raises(GenerationValidationError) as exc_info:
        validate_or_raise(b.mesh)
    assert exc_info.value.report.ok is False
    assert "not watertight" in str(exc_info.value) or exc_info.value.report.errors


def test_planar_quad_faces_pass_the_planarity_check():
    report = validate_generated_mesh(_watertight_box())
    assert report.planar
    assert report.non_planar_face_count == 0


def test_triangle_only_mesh_is_trivially_planar():
    b = MeshBuilder()
    v1 = b.vertex((0.0, 0.0, 0.0))
    v2 = b.vertex((1.0, 0.0, 0.0))
    v3 = b.vertex((0.0, 1.0, 0.3))  # a lone triangle can never be non-planar
    b.face([v1, v2, v3], "wood")
    report = validate_generated_mesh(b.mesh, require_watertight=False, require_consistent_winding=False)
    assert report.planar
    assert report.non_planar_face_count == 0


def test_a_genuinely_non_planar_quad_is_detected():
    b = MeshBuilder()
    v1 = b.vertex((0.0, 0.0, 0.0))
    v2 = b.vertex((1.0, 0.0, 0.0))
    v3 = b.vertex((1.0, 1.0, 0.0))
    v4 = b.vertex((0.0, 1.0, 0.5))  # pushed out of the plane the other three define
    b.face([v1, v2, v3, v4], "wood")
    report = validate_generated_mesh(b.mesh, require_watertight=False, require_consistent_winding=False)
    assert not report.planar
    assert report.non_planar_face_count == 1
    # informational only -- does not fail the mesh (see validation.py's own docstring on this)
    assert report.ok


def test_validate_or_raise_does_not_raise_for_a_good_mesh():
    validate_or_raise(_watertight_box())  # must not raise
