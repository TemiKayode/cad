# Changelog

All notable changes to this project are documented here, grouped by
the build phase they belong to (see `README.md` for what each capability
actually does — this file is a historical record, not feature docs).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — Part 6 (P1–P7) and Part 7 (C1–C8)

### Part 7 — Pro CAD tools (C1–C8)
- **C1**: mirror, linear/circular array, a shapely-backed offset tool
  (correct on concave corners), fillet, and trim/extend for the 2D
  sketch demo.
- **C2**: freehand/polygon curve segments export as real DXF `SPLINE`
  entities (not flattened polylines); DXF import reads
  `CIRCLE`/`ARC`/`ELLIPSE`/`SPLINE` back in.
- **C3**: print sheets (page setup + a collaboratively-editable title
  block) with independent PDF export.
- **C4**: glTF (.glb) and 3MF mesh export, STEP import (tessellated
  back into live, editable geometry), and a documented IFC/DWG
  feasibility assessment.
- **C5**: reusable components — turn any path into a live definition;
  every placed instance re-resolves from the definition on render, so
  editing the master updates every instance immediately.
- **C6**: real mesh boolean operations (Union/Subtract/Intersect via
  trimesh/manifold3d), a B-Rep design writeup, and a flag-gated
  parametric-Box prototype.
- **C7**: PWA installability (manifest + service worker) and
  touch/pinch-zoom support for the 2D canvas.
- **C8**: viewport culling for the 2D canvas and edge-line LOD for the
  3D scene past 300 faces, opt-in soft per-room path/face budgets, and
  committed, rerunnable performance benchmarks (`docs/perf_benchmarks.md`).

### Part 6 — Accounts, permissions, organizations (P1–P7)
- **P1**: user accounts — magic-link and OAuth sign-in, server-side
  sessions.
- **P2**: document ownership and per-person permissions (owner/editor/
  commenter/viewer), composing with the pre-existing token system.
- **P3**: organizations and teams, with org-owned documents visible to
  every active member automatically.
- **P4**: per-org SSO (any standard OIDC provider), per-user quotas,
  and an operator admin panel.
- **P5**: 3D comments, `@mention` notifications, and a per-room
  activity feed.
- **P6**: org subscriptions via Stripe Checkout and a billing portal.
- **P7**: GDPR data export/account deletion and abuse reporting.

### Fixed
- Extrude tool: wrong-direction winding on every extrude, and a
  repeated-click trap that stacked non-manifold geometry.

### Infrastructure
- Fly.io deployment config and a GitHub Actions workflow to deploy on
  every push to `main` once CI passes.
- README rewritten for a production-facing audience.

## [0.1.0] — Parts 1–5

- **Part 1** (phases 1–9): platform hardening — rate limiting, resource
  ceilings, CORS, shared-secret room tokens, structured error handling.
- **Part 2** (phases 10–17): product features — version history,
  constraints, dimensions, groups, measure/dimension tools, shape
  primitives.
- **Part 3** (D1–D8): design system and UX polish — design tokens,
  command palette, keyboard shortcuts, accessibility, remote cursor
  presence.
- **Part 4** (18–19): deployment — Docker Compose, Kubernetes manifests
  (validated on a real cluster, both single-replica and
  Postgres+Redis-scaled modes), Caddy TLS reverse proxy, automated
  backups, Grafana/Prometheus monitoring.
- **Part 5** (G1–G7): AI text-to-3D generation — a 14-generator
  registry, deterministic scene-layout solver, a sandboxed geometry
  DSL with validate/repair/fallback, per-generation provenance and
  undo, a 66-case eval harness in CI, and an optional hosted Meshy AI
  tier.

The core CRDT engine (`LWWRegister`/`LWWMap`/`LWWElementSet`, RGA,
vector clocks) and the initial 2D sketch / 3D mesh collaborative
editors were built as part of this foundational release.
