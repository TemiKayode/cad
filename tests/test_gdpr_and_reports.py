"""Part 6, Phase P7: GDPR data export/account deletion, and abuse
reporting -- both accounts-mode only (see AccountStore.delete_user_
account's docstring for why account deletion releases an owned room
rather than deleting it, and billing.py's precedent for why any
feature touching `auth.get_account_store()` must stay gated behind
`accounts_enabled()`: the store is a lazy singleton that would
otherwise create the full accounts schema even in a tokens-only
deployment, breaking the "byte-for-byte unaffected" guarantee).

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store, so nothing here can leak into another
test or touch a real database file.
"""

from fastapi.testclient import TestClient

from crdt_cad.server import auth
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


# -- GDPR export --------------------------------------------------------------------


def test_export_requires_accounts_mode():
    resp = _client().get("/api/account/export")
    assert resp.status_code == 404


def test_export_requires_sign_in(monkeypatch):
    _enable_accounts(monkeypatch)
    resp = _client().get("/api/account/export")
    assert resp.status_code == 401


def test_export_returns_profile_and_owned_data(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")

    with client.websocket_connect("/ws/mesh/exportroom1") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()  # claims exportroom1 for alice

    resp = client.get("/api/account/export")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"]["email"] == "alice@example.com"
    assert any(r["room_id"] == "exportroom1" for r in body["owned_rooms"])
    assert body["organizations"] == []
    assert body["notifications"] == []
    assert "exported_at" in body


# -- GDPR account deletion ------------------------------------------------------------


def test_delete_requires_accounts_mode():
    resp = _client().post("/api/account/delete")
    assert resp.status_code == 404


def test_delete_requires_sign_in(monkeypatch):
    _enable_accounts(monkeypatch)
    resp = _client().post("/api/account/delete")
    assert resp.status_code == 401


def test_delete_blocks_sole_org_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org_resp = client.post("/api/orgs", json={"name": "Acme"})
    assert org_resp.status_code == 200, org_resp.text

    resp = client.post("/api/account/delete")
    assert resp.status_code == 400
    assert "last admin" in resp.json()["detail"]

    # Account must still exist and work after the blocked deletion.
    me = client.get("/api/auth/me").json()
    assert me["signed_in"] is True


def test_delete_succeeds_once_no_longer_sole_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = client.post("/api/orgs", json={"name": "Acme"}).json()
    client.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "admin"})
    # A pending invite doesn't count as "someone else can take over" --
    # bob has to actually sign in to activate it (matches P3's own
    # pending-invite semantics; count_org_admins only counts active ones).
    _sign_in(_client(), "bob@example.com")

    resp = client.post("/api/account/delete")
    assert resp.status_code == 200, resp.text

    # The session cookie is cleared and no longer resolves to a user.
    me = client.get("/api/auth/me").json()
    assert me["signed_in"] is False


def test_delete_releases_owned_room_rather_than_orphaning_it(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")

    with client.websocket_connect("/ws/mesh/deleteroom1") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
    assert auth.get_account_store().get_room_ownership("mesh", "deleteroom1") is not None

    resp = client.post("/api/account/delete")
    assert resp.status_code == 200, resp.text
    assert auth.get_account_store().get_room_ownership("mesh", "deleteroom1") is None


def test_delete_removes_grants_and_memberships(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")

    with owner.websocket_connect("/ws/mesh/grantroom1") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
    auth.get_account_store().set_room_grant("mesh", "grantroom1", bob["user_id"], "viewer")

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    resp = bob_client.post("/api/account/delete")
    assert resp.status_code == 200, resp.text
    assert auth.get_account_store().get_room_grant("mesh", "grantroom1", bob["user_id"]) is None
    assert auth.get_account_store().get_user(bob["user_id"]) is None


# -- abuse reports --------------------------------------------------------------------


def test_report_requires_accounts_mode():
    client = _client()
    with client.websocket_connect("/ws/mesh/reportroom1") as ws:
        _hello(ws, actor="a")
        ws.receive_json()
    resp = client.post("/api/mesh/reportroom1/report", json={"reason": "spam"})
    assert resp.status_code == 404


def test_report_works_for_signed_in_user(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    with client.websocket_connect("/ws/mesh/reportroom2") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()

    resp = client.post("/api/mesh/reportroom2/report", json={"reason": "spam", "details": "full of ads"})
    assert resp.status_code == 200, resp.text
    report_id = resp.json()["report_id"]

    reports = auth.get_account_store().list_abuse_reports()
    assert len(reports) == 1
    assert reports[0]["report_id"] == report_id
    assert reports[0]["reporter_user_id"] is not None
    assert reports[0]["status"] == "open"


def test_report_works_for_anonymous_visitor(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    with owner.websocket_connect("/ws/mesh/reportroom3") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()  # claims the room
    # A claimed room with visibility=public is reachable by anyone,
    # signed in or not (see _account_role_for_room's docstring).
    assert owner.post("/api/mesh/reportroom3/visibility", json={"visibility": "public"}).status_code == 200

    anon = _client()
    with anon.websocket_connect("/ws/mesh/reportroom3") as ws:
        _hello(ws, actor="anon")
        ws.receive_json()

    resp = anon.post("/api/mesh/reportroom3/report", json={"reason": "harassment"})
    assert resp.status_code == 200, resp.text
    reports = auth.get_account_store().list_abuse_reports()
    assert reports[0]["reporter_user_id"] is None


def test_report_requires_a_reason(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    with client.websocket_connect("/ws/mesh/reportroom4") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
    resp = client.post("/api/mesh/reportroom4/report", json={"reason": "   "})
    assert resp.status_code == 400


# -- admin review of reports -----------------------------------------------------------


def test_admin_can_list_and_resolve_reports(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin = _client()
    _sign_in(admin, "admin@example.com")

    store = auth.get_account_store()
    report = store.create_abuse_report(None, "mesh", "someroom", "spam", "details")

    resp = admin.get("/api/admin/reports")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resolve = admin.post(f"/api/admin/reports/{report['report_id']}/resolve", json={"status": "resolved"})
    assert resolve.status_code == 200, resolve.text

    open_reports = admin.get("/api/admin/reports?status=open").json()
    assert open_reports == []
    resolved_reports = admin.get("/api/admin/reports?status=resolved").json()
    assert len(resolved_reports) == 1


def test_admin_resolve_rejects_bad_status(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_ADMIN_EMAILS", "admin@example.com")
    admin = _client()
    _sign_in(admin, "admin@example.com")
    store = auth.get_account_store()
    report = store.create_abuse_report(None, "mesh", "someroom", "spam", "")
    resp = admin.post(f"/api/admin/reports/{report['report_id']}/resolve", json={"status": "not-a-real-status"})
    assert resp.status_code == 400


def test_non_admin_cannot_list_reports(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    resp = client.get("/api/admin/reports")
    assert resp.status_code == 403
