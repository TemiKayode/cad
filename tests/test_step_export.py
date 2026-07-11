"""Tests for STEP export (Phase 9 stretch item) -- skips cleanly if
`build123d` isn't installed, so a plain `pytest tests/` from a fresh
checkout never needs it (it's a genuinely heavy optional dependency --
`pip install crdt-cad[step]` -- deliberately not part of the `dev`
extra the way asyncpg/redis are, given its much larger transitive
dependency footprint; a contributor who wants to actually exercise this
file installs it separately).
"""

import pytest

pytest.importorskip("build123d")

from crdt_cad.export.step_export import mesh_from_step_bytes, mesh_to_step_bytes  # noqa: E402


def test_empty_mesh_returns_empty_bytes():
    assert mesh_to_step_bytes({}, {}) == b""


def test_face_with_fewer_than_3_live_vertices_is_skipped():
    verts = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0)}
    faces = {"f1": ["a", "b", "missing"]}
    assert mesh_to_step_bytes(verts, faces) == b""


def test_single_triangle_exports_valid_step_bytes():
    verts = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (0.0, 1.0, 0.0)}
    faces = {"f1": ["a", "b", "c"]}
    data = mesh_to_step_bytes(verts, faces)
    assert data.startswith(b"ISO-10303-21;")
    assert b"ADVANCED_FACE" in data


def test_closed_box_exports_a_real_manifold_solid():
    """A genuinely closed mesh should produce a MANIFOLD_SOLID_BREP, not
    just a loose bag of faces -- the case build123d's Solid(Shell(...))
    only produces a nonzero-volume result for (confirmed directly while
    building this: an open shell silently gives volume 0.0 rather than
    raising)."""
    verts = {
        "v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (1.0, 1.0, 0.0), "v3": (0.0, 1.0, 0.0),
        "v4": (0.0, 0.0, 1.0), "v5": (1.0, 0.0, 1.0), "v6": (1.0, 1.0, 1.0), "v7": (0.0, 1.0, 1.0),
    }
    faces = {
        "bottom": ["v0", "v3", "v2", "v1"],
        "top": ["v4", "v5", "v6", "v7"],
        "front": ["v0", "v1", "v5", "v4"],
        "back": ["v3", "v7", "v6", "v2"],
        "left": ["v0", "v4", "v7", "v3"],
        "right": ["v1", "v2", "v6", "v5"],
    }
    data = mesh_to_step_bytes(verts, faces)
    assert b"MANIFOLD_SOLID_BREP" in data


def test_incomplete_wip_mesh_exports_as_an_open_shape_not_a_false_solid():
    """A single triangle floating in space (the same "incomplete WIP
    mesh" case crdt_cad.geometry.mesh_validity deliberately never flags
    as a problem) must not be exported as if it were a closed solid --
    there is no MANIFOLD_SOLID_BREP to claim."""
    verts = {"a": (0.0, 0.0, 0.0), "b": (5.0, 0.0, 0.0), "c": (0.0, 5.0, 0.0)}
    faces = {"floor": ["a", "b", "c"]}
    data = mesh_to_step_bytes(verts, faces)
    assert b"MANIFOLD_SOLID_BREP" not in data
    assert b"ADVANCED_FACE" in data


def test_nonplanar_face_falls_back_to_a_fan_triangulation_instead_of_failing():
    """Nothing in this project enforces face planarity -- a face whose
    vertices have drifted non-planar must still export (as a fan of
    trivially-planar triangles), not raise, since build123d's own
    Face(wire) rejects a non-planar wire outright (confirmed directly:
    ValueError: Cannot build face(s): wires not planar)."""
    verts = {
        "a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (1.0, 1.0, 0.0), "d": (0.0, 1.0, 0.5),
    }
    faces = {"f1": ["a", "b", "c", "d"]}
    data = mesh_to_step_bytes(verts, faces)
    assert data.startswith(b"ISO-10303-21;")
    assert data.count(b"ADVANCED_FACE") >= 2  # fan of >= 2 triangles, not 1 quad


# -- STEP import (Part 7 C4) ---------------------------------------------------


def test_mesh_from_step_bytes_round_trips_a_closed_box():
    """Exports a real closed box via the existing writer, re-imports it,
    and confirms the tessellated result is welded back down to exactly
    the 8 corners a naive OpenCascade tessellation would otherwise
    triple/quadruple (one independent vertex triple per triangle,
    per-face, with no cross-face sharing)."""
    verts = {
        "v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (1.0, 1.0, 0.0), "v3": (0.0, 1.0, 0.0),
        "v4": (0.0, 0.0, 1.0), "v5": (1.0, 0.0, 1.0), "v6": (1.0, 1.0, 1.0), "v7": (0.0, 1.0, 1.0),
    }
    faces = {
        "bottom": ["v0", "v3", "v2", "v1"],
        "top": ["v4", "v5", "v6", "v7"],
        "front": ["v0", "v1", "v5", "v4"],
        "back": ["v3", "v7", "v6", "v2"],
        "left": ["v0", "v4", "v7", "v3"],
        "right": ["v1", "v2", "v6", "v5"],
    }
    data = mesh_to_step_bytes(verts, faces)
    mesh = mesh_from_step_bytes(data)
    assert len(mesh.vertices) == 8
    # every triangle references 3 distinct welded vertex ids
    for loop in mesh.faces.values():
        assert len(loop) == 3
        assert len(set(loop)) == 3
    # welded positions match the original 8 corners (order/ids may differ)
    original_positions = {tuple(round(c, 6) for c in p) for p in verts.values()}
    imported_positions = set(mesh.vertices.values())
    assert imported_positions == original_positions


def test_mesh_from_step_bytes_raises_on_a_malformed_file():
    with pytest.raises(ValueError):
        mesh_from_step_bytes(b"not a real step file")
