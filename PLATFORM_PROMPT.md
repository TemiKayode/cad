# Platform brief: identity, teams, collaboration depth, and business readiness

## Context

This is **Part 6 of the improvement plan** for `crdt-cad`. Read `README.md`
first, then `docs/configuration.md` and `src/crdt_cad/server/security.py` —
the current auth model is the thing this brief replaces the *foundation*
of: a single shared secret (`CRDT_CAD_SECRET`) mints signed room tokens
with an editor/viewer role claim; display names live in `localStorage`;
there are **no users, no organizations, no per-person permissions, and no
SSO**. That model stays as the zero-config default (local/self-host
simplicity is a feature), but every monetization and worldwide-usage path
runs through real identity, so this brief builds it — opt-in, layered on
top, never breaking the tokens-only mode.

Every working rule from Part 1 (`IMPROVEMENT_PROMPT.md`) applies: no
unverifiable claims, unit tests + e2e + README + docs per phase, one
commit per phase, no Claude co-author trailer. Two platform-specific
rules:

1. **Zero-config mode is sacred.** `docker compose up` with no env vars
   must keep working exactly as today (anonymous, room-token-optional).
   Every feature here activates only when its env/config is present, and
   the full existing test suite must pass in both modes.
2. **Never store what you can't protect.** Passwords are not stored at
   all (magic links + OAuth only). Sessions are server-side. Anything
   personal gets a deletion path (P7) in the same phase it's introduced,
   not later.

## Phase P1 — User accounts

- `users` + `sessions` tables behind the existing `DocumentStore`-style
  interface pattern (SQLite + Postgres implementations, migration
  script for existing deployments).
- Sign-in: **email magic links** (SMTP config via env; console-echo
  fallback for dev so it's testable with zero infra) and **OAuth**
  (Google + GitHub via standard OIDC/OAuth2 — use `authlib`, not
  hand-rolled flows). No passwords, ever.
- Server-side session cookie (HttpOnly, SameSite, Secure behind TLS),
  session list + revoke ("sign out everywhere").
- Profile: display name + avatar color move from `localStorage` to the
  account (falling back to localStorage when signed out). Presence,
  comments, and version history attribute to the account when present.
- Activation: `CRDT_CAD_AUTH_MODE=accounts` (default remains `tokens`).
  Tests cover both modes end-to-end, including "signed-out user in
  accounts mode can still open a public room".

## Phase P2 — Document ownership and per-person permissions

- Rooms gain an owner (the creating account) and a visibility setting:
  **private** (owner + invitees), **link** (anyone with the link, role
  chosen per link — subsumes today's share links), **public**.
- Per-user grants: owner / editor / commenter / viewer, enforced
  server-side at the WS `hello` and every REST endpoint (extend the
  existing viewer-role enforcement — `tests/test_workspace.py` shows the
  pattern). "Commenter" is new: comments allowed, geometry ops rejected.
- The home page becomes account-aware: your documents, shared-with-you,
  public. Anonymous/token mode keeps today's flat room list.
- Migration honesty: pre-existing rooms have no owner — they stay
  ownerless-public until claimed by an admin tool, documented plainly.

## Phase P3 — Organizations and teams

- `orgs` + memberships (admin/member), org-owned documents, invite flow
  (email link + pending-invite state), transfer document to org.
- Org settings page: name, members list, role management, leave/remove.
- Per-org defaults: new-document visibility, allowed share-link roles.

## Phase P4 — SSO, quotas, and the admin panel

- **OIDC SSO** for orgs (works with Okta/Entra/Google Workspace):
  org-configured issuer/client, enforced domain capture optional. SAML
  is explicitly out of scope (document why: OIDC covers the realistic
  self-host audience; SAML without a real IdP to verify against would
  violate the no-unverifiable-claims rule — mark it roadmap).
- **Per-account quotas and rate limits**: generation credits/day,
  documents count, share links — configurable per deployment; the
  existing per-IP limits remain as the outer wall (fixes the
  school/office-NAT problem where one IP is many users).
- **Operator admin panel** (`/admin`, first admin bootstrapped via env):
  users/orgs/rooms listing, usage numbers (reuse Prometheus counters),
  disable user, claim ownerless rooms, delete room with confirmation.

## Phase P5 — Collaboration depth

All of this rides on identity from P1 (attribution) but must degrade to
anonymous actor names in tokens mode:

- **3D comments**: port the 2D `comments` component pattern to
  `MeshCRDT` (pinned to a vertex/face id), with the same LWW semantics
  and UI treatment.
- **@mentions** in comments (autocomplete from room participants /
  org members), producing a notification.
- **Notifications**: an in-app inbox (bell) fed by mentions, shares,
  and comment replies; email notification per user preference (reuses
  P1's SMTP plumbing). Store-and-forward — offline users see them on
  next visit.
- **Activity feed** per document: joined/edited/commented/generated/
  restored events, derived from data the server already has (presence,
  ops attribution, version checkpoints) — no new tracking of op
  content.
- Explicitly out of scope: real-time chat (comments + mentions cover
  the collaboration need; chat is a different product surface — record
  in Roadmap, don't half-build).

## Phase P6 — Billing readiness (Stripe, honestly scoped)

- Plan model: `free` / `pro` / `team` as *deployment-configurable
  quota bundles* (P4's quotas keyed off plan). Self-hosters ignore all
  of it.
- Stripe integration behind `STRIPE_API_KEY`: checkout for
  plan upgrade, customer portal for management, webhook handler
  (signature-verified) updating the account's plan. Test-mode
  end-to-end with Stripe's test cards + CLI webhook forwarding is the
  verification bar — **live-mode is explicitly not claimable** without
  a real account; say so in README.
- AI generation credits as the first metered feature: plan grants
  N/month, admin-adjustable, decremented where the generate endpoint
  already rate-limits.

## Phase P7 — Trust, legal, and lifecycle

- **Data lifecycle**: account deletion (self-serve: reassign-or-delete
  owned rooms, purge personal data, tombstone the actor id — document
  the CRDT implication: ops persist, attribution is anonymized) and
  full personal-data export (JSON) — the GDPR pair.
- **Abuse handling for shared rooms**: a report action on
  public/link-shared rooms, admin review queue in P4's panel,
  room-disable switch. Worldwide public sharing without this is
  negligent.
- **Policy scaffolding**: `PRIVACY.md` + `TERMS.md` templates with
  deployment-fill-in markers (operator name, jurisdiction) — clearly
  labeled as templates for the self-hoster to adapt with counsel, not
  legal advice from this repo.
- Transactional email hygiene: all P1/P5 emails get unsubscribe/
  preference handling, rate caps, and a dev-mode console transport.

## Definition of done

Both auth modes fully tested (unit + e2e) on every phase; zero-config
mode byte-for-byte compatible with today's behavior; README gains an
"Accounts & organizations" section with the same honesty discipline
(what's verified, what's config-provided-only — Stripe live-mode, SAML);
`docs/configuration.md` updated for every new env var; one commit per
phase, no Claude co-author trailer.
