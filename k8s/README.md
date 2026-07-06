# Kubernetes manifests -- status: validated on a real cluster (kind)

Phase 18 stood up a real local Kubernetes cluster with **kind**
(Kubernetes-in-Docker) and validated every mode and manifest in this
directory against it end-to-end -- not just `kubectl apply --dry-run`.
What follows is an accurate account of what was verified and how; see
`DEPLOYMENT_PROMPT.md` for the brief this satisfies.

## What was verified

- **Mode A** (default: 1 replica, SQLite + PVC) -- `kind create cluster`,
  `docker build`, `kind load docker-image`, `kubectl apply -k k8s/`.
  Rollout completed and readiness/liveness probes passed with **no
  manifest fixes needed** on the first pass. Verified with real browser
  clients through `kubectl port-forward`: two tabs converging in one
  room, **Save** persisting, and -- the real test of a PVC -- deleting
  the pod outright and confirming the replacement pod rehydrates the
  exact same document from the volume, not an empty one.
- **Mode B** (the Phase 7 configuration: `CRDT_CAD_DATABASE_URL` +
  `CRDT_CAD_REDIS_URL`, `replicas: 3`, `RollingUpdate`, no PVC) -- via the
  `k8s/dev/` overlay (`kubectl apply -k k8s/dev/`). Verified:
  - Two WebSocket clients connected to **different, explicitly identified
    pods** (via direct pod `port-forward`s, confirmed with the
    `X-Served-By` response header -- see "Pod identity" below) see each
    other's edits live through Redis fan-out, bidirectionally.
  - A full `kubectl rollout restart` across all 3 replicas loses no
    persisted data (Postgres-backed) -- confirmed by drawing+saving,
    restarting every pod, and reconnecting through a brand-new pod that
    had never seen the room before.
  - The existing e2e convergence suite (`tests/e2e/test_collaboration_e2e.py`
    -- including the offline/Time-Travel Merge flow) passes **unmodified**
    against the live cluster entry point via
    `CRDT_CAD_E2E_LIVE_SERVER_URL` (see `tests/e2e/conftest.py`), not just
    against a local `uvicorn` process.
  - A protocol-level race test (`tests/test_postgres_store.py::
    test_concurrent_stores_against_fresh_database_all_initialize`) covers
    the one real bug this phase found: every replica's `PostgresStore`
    racing `CREATE TABLE IF NOT EXISTS` against the same brand-new
    database on cold start (a genuine Postgres catalog race, not a logic
    bug) -- fixed with a Postgres advisory lock around schema init (see
    `crdt_cad/persistence/store.py`).
- **HPA** -- `metrics-server` installed on kind (with the
  `--kubelet-insecure-tls` flag kind's self-signed kubelet certs need),
  `hpa.yaml` applied against the Mode B deployment, and real CPU load
  driven against `/api/solve` (the constraint solver -- genuine CPU work).
  Confirmed scaling 3 -> 6 replicas (`maxReplicas`) under load and back
  down to `minReplicas` once load stopped (subject to the default 5-minute
  scale-down stabilization window). The custom-metric path
  (`crdt_cad_active_connections` via a Prometheus Adapter, for scaling on
  actual WebSocket load rather than CPU) remains future work, not done
  here -- CPU-based scaling is what's verified.
- **Ingress** -- `ingress-nginx` installed on kind, `k8s/ingress.yaml`
  (promoted from the old `.example`) applied with a TLS secret, and a
  real browser confirmed `wss://` end-to-end through the TLS-terminating
  ingress (not `ws://` falling back) -- including drawing working over
  that connection. The `proxy-read-timeout`/`proxy-send-timeout: "3600"`
  annotations exist because nginx-ingress's default 60s idle timeout would
  otherwise silently drop long-lived idle WebSocket connections.
- **CI** -- `.github/workflows/ci.yml`'s `k8s-smoke` job spins up a kind
  cluster on every push/PR, applies Mode A, and runs
  `scripts/k8s_smoke_test.py` (a real WebSocket handshake + op + save +
  fresh-reconnect-sees-it round-trip) against it, so these manifests can't
  silently rot back to "structurally plausible but never actually tried."
  Mode B is **not** run in CI (heavier: needs the dev Postgres/Redis
  overlay too) -- it's validated manually per this document, not on every
  push.

## What wasn't verified (explicitly, so this stays honest)

- No managed-cloud cluster (EKS/GKE/AKS) -- only kind. The manifests are
  vanilla enough that this should translate directly, but "should" isn't
  "verified."
- No real Certificate Authority -- the ingress TLS test used a locally
  generated self-signed cert, not Let's Encrypt/cert-manager. For a real
  public deployment, either point `k8s/ingress.yaml`'s `secretName` at a
  cert-manager `Certificate` or use the Caddy-based path in
  `docs/deployment.md` instead, which does get you real automatic HTTPS.
- Postgres/Redis in `k8s/dev/` are single-node, no-persistence-guarantees,
  local-validation-only (see the comments in `k8s/dev/postgres.yaml` and
  `redis.yaml`) -- a production cluster brings its own managed services.
- The custom-metric HPA path (scale on WebSocket connections, not CPU) --
  documented as future work above, not implemented.

## Read this before scaling replicas > 1

By **default** (no extra env vars set), two pieces of state live in **one
process's memory/disk**, not in anything shared across pods:

1. **Room state itself.** `RoomManager`/`Room` in `crdt_cad.server.app`
   hold the live `DrawingDocument`/`MeshCRDT` objects and the set of
   connected WebSocket clients for each room, in-process. Two pods have
   two independent, unsynchronized copies of "which rooms exist and
   who's in them" *unless* Redis fan-out is configured (Mode B).
2. **The SQLite file.** `SQLiteStore` writes to one file. Two pods
   writing to the same file (even over a shared volume) is not safe --
   SQLite allows one writer at a time and isn't designed for
   multi-process access over network filesystems.

**So `deployment.yaml` defaults to `replicas: 1`, and that's the only
correct configuration until you switch to Mode B** (either by hand,
following the recipe below, or via the pre-built `k8s/dev/` overlay for
local validation). `hpa.yaml` is **not safe to apply against the default
Mode A configuration** -- scaling it as shipped would just partition your
users across islands that can't see each other.

## Scaling to replicas > 1 (Mode B)

Both prerequisites are real, tested, live-verified code (Phase 7, landed
against Kubernetes specifically in Phase 18.2 -- see above):

1. **`PostgresStore`** (`crdt_cad.persistence.store`) -- a Postgres-backed
   `DocumentStore` implementation (via `asyncpg`), selected by setting
   `CRDT_CAD_DATABASE_URL`. Replaces the per-pod SQLite file with one
   database every pod reads/writes, so room state survives independently
   of which pod happens to be handling it, and a pod restart doesn't
   lose anything another pod already persisted.
2. **Redis pub/sub fan-out** (`crdt_cad.server.pubsub`) -- selected by
   setting `CRDT_CAD_REDIS_URL`. An op applied on pod A gets published to
   `room:{kind}:{room_id}`; every other pod subscribed to that channel
   applies it to its own in-memory document *and* relays it to its own
   locally-connected clients.

### Try it locally: `k8s/dev/` (what Phase 18.2 actually used)

```
kubectl apply -k k8s/dev/
```

This composes the base manifests (`k8s/base/`) with a throwaway
single-node Postgres + Redis (`k8s/dev/postgres.yaml`, `redis.yaml` --
**local validation only, not production-grade**, see the comments in
those files) and patches the Deployment into the scaled configuration
(`k8s/dev/deployment-scale-patch.yaml`: `replicas: 3`, `RollingUpdate`,
`CRDT_CAD_DATABASE_URL`/`CRDT_CAD_REDIS_URL` set, `CRDT_CAD_DB_PATH` and
the PVC removed). The `crdt-cad-database` Secret
(`k8s/dev/secret.yaml`) is committed in plaintext deliberately -- it's
throwaway credentials for a throwaway dev Postgres, not a real secret.

`k8s/dev/kustomization.yaml` references `../base`, not `../` -- kustomize
refuses ("cycle detected") when an overlay's own directory is nested
inside the directory tree its base resources come from, which is exactly
`k8s/dev/` relative to `k8s/`. `k8s/kustomization.yaml` is a thin
passthrough to `k8s/base/` so `kubectl apply -k k8s/` (Mode A) keeps
working unchanged.

### For a real production cluster

1. Stand up (or point at) a real managed Postgres and Redis -- not
   included in these manifests, since a production cluster almost always
   has a preferred way to provision both that this project shouldn't
   assume.
2. Create a `Secret` named `crdt-cad-database` with a `url` key holding
   the real Postgres DSN, and know your real Redis URL.
3. Apply `k8s/base/` plus your own patch mirroring
   `k8s/dev/deployment-scale-patch.yaml` and
   `k8s/dev/pvc-delete-patch.yaml` (or just hand-edit a copy of
   `k8s/base/deployment.yaml`).
4. `hpa.yaml` is then safe to apply (`kubectl apply -f k8s/hpa.yaml`,
   still not part of the default `kustomize` set -- apply it explicitly).
5. `ingress.yaml` similarly (`kubectl apply -f k8s/ingress.yaml`) --
   change `host` to your real domain and `secretName` to a cert-manager
   `Certificate` or a Secret you created from a real certificate.

## Pod identity (`X-Served-By`)

Every response carries an `X-Served-By: <pod name>` header (and `/health`
echoes it as `served_by`) -- added in Phase 18.2 specifically so
multi-pod fan-out could be verified by reading which pod actually
answered a request, rather than trusting the Service's load-balancing
blindly. Harmless to leave in a production deployment; useful for
debugging "which replica is misbehaving" regardless.

## Files

- `base/deployment.yaml` -- the app container, `replicas: 1` by default,
  resource requests/limits, liveness/readiness probes against `/health`,
  and the commented-out env entries for scaling past 1.
- `base/service.yaml` -- ClusterIP Service on port 80 -> 8000.
- `base/pvc.yaml` -- 1Gi PersistentVolumeClaim for the SQLite file
  (`ReadWriteOnce` -- ties the volume to whichever single node the one
  pod lands on). Only relevant while running Mode A -- dropped entirely
  in Mode B (see `k8s/dev/pvc-delete-patch.yaml`).
- `kustomization.yaml` -- thin passthrough to `base/`, so
  `kubectl apply -k k8s/` applies deployment + service + PVC (Mode A).
- `dev/` -- the Mode B local-validation overlay: `postgres.yaml`,
  `redis.yaml`, `secret.yaml`, `deployment-scale-patch.yaml`,
  `pvc-delete-patch.yaml`, `kustomization.yaml`. See above.
- `hpa.yaml` -- HorizontalPodAutoscaler on CPU, **disabled by
  omission from `kustomization.yaml`'s default apply set** until Mode B
  is in place. Apply it manually (`kubectl apply -f k8s/hpa.yaml`).
  Verified scaling 1->6->1 against a real kind cluster under real CPU
  load (Phase 18.3).
- `ingress.yaml` -- nginx-ingress Ingress with TLS + WebSocket-friendly
  proxy timeouts, **not part of the default apply set** (not every
  target has nginx-ingress) -- apply manually
  (`kubectl apply -f k8s/ingress.yaml`) after changing `host` and
  `secretName`. Verified end-to-end on kind (Phase 18.3), including a
  real browser's `wss://` connection.
