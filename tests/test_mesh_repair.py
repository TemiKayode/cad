import sys

import pytest

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.mesh_repair import _fan_triangulate_fallback, repair_for_printing
from crdt_cad.ai.procedural_house import build_house_mesh


def test_repair_produces_a_valid_triangle_soup():
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    vertices, triangles = repair_for_printing(mesh.vertices, mesh.faces)
    assert len(vertices) > 0
    assert len(triangles) > 0
    for tri in triangles:
        assert len(tri) == 3
        for idx in tri:
            assert 0 <= idx < len(vertices)


def test_repair_every_vertex_is_a_3_tuple_of_floats():
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    vertices, _ = repair_for_printing(mesh.vertices, mesh.faces)
    for v in vertices:
        assert len(v) == 3
        assert all(isinstance(c, float) for c in v)


def test_repair_handles_a_single_box_without_crashing():
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    vertices, triangles = repair_for_printing(mesh.vertices, mesh.faces)
    # a box has 6 quad faces -> at least 12 triangles worth of surface,
    # possibly more once non-manifold repair runs
    assert len(triangles) >= 6


def test_poisson_reconstruction_produces_a_denser_watertight_resurfacing():
    """Confirms poisson_reconstruct is a real, working opt-in path, and
    documents *why* it defaults to off: it resamples the whole surface,
    producing many more triangles than the crisp input had."""
    pytest.importorskip("pymeshlab")
    mesh = build_house_mesh(HouseSpec(bedrooms=4, floors=1))
    plain_vertices, plain_triangles = repair_for_printing(mesh.vertices, mesh.faces)
    poisson_vertices, poisson_triangles = repair_for_printing(
        mesh.vertices, mesh.faces, poisson_reconstruct=True, poisson_depth=6
    )
    assert len(poisson_triangles) > len(plain_triangles)
    assert len(poisson_vertices) > len(plain_vertices)


def test_fallback_triangulation_used_when_pymeshlab_unavailable(monkeypatch):
    """Simulates pymeshlab not being installed by removing it from
    sys.modules and blocking re-import -- confirms the graceful
    degradation path (not just the happy path) actually works."""
    monkeypatch.setitem(sys.modules, "pymeshlab", None)  # `import pymeshlab` raises ImportError
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    vertices, triangles = repair_for_printing(mesh.vertices, mesh.faces)
    expected_vertices, expected_triangles = _fan_triangulate_fallback(mesh.vertices, mesh.faces)
    assert vertices == expected_vertices
    assert triangles == expected_triangles
    assert len(triangles) == len(mesh.faces) * 2  # every face here is a quad -> 2 triangles


def test_fan_triangulate_fallback_directly():
    positions = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (1.0, 1.0, 0.0), "d": (0.0, 1.0, 0.0)}
    faces = {"f1": ["a", "b", "c", "d"]}
    vertices, triangles = _fan_triangulate_fallback(positions, faces)
    assert len(vertices) == 4
    assert triangles == [(0, 1, 2), (0, 2, 3)]


def test_repair_gracefully_falls_back_on_pymeshlab_internal_error(monkeypatch):
    """If pymeshlab is installed but the repair pipeline itself raises
    (a real filter error, bad params, etc.), repair_for_printing must
    still return a usable result rather than propagating the exception."""
    pymeshlab = pytest.importorskip("pymeshlab")

    class ExplodingMeshSet(pymeshlab.MeshSet):
        def add_mesh(self, *args, **kwargs):
            raise RuntimeError("simulated pymeshlab failure")

    monkeypatch.setattr(pymeshlab, "MeshSet", ExplodingMeshSet)
    mesh = build_house_mesh(HouseSpec(bedrooms=1, floors=1))
    vertices, triangles = repair_for_printing(mesh.vertices, mesh.faces)
    assert len(triangles) == len(mesh.faces) * 2
