# Deployment

Three paths, roughly in order of effort: a single VPS with Docker
Compose + Caddy (this document), Fly.io (also this document, config
provided), or Kubernetes (`k8s/README.md`, validated on a real cluster
in Phase 18). Pick one -- none of this is required to run the project
locally (see the Quickstart in the main README).

## VPS (Hetzner / DigitalOcean / Lightsail / any Docker-capable box)

This is the `docker-compose.prod.yml` + Caddy stack, verified locally
end-to-end (Phase 19.1): real HTTPS, `wss://` through the proxy, the
room-token auth flow, and the rate limiter correctly reading the real
client IP instead of Caddy's own (see "Client IP behind a proxy" below).

1. **Provision a VM** with a public IP and ports 80/443 reachable
   (any $5/mo box works -- this app is CPU-light at rest). Point a DNS
   `A` record at it now if you have a domain; Caddy needs to be able to
   complete an HTTP-01 challenge against it to get a real certificate.
2. **Install Docker** (the box's OS package, or Docker's own install
   script -- either works; this project has no opinion beyond "Docker
   Compose v2 available").
3. **Clone this repo** onto the box.
4. **Set the required environment variables** in a `.env` file next to
   `docker-compose.prod.yml` (Compose reads `.env` automatically; never
   commit this file):

   ```
   CRDT_CAD_SECRET=<a long random string>
   CRDT_CAD_DOMAIN=your-real-domain.example
   ```

   **`CRDT_CAD_SECRET` is mandatory for any deployment reachable from the
   public internet.** Without it, room auth is off and anyone who
   guesses or is given a room URL has full editor access -- see the main
   README's "Security" section for exactly what this does and doesn't
   protect. `docker-compose.prod.yml` refuses to start without it
   (Compose's `${VAR:?message}` syntax), so this can't be silently
   forgotten.

5. **Start the stack:**

   ```
   docker compose -f docker-compose.prod.yml up -d
   ```

   Caddy obtains a real Let's Encrypt certificate for `CRDT_CAD_DOMAIN`
   automatically on first request (or a locally-trusted self-signed one
   if `CRDT_CAD_DOMAIN` is left at its `localhost` default -- useful for
   testing the stack itself before pointing a real domain at it).
6. **Done.** `https://your-domain/` serves the 2D sketch demo,
   `https://your-domain/3d` the mesh demo, both over a real `wss://`
   connection.

Persisted data (SQLite, or Postgres if you've wired that in -- see
`k8s/README.md`'s "Scaling to replicas > 1" for the same idea outside
Kubernetes) lives in the `crdt-cad-data` named volume, independent of
container recreation. See "Backups" below for taking it somewhere else
too.

### Client IP behind a proxy

Every request to this app now passes through Caddy, so
`request.client.host` (what the server would see without this) is
always *Caddy's own* address, not the real visitor's -- every visitor
would share one bucket in the `/generate` rate limiter, and one abusive
client could exhaust it for everyone. `docker-compose.prod.yml` sets
`CRDT_CAD_TRUST_PROXY_HEADERS=1`, which makes the server trust the last
hop of `X-Forwarded-For` (which Caddy sets to the real connecting peer,
not something a client can spoof past Caddy) instead. Only set this
when a reverse proxy is genuinely the sole way to reach the process --
see `crdt_cad.server.security.client_ip`'s docstring.

## Fly.io

The fastest path to a public HTTPS URL with no server to patch. See
`fly.toml` in the repo root.

```
fly launch --no-deploy   # first time only -- creates the app, keep the committed fly.toml
fly secrets set CRDT_CAD_SECRET=<a long random string>
fly volumes create crdt_cad_data --size 1 --region <your region>
fly deploy
```

**Honesty note:** `fly.toml` was checked for valid TOML syntax and
cross-referenced against Fly's documented v2 machine-config schema by
hand. `flyctl config validate` itself validates against the live Fly
platform API, which needs an authenticated account/token -- not
available in this environment, so this config has **not been
live-deployed or platform-validated**. Treat it as a strong starting
point, not a verified deployment, until someone with Fly credentials
runs:

```
fly config validate    # checks against the real platform -- needs `fly auth login` first
```

## Kubernetes

See `k8s/README.md` -- validated end-to-end against a real cluster
(kind) in Phase 18: single-replica (Mode A) and horizontally-scaled
Postgres+Redis (Mode B), HPA, and a TLS-terminating ingress with
`wss://` confirmed from a real browser.

## Backups

### SQLite mode (the default)

`scripts/backup_sqlite.py` uses SQLite's *online backup API*
(`sqlite3.Connection.backup()`) -- safe to run against a live writer,
unlike a plain file copy, which can capture a torn, inconsistent page
mix if the server happens to be mid-write. Verified with an automated
restore test (`tests/test_backup_sqlite.py`): back up a room, wipe the
database file entirely, restore, confirm the document loads back
byte-for-byte.

```
python scripts/backup_sqlite.py /path/to/crdt_cad.db /path/to/backups/ --keep 7
```

Writes a timestamped copy and (with `--keep N`) prunes older backups
beyond the newest N. Run this from cron, or as a Compose sidecar with
the same volume mounted read-only:

```yaml
  backup:
    image: python:3.12-slim
    volumes:
      - crdt-cad-data:/data:ro
      - ./backups:/backups
      - ./scripts:/scripts:ro
    entrypoint: ["sh", "-c", "while true; do python /scripts/backup_sqlite.py /data/crdt_cad.db /backups --keep 7; sleep 86400; done"]
```

**Restore:**

```
docker compose -f docker-compose.prod.yml stop crdt-cad
cp /path/to/backups/crdt_cad-<timestamp>.db /var/lib/docker/volumes/<...>/crdt_cad.db   # or wherever the volume is mounted
docker compose -f docker-compose.prod.yml start crdt-cad
```

### Postgres mode (Mode B / horizontal scaling)

Standard `pg_dump`/`pg_restore` -- verified against the Phase 18.2 dev
Postgres (byte-for-byte round trip: write a marker row, dump, delete the
row, restore, confirm the marker is back). `tests/test_backup_postgres.py`
automates the same check against any reachable Postgres with
`pg_dump`/`pg_restore` on `PATH` (the `postgresql-client` package).

```
pg_dump "$CRDT_CAD_DATABASE_URL" -Fc -f crdt_cad-$(date +%Y%m%dT%H%M%SZ).dump
```

**Restore** (`--clean --if-exists` drops and recreates existing objects,
so this is safe to run against a database that already has the old
schema/data in it):

```
pg_restore -d "$CRDT_CAD_DATABASE_URL" --clean --if-exists crdt_cad-<timestamp>.dump
```

### Retention

Keep daily backups for at least a week (`--keep 7` above), with a
monthly off-box copy (S3, Backblaze, another VM -- anything not on the
same disk as the live database) for real disaster recovery. Losing both
the live database *and* its only backup copy to the same disk failure
defeats the point of backing up at all.

## Monitoring (Prometheus + Grafana)

An optional, pre-provisioned monitoring stack (Phase 19.5) lives in
`docker-compose.monitoring.yml` + `monitoring/`:

```
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
```

- **Prometheus** (`http://localhost:9090`) scrapes the app's `/metrics`
  every 5s and loads the two example alert rules in
  `monitoring/alerts.yml` (server down; abnormal geometry-rejection
  ratio -- deliberately a starter set, not an exhaustive rulebook).
- **Grafana** (`http://localhost:3000`, login `admin`/`admin`) is
  provisioned with the datasource and the crdt-cad dashboard
  (`monitoring/grafana-dashboard.json`) automatically -- no manual
  import step. Panels: active connections, active rooms, ops relayed/s,
  new connections/s, geometry rejections, merge-apply latency.

This was verified live, not just written: the stack was brought up, the
Prometheus target confirmed `up` with both alert rules loaded, and the
dashboard confirmed rendering real traffic generated by the load test
below (screenshot: `docs/screenshots/grafana_dashboard.png`). For a real
deployment, keep ports 9090/3000 firewalled or behind auth -- neither
container ships with the app's token auth in front of it.

## Load testing

`scripts/load_test.py` drives N rooms x M real WebSocket protocol
participants (hello/snapshot handshake, genuine `DrawingDocument`-minted
ops) against any running deployment and reports throughput, fan-out
delivery, sender-to-receiver op latency, and server memory growth:

```
python scripts/load_test.py http://127.0.0.1:8000 \
    --rooms 10 --clients 5 --duration 120 --rate 5
```

**Findings on this machine** (Docker Desktop on Windows, single app
container, load generator sharing the same machine -- treat as relative
signal, not cloud benchmark numbers), recorded from the actual runs:

The first 50-client run found a real ceiling bug, since fixed: the
server persisted a full-document snapshot for **every accepted ops
message**, so per-message cost grew with document size until the event
loop drowned (52% fan-out delivery, 12s mean op latency, mass keepalive
disconnects at 50 clients x 5 ops/s). The fix is the debounced persist
(`CRDT_CAD_PERSIST_MIN_INTERVAL_SECONDS`, default `0.5`) -- at most one
durable persist per room per interval with a trailing flush, regression-
tested in `tests/test_persist_debounce.py`.

**Post-fix results**, 30s runs at increasing concurrency (100% fan-out
delivery and 0 server rejections at every level tested, up to 600
concurrent clients -- lossless throughout, degrading in latency, not in
correctness):

| Clients (rooms x per-room) | Mean op latency | p95 | Server RSS growth |
|---|---|---|---|
| 50 (10x5) | 12ms | 33ms | +15 MB |
| 100 (20x5) | 23ms | 71ms | +14 MB |
| 200 (40x5) | 196ms | 1.27s | +28 MB |
| 400 (80x5) | 294ms | 930ms | +45 MB |
| 600 (120x5) | 665ms | 1.63s | +64 MB |

**Comfortable ceiling on this machine: roughly 100-200 concurrent
clients** before latency visibly climbs past what feels live; beyond
that it degrades gracefully rather than failing outright (still
zero-loss at 600). A real deployment expecting more concurrent load
than that on sustained bursts is exactly what Mode B (Postgres + Redis,
`k8s/README.md`) exists for -- horizontal scaling across processes, not
squeezing more out of one.

**Memory growth**: running several short back-to-back load tests showed
RSS climbing roughly linearly (368 -> 403 MB across 4 rounds) -- but
each round's `run_id` mints brand-new rooms (`scripts/load_test.py`
never reuses room ids across invocations), and this codebase's rooms
never expire from memory once created (by design, capped only by
`CRDT_CAD_MAX_ROOMS_PER_SERVER`) -- so that growth is consistent with
"more rooms exist," not a per-request leak, and multiple short
invocations aren't a fair way to test for one. Isolating the actual
question -- does memory grow unboundedly from *continued editing of the
same rooms* -- with a single sustained 3-minute run against a **fixed**
set of 5 rooms / 50 clients (42,637 ops, no new rooms after the first
few seconds) still showed +27 MB growth. The likelier explanation is
mundane: `RGA<Point>` paths grow with every `append_point` call, so 3
minutes of continuous drawing at 5 ops/s/client legitimately grows each
room's *document* by thousands of points -- more real content correctly
costs more RAM, which isn't a leak. Distinguishing that conclusively
from a genuine leak would need profiling content size against RSS
directly (or a soak that edits without ever growing document size, e.g.
repeatedly moving one existing point) -- not done here, so treat "no
leak" as the likely read on this data, not a certainty.

## Configuration reference

See `docs/configuration.md` for every `CRDT_CAD_*` environment variable
this project reads, its default, and which deployment mode needs it.
