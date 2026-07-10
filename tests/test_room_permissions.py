"""Part 6, Phase P2: document ownership and per-person permissions --
rooms gain an owner (the creating account) and a visibility setting
(private/link/public), plus per-user role grants (owner/editor/
commenter/viewer) layered on top of the pre-existing token-role system
(Phase 17) without changing its behavior when accounts mode is off.

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store, so nothing here can leak into another
test or touch a real database file.
"""

import pytest
from fastapi.testclient import TestClient

from crdt_cad.persistence.accounts import InMemoryAccountStore
from crdt_cad.server import app as app_module
from crdt_cad.server import auth, security
from crdt_cad.server.app import app


def _client() -> TestClient:
    return TestClient(app)


def _enable_accounts(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_AUTH_MODE", "accounts")
    monkeypatch.setenv("CRDT_CAD_SECRET", "test-deployment-secret")
    monkeypatch.setenv("CRDT_CAD_AUTH_DEV_ECHO", "1")


def _sign_in(client: TestClient, email: str) -> str:
    resp = client.post("/api/auth/request-link", json={"email": email})
    assert resp.status_code == 200, resp.text
    link = resp.json()["dev_link"]
    verify = client.get(link, follow_redirects=False)
    assert verify.status_code == 303
    return verify.cookies[auth.SESSION_COOKIE]


def _hello(ws, actor="a", token=None):
    msg = {"type": "hello", "actor": actor}
    if token is not None:
        msg["token"] = token
    ws.send_json(msg)


# -- storage layer (accounts.py) -----------------------------------------------


def test_claim_room_is_first_claim_wins():
    store = InMemoryAccountStore()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    assert store.claim_room("mesh", "r1", alice["user_id"]) is True
    assert store.claim_room("mesh", "r1", bob["user_id"]) is False
    assert store.get_room_ownership("mesh", "r1")["owner_user_id"] == alice["user_id"]


def test_claim_room_defaults_to_private():
    store = InMemoryAccountStore()
    alice = store.create_or_get_user("alice@example.com")
    store.claim_room("mesh", "r1", alice["user_id"])
    assert store.get_room_ownership("mesh", "r1")["visibility"] == "private"


def test_unowned_room_has_no_ownership_row():
    store = InMemoryAccountStore()
    assert store.get_room_ownership("mesh", "never-claimed") is None


def test_set_visibility_no_op_on_unowned_room():
    store = InMemoryAccountStore()
    assert store.set_room_visibility("mesh", "never-claimed", "public") is False


def test_grant_and_revoke_round_trip():
    store = InMemoryAccountStore()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    store.claim_room("mesh", "r1", alice["user_id"])
    store.set_room_grant("mesh", "r1", bob["user_id"], "editor")
    assert store.get_room_grant("mesh", "r1", bob["user_id"]) == "editor"
    grants = store.list_room_grants("mesh", "r1")
    assert len(grants) == 1 and grants[0]["email"] == "bob@example.com" and grants[0]["role"] == "editor"
    store.revoke_room_grant("mesh", "r1", bob["user_id"])
    assert store.get_room_grant("mesh", "r1", bob["user_id"]) is None


def test_grant_upsert_overwrites_prior_role():
    store = InMemoryAccountStore()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    store.claim_room("mesh", "r1", alice["user_id"])
    store.set_room_grant("mesh", "r1", bob["user_id"], "viewer")
    store.set_room_grant("mesh", "r1", bob["user_id"], "commenter")
    assert store.get_room_grant("mesh", "r1", bob["user_id"]) == "commenter"


def test_list_owned_and_granted_rooms():
    store = InMemoryAccountStore()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    store.claim_room("mesh", "r1", alice["user_id"])
    store.claim_room("drawing", "r2", alice["user_id"])
    store.set_room_grant("mesh", "r1", bob["user_id"], "viewer")
    owned = store.list_owned_rooms(alice["user_id"])
    assert {(r["kind"], r["room_id"]) for r in owned} == {("mesh", "r1"), ("drawing", "r2")}
    granted = store.list_granted_rooms(bob["user_id"])
    assert granted == [{"kind": "mesh", "room_id": "r1", "role": "viewer"}]


# -- role composition (app.py's _effective_role / _better_role) ----------------


def test_better_role_picks_higher_rank():
    assert app_module._better_role("viewer", "editor") == "editor"
    assert app_module._better_role("owner", "editor") == "owner"
    assert app_module._better_role(None, "viewer") == "viewer"
    assert app_module._better_role(None, None) is None


def test_account_role_none_when_accounts_disabled(monkeypatch):
    monkeypatch.delenv("CRDT_CAD_AUTH_MODE", raising=False)
    assert app_module._account_role_for_room("mesh", "anyroom", None) is None


def test_account_role_none_for_unowned_room(monkeypatch):
    _enable_accounts(monkeypatch)
    assert app_module._account_role_for_room("mesh", "never-claimed", None) is None


def test_account_role_public_room_open_to_anonymous(monkeypatch):
    _enable_accounts(monkeypatch)
    store = auth.get_account_store()
    alice = store.create_or_get_user("alice@example.com")
    store.claim_room("mesh", "pubroom", alice["user_id"], visibility="public")
    assert app_module._account_role_for_room("mesh", "pubroom", None) == "editor"


def test_account_role_private_room_denies_anonymous_and_stranger(monkeypatch):
    _enable_accounts(monkeypatch)
    store = auth.get_account_store()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    store.claim_room("mesh", "privroom", alice["user_id"], visibility="private")
    assert app_module._account_role_for_room("mesh", "privroom", None) is None
    assert app_module._account_role_for_room("mesh", "privroom", bob) is None
    assert app_module._account_role_for_room("mesh", "privroom", alice) == "owner"


def test_account_role_grant_applies_to_grantee_only(monkeypatch):
    _enable_accounts(monkeypatch)
    store = auth.get_account_store()
    alice = store.create_or_get_user("alice@example.com")
    bob = store.create_or_get_user("bob@example.com")
    carol = store.create_or_get_user("carol@example.com")
    store.claim_room("mesh", "privroom", alice["user_id"], visibility="private")
    store.set_room_grant("mesh", "privroom", bob["user_id"], "commenter")
    assert app_module._account_role_for_room("mesh", "privroom", bob) == "commenter"
    assert app_module._account_role_for_room("mesh", "privroom", carol) is None


# -- claim-on-first-touch (WS hello) --------------------------------------------


def test_fresh_room_opened_by_signed_in_user_is_claimed(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    with client.websocket_connect("/ws/mesh/freshroom1") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "owner"
    me = client.get("/api/auth/me").json()
    ownership = auth.get_account_store().get_room_ownership("mesh", "freshroom1")
    assert ownership is not None
    assert ownership["owner_user_id"] == me["user"]["user_id"]
    assert ownership["visibility"] == "private"


def test_fresh_room_opened_anonymously_stays_unowned(monkeypatch):
    _enable_accounts(monkeypatch)
    # _enable_accounts sets CRDT_CAD_SECRET, so -- unrelated to P2 --
    # every room already requires a valid token to open at all (that's
    # pre-existing Phase 17 behavior); a normal anonymous join-link
    # visitor carries one, so mint the same editor token that
    # represents.
    token = security.mint_room_token("mesh", "freshroom2")
    client = _client()
    with client.websocket_connect("/ws/mesh/freshroom2") as ws:
        _hello(ws, token=token)
        snap = ws.receive_json()
        assert snap["role"] == "editor"
    assert auth.get_account_store().get_room_ownership("mesh", "freshroom2") is None


def test_preexisting_room_is_never_retroactively_claimed(monkeypatch):
    """A room that already has persisted content (created before accounts
    mode existed, or simply opened anonymously first) must stay
    ownerless even when a signed-in user opens it later -- only a
    genuinely brand-new room gets claimed."""
    client = _client()
    with client.websocket_connect("/ws/mesh/oldroom1") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()

    _enable_accounts(monkeypatch)
    app_module.mesh_room_manager.rooms.clear()  # simulate a server restart: room reloads from the store
    _sign_in(client, "alice@example.com")
    token = security.mint_room_token("mesh", "oldroom1")  # see freshroom2's comment: CRDT_CAD_SECRET now gates every room
    with client.websocket_connect("/ws/mesh/oldroom1") as ws:
        _hello(ws, token=token)
        snap = ws.receive_json()
        assert snap["role"] == "editor"  # not "owner" -- claim-on-first-touch did not fire
    assert auth.get_account_store().get_room_ownership("mesh", "oldroom1") is None


# -- visibility enforcement (WS) -------------------------------------------------


def test_private_room_ws_connect_refused_for_anonymous(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/privroomws1") as ws:
        _hello(ws)
        ws.receive_json()

    from fastapi import WebSocketDisconnect

    stranger = _client()
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with stranger.websocket_connect("/ws/mesh/privroomws1") as ws:
            _hello(ws)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


def test_private_room_ws_connect_allowed_for_grantee(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/privroomws2") as ws:
        _hello(ws)
        ws.receive_json()

    bob = auth.get_account_store().create_or_get_user("bob@example.com")
    auth.get_account_store().set_room_grant("mesh", "privroomws2", bob["user_id"], "editor")

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    with bob_client.websocket_connect("/ws/mesh/privroomws2") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "editor"


def test_commenter_role_ops_rejected_same_as_viewer(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/commroomws1") as ws:
        _hello(ws)
        ws.receive_json()

    bob = auth.get_account_store().create_or_get_user("bob@example.com")
    auth.get_account_store().set_room_grant("mesh", "commroomws1", bob["user_id"], "commenter")

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    with bob_client.websocket_connect("/ws/mesh/commroomws1") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "commenter"
        ws.send_json({"type": "ops", "ops": [], "from": "a"})
        reply = ws.receive_json()
        assert reply["type"] == "rejected"


def test_share_link_token_still_works_on_a_private_room(monkeypatch):
    """Never-breaking-tokens-mode guarantee: a share-link token minted
    before (or regardless of) any account-based visibility change must
    keep granting access -- the two systems compose, they don't replace
    each other."""
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/privroomws3") as ws:
        _hello(ws)
        ws.receive_json()

    viewer_token = security.mint_room_token("mesh", "privroomws3", role="viewer")
    stranger = _client()
    with stranger.websocket_connect("/ws/mesh/privroomws3") as ws:
        _hello(ws, token=viewer_token)
        snap = ws.receive_json()
        assert snap["role"] == "viewer"


# -- ownership-management REST endpoints -----------------------------------------


def test_sharing_endpoint_requires_accounts_mode():
    resp = _client().get("/api/mesh/anyroom/sharing")
    assert resp.status_code == 404


def test_sharing_endpoint_404_for_unowned_room(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    resp = client.get("/api/mesh/never-claimed/sharing")
    assert resp.status_code == 404


def test_only_owner_can_view_or_change_sharing(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/shareroomrest1") as ws:
        _hello(ws)
        ws.receive_json()

    ok = owner_client.get("/api/mesh/shareroomrest1/sharing")
    assert ok.status_code == 200
    assert ok.json()["visibility"] == "private"

    stranger = _client()
    _sign_in(stranger, "bob@example.com")
    denied = stranger.get("/api/mesh/shareroomrest1/sharing")
    assert denied.status_code == 403

    change = owner_client.post("/api/mesh/shareroomrest1/visibility", json={"visibility": "public"})
    assert change.status_code == 200
    assert auth.get_account_store().get_room_ownership("mesh", "shareroomrest1")["visibility"] == "public"

    change_denied = stranger.post("/api/mesh/shareroomrest1/visibility", json={"visibility": "private"})
    assert change_denied.status_code == 403


def test_visibility_rejects_invalid_value(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/shareroomrest2") as ws:
        _hello(ws)
        ws.receive_json()
    resp = owner_client.post("/api/mesh/shareroomrest2/visibility", json={"visibility": "nonsense"})
    assert resp.status_code == 400


def test_owner_can_grant_and_revoke_by_email(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/grantroomrest1") as ws:
        _hello(ws)
        ws.receive_json()

    grant = owner_client.post(
        "/api/mesh/grantroomrest1/grant", json={"email": "bob@example.com", "role": "editor"}
    )
    assert grant.status_code == 200
    bob_id = grant.json()["user_id"]
    assert auth.get_account_store().get_room_grant("mesh", "grantroomrest1", bob_id) == "editor"

    revoke = owner_client.delete(f"/api/mesh/grantroomrest1/grant/{bob_id}")
    assert revoke.status_code == 200
    assert auth.get_account_store().get_room_grant("mesh", "grantroomrest1", bob_id) is None


def test_grant_rejects_invalid_role(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/grantroomrest2") as ws:
        _hello(ws)
        ws.receive_json()
    resp = owner_client.post("/api/mesh/grantroomrest2/grant", json={"email": "bob@example.com", "role": "owner"})
    assert resp.status_code == 400


# -- workspace room listing -------------------------------------------------------


def test_unowned_rooms_always_listed_regardless_of_accounts_mode(monkeypatch):
    """Byte-for-byte zero-config guarantee: a room nobody has ever
    claimed is listed for everyone, exactly like before this phase."""
    client = _client()
    with client.websocket_connect("/ws/mesh/listedroom1") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()

    _enable_accounts(monkeypatch)
    stranger = _client()
    rooms = stranger.get("/api/workspace/rooms").json()
    assert any(r["room_id"] == "listedroom1" and r["visibility"] is None for r in rooms)


def test_private_room_hidden_from_non_owner_non_grantee_in_listing(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/listedroom2") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})  # a room only appears in list_rooms_detailed once persisted
        ws.receive_json()

    owned = [r for r in owner_client.get("/api/workspace/rooms").json() if r["room_id"] == "listedroom2"]
    assert len(owned) == 1 and owned[0]["visibility"] == "private" and owned[0]["your_role"] == "owner"

    stranger = _client()
    _sign_in(stranger, "bob@example.com")
    hidden = [r for r in stranger.get("/api/workspace/rooms").json() if r["room_id"] == "listedroom2"]
    assert hidden == []

    anon = _client()
    hidden_anon = [r for r in anon.get("/api/workspace/rooms").json() if r["room_id"] == "listedroom2"]
    assert hidden_anon == []


def test_public_owned_room_listed_for_everyone(monkeypatch):
    _enable_accounts(monkeypatch)
    owner_client = _client()
    _sign_in(owner_client, "alice@example.com")
    with owner_client.websocket_connect("/ws/mesh/listedroom3") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()
    owner_client.post("/api/mesh/listedroom3/visibility", json={"visibility": "public"})

    anon = _client()
    listed = [r for r in anon.get("/api/workspace/rooms").json() if r["room_id"] == "listedroom3"]
    assert len(listed) == 1 and listed[0]["visibility"] == "public" and listed[0]["owner_display_name"] == "alice"
