import pytest

from crdt_cad.persistence.store import InMemoryStore
from crdt_cad.server import app as app_module


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
