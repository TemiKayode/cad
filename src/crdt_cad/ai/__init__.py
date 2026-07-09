"""Text-to-3D generation pipeline (Part 5, phases G1+): a registry of
deterministic generators, dispatched to by an LLM (or a heuristic
fallback) that never emits geometry itself.

Honest scope, stated up front: this does not wrap TripoSR / Hunyuan3D /
Meshy or any image-to-3D neural network by default. Those are
multi-gigabyte, GPU-hosted models that cannot responsibly be
"integrated" (downloaded, run, and verified) inside this project's
environment -- doing so without ever running them would just be
pretending. ``meshy_adapter.py`` is the one opt-in, hosted-API exception
(Phase 9 / G7), gated behind ``MESHY_API_KEY``. Instead:

- ``registry.py`` holds every generator: a bounded pydantic spec, a
  deterministic ``build`` function returning watertight geometry, and a
  description/keywords for dispatch. ``crdt_cad.ai.generators`` is the
  package of generator modules themselves (house, primitives, furniture,
  architectural elements, wall openings) -- importing it (which this
  package's own import below does) populates the registry as a side
  effect of each module registering itself at import time.
- ``interpreter.py`` calls Claude (``claude-fable-5``) with the registry
  presented as a tool catalog to pick a generator and fill its spec --
  exactly the part of this pipeline an LLM is actually well-suited for.
  If no API key is configured, or the call fails for any reason, it
  falls back to a deterministic keyword dispatcher, so the feature works
  end to end without external credentials.
- ``mesh_builder.py`` holds the shared primitive builders (box, cylinder,
  cone, torus, extruded polygon/profile) every generator assembles from,
  plus the ``trimesh`` conversion helpers CSG (door/window) and
  pre-commit validation both need.
- ``validation.py`` is the pre-commit gate (watertight/manifold/bounds)
  every generated mesh must pass before it's turned into ops -- a
  failure is a typed, visible error, never a silently-injected broken
  mesh (see ``GenerationValidationError``).
- ``mesh_repair.py`` wraps ``pymeshlab`` for the separate, genuinely
  useful job of preparing a mesh for 3D printing (dedup vertices, drop
  non-manifold geometry, optional Poisson reconstruction for a truly
  watertight surface) -- applied at STL-export time, not baked into the
  collaborative view, since Poisson reconstruction resamples/smooths
  geometry in a way that would blur crisp architectural edges.
- ``generator.py`` orchestrates prompt -> (generator, spec) -> mesh ->
  batched ``MeshOp`` list, chunked so a large generated mesh doesn't
  arrive at the WebSocket relay as one giant message (see its module
  docstring).
"""

from crdt_cad.ai import generators  # noqa: F401 -- populates the registry as an import side effect
from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.procedural_house import GeneratedMesh, build_house_mesh
from crdt_cad.ai.registry import REGISTRY

__all__ = ["HouseSpec", "GeneratedMesh", "build_house_mesh", "REGISTRY"]
