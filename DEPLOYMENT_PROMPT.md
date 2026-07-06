# Deployment brief: validate Kubernetes, finish landing Phase 7, and ship crdt-cad to production

## Context

This is **Part 4 of the improvement plan** for `crdt-cad`. Read `README.md`
and `k8s/README.md` first. Every working rule from Part 1
(`IMPROVEMENT_PROMPT.md`) applies: no unverifiable claims, tests + README
update + verification per phase, phase-by-phase commits, no Claude
co-author trailer on commits.

## Current status — audit before you start (do not redo finished work)

- **Part 1 (phases 1–9): COMPLETE and committed** — security
  (`server/security.py`), CI + e2e (`.github/workflows`, `tests/e2e/`),
  IndexedDB outbox, 3D undo, frontier resync, mesh Validation Fork,
  **Phase 7** (`persistence/store.py` PostgresStore, `server/pubsub.py`
  Redis fan-out — verified with two real uvicorn processes sharing real
  Postgres/Redis containers), curves, stretch goals (constraint UI, STEP
  export, Meshy adapter).
- **Part 2 (phases 10–17): COMPLETE and committed** — viewport, primitives,
  selection editing, dimensions, constraints UI, designer features, 3D
  usability, workspace/home page.
- **Part 3 (`UI_UX_PROMPT.md`, D1–D8): IN PROGRESS** — D1 (design tokens)
  exists as uncommitted work (`demo/static/tokens.css`,
  `demo/static/icons.svg`, modified demo files). Finish and commit D1 per
  that brief before starting here (or stash it cleanly); do not let this
  brief's commits mix with it. D2–D8 remain open and are governed by
  `UI_UX_PROMPT.md`, not this file.
- **The four prompt briefs themselves are untracked** — commit them
  (one `docs:` commit) so the plan is versioned with the code.
- **The one remaining "not validated" claim in the project:** `k8s/` was
  never applied to any live cluster. Phase 7's *code* is verified at the
  process level; its *Kubernetes wiring* (Secrets, env plumbing, multi-pod
  fan-out through a Service, HPA) is not. Closing that gap is Phase 18.
  There is also no production deployment story (TLS/wss, backups, ops
  docs, published image) — that is Phase 19.

## Phase 18 — Kubernetes validation: fix `k8s/` and finish landing Phase 7

Goal: remove the "not validated against a live cluster" caveat from
`k8s/README.md` — honestly, by actually doing it. Docker is available on
this machine; use **kind** (or k3d if kind fights Windows) to run a real
local cluster inside Docker.

### 18.1 — Stand up the cluster and validate Mode A (default: 1 replica, SQLite + PVC)

- Create a kind cluster, build the image, `kind load docker-image
  crdt-cad:latest`, then `kubectl apply -k k8s/`.
- Fix whatever breaks — likely candidates: `imagePullPolicy` vs kind's
  loaded images, PVC binding on kind's default `standard` StorageClass,
  probe timing. Every fix goes into the manifests, not one-off kubectl
  surgery.
- Verify: rollout completes, readiness/liveness probes pass,
  `kubectl port-forward` + two real WebSocket clients in one room see each
  other's edits, **Save** persists, then `kubectl delete pod` and confirm
  the replacement pod rehydrates the same document from the PVC.

### 18.2 — Validate Mode B (the Phase 7 configuration: replicas > 1)

This is what finally lands Phase 7 end-to-end:

- Add `k8s/dev/` manifests for a single-node Postgres and Redis
  (clearly commented as *local validation only, not production-grade* —
  production clusters bring their own managed services, per
  `k8s/README.md`). Keep them out of the root kustomization; add a
  `k8s/dev/kustomization.yaml` overlay that composes base + dev + the
  scaled configuration.
- Create the `crdt-cad-database` Secret, enable `CRDT_CAD_DATABASE_URL` /
  `CRDT_CAD_REDIS_URL`, switch to `RollingUpdate`, set `replicas: 3`,
  drop the PVC — i.e., execute the exact recipe `k8s/README.md` describes,
  and fix the recipe wherever reality disagrees with it.
- **The critical test:** two WebSocket clients connected through the
  Service to *different pods* (verify pod identity via `X-Served-By`
  header or pod-name logging — add one if there's no way to tell) must see
  each other's edits via Redis fan-out, and a full
  `kubectl rollout restart` must lose no persisted data (Postgres).
- Run the existing e2e convergence scenarios against the cluster entry
  point instead of a local uvicorn — the offline/Time-Travel Merge flow
  must survive pod-hopping reconnects (session state is per-connection;
  if a reconnect landing on a different pod breaks delta resync, that is
  a real Phase 7 bug to fix, not a caveat to document).

### 18.3 — HPA and ingress

- Install metrics-server on kind (with the kind-specific TLS flag),
  apply `hpa.yaml`, drive CPU load, and watch it actually scale 1→N and
  back. Document the custom-metric (`crdt_cad_active_connections` via
  Prometheus Adapter) path as future work — CPU-based scaling verified is
  enough for this phase.
- Promote `ingress.yaml.example` to a real, tested `ingress.yaml`:
  nginx-ingress on kind, WebSocket passthrough confirmed (proxy read
  timeout raised so idle WS connections aren't cut), TLS with a
  self-signed/cert-manager certificate, `wss://` verified end-to-end from
  a browser. Keep it optional in the kustomization (not every target has
  nginx-ingress), but no longer a `.example` guess.

### 18.4 — Make it stay validated

- Add a CI job (`helm/kind-action` or equivalent) that spins up kind,
  applies Mode A, waits for readiness, and runs a smoke WebSocket
  round-trip — so the manifests can never silently rot back to
  "structurally plausible". Mode B in CI is optional (heavier); if
  skipped, say so in `k8s/README.md`.
- Rewrite `k8s/README.md`: replace the "not validated" caveat with
  exactly what was verified (kind, Mode A + Mode B + HPA + ingress) and
  what wasn't (no managed-cloud cluster, no real cert authority, dev-grade
  Postgres/Redis). Update the main `README.md` status table row for
  Kubernetes the same way.

## Phase 19 — Production deployment and operations

Goal: someone with a domain and a $5 VM (or a Fly.io account) can take
this repo to a secured public deployment by following committed,
verified artifacts — and the operational basics (TLS, backups, shutdown,
monitoring, published image) actually exist.

### 19.1 — TLS front door

- Add `docker-compose.prod.yml`: the app container plus **Caddy** with
  automatic HTTPS (Let's Encrypt), WebSocket proxying, and the app bound
  to the internal network only. A committed `Caddyfile` with the domain
  as the single thing to edit.
- Verify locally with a self-signed/`localhost` Caddy setup: `wss://`
  end-to-end, the security phase's token flow working through the proxy,
  correct client IPs reaching the rate limiter (`X-Forwarded-For`
  handling — fix the server if it rates-limits the proxy instead of the
  client).
- `docs/deployment.md`: a concise VPS runbook (Hetzner/DO/Lightsail
  generic): provision, install Docker, clone, set env (`CRDT_CAD_SECRET`
  mandatory for public exposure — say so in bold), `docker compose -f
  docker-compose.prod.yml up -d`, DNS, done. Plus the Kubernetes path
  pointing at `k8s/README.md`.

### 19.2 — Fly.io config (the fastest public path)

- Commit a `fly.toml`: single machine, volume mounted at the SQLite path,
  `min_machines_running = 1`, auto-stop disabled (WebSockets), internal
  port 8000, and a documented `fly deploy` sequence in
  `docs/deployment.md`.
- Honesty rule: if no Fly account/token is available in this environment,
  validate the config file structurally (`fly config validate` if the CLI
  is installable, otherwise schema-check) and mark it "config provided,
  not live-deployed" — do not claim a deployment that didn't happen.

### 19.3 — Backups and restore (both storage modes)

- SQLite mode: a `scripts/backup_sqlite.py` using the SQLite online
  backup API (`sqlite3 .backup` semantics — safe against a live writer;
  never plain file-copy), a documented cron/Compose-sidecar example, and
  a **tested restore procedure** (back up a room, wipe, restore, verify
  the document loads intact — automate this as a test).
- Postgres mode: documented `pg_dump`/restore, tested against the Phase 18
  dev Postgres.
- Retention guidance (keep N daily) in `docs/deployment.md`.

### 19.4 — Graceful shutdown and config reference

- Verify (and fix if needed) SIGTERM behavior: in-flight ops persisted,
  every room snapshot-saved on shutdown, WebSockets closed with a proper
  close code so clients auto-reconnect instead of erroring. Add a test
  that shutdown persists an unsaved room. This is what makes
  `RollingUpdate` and VM reboots safe.
- `docs/configuration.md`: one table of **every** `CRDT_CAD_*` env var
  (there are many by now — security, batching, DB, Redis, ceilings),
  each with default, effect, and which deployment mode needs it. Link it
  from README; keep it exhaustive — grep the codebase for `CRDT_CAD_` and
  reconcile, so no undocumented knob remains.

### 19.5 — Observability and load

- Commit a Grafana dashboard JSON (`monitoring/grafana-dashboard.json`)
  built against the real `/metrics` names (connections, ops relayed,
  rejections, merge latency), plus an optional
  `docker-compose.monitoring.yml` (Prometheus + Grafana, pre-provisioned
  datasource/dashboard). Verify locally: generate traffic, screenshot the
  live dashboard into `docs/screenshots/`.
- Two example Prometheus alert rules (server down, abnormal rejection
  rate) in `monitoring/alerts.yml`.
- A load/soak script (`scripts/load_test.py`, asyncio WebSocket clients:
  N rooms × M clients drawing for T minutes). Run it against the local
  Docker deployment; record findings (max comfortable clients on this
  machine, memory growth flat or not) honestly in `docs/deployment.md`.
  If it finds a leak or a ceiling bug, fixing that is in scope for this
  phase.

### 19.6 — Published image

- Extend CI: on version tag, build and push a multi-tag image to GitHub
  Container Registry (`ghcr.io/<owner>/crdt-cad:latest` + version tag),
  and update Quickstart so `docker run ghcr.io/...` works without cloning.
  Update `docker-compose.prod.yml` and `k8s/deployment.yaml` to reference
  the published image (with the local-build path kept as a comment).

## Definition of done

- Phases 18 and 19 committed phase-by-phase (18.1–18.4, 19.1–19.6 may be
  grouped sensibly, but k8s work and prod-ops work stay in separate
  commits); full pytest + e2e suites green throughout.
- `kubectl apply -k k8s/` (Mode A) and the `k8s/dev` overlay (Mode B) both
  work on a fresh kind cluster by following only committed docs.
- The words "not validated against a live cluster" no longer appear in
  the repo — replaced by an accurate statement of what was verified where.
- `README.md` status table updated: Kubernetes row → validated-on-kind;
  new rows for TLS/prod compose, backups, monitoring, published image.
  The Roadmap keeps its honest list of what still isn't done (managed-
  cloud validation, Prometheus-Adapter HPA metric, live Fly deployment if
  it couldn't be performed).
- Part 3 (`UI_UX_PROMPT.md`) remains a separate track — note its status
  in the final report but do not fold its work into these commits.
