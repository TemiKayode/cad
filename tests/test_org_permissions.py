"""Part 6, Phase P3: organizations and teams -- orgs + admin/member
memberships, org-owned documents, invite-by-e-mail with a genuine
pending-invite state (consumed on the invitee's first real sign-in),
and per-org defaults (new-document visibility, allowed share-link
roles), all layered on top of Part 6 P2's ownership/visibility model
without changing its behavior for a personally-owned room.

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store, so nothing here can leak into another
test or touch a real database file.
"""

import pytest
from fastapi.testclient import TestClient

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


def _create_org(client: TestClient, name: str) -> dict:
    resp = client.post("/api/orgs", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- creating and listing orgs ---------------------------------------------------


def test_create_org_requires_accounts_mode():
    resp = _client().post("/api/orgs", json={"name": "Acme"})
    assert resp.status_code == 404


def test_create_org_requires_sign_in(monkeypatch):
    _enable_accounts(monkeypatch)
    resp = _client().post("/api/orgs", json={"name": "Acme"})
    assert resp.status_code == 401


def test_creator_becomes_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    assert org["default_visibility"] == "private"
    assert org["allowed_share_link_roles"] == ["viewer", "editor"]

    detail = client.get(f"/api/orgs/{org['org_id']}").json()
    assert len(detail["members"]) == 1
    assert detail["members"][0]["email"] == "alice@example.com"
    assert detail["members"][0]["role"] == "admin"
    assert detail["members"][0]["status"] == "active"


def test_list_my_orgs_only_shows_my_active_memberships(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    _create_org(alice, "Acme")

    bob = _client()
    _sign_in(bob, "bob@example.com")
    assert bob.get("/api/orgs").json() == []
    assert len(alice.get("/api/orgs").json()) == 1


def test_org_detail_requires_membership(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")

    stranger = _client()
    _sign_in(stranger, "carol@example.com")
    resp = stranger.get(f"/api/orgs/{org['org_id']}")
    assert resp.status_code == 403


# -- inviting members and the pending-invite state -------------------------------


def test_invite_new_email_is_pending_until_they_sign_in(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")

    invite = alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert invite.status_code == 200
    assert invite.json()["status"] == "pending"

    detail = alice.get(f"/api/orgs/{org['org_id']}").json()
    bob_row = next(m for m in detail["members"] if m["email"] == "bob@example.com")
    assert bob_row["status"] == "pending"

    bob = _client()
    _sign_in(bob, "bob@example.com")  # the real acceptance mechanism -- their first sign-in
    detail2 = alice.get(f"/api/orgs/{org['org_id']}").json()
    bob_row2 = next(m for m in detail2["members"] if m["email"] == "bob@example.com")
    assert bob_row2["status"] == "active"


def test_invite_existing_account_is_active_immediately(monkeypatch):
    _enable_accounts(monkeypatch)
    bob = _client()
    _sign_in(bob, "bob@example.com")  # bob already has an account

    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    invite = alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert invite.json()["status"] == "active"


def test_only_admin_can_invite(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})

    bob = _client()
    _sign_in(bob, "bob@example.com")
    resp = bob.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "carol@example.com", "role": "member"})
    assert resp.status_code == 403


def test_invite_rejects_invalid_role(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    resp = alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "owner"})
    assert resp.status_code == 400


# -- role management and last-admin guards ----------------------------------------


def test_admin_can_promote_and_demote_members(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")
    alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})

    promote = alice.post(f"/api/orgs/{org['org_id']}/members/{bob['user_id']}/role", json={"role": "admin"})
    assert promote.status_code == 200
    assert auth.get_account_store().get_org_membership(org["org_id"], bob["user_id"])["role"] == "admin"

    demote = alice.post(f"/api/orgs/{org['org_id']}/members/{bob['user_id']}/role", json={"role": "member"})
    assert demote.status_code == 200


def test_cannot_demote_the_last_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    alice_id = auth.get_account_store().get_user_by_email("alice@example.com")["user_id"]
    resp = alice.post(f"/api/orgs/{org['org_id']}/members/{alice_id}/role", json={"role": "member"})
    assert resp.status_code == 400


def test_cannot_remove_the_last_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    alice_id = auth.get_account_store().get_user_by_email("alice@example.com")["user_id"]
    resp = alice.delete(f"/api/orgs/{org['org_id']}/members/{alice_id}")
    assert resp.status_code == 400


def test_member_can_be_removed_by_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")
    alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    resp = alice.delete(f"/api/orgs/{org['org_id']}/members/{bob['user_id']}")
    assert resp.status_code == 200
    assert auth.get_account_store().get_org_membership(org["org_id"], bob["user_id"]) is None


def test_leave_org_self_service(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    bob = _client()
    _sign_in(bob, "bob@example.com")
    alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    resp = bob.post(f"/api/orgs/{org['org_id']}/leave")
    assert resp.status_code == 200
    bob_id = auth.get_account_store().get_user_by_email("bob@example.com")["user_id"]
    assert auth.get_account_store().get_org_membership(org["org_id"], bob_id) is None


def test_last_admin_cannot_leave(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    resp = alice.post(f"/api/orgs/{org['org_id']}/leave")
    assert resp.status_code == 400


# -- org defaults --------------------------------------------------------------


def test_only_admin_can_set_org_defaults(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    bob = _client()
    _sign_in(bob, "bob@example.com")
    alice.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    resp = bob.post(f"/api/orgs/{org['org_id']}/defaults", json={"default_visibility": "public"})
    assert resp.status_code == 403


def test_set_org_defaults_validates_values(monkeypatch):
    _enable_accounts(monkeypatch)
    alice = _client()
    _sign_in(alice, "alice@example.com")
    org = _create_org(alice, "Acme")
    bad_vis = alice.post(f"/api/orgs/{org['org_id']}/defaults", json={"default_visibility": "nonsense"})
    assert bad_vis.status_code == 400
    bad_roles = alice.post(f"/api/orgs/{org['org_id']}/defaults", json={"allowed_share_link_roles": ["owner"]})
    assert bad_roles.status_code == 400

    ok = alice.post(
        f"/api/orgs/{org['org_id']}/defaults",
        json={"default_visibility": "link", "allowed_share_link_roles": ["viewer"]},
    )
    assert ok.status_code == 200
    assert auth.get_account_store().get_org(org["org_id"])["default_visibility"] == "link"


# -- transferring a room to an org, and org-based room access -------------------


def test_transfer_requires_room_owner(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/transferroom1") as ws:
        _hello(ws)
        ws.receive_json()

    stranger = _client()
    _sign_in(stranger, "carol@example.com")
    resp = stranger.post("/api/mesh/transferroom1/transfer", json={"org_id": org["org_id"]})
    assert resp.status_code == 403


def test_transfer_requires_target_org_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/transferroom2") as ws:
        _hello(ws)
        ws.receive_json()

    # alice owns the room but is only a MEMBER (not admin) of a second org
    other_org_owner = _client()
    _sign_in(other_org_owner, "dave@example.com")
    other_org = _create_org(other_org_owner, "Beta")
    other_org_owner.post(f"/api/orgs/{other_org['org_id']}/invite", json={"email": "alice@example.com", "role": "member"})

    resp = owner.post("/api/mesh/transferroom2/transfer", json={"org_id": other_org["org_id"]})
    assert resp.status_code == 403


def test_transfer_sets_org_ownership_and_default_visibility(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    owner.post(f"/api/orgs/{org['org_id']}/defaults", json={"default_visibility": "public"})
    with owner.websocket_connect("/ws/mesh/transferroom3") as ws:
        _hello(ws)
        ws.receive_json()

    resp = owner.post("/api/mesh/transferroom3/transfer", json={"org_id": org["org_id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_org_id"] == org["org_id"]
    assert body["visibility"] == "public"

    ownership = auth.get_account_store().get_room_ownership("mesh", "transferroom3")
    assert ownership["owner_org_id"] == org["org_id"]
    assert ownership["visibility"] == "public"


def test_org_member_gets_editor_on_private_org_owned_room(monkeypatch):
    """The actual "only members of the same team can see projects"
    feature: a private, org-owned room is accessible to every active
    org member, not just the original creator or explicit grantees."""
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/teamroom1") as ws:
        _hello(ws)
        ws.receive_json()
    owner.post("/api/mesh/teamroom1/transfer", json={"org_id": org["org_id"]})  # default_visibility stays "private"

    bob = _client()
    _sign_in(bob, "bob@example.com")
    owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})

    with bob.websocket_connect("/ws/mesh/teamroom1") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "editor"


def test_org_admin_gets_owner_role_on_org_owned_room(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/teamroom2") as ws:
        _hello(ws)
        ws.receive_json()
    owner.post("/api/mesh/teamroom2/transfer", json={"org_id": org["org_id"]})

    bob = _client()
    _sign_in(bob, "bob@example.com")
    owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "admin"})

    with bob.websocket_connect("/ws/mesh/teamroom2") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "owner"

    # and, per require_owner_access's P3 extension, bob (an org admin, not
    # the room's personal owner) can manage its sharing directly:
    resp = bob.get("/api/mesh/teamroom2/sharing")
    assert resp.status_code == 200


def test_non_org_member_still_denied_on_private_org_owned_room(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/teamroom3") as ws:
        _hello(ws)
        ws.receive_json()
    owner.post("/api/mesh/teamroom3/transfer", json={"org_id": org["org_id"]})

    stranger = _client()
    _sign_in(stranger, "carol@example.com")

    from fastapi import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with stranger.websocket_connect("/ws/mesh/teamroom3") as ws:
            _hello(ws)
            ws.receive_json()
    assert exc_info.value.code == app_module.WS_CLOSE_UNAUTHORIZED


def test_explicit_room_grant_overrides_org_member_default(monkeypatch):
    """A per-room grant (Part 6 P2) is more specific than the org's
    blanket member-gets-editor default, and wins."""
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/teamroom4") as ws:
        _hello(ws)
        ws.receive_json()
    owner.post("/api/mesh/teamroom4/transfer", json={"org_id": org["org_id"]})

    bob = _client()
    _sign_in(bob, "bob@example.com")
    owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    bob_id = auth.get_account_store().get_user_by_email("bob@example.com")["user_id"]
    owner.post("/api/mesh/teamroom4/grant", json={"email": "bob@example.com", "role": "viewer"})

    with bob.websocket_connect("/ws/mesh/teamroom4") as ws:
        _hello(ws)
        snap = ws.receive_json()
        assert snap["role"] == "viewer"  # not "editor" -- the explicit grant wins


def test_workspace_listing_shows_org_name_for_org_owned_room(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    with owner.websocket_connect("/ws/mesh/teamroom5") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()
    owner.post("/api/mesh/teamroom5/transfer", json={"org_id": org["org_id"]})

    bob = _client()
    _sign_in(bob, "bob@example.com")
    owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})

    rooms = [r for r in bob.get("/api/workspace/rooms").json() if r["room_id"] == "teamroom5"]
    assert len(rooms) == 1
    assert rooms[0]["org_name"] == "Acme"
    assert rooms[0]["your_role"] == "editor"


# -- share-link role restriction --------------------------------------------------


def test_org_can_restrict_allowed_share_link_roles(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    owner.post(f"/api/orgs/{org['org_id']}/defaults", json={"allowed_share_link_roles": ["viewer"]})
    with owner.websocket_connect("/ws/mesh/linkroom1") as ws:
        _hello(ws)
        ws.receive_json()
    owner.post("/api/mesh/linkroom1/transfer", json={"org_id": org["org_id"]})

    viewer_link = owner.post("/api/mesh/linkroom1/share-link", json={"role": "viewer"})
    assert viewer_link.status_code == 200

    editor_link = owner.post("/api/mesh/linkroom1/share-link", json={"role": "editor"})
    assert editor_link.status_code == 403


def test_personal_room_share_link_unaffected_by_org_restrictions(monkeypatch):
    """A room that was never transferred to any org keeps today's
    unrestricted share-link behavior regardless of what any org's
    defaults happen to be."""
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    with owner.websocket_connect("/ws/mesh/linkroom2") as ws:
        _hello(ws)
        ws.receive_json()
    resp = owner.post("/api/mesh/linkroom2/share-link", json={"role": "editor"})
    assert resp.status_code == 200
