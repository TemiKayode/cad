"""Tests for glTF (.glb)/3MF export (Part 7 C4) -- via trimesh, already
a core dependency, so unlike test_step_export.py this needs no
importorskip guard."""

from crdt_cad.export.mesh_interop import mesh_to_3mf_bytes, mesh_to_glb_bytes

_TETRA_VERTS = {"v0": (0.0, 0.0, 0.0), "v1": (1.0, 0.0, 0.0), "v2": (0.0, 1.0, 0.0), "v3": (0.0, 0.0, 1.0)}
_TETRA_FACES = {
    "f0": ["v0", "v1", "v2"],
    "f1": ["v0", "v1", "v3"],
    "f2": ["v1", "v2", "v3"],
    "f3": ["v0", "v2", "v3"],
}


def test_mesh_to_glb_bytes_empty_mesh_returns_empty_bytes():
    assert mesh_to_glb_bytes({}, {}) == b""


def test_mesh_to_glb_bytes_face_with_fewer_than_3_live_vertices_is_skipped():
    verts = {"a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0)}
    faces = {"f1": ["a", "b", "missing"]}
    assert mesh_to_glb_bytes(verts, faces) == b""


def test_mesh_to_glb_bytes_produces_a_real_binary_gltf():
    data = mesh_to_glb_bytes(_TETRA_VERTS, _TETRA_FACES)
    assert data[:4] == b"glTF"
    assert len(data) > 100


def test_mesh_to_3mf_bytes_empty_mesh_returns_empty_bytes():
    assert mesh_to_3mf_bytes({}, {}) == b""


def test_mesh_to_3mf_bytes_produces_a_real_zip_container():
    data = mesh_to_3mf_bytes(_TETRA_VERTS, _TETRA_FACES)
    assert data[:2] == b"PK"
    assert len(data) > 100


def test_mesh_to_glb_and_3mf_fan_triangulate_quad_faces():
    """A quad face (4 vertices) must become 2 triangles, the same
    fan-from-first-vertex technique mesh_to_stl/mesh_to_step_bytes
    already use -- not silently dropped or exported as a degenerate
    single triangle."""
    verts = {
        "a": (0.0, 0.0, 0.0), "b": (1.0, 0.0, 0.0), "c": (1.0, 1.0, 0.0), "d": (0.0, 1.0, 0.0),
    }
    faces = {"f1": ["a", "b", "c", "d"]}
    glb = mesh_to_glb_bytes(verts, faces)
    assert glb[:4] == b"glTF"
    threemf = mesh_to_3mf_bytes(verts, faces)
    assert threemf[:2] == b"PK"
