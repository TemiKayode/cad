# Kubernetes manifests -- status: written, **not validated against a live cluster**

No Kubernetes cluster was reachable in the environment these were authored
in (`kubectl` is installed but there's no API server to talk to, and
standing one up -- `kind`/`minikube` -- wasn't done without asking first).
These manifests are structurally checked (valid YAML, sane resource
shapes) but have not been `kubectl apply`'d anywhere. Treat them as a
solid starting point, not a verified deployment.

## Read this before scaling replicas > 1

Two pieces of state currently live **in one process's memory**, not in
anything shared across pods:

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

**So: `deployment.yaml` defaults to `replicas: 1`, and that's the only
currently-correct configuration.** `hpa.yaml` is included because the
brief asks for autoscaling manifests, but it is **not safe to enable
yet** -- scaling this deployment today would just partition your users
across islands that can't see each other, per point 1 above.

To actually make this horizontally scalable (which is exactly what the
brief's "PostgreSQL (JSONB) or an append-only event log" persistence
option, and a pub/sub broker for room state, are for):

1. Swap `SQLiteStore` for a `DocumentStore` implementation backed by
   Postgres (or keep SQLite only if you pin `replicas: 1` forever).
2. Add a shared pub/sub layer (Redis pub/sub, or the Kafka event log
   the brief mentions) that every pod subscribes to per room, so an op
   applied on pod A gets broadcast to clients connected to pod B too.
   `Room.broadcast()` in `app.py` is the one place that would need to
   publish to that layer instead of (or in addition to) iterating
   `self.clients` directly.

Until then, run one replica behind the Service and it works exactly
like the local/Docker Compose setup, just on a cluster.

## Files

- `deployment.yaml` -- the app container, `replicas: 1`, resource
  requests/limits, liveness/readiness probes against `/health`.
- `service.yaml` -- ClusterIP Service on port 80 -> 8000.
- `pvc.yaml` -- 1Gi PersistentVolumeClaim for the SQLite file
  (`ReadWriteOnce` -- ties the volume to whichever single node the one
  pod lands on, consistent with the single-replica constraint above).
- `hpa.yaml` -- HorizontalPodAutoscaler on CPU, **disabled by
  omission from `kustomization.yaml`'s default apply set** until the
  prerequisites above are done. Apply it manually if you've done the
  Postgres/broker work and know what you're doing.
- `kustomization.yaml` -- so `kubectl apply -k k8s/` applies
  deployment + service + PVC (not the HPA, per above).
