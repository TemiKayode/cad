import pytest

from crdt_cad.persistence.store import InMemoryStore
from crdt_cad.server import app as app_module
from crdt_cad.server import security


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    """Every test gets a fresh in-memory store and empty rooms, so the
    test suite never touches the real data/crdt_cad.db file (now that
    every accepted ops batch triggers a background persist) and tests
    never leak room state into each other."""
    fresh_store = InMemoryStore()
    monkeypatch.setattr(app_module, "store", fresh_store)
    monkeypatch.setattr(app_module.drawing_room_manager, "store", fresh_store)
    monkeypatch.setattr(app_module.mesh_room_manager, "store", fresh_store)
    app_module.drawing_room_manager.rooms.clear()
    app_module.mesh_room_manager.rooms.clear()
    yield fresh_store


@pytest.fixture(autouse=True)
def no_meshy_api_key_by_default(monkeypatch):
    """Every test starts with MESHY_API_KEY unset, regardless of the
    real shell environment -- otherwise a developer's own environment
    variable would make generate_mesh_ops (and every test that calls it)
    silently start attempting real network calls to Meshy during a plain
    test run. Tests that specifically want it set (see
    tests/test_meshy_adapter.py) set it explicitly via monkeypatch,
    which layers fine on top of this."""
    monkeypatch.delenv("MESHY_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def isolated_account_store(monkeypatch):
    """Part 6 P1: every test gets a fresh in-memory accounts store and
    starts in the default tokens mode (accounts fully inert), so no test
    can touch a real accounts table or leak sessions/users into another.
    Tests that exercise accounts mode set CRDT_CAD_AUTH_MODE themselves
    via monkeypatch, which layers on top of this."""
    from crdt_cad.persistence.accounts import InMemoryAccountStore
    from crdt_cad.server import auth

    fresh = InMemoryAccountStore()
    monkeypatch.setattr(auth, "_account_store", fresh)
    monkeypatch.delenv("CRDT_CAD_AUTH_MODE", raising=False)
    monkeypatch.delenv("CRDT_CAD_AUTH_DEV_ECHO", raising=False)
    monkeypatch.delenv("CRDT_CAD_SMTP_HOST", raising=False)
    monkeypatch.delenv("CRDT_CAD_ADMIN_EMAILS", raising=False)
    yield fresh


@pytest.fixture(autouse=True)
def isolated_rate_limiter(monkeypatch):
    """The per-IP /generate rate limiter (crdt_cad.server.security.
    generate_rate_limiter) is a process-lifetime singleton by design in
    production -- but that means its per-IP token buckets would otherwise
    persist across every test in the same pytest process (all sharing the
    TestClient's fake IP), starving later tests of their own rate-limit
    budget. Give every test a fresh instance instead."""
    monkeypatch.setattr(
        security, "generate_rate_limiter",
        security.PerKeyRateLimiter(rate=security.generate_per_minute() / 60.0, capacity=security.generate_burst()),
    )
