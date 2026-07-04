"""Tests for crdt_cad.server.security and its wiring into app.py: room
tokens, CORS origin selection, rate limiting, and resource ceilings.

`isolated_store` and `isolated_rate_limiter` (autouse) fixtures live in
tests/conftest.py and apply here too.
"""

import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.document import DrawingDocument
from crdt_cad.server import app as app_module
from crdt_cad.server import security
from crdt_cad.server.app import app


def _client() -> TestClient:
    return TestClient(app)


def _one_layer_op() -> dict:
    """A single real, valid `layer` op -- cheap to mint repeatedly for
    rate-limit tests, as opposed to a hand-rolled fake payload (which, if
    malformed, would previously crash the server's message handler
    entirely instead of yielding a clean per-op rejection -- see
    test_malformed_op_is_rejected_cleanly_not_a_crashed_connection)."""
    doc = DrawingDocument(LamportClock(actor="ratelimit-actor"))
    _, ops = doc.add_layer("L")
    return ops[0].to_dict()


def _hello(ws, actor="a", token=None):
    msg = {"type": "hello", "actor": actor}
    if token is not None:
        msg["token"] = token
    ws.send_json(msg)


# -- security.py unit tests -----------------------------------------------------


def test_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    assert security.auth_enabled() is False
    assert security.verify_room_token(None, "mesh", "any-room") is True
    assert security.verify_room_token("garbage", "mesh", "any-room") is True


def test_token_round_trip_scoped_to_kind_and_room(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    token = security.mint_room_token("mesh", "roomA")
    assert security.verify_room_token(token, "mesh", "roomA") is True
    assert security.verify_room_token(token, "mesh", "roomB") is False
    assert security.verify_room_token(token, "drawing", "roomA") is False
    assert security.verify_room_token(None, "mesh", "roomA") is False
    assert security.verify_room_token("not-a-real-token", "mesh", "roomA") is False


def test_token_expires(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    monkeypatch.setenv("CRDT_CAD_TOKEN_MAX_AGE_SECONDS", "0")
    token = security.mint_room_token("mesh", "roomA")
    # itsdangerous timestamps have whole-second resolution, so max_age=0
    # only actually rejects once a full second has elapsed -- a few ms
    # isn't enough to distinguish "expired" from "same second, not yet".
    time.sleep(1.05)
    assert security.verify_room_token(token, "mesh", "roomA") is False


def test_secret_matches_is_constant_time_correct(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    assert security.secret_matches("s3cret") is True
    assert security.secret_matches("wrong") is False
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    assert security.secret_matches("anything") is False


def test_cors_origins_defaults(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    monkeypatch.delenv("CRDT_CAD_CORS_ORIGINS", raising=False)
    assert security.cors_origins() == ["*"]

    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    assert security.cors_origins() == []

    monkeypatch.setenv("CRDT_CAD_CORS_ORIGINS", "https://a.example, https://b.example")
    assert security.cors_origins() == ["https://a.example", "https://b.example"]


def test_token_bucket_refills_over_time():
    bucket = security.TokenBucket(rate=100.0, capacity=2.0)
    assert bucket.allow(2.0) is True
    assert bucket.allow(1.0) is False
    time.sleep(0.02)  # 100/sec * 0.02s = ~2 tokens refilled
    assert bucket.allow(1.0) is True


def test_per_key_rate_limiter_tracks_keys_independently():
    limiter = security.PerKeyRateLimiter(rate=0.0, capacity=1.0)
    assert limiter.allow("1.1.1.1") is True
    assert limiter.allow("1.1.1.1") is False  # exhausted, no refill (rate=0)
    assert limiter.allow("2.2.2.2") is True  # different key, untouched bucket


# -- no-secret default: today's behavior is unchanged ----------------------------


def test_no_secret_ws_connects_without_a_token(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    client = _client()
    with client.websocket_connect("/ws/openroom") as ws:
        _hello(ws)
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"


def test_no_secret_export_endpoint_works_without_a_token(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    client = _client()
    resp = client.get("/api/rooms/openroom2/export/json")
    assert resp.status_code == 200


def test_auth_required_endpoint_reflects_state(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    assert _client().get("/api/auth/required").json() == {"required": False}
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    assert _client().get("/api/auth/required").json() == {"required": True}


# -- auth enabled: tokens gate WS and REST access --------------------------------


def test_token_endpoint_mints_a_working_token(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    client = _client()
    resp = client.post("/api/auth/token", json={"secret": "s3cret", "kind": "mesh", "room_id": "r1"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert security.verify_room_token(token, "mesh", "r1") is True


def test_token_endpoint_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    resp = _client().post("/api/auth/token", json={"secret": "nope", "kind": "mesh", "room_id": "r1"})
    assert resp.status_code == 403


def test_token_endpoint_disabled_when_no_secret_configured(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    resp = _client().post("/api/auth/token", json={"secret": "anything", "kind": "mesh", "room_id": "r1"})
    assert resp.status_code == 400


def test_ws_hello_without_token_is_rejected_when_secret_configured(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    client = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/secureroom") as ws:
            _hello(ws)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


def test_ws_hello_with_valid_token_connects(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    token = security.mint_room_token("drawing", "secureroom2")
    client = _client()
    with client.websocket_connect("/ws/secureroom2") as ws:
        _hello(ws, token=token)
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"


def test_ws_token_scoped_to_room_rejects_a_different_room(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    token = security.mint_room_token("drawing", "roomA")
    client = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/roomB") as ws:
            _hello(ws, token=token)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


def test_ws_token_scoped_to_kind_rejects_the_other_kind(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    token = security.mint_room_token("mesh", "sharedname")
    client = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/sharedname") as ws:  # drawing endpoint, mesh token
            _hello(ws, token=token)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


def test_export_endpoint_requires_token_when_secret_configured(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    client = _client()
    resp = client.get("/api/rooms/secureroom3/export/json")
    assert resp.status_code == 401

    token = security.mint_room_token("drawing", "secureroom3")
    resp2 = client.get(f"/api/rooms/secureroom3/export/json?token={token}")
    assert resp2.status_code == 200


def test_export_endpoint_accepts_bearer_header_token(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    token = security.mint_room_token("mesh", "secureroom4")
    client = _client()
    resp = client.get(
        "/api/mesh/secureroom4/export/json",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_generate_endpoint_requires_token_when_secret_configured(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_SECRET", "s3cret")
    client = _client()
    resp = client.post("/api/mesh/secureroom5/generate", json={"prompt": "a house"})
    assert resp.status_code == 401


# -- resource ceilings ------------------------------------------------------------


def test_ws_rejects_oversized_message(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_MAX_WS_MESSAGE_BYTES", "100")
    client = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/bigmsgroom") as ws:
            _hello(ws)
            ws.receive_json()  # snapshot
            ws.send_json({"type": "ops", "ops": [{"pad": "x" * 500}]})
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_MESSAGE_TOO_LARGE


def test_ws_rejects_too_many_ops_in_one_message(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_MAX_OPS_PER_MESSAGE", "3")
    client = _client()
    with client.websocket_connect("/ws/manyopsroom") as ws:
        _hello(ws)
        ws.receive_json()  # snapshot
        ws.send_json({"type": "ops", "ops": [{"target": "layer", "payload": {}}] * 10})
        reply = ws.receive_json()
        assert reply["type"] == "rejected"
        assert "too many ops" in reply["reason"]


def test_ws_per_connection_rate_limit_trips(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_WS_OPS_PER_SECOND", "0")
    monkeypatch.setenv("CRDT_CAD_WS_OPS_BURST", "1")
    client = _client()
    with client.websocket_connect("/ws/ratelimitroom") as ws:
        _hello(ws)
        ws.receive_json()  # snapshot

        # first op consumes the entire burst budget (capacity=1) and is a
        # real, valid op -- it's accepted, but nothing is broadcast back to
        # the sender itself (broadcast excludes the sender), so there's no
        # reply to wait for here.
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})

        # second op arrives with zero refill (rate=0) -- must be rejected
        ws.send_json({"type": "ops", "ops": [_one_layer_op()]})
        second = ws.receive_json()
        assert second["type"] == "rejected"
        assert "rate limit" in second["reason"]


def test_malformed_op_is_rejected_cleanly_not_a_crashed_connection():
    """Regression test: a malformed op used to raise an uncaught exception
    inside the message handler, which silently ended the connection with
    no reply the client could react to (discovered while writing the rate
    limit test above, which originally used fake payloads for exactly this
    reason). Now it's rejected the same way a geometry-invalid op is."""
    client = _client()
    with client.websocket_connect("/ws/malformedroom") as ws:
        _hello(ws)
        ws.receive_json()  # snapshot
        ws.send_json({"type": "ops", "ops": [{"target": "layer", "payload": {}}]})  # missing "id"
        reply = ws.receive_json()
        assert reply["type"] == "rejected"
        assert "malformed op" in reply["reason"]

        # the connection must still be alive afterward
        ws.send_json({"type": "save"})
        saved = ws.receive_json()
        assert saved["type"] == "saved"


def test_ws_max_clients_per_room(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_MAX_CLIENTS_PER_ROOM", "1")
    client = _client()
    with client.websocket_connect("/ws/crowdedroom") as ws1:
        _hello(ws1, actor="a")
        ws1.receive_json()  # snapshot

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/crowdedroom") as ws2:
                ws2.receive_json()
        assert exc_info.value.code == app_module.WS_CLOSE_TOO_MANY_CLIENTS


def test_ws_max_rooms_per_server(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_MAX_ROOMS_PER_SERVER", "1")
    client = _client()
    with client.websocket_connect("/ws/onlyroom") as ws1:
        _hello(ws1)
        ws1.receive_json()  # snapshot

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/anotherroom") as ws2:
                ws2.receive_json()
        assert exc_info.value.code == app_module.WS_CLOSE_SERVER_AT_CAPACITY


def test_generate_endpoint_per_ip_rate_limit(monkeypatch):
    monkeypatch.setattr(
        security, "generate_rate_limiter", security.PerKeyRateLimiter(rate=0.0, capacity=1.0)
    )
    client = _client()
    ok = client.post("/api/mesh/genlimitroom/generate", json={"prompt": "a 1 bedroom house"})
    assert ok.status_code == 200
    limited = client.post("/api/mesh/genlimitroom2/generate", json={"prompt": "a 1 bedroom house"})
    assert limited.status_code == 429
