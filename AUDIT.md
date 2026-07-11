# Deep-dive audit — 2026-07-11

A full-repo health check: what was actually verified today, every defect
found, everything missing or omitted, and a prioritized path to an
excellent state. Same honesty rules as the README: only claims that were
actually tested are stated as verified.

## Verified working today (checked, not assumed)

| Check | Result |
|---|---|
| Full unit suite on the current (mid-C8) working tree | **863 passed, 3 skipped**, ~65s |
| `ruff check .` | Clean |
| `TODO`/`FIXME`/`HACK` markers in `src/` | **Zero** — rare and genuinely excellent |
| Admin surface (`/api/admin/*`, Part 6 P4) | Every route enforces `require_platform_admin()` via `Depends` — properly gated |
| Env-var documentation coverage (`CRDT_CAD_*` in code vs `docs/configuration.md`) | Complete except one (E4 below) |
| Plan execution | Parts 1–6 fully committed (phases 1–19, D1–D8, G1–G7, P1–P7); Part 7 C1–C7 committed; **C8 in flight** in the working tree (perf benchmarks + budgets) |
| TLS from this machine to fly.io / github.com | Fine — isolates E1 below to the app, not the network |

## Errors found (ranked by impact)

### E1 — The "live" deployment is DOWN, and nothing noticed
`https://cad-hxpczw.fly.dev` fails TLS handshake from two independent
clients. Root cause found via `fly status`: **"trial has ended, please
add a credit card"** — Fly suspended the machines. Two problems in one:

1. The deployment documented as "live, verified" (commit `50491e3`) is
   currently unreachable — the claim is stale until a card is added at
   https://fly.io/trial (or the claim is softened to "was deployed and
   verified on 2026-07-11; hosting lapsed with the trial").
2. **There is no uptime monitoring.** The site died silently; only this
   audit noticed. A free external check (healthchecks.io, UptimeRobot)
   against `/health` with an e-mail alert is a 10-minute fix and should
   exist before any URL is ever shared publicly.

### E2 — GitHub Actions has been billing-locked since v0.1.0
Every CI run fails in seconds with "account locked due to a billing
issue" (confirmed again today on the C7 push — the jobs never start).
Cascade of consequences, all currently true:

- **Every Part 6 and Part 7 commit has landed without CI validation**
  (local suites were run, but the shared safety net is down).
- The **GHCR image has never been published** — the `v0.1.0` release
  workflow has never succeeded; `ghcr.io/temikayode/crdt-cad` does not
  exist despite the README Quickstart describing it.
- The **Fly-deploy workflow is perpetually `skipped`** (it gates on CI
  passing, which never happens).

Fix is on github.com → Settings → Billing (payment info / support
ticket — the account owes $0, so this is a stale lock; see the earlier
walkthrough). Then: `gh run rerun` the release run and watch CI go
green on the next push. **This is the single highest-leverage 15
minutes available.**

### E3 — Docker image runs as root and ships a compiler
The `Dockerfile` has no `USER` directive (container processes run as
root — unnecessary blast radius if the app is ever compromised) and
`build-essential` stays in the final image (bigger image, bigger attack
surface). Fix: multi-stage build (builder stage compiles wheels; final
stage copies site-packages), add a non-root `USER app` with ownership
of `/data`, and add a container-level `HEALTHCHECK` (compose has one;
the bare image doesn't).

### E4 — One undocumented env var
`CRDT_CAD_MESHY_FACE_BUDGET` (G7's imported-mesh face budget) is read
in code but absent from `docs/configuration.md`, which claims to be
exhaustive. One-row fix.

*No further code-level defects were found: the suite is green on a
mid-phase tree, no dead markers, the newest security surface is
properly gated, and doc/code drift is minimal. The codebase itself is
in unusually good shape — today's real risks are operational (E1, E2),
not code.*

## Missing / omitted (never existed anywhere in the plan)

### Repository & community health
- **`SECURITY.md`** — no vulnerability-reporting policy. For a project
  that ships auth, sessions, and an admin panel, this is the most
  important missing file. (GitHub renders it in the Security tab.)
- **`CONTRIBUTING.md`**, **`CODE_OF_CONDUCT.md`**, issue templates, PR
  template — table stakes for "used worldwide" open source.
- **`CHANGELOG.md` / GitHub Releases** — v0.1.0 exists as a bare tag
  with no release notes; 60+ meaningful commits have no user-facing
  history.

### Supply chain & reproducibility
- **No dependency lockfile** — `pyproject.toml` has ranges only, so no
  two installs are guaranteed identical (a `numpy` or `fastapi` minor
  release can break a fresh clone while CI was green). Adopt `uv lock`
  (or pip-tools) and install from the lock in CI/Docker.
- **No Dependabot/Renovate** — nothing watches for vulnerable
  dependencies.
- **No image vulnerability scanning** (a `trivy` job is ~10 lines of
  CI) and no SBOM/provenance on the published image.

### Quality gates
- **No type checking** — ruff lints style but nothing checks types;
  `mypy` (or pyright) in gradual mode would catch a real class of bug
  the suite can't. Start permissive, ratchet per-module.
- **No performance regression gate** — C8 is producing benchmarks
  (`docs/perf_benchmarks.md`, `scripts/bench_*.py`, in flight); once
  numbers exist, a CI job asserting "large-doc benchmark within X% of
  recorded baseline" keeps them true.
- **Load test not in CI** — `scripts/load_test.py` is run manually;
  a small-scale smoke variant could run per-push.

### Operations
- **No uptime monitoring or alerting** (see E1 — this already bit).
- **Backups are scripts, not schedules** — `backup_sqlite.py` and the
  documented `pg_dump` flow exist and are restore-tested, but nothing
  verifies they actually *run* anywhere on a schedule.
- **No error tracking** (Sentry/GlitchTip) — server exceptions land in
  container logs nobody tails.

### Product (known, deliberate descopes — recorded so they're not lost
now that the planning briefs were deleted from the repo)
- i18n/localization; deep canvas screen-reader accessibility; real-time
  chat (descoped in favor of comments/mentions); DWG import (assessed
  as impractical — DXF is the supported answer); SAML (OIDC only);
  live-mode Stripe and live OAuth provider verification (test/config
  verified only); marketing site & product analytics; C8 large-document
  work (in flight now).

## Enhancement suggestions (beyond fixing the above)

1. **Release hygiene**: publish `v0.1.x` GitHub Releases with notes
   generated per phase; the commit history is excellent raw material.
2. **README front door**: add one animated GIF (two cursors editing,
   offline merge) — screenshots undersell a real-time tool; link the
   auto-generated API docs (`/docs`) which FastAPI already serves.
3. **Demo playground**: a seeded public room (reset hourly) linked from
   the README so visitors experience collaboration without a second
   device — the growth loop for stars and job-search visibility alike.
4. **Type the wire protocol**: the WS message shapes are documented in
   prose; a `TypedDict`/pydantic model per message type plus one
   validation path would harden the protocol against drift as P-phase
   features multiply message kinds.
5. **Structured logging**: swap `logging.basicConfig` for JSON logs
   behind an env flag so hosted deployments can ship logs anywhere.
6. **Account-keyed rate limits everywhere**: P4 added quotas; the older
   per-IP limiter on `/generate` predates accounts and could prefer the
   account identity when present (NAT'd schools share IPs).
7. **Session housekeeping**: expired-session rows are reaped lazily on
   read; a periodic sweep (like the version-checkpoint loop) keeps the
   table bounded on busy deployments.

## Path to excellent — priority order

| # | Action | Effort | Unblocks |
|---|---|---|---|
| 1 | Clear GitHub billing lock; rerun release; confirm CI green | 15 min (user-only) | CI safety net, GHCR image, deploy workflow |
| 2 | Add card to Fly (or update the claim honestly); add uptime check on `/health` | 20 min | The public URL and knowing when it breaks |
| 3 | Land C8 (in flight) → **all seven parts complete** | already moving | Plan completion |
| 4 | `SECURITY.md` + `CONTRIBUTING.md` + issue/PR templates + v0.1.0 release notes | 1–2 h | Community readiness |
| 5 | Dockerfile hardening (multi-stage, non-root, HEALTHCHECK) | 1–2 h | E3 |
| 6 | Lockfile (`uv lock`) + Dependabot + trivy job | 2–3 h | Supply chain |
| 7 | mypy in CI (gradual) | 2–4 h | Type-level bug class |
| 8 | Document `CRDT_CAD_MESHY_FACE_BUDGET`; wire perf baseline gate once C8 lands | 30 min | E4, perf trust |
| 9 | Demo playground + README GIF + GitHub Release cadence | half day | Growth |

**Bottom line**: the code is in excellent shape — 863 green tests on a
work-in-progress tree, zero debt markers, gated admin surface, honest
docs. What stands between this and an excellent *product* is almost
entirely operational: two billing walls only the account owner can
clear, the community/supply-chain scaffolding above, and making the
live URL stay alive — visibly.
