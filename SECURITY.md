# Security Policy

crdt-cad ships real authentication (magic-link and OAuth sign-in),
server-side sessions, per-room permissions, an admin panel, and
optional billing integration. Please report vulnerabilities privately
rather than through a public GitHub issue.

## Reporting a vulnerability

Email **Temitayokayode5@gmail.com** with:

- A description of the issue and its potential impact.
- Steps to reproduce (a minimal repro is very helpful).
- The affected version/commit, if known.

You should get an acknowledgement within 5 business days. Please don't
publicly disclose the issue until a fix has shipped, or 90 days have
passed with no response, whichever comes first.

## Supported versions

This project is pre-1.0 and moves quickly. Security fixes are only
guaranteed for the latest commit on `main` — there is no long-term
support branch at this stage.

## Scope

In scope: the FastAPI server (`src/crdt_cad/server/`), the CRDT/merge
layer (`src/crdt_cad/crdt/`), authentication and session handling
(`auth.py`), authorization/room-permission enforcement, and the
frontend JS as served by this repo. Out of scope: vulnerabilities in
third-party dependencies (report those upstream), and issues that
require an attacker to already have platform-admin credentials on a
deployment they don't own.

## What "self-hosted" means for security

Every deployment is independently operated — there is no shared
crdt-cad-hosted service. A vulnerability report about *your own*
deployment's configuration (e.g. a weak `CRDT_CAD_SECRET`, an open
CORS policy you configured yourself) is a configuration issue, not a
project vulnerability; see `docs/configuration.md` and
`docs/deployment.md` for the hardening options every deployment should
review before going to production.
