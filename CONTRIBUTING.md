# Contributing to crdt-cad

Thanks for considering a contribution. This is a from-scratch CRDT
implementation with real-time collaboration guarantees — correctness
and test coverage matter more here than in most projects, so please
read this before opening a PR.

## Getting set up

```bash
python -m venv .venv
./.venv/Scripts/pip install -e ".[dev]"      # Windows; use .venv/bin/pip on macOS/Linux
./.venv/Scripts/python -m pytest tests/ -v   # should be fully green before you start
./.venv/Scripts/python -m uvicorn crdt_cad.server.app:app --reload
```

No build step for the frontend — `demo/static/*.js` are plain scripts
served as-is. Edit and refresh.

## Before opening a PR

- `pytest tests/` must pass. Non-browser tests run by default; browser
  tests (`pytest -m e2e`) need Chromium (`playwright install chromium`)
  and are excluded from the default run so a fresh checkout still
  passes without them — please run them too if you touched any
  frontend code.
- `ruff check .` must be clean.
- If you add a new `CRDT_CAD_*` environment variable, document it in
  `docs/configuration.md` in the same commit — an audit of this repo
  found undocumented env vars costly to track down later.
- If you touch CRDT merge logic (`src/crdt_cad/crdt/`), add or extend
  a convergence test, ideally a Hypothesis property test alongside the
  existing ones in `tests/test_rga.py` — "it works in my manual test"
  is not sufficient evidence for a data structure whose entire point is
  correctness under arbitrary concurrent interleavings.
- If you touch the WebSocket protocol or the pointer/gesture handling
  in `sketch.js`/`mesh3d.js`, a live-Playwright check (open two tabs,
  drive real events) catches a different class of bug than unit tests
  do — several real bugs in this repo's history were only caught this
  way. See existing `tests/e2e/` files for the pattern.

## Commit style

One logical change per commit; the message explains *why*, not just
*what* (the diff already shows what changed). Look at recent commit
history for the house style.

## Reporting bugs

Open a GitHub issue with: what you did, what you expected, what
happened instead, and (for anything collaborative/merge-related) the
exact sequence of concurrent actions if you can reproduce it — CRDT
bugs are almost always about *ordering*, so the sequence matters more
than the end state.

## Reporting security issues

Do not open a public issue — see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree your contributions are licensed under this
project's [MIT license](LICENSE).
