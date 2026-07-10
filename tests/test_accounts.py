"""Part 6, Phase P1: user accounts -- magic-link sign-in, server-side
sessions, and the sacred zero-config guarantee (tokens mode must behave
byte-for-byte as if this feature didn't exist).

OAuth is covered at the configuration/gating level here (provider
discovery, unconfigured-provider 404s); the full browser redirect dance
needs a real provider and is exercised only when one is configured --
same honesty split as the Meshy adapter's mock-vs-live testing.
"""

import pytest
from fastapi.testclient import TestClient

from crdt_cad.persistence.accounts import InMemoryAccountStore, SQLiteAccountStore
from crdt_cad.server import app as app_module
from crdt_cad.server import auth

client = TestClient(app_module.app)


def _enable_accounts(monkeypatch, echo=True):
    monkeypatch.setenv("CRDT_CAD_AUTH_MODE", "accounts")
    monkeypatch.setenv("CRDT_CAD_SECRET", "test-deployment-secret")
    if echo:
        monkeypatch.setenv("CRDT_CAD_AUTH_DEV_ECHO", "1")


def _sign_in(email="alice@example.com"):
    """Full magic-link round trip. The TestClient's cookie jar captures
    the session cookie from the 303 itself (setting it manually too
    would create a duplicate jar entry -- httpx raises CookieConflict on
    the next read). Returns the raw session token."""
    resp = client.post("/api/auth/request-link", json={"email": email})
    assert resp.status_code == 200, resp.text
    link = resp.json()["dev_link"]
    verify = client.get(link, follow_redirects=False)
    assert verify.status_code == 303
    return verify.cookies[auth.SESSION_COOKIE]


@pytest.fixture(autouse=True)
def fresh_client_cookies():
    client.cookies.clear()
    yield
    client.cookies.clear()


# -- the zero-config guarantee ----------------------------------------------------


def test_tokens_mode_me_reports_mode_and_nothing_else():
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "mode": "tokens", "signed_in": False, "user": None, "oauth_providers": [], "is_platform_admin": False,
    }


def test_tokens_mode_sign_in_routes_are_404(monkeypatch):
    assert client.post("/api/auth/request-link", json={"email": "a@b.co"}).status_code == 404
    assert client.get("/api/auth/verify?token=whatever").status_code == 404
    assert client.post("/api/auth/logout").status_code == 404


# -- magic-link flow ----------------------------------------------------------


def test_magic_link_round_trip_creates_user_and_session(monkeypatch):
    _enable_accounts(monkeypatch)
    _sign_in("alice@example.com")
    me = client.get("/api/auth/me").json()
    assert me["signed_in"] is True
    assert me["user"]["email"] == "alice@example.com"
    assert me["user"]["display_name"] == "alice"  # local-part default


def test_email_identity_is_case_insensitive(monkeypatch):
    _enable_accounts(monkeypatch)
    _sign_in("Alice@Example.COM")
    first = client.get("/api/auth/me").json()["user"]["user_id"]
    client.cookies.clear()
    _sign_in("alice@example.com")
    assert client.get("/api/auth/me").json()["user"]["user_id"] == first


def test_dev_link_requires_explicit_opt_in(monkeypatch):
    """No SMTP + no CRDT_CAD_AUTH_DEV_ECHO must NOT hand the requester a
    working sign-in link -- that would let any visitor sign in as any
    address on a deployment that forgot to configure mail."""
    _enable_accounts(monkeypatch, echo=False)
    resp = client.post("/api/auth/request-link", json={"email": "victim@example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"sent": False}


def test_garbage_email_rejected(monkeypatch):
    _enable_accounts(monkeypatch)
    assert client.post("/api/auth/request-link", json={"email": "not-an-email"}).status_code == 422


def test_tampered_magic_token_rejected(monkeypatch):
    _enable_accounts(monkeypatch)
    assert client.get("/api/auth/verify?token=forged.token.here").status_code == 400


def test_expired_magic_token_rejected(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_MAGIC_LINK_MAX_AGE_SECONDS", "-1")
    token = auth.mint_magic_token("late@example.com")
    assert client.get(f"/api/auth/verify?token={token}").status_code == 410


def test_accounts_mode_without_secret_is_a_clear_503(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_AUTH_MODE", "accounts")
    monkeypatch.delenv("CRDT_CAD_SECRET", raising=False)
    resp = client.post("/api/auth/request-link", json={"email": "a@b.co"})
    assert resp.status_code == 503
    assert "CRDT_CAD_SECRET" in resp.json()["detail"]


# -- sessions ----------------------------------------------------------


def test_logout_invalidates_the_session_server_side(monkeypatch):
    _enable_accounts(monkeypatch)
    stolen_cookie = _sign_in()
    assert client.post("/api/auth/logout").status_code == 204
    # Even replaying the old cookie value must fail: the session row is
    # gone, not just the browser cookie.
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, stolen_cookie)
    assert client.get("/api/auth/me").json()["signed_in"] is False


def test_logout_everywhere_kills_every_session(monkeypatch, isolated_account_store):
    _enable_accounts(monkeypatch)
    _sign_in("bob@example.com")
    user_id = client.get("/api/auth/me").json()["user"]["user_id"]
    # a second device
    other = auth.create_session(user_id)
    resp = client.post("/api/auth/logout-everywhere")
    assert resp.status_code == 200
    assert resp.json()["sessions_removed"] == 2
    assert isolated_account_store.get_session(auth._hash_token(other)) is None


def test_session_cookie_is_httponly_and_lax():
    # Inspect the Set-Cookie header itself -- attributes aren't exposed
    # via response.cookies.
    from crdt_cad.server.auth import _set_session_cookie
    from fastapi import Response

    class _Req:
        class url:
            scheme = "http"

        cookies = {}

    response = Response()
    _set_session_cookie(response, "tok", _Req())
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


def test_profile_update(monkeypatch):
    _enable_accounts(monkeypatch)
    _sign_in()
    resp = client.patch("/api/auth/profile", json={"display_name": "Alice the Architect"})
    assert resp.status_code == 200
    assert client.get("/api/auth/me").json()["user"]["display_name"] == "Alice the Architect"


def test_profile_requires_sign_in(monkeypatch):
    _enable_accounts(monkeypatch)
    assert client.patch("/api/auth/profile", json={"display_name": "x"}).status_code == 401


# -- OAuth gating ----------------------------------------------------------


def test_oauth_providers_absent_by_default(monkeypatch):
    _enable_accounts(monkeypatch)
    assert client.get("/api/auth/me").json()["oauth_providers"] == []


def test_oauth_provider_discovery_from_env(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("CRDT_CAD_OAUTH_GITHUB_CLIENT_SECRET", "csecret")
    assert client.get("/api/auth/me").json()["oauth_providers"] == ["github"]


def test_unconfigured_oauth_provider_404s(monkeypatch):
    _enable_accounts(monkeypatch)
    assert client.get("/api/auth/oauth/google/start", follow_redirects=False).status_code == 404


# -- the store itself (both concrete backends behave identically) ---------------


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_store_user_and_session_lifecycle(backend, tmp_path):
    store = InMemoryAccountStore() if backend == "memory" else SQLiteAccountStore(tmp_path / "accounts.db")
    user = store.create_or_get_user("Case@Example.com", display_name="Case")
    assert user["email"] == "case@example.com"
    again = store.create_or_get_user("case@example.com")
    assert again["user_id"] == user["user_id"]

    assert store.update_profile(user["user_id"], display_name="Renamed", avatar_color="#4dabf7")
    assert store.get_user(user["user_id"])["display_name"] == "Renamed"
    assert store.get_user_by_email("CASE@example.com")["avatar_color"] == "#4dabf7"
    assert not store.update_profile("no-such-user", display_name="x")

    import time as _time

    store.create_session("hash-live", user["user_id"], expires_at=_time.time() + 60)
    store.create_session("hash-dead", user["user_id"], expires_at=_time.time() - 1)
    assert store.get_session("hash-live")["user_id"] == user["user_id"]
    assert store.get_session("hash-dead") is None, "expired sessions read as absent"
    store.touch_session("hash-live")
    assert store.delete_user_sessions(user["user_id"]) == 1  # dead one already reaped
    assert store.get_session("hash-live") is None
