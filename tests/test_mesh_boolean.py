"""Tests for 3D mesh boolean operations (Part 7 C6) -- via trimesh's
boolean API (already a core dependency), no importorskip guard needed."""

import pytest

from crdt_cad.geometry.mesh_boolean import compute_mesh_boolean


def _box(x0, y0, z0, x1, y1, z1, prefix):
    vertices = {
        f"{prefix}0": (x0, y0, z0), f"{prefix}1": (x1, y0, z0), f"{prefix}2": (x1, y1, z0), f"{prefix}3": (x0, y1, z0),
        f"{prefix}4": (x0, y0, z1), f"{prefix}5": (x1, y0, z1), f"{prefix}6": (x1, y1, z1), f"{prefix}7": (x0, y1, z1),
    }
    faces = {
        f"{prefix}bottom": [f"{prefix}0", f"{prefix}3", f"{prefix}2", f"{prefix}1"],
        f"{prefix}top": [f"{prefix}4", f"{prefix}5", f"{prefix}6", f"{prefix}7"],
        f"{prefix}front": [f"{prefix}0", f"{prefix}1", f"{prefix}5", f"{prefix}4"],
        f"{prefix}back": [f"{prefix}3", f"{prefix}7", f"{prefix}6", f"{prefix}2"],
        f"{prefix}left": [f"{prefix}0", f"{prefix}4", f"{prefix}7", f"{prefix}3"],
        f"{prefix}right": [f"{prefix}1", f"{prefix}2", f"{prefix}6", f"{prefix}5"],
    }
    return vertices, faces


def test_union_of_two_overlapping_boxes_has_correct_volume():
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    result = compute_mesh_boolean("union", va, fa, vb, fb)
    from crdt_cad.export.mesh_interop import triangulated_trimesh

    tri = triangulated_trimesh(result.vertices, result.faces)
    assert tri.is_watertight
    assert tri.volume == pytest.approx(12.0, abs=0.01)  # 8 + 8 - 4 overlap


def test_subtract_removes_the_overlap_region():
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    result = compute_mesh_boolean("subtract", va, fa, vb, fb)
    from crdt_cad.export.mesh_interop import triangulated_trimesh

    tri = triangulated_trimesh(result.vertices, result.faces)
    assert tri.is_watertight
    assert tri.volume == pytest.approx(4.0, abs=0.01)


def test_intersect_keeps_only_the_overlap_region():
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    result = compute_mesh_boolean("intersect", va, fa, vb, fb)
    from crdt_cad.export.mesh_interop import triangulated_trimesh

    tri = triangulated_trimesh(result.vertices, result.faces)
    assert tri.is_watertight
    assert tri.volume == pytest.approx(4.0, abs=0.01)


def test_unknown_op_raises_value_error():
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    with pytest.raises(ValueError, match="unknown boolean op"):
        compute_mesh_boolean("bogus", va, fa, vb, fb)


def test_empty_operand_raises_value_error():
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    with pytest.raises(ValueError, match="at least one face"):
        compute_mesh_boolean("union", {}, {}, vb, fb)


def test_non_overlapping_intersect_raises_value_error():
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vc, fc = _box(100, 100, 100, 102, 102, 102, "c")
    with pytest.raises(ValueError, match="empty mesh"):
        compute_mesh_boolean("intersect", va, fa, vc, fc)


def test_boolean_normalizes_inside_out_operands_before_running():
    """Real, live-caught regression: a Box placed via the 3D demo's own
    primitive tool triangulates to a *negative*-volume ("inside-out")
    mesh -- watertight (is_watertight is about edge-manifoldness, not
    orientation), but manifold3d flatly refuses it with "Not all
    meshes are volumes!" unless normalized first. Reverses every face
    loop's vertex order (the same shape a real inward-wound mesh has)
    to reproduce that exact failure mode without needing the real 3D
    UI, and confirms compute_mesh_boolean now handles it transparently."""
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vb, fb = _box(1, 0, 0, 3, 2, 2, "b")
    fa_inverted = {fid: list(reversed(loop)) for fid, loop in fa.items()}
    fb_inverted = {fid: list(reversed(loop)) for fid, loop in fb.items()}

    from crdt_cad.export.mesh_interop import triangulated_trimesh

    # confirm the fixture actually reproduces a negative-volume operand
    # before relying on it to test the fix
    assert triangulated_trimesh(va, fa_inverted).volume < 0

    result = compute_mesh_boolean("union", va, fa_inverted, vb, fb_inverted)
    tri = triangulated_trimesh(result.vertices, result.faces)
    assert tri.is_watertight
    assert tri.volume == pytest.approx(12.0, abs=0.01)


def test_non_overlapping_subtract_leaves_a_unchanged():
    """trimesh/manifold3d correctly treat a non-overlapping subtract as a
    no-op on `a`, not an empty result -- worth asserting explicitly since
    it's the one boolean op where "no overlap" is a legitimate, useful
    outcome rather than an error."""
    va, fa = _box(0, 0, 0, 2, 2, 2, "a")
    vc, fc = _box(100, 100, 100, 102, 102, 102, "c")
    result = compute_mesh_boolean("subtract", va, fa, vc, fc)
    from crdt_cad.export.mesh_interop import triangulated_trimesh

    tri = triangulated_trimesh(result.vertices, result.faces)
    assert tri.volume == pytest.approx(8.0, abs=0.01)
