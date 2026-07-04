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
