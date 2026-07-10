"""Part 6, Phase P4: SSO domain capture, disabled accounts, per-account
quotas, and the operator admin panel -- all opt-in, layered on top of
Part 6 P1-P3 without changing behavior for a deployment that never
touches any of these knobs.

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store; `isolated_account_store` also clears
CRDT_CAD_ADMIN_EMAILS so no developer's real shell environment can leak
into a test.
"""

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


# -- platform admin bootstrap (CRDT_CAD_ADMIN_EMAILS) ---------------------------


def test_not_platform_admin_without_env_var(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    assert client.get("/api/auth/me").json()["is_platform_admin"] is False


def test_platform_admin_env_var_grants_access(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "alice@example.com, other@example.com")
    client = _client()
    _sign_in(client, "alice@example.com")
    assert client.get("/api/auth/me").json()["is_platform_admin"] is True

    someone_else = _client()
    _sign_in(someone_else, "bob@example.com")
    assert someone_else.get("/api/auth/me").json()["is_platform_admin"] is False


def test_admin_routes_require_accounts_mode():
    resp = _client().get("/api/admin/users")
    assert resp.status_code == 404


def test_admin_routes_require_platform_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "bob@example.com")
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/orgs").status_code == 403
    assert client.get("/api/admin/rooms").status_code == 403


# -- disabling a user -------------------------------------------------------------


def test_admin_can_disable_and_reenable_a_user(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin_client = _client()
    _sign_in(admin_client, "admin@example.com")

    victim_client = _client()
    _sign_in(victim_client, "victim@example.com")
    assert victim_client.get("/api/auth/me").json()["signed_in"] is True
    victim_id = victim_client.get("/api/auth/me").json()["user"]["user_id"]

    resp = admin_client.post(f"/api/admin/users/{victim_id}/disabled", json={"disabled": True})
    assert resp.status_code == 200, resp.text

    # The victim's existing session cookie is now inert -- current_user()
    # refuses a disabled account without any separate session revocation.
    assert victim_client.get("/api/auth/me").json()["signed_in"] is False

    resp = admin_client.post(f"/api/admin/users/{victim_id}/disabled", json={"disabled": False})
    assert resp.status_code == 200, resp.text
    # A fresh sign-in works again once re-enabled.
    victim_client2 = _client()
    _sign_in(victim_client2, "victim@example.com")
    assert victim_client2.get("/api/auth/me").json()["signed_in"] is True


def test_disabled_account_cannot_verify_a_new_magic_link(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin_client = _client()
    _sign_in(admin_client, "admin@example.com")

    victim_client = _client()
    resp = victim_client.post("/api/auth/request-link", json={"email": "victim2@example.com"})
    link = resp.json()["dev_link"]
    verify = victim_client.get(link, follow_redirects=False)
    assert verify.status_code == 303
    victim_id = victim_client.get("/api/auth/me").json()["user"]["user_id"]

    admin_client.post(f"/api/admin/users/{victim_id}/disabled", json={"disabled": True})

    victim_client2 = _client()
    resp2 = victim_client2.post("/api/auth/request-link", json={"email": "victim2@example.com"})
    link2 = resp2.json()["dev_link"]
    verify2 = victim_client2.get(link2, follow_redirects=False)
    assert verify2.status_code == 403


# -- admin room claim/delete --------------------------------------------------


def test_admin_can_claim_an_ownerless_room(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin_client = _client()
    _sign_in(admin_client, "admin@example.com")

    # A real user exists (someone_id), but the room itself was touched
    # anonymously -- e.g. created before accounts mode was ever turned
    # on -- so it genuinely has no owner despite someone_id being real.
    someone = _client()
    _sign_in(someone, "someone@example.com")
    someone_id = someone.get("/api/auth/me").json()["user"]["user_id"]

    anon_client = _client()
    token = security.mint_room_token("mesh", "abandoned1")
    with anon_client.websocket_connect("/ws/mesh/abandoned1") as ws:
        _hello(ws, token=token)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()
    assert auth.get_account_store().get_room_ownership("mesh", "abandoned1") is None

    resp = admin_client.post(
        "/api/admin/rooms/claim",
        json={"kind": "mesh", "room_id": "abandoned1", "owner_user_id": someone_id},
    )
    assert resp.status_code == 200, resp.text
    ownership = auth.get_account_store().get_room_ownership("mesh", "abandoned1")
    assert ownership["owner_user_id"] == someone_id
    assert ownership["visibility"] == "public"

    # Claiming an already-owned room fails cleanly.
    resp2 = admin_client.post(
        "/api/admin/rooms/claim",
        json={"kind": "mesh", "room_id": "abandoned1", "owner_user_id": someone_id},
    )
    assert resp2.status_code == 409


def test_admin_can_delete_a_room(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin_client = _client()
    _sign_in(admin_client, "admin@example.com")

    with admin_client.websocket_connect("/ws/mesh/deleteme1") as ws:
        _hello(ws)
        ws.receive_json()
        ws.send_json({"type": "save"})
        ws.receive_json()
    assert auth.get_account_store().get_room_ownership("mesh", "deleteme1") is not None
    assert app_module.store.load("mesh", "deleteme1") is not None

    resp = admin_client.delete("/api/admin/rooms/mesh/deleteme1")
    assert resp.status_code == 200, resp.text
    assert app_module.store.load("mesh", "deleteme1") is None
    assert auth.get_account_store().get_room_ownership("mesh", "deleteme1") is None
    assert "deleteme1" not in app_module.mesh_room_manager.rooms


# -- quotas ---------------------------------------------------------------------


def test_generation_quota_blocks_after_limit(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setattr(app_module, "QUOTA_GENERATIONS_PER_DAY", 1)
    client = _client()
    _sign_in(client, "alice@example.com")
    token = security.mint_room_token("mesh", "quotaroom1")

    resp1 = client.post(f"/api/mesh/quotaroom1/generate?token={token}", json={"prompt": "a small cube"})
    assert resp1.status_code != 429

    resp2 = client.post(f"/api/mesh/quotaroom1/generate?token={token}", json={"prompt": "a small cube"})
    assert resp2.status_code == 429
    assert "quota" in resp2.json()["detail"].lower()


def test_generation_quota_does_not_apply_to_anonymous_tokens_mode(monkeypatch):
    # QUOTA_* is opt-in and keyed to a signed-in user; a tokens-mode
    # deployment (the zero-config default) never has a `user`, so the
    # quota check is always a no-op for it regardless of the env var.
    monkeypatch.setattr(app_module, "QUOTA_GENERATIONS_PER_DAY", 1)
    client = _client()
    resp1 = client.post("/api/mesh/quotaroom2/generate", json={"prompt": "a small cube"})
    resp2 = client.post("/api/mesh/quotaroom2/generate", json={"prompt": "a small cube"})
    assert resp1.status_code != 429
    assert resp2.status_code != 429


def test_share_link_quota_blocks_after_limit(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setattr(app_module, "QUOTA_SHARE_LINKS_PER_DAY", 1)
    client = _client()
    _sign_in(client, "alice@example.com")
    token = security.mint_room_token("mesh", "shareroom1")
    with client.websocket_connect("/ws/mesh/shareroom1") as ws:
        _hello(ws, token=token)
        ws.receive_json()

    resp1 = client.post("/api/mesh/shareroom1/share-link", json={"role": "viewer"})
    assert resp1.status_code == 200, resp1.text
    resp2 = client.post("/api/mesh/shareroom1/share-link", json={"role": "viewer"})
    assert resp2.status_code == 429


def test_owned_documents_quota_skips_claim_over_limit(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setattr(app_module, "QUOTA_OWNED_DOCUMENTS", 1)
    client = _client()
    _sign_in(client, "alice@example.com")

    with client.websocket_connect("/ws/mesh/docroom1") as ws:
        _hello(ws)
        ws.receive_json()
    assert auth.get_account_store().get_room_ownership("mesh", "docroom1") is not None

    token = security.mint_room_token("mesh", "docroom2")
    with client.websocket_connect("/ws/mesh/docroom2") as ws:
        _hello(ws, token=token)
        snap = ws.receive_json()
        # The connection itself still succeeds -- a quota degrades
        # gracefully to "not claimed", never a broken session.
        assert snap["role"] == "editor"
    assert auth.get_account_store().get_room_ownership("mesh", "docroom2") is None


# -- SSO domain capture -----------------------------------------------------------


def _configure_org_sso(client: TestClient, org_id: str, domain: str) -> None:
    resp = client.post(
        f"/api/orgs/{org_id}/sso",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
            "domain": domain,
        },
    )
    assert resp.status_code == 200, resp.text


def test_domain_capture_blocks_magic_link_request(monkeypatch):
    _enable_accounts(monkeypatch)
    admin_client = _client()
    _sign_in(admin_client, "founder@acme.com")
    org = admin_client.post("/api/orgs", json={"name": "Acme"}).json()
    _configure_org_sso(admin_client, org["org_id"], "acme.com")

    stranger = _client()
    resp = stranger.post("/api/auth/request-link", json={"email": "newhire@acme.com"})
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "sso_required"
    assert detail["org_id"] == org["org_id"]


def test_domain_capture_does_not_affect_other_domains(monkeypatch):
    _enable_accounts(monkeypatch)
    admin_client = _client()
    _sign_in(admin_client, "founder@acme.com")
    org = admin_client.post("/api/orgs", json={"name": "Acme"}).json()
    _configure_org_sso(admin_client, org["org_id"], "acme.com")

    stranger = _client()
    resp = stranger.post("/api/auth/request-link", json={"email": "person@example.com"})
    assert resp.status_code == 200


def test_sso_config_rejects_duplicate_domain(monkeypatch):
    _enable_accounts(monkeypatch)
    admin1 = _client()
    _sign_in(admin1, "founder1@acme.com")
    org1 = admin1.post("/api/orgs", json={"name": "Acme"}).json()
    _configure_org_sso(admin1, org1["org_id"], "shared.com")

    admin2 = _client()
    _sign_in(admin2, "founder2@other.com")
    org2 = admin2.post("/api/orgs", json={"name": "Other"}).json()
    resp = admin2.post(
        f"/api/orgs/{org2['org_id']}/sso",
        json={
            "issuer": "https://idp.example.com",
            "client_id": "x",
            "client_secret": "y",
            "domain": "shared.com",
        },
    )
    assert resp.status_code == 409


def test_sso_config_is_org_admin_only(monkeypatch):
    _enable_accounts(monkeypatch)
    admin_client = _client()
    _sign_in(admin_client, "founder@acme.com")
    org = admin_client.post("/api/orgs", json={"name": "Acme"}).json()

    member_client = _client()
    _sign_in(member_client, "member@acme.com")
    admin_client.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "member@acme.com", "role": "member"})

    resp = member_client.post(
        f"/api/orgs/{org['org_id']}/sso",
        json={"issuer": "https://idp.example.com", "client_id": "x", "client_secret": "y", "domain": "acme.com"},
    )
    assert resp.status_code == 403


def test_sso_start_404s_when_not_configured(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "founder@acme.com")
    org = client.post("/api/orgs", json={"name": "Acme"}).json()
    resp = client.get(f"/api/auth/sso/{org['org_id']}/start", follow_redirects=False)
    assert resp.status_code == 404
