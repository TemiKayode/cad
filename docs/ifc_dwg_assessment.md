# IFC and DWG: feasibility assessment

Part 7 C4 asked for an honest look at BIM (IFC) and native AutoCAD
(DWG) interop, alongside the SVG/DXF/PDF/STL/STEP/glTF/3MF formats this
project actually implements. Both are out of scope for this project,
for two different reasons — one a real data-model gap, the other a
licensing wall — laid out below rather than silently skipped. This
mirrors the same honesty this project already applies elsewhere (the
Meshy tier's "not live-verified, no API key available" note, the
architecture critique responses in the main README): a capability is
either built and verified, or its absence is explained, never implied
by omission.

## IFC (Industry Foundation Classes)

**What it is.** An open ISO standard (ISO 16739) for Building
Information Modeling data exchange, published by buildingSMART. An IFC
file is physically encoded the same way a STEP file is (the shared ISO
10303-21 "Part 21" exchange structure this project's own STEP export
already writes) — but the *schema* on top of that encoding is a
building-domain data model: walls, doors, windows, spaces, building
storeys, MEP systems, structural members, property sets, quantities,
classification systems, and a spatial-containment hierarchy relating
all of them. It is roughly two orders of magnitude larger than the
plain B-Rep/faceted-geometry schema `step_export.py` already targets.

**Library availability is not the blocker.** `ifcopenshell` is a real,
actively maintained, OpenCascade-backed open-source library with
modern pip wheels — confirmed directly (not assumed) while writing this
assessment: `pip download ifcopenshell` resolves a genuine prebuilt
`ifcopenshell-0.8.5-py314-none-win_amd64.whl` for this exact platform,
the same "conda-only was the old story, a real wheel exists now"
re-evaluation `step_export.py`'s own module docstring already made for
`build123d`/`cadquery-ocp-novtk`. Reading/writing an IFC *file* is not
the hard part.

**The real blocker is this project's own data model.** `MeshCRDT` is a
flat, generic triangle-soup CRDT — vertices, edges, and untyped faces,
with no concept of a wall vs. a window vs. a room, no storeys, no
property sets, no spatial hierarchy. A real IFC export needs *that*
information to exist somewhere first; there is currently nowhere for
"this face group is a load-bearing wall on Level 2" to live in the
document model. Building that data model — a whole additional typed
layer above the geometry, its own CRDT components, its own UI for
tagging/authoring it — is a project on the scale of Part 6's
account/permission system, not an export-function-sized addition.

**What *would* be honest and small, if ever picked up:** exporting
every current face group as an anonymous `IfcBuildingElementProxy`
wrapping an `IfcTriangulatedFaceSet`, with no semantic typing at all —
technically valid IFC, importable as reference geometry into a real BIM
tool (Revit, ArchiCAD, BlenderBIM), but carrying exactly the same
"faceted, no semantic authoring, geometry only" honesty this project's
STEP export already commits to for B-Rep. That's a plausible, bounded
follow-up. A full semantic BIM round-trip is not, and isn't attempted
here.

## DWG (AutoCAD's native format)

**What it is.** Autodesk's proprietary binary CAD format — unlike DXF
(which Autodesk *does* publish a full specification for, which is
exactly why this project already has full-fidelity DXF import/export,
including native SPLINE/ARC/CIRCLE/ELLIPSE round-tripping as of Part 7
C2), DWG has no official public specification at all.

**This is a licensing wall, not a technical-difficulty one.** The
options for reading/writing real DWG files:

- **The Open Design Alliance (ODA) SDK** — the industry-standard,
  most-complete reverse-engineered DWG implementation, and what most
  commercial non-Autodesk CAD tools actually license to read/write
  DWG. It is a **commercial, proprietary SDK** requiring an ODA
  membership/license agreement — not something `pip install`-able into
  an open-source project's dependency list, unlike every other format
  this project supports.
- **LibreDWG** (GNU project, LGPL) — the real open-source alternative,
  but with materially weaker coverage than ezdxf's DXF support: write
  support is limited/experimental, fidelity varies by DWG version, and
  there is no mature high-level Python binding comparable to `ezdxf`'s
  polish to build on. Adopting it would mean owning a fragile,
  partial-fidelity binary parser as a core dependency — a real
  regression from this project's "faithful, editable in any real CAD
  tool" bar for every other export format.

**The practical, standard workaround — already fully supported here:**
DWG↔DXF conversion is a one-click, lossless "Save As" in AutoCAD and
every AutoCAD-family tool, precisely because Autodesk designed DXF as
DWG's own official interchange format. The honest recommendation for a
user who needs DWG interop with this project is exactly that existing,
zero-effort path: **save as DXF and use this project's own DXF
import/export** (`drawing_from_dxf_bytes`/`drawing_to_dxf_bytes` in
`src/crdt_cad/export/dxf_io.py`) — not a gap this project papers over,
but the same interop path virtually every non-Autodesk CAD tool already
relies on for DWG compatibility.

## Summary

| Format | Blocker | Re-evaluate later? |
|---|---|---|
| IFC | This project has no BIM-semantic data model to export *from* (not a library-availability problem) | Yes — geometry-only anonymous-proxy IFC export is a bounded, plausible follow-up; full semantic BIM authoring is not |
| DWG | No open-source implementation matches this project's fidelity bar; the complete one (ODA) is commercially licensed | Only if ODA licensing terms ever change, or LibreDWG's write fidelity matures substantially — track upstream, don't re-attempt speculatively |
