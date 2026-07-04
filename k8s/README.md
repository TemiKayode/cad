# Kubernetes manifests -- status: written, **not validated against a live cluster**

No Kubernetes cluster was reachable in the environment these were authored
in (`kubectl` is installed but there's no API server to talk to, and
standing one up -- `kind`/`minikube` -- wasn't done without asking first).
These manifests are structurally checked (valid YAML, sane resource
shapes) but have not been `kubectl apply`'d anywhere. Treat them as a
solid starting point, not a verified deployment.

## Read this before scaling replicas > 1

By **default** (no extra env vars set), two pieces of state live in **one
process's memory/disk**, not in anything shared across pods:

1. **Room state itself.** `RoomManager`/`Room` in `crdt_cad.server.app`
   hold the live `DrawingDocument`/`MeshCRDT` objects and the set of
   connected WebSocket clients for each room, in-process. Two pods have
   two independent, unsynchronized copies of "which rooms exist and
   who's in them." A client connecting to pod A and another connecting
   to pod B for the *same room id* would silently end up in two
   different worlds -- no error, just missing collaborators and no
   error message explaining why.
2. **The SQLite file.** `SQLiteStore` writes to one file. Two pods
   writing to the same file (even over a shared volume) is not safe --
   SQLite allows one writer at a time and isn't designed for
   multi-process access over network filesystems.

**So `deployment.yaml` defaults to `replicas: 1`, and that's the only
correct configuration until you do the two things below.** `hpa.yaml` is
included because the brief asks for autoscaling manifests, but it is
**not safe to enable** in the default configuration -- scaling this
deployment as shipped would just partition your users across islands
that can't see each other, per point 1 above.

## Scaling to replicas > 1

Both prerequisites now exist and are real, tested, live-verified code
(Phase 7 -- see the README's "Horizontal scaling seam" section for the
full design and what live-verification actually covered):

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
   locally-connected clients. Without this, pod B's clients would never
   see pod A's edits even with Postgres in the picture.

To actually enable this on the cluster:

1. Stand up (or point at an existing) Postgres and Redis -- not included
   in these manifests, since a production cluster almost always already
   has a preferred way to provision both (a managed service, an operator,
   etc.) that this project shouldn't assume.
2. Create a `Secret` named `crdt-cad-database` with a `url` key holding
   the Postgres DSN (`postgresql://user:pass@host:5432/dbname`), and know
   your Redis URL (`redis://host:6379/0`).
3. In `deployment.yaml`, uncomment the `CRDT_CAD_DATABASE_URL` /
   `CRDT_CAD_REDIS_URL` env entries, remove `CRDT_CAD_DB_PATH` and the
   `data` volume/volumeMount (no longer needed -- state lives in Postgres
   now), delete `pvc.yaml` from `kustomization.yaml`, switch `strategy`
   from `Recreate` to `RollingUpdate` (safe now that two pods briefly
   running together isn't a SQLite-file conflict), and raise `replicas`.
4. `hpa.yaml` is then safe to apply too (`kubectl apply -f k8s/hpa.yaml`,
   still not part of the default `kustomize` set -- apply it explicitly).

**What was and wasn't live-verified:** the Postgres/Redis integration
itself was verified for real -- two separate `uvicorn` *processes*
(not just two `Room` objects in one test process) sharing one real
Postgres and one real Redis container, with a genuine WebSocket client
on each, confirming an edit on process A reaches process B's client via
Redis and durably persists to the shared Postgres store (see the
README's Testing section). What's **not** verified is any of this
actually running as Kubernetes pods -- the Secret/env wiring above is
structurally reasonable but untested against a live cluster, same
caveat as the rest of this directory.

## Files

- `deployment.yaml` -- the app container, `replicas: 1` by default,
  resource requests/limits, liveness/readiness probes against `/health`,
  and the commented-out env entries described above for scaling past 1.
- `service.yaml` -- ClusterIP Service on port 80 -> 8000.
- `pvc.yaml` -- 1Gi PersistentVolumeClaim for the SQLite file
  (`ReadWriteOnce` -- ties the volume to whichever single node the one
  pod lands on). Only relevant while running the default single-replica,
  SQLite-backed configuration -- drop it once Postgres is in the picture.
- `hpa.yaml` -- HorizontalPodAutoscaler on CPU, **disabled by
  omission from `kustomization.yaml`'s default apply set** until the
  prerequisites above are done. Apply it manually if you've done the
  Postgres/Redis work and know what you're doing.
- `kustomization.yaml` -- so `kubectl apply -k k8s/` applies
  deployment + service + PVC (not the HPA, per above).
