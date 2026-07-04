"""Text-to-3D generation pipeline for architectural prompts.

Honest scope, stated up front: this does not wrap TripoSR / Hunyuan3D /
Meshy or any image-to-3D neural network. Those are multi-gigabyte,
GPU-hosted models that cannot responsibly be "integrated" (downloaded,
run, and verified) inside this project's environment -- doing so
without ever running them would just be pretending. Instead:

- ``interpreter.py`` calls Claude (``claude-fable-5``) to turn a free-text
  architectural prompt into a structured, bounded specification (bedroom
  count, floors, floor material, style) -- exactly the part of this
  pipeline an LLM is actually well-suited for. If no API key is
  configured, or the call fails for any reason, it falls back to a
  deterministic keyword/regex parser, so the feature works end to end
  without external credentials.
- ``procedural_house.py`` deterministically builds an actual watertight
  building mesh (floor/roof slabs, exterior walls, interior partitions)
  from that specification -- geometry construction, not hallucination,
  so it's correct by construction and directly testable.
- ``mesh_repair.py`` wraps ``pymeshlab`` for the separate, genuinely
  useful job of preparing a mesh for 3D printing (dedup vertices, drop
  non-manifold geometry, optional Poisson reconstruction for a truly
  watertight surface) -- applied at STL-export time, not baked into the
  collaborative view, since Poisson reconstruction resamples/smooths
  geometry in a way that would blur crisp architectural edges.
- ``generator.py`` orchestrates prompt -> spec -> mesh -> batched
  ``MeshOp`` list, chunked so a large generated house doesn't arrive at
  the WebSocket relay as one giant message (see its module docstring).
"""

from crdt_cad.ai.house_spec import HouseSpec
from crdt_cad.ai.procedural_house import GeneratedMesh, build_house_mesh

__all__ = ["HouseSpec", "GeneratedMesh", "build_house_mesh"]
