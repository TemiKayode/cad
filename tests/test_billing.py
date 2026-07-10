"""Part 6, Phase P6: org subscriptions via Stripe Checkout + the Stripe
billing portal, webhook-driven plan/status sync, and a free-plan seat
cap -- all opt-in behind CRDT_CAD_STRIPE_SECRET_KEY, so a deployment
that never sets it keeps Part 3's original unlimited org membership
byte-for-byte (see `test_seat_cap_does_not_apply_when_billing_disabled`).

No real Stripe test-mode account is reachable from this sandbox, so
the actual `stripe.StripeClient` call boundary is mocked
(`crdt_cad.server.billing._client`) rather than live-verified -- the
same honesty rule this project already applies to the Meshy AI tier.
`handle_webhook_event` itself needs no mocking at all: it only ever
reads a plain dict-like event, so these tests exercise the real,
unmocked function against synthetic Stripe-shaped payloads.

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store, so nothing here can leak into another
test or touch a real database file.
"""

from fastapi.testclient import TestClient

from crdt_cad.persistence.accounts import InMemoryAccountStore
from crdt_cad.server import auth
from crdt_cad.server import billing
from crdt_cad.server.app import app


def _client() -> TestClient:
    return TestClient(app)


def _enable_accounts(monkeypatch):
    monkeypatch.setenv("CRDT_CAD_AUTH_MODE", "accounts")
    monkeypatch.setenv("CRDT_CAD_SECRET", "test-deployment-secret")
    monkeypatch.setenv("CRDT_CAD_AUTH_DEV_ECHO", "1")


def _enable_billing(monkeypatch, price_id="price_test123"):
    monkeypatch.setenv("CRDT_CAD_STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("CRDT_CAD_STRIPE_WEBHOOK_SECRET", "whsec_dummy")
    if price_id:
        monkeypatch.setenv("CRDT_CAD_STRIPE_PRICE_ID", price_id)


def _sign_in(client: TestClient, email: str) -> str:
    resp = client.post("/api/auth/request-link", json={"email": email})
    assert resp.status_code == 200, resp.text
    link = resp.json()["dev_link"]
    verify = client.get(link, follow_redirects=False)
    assert verify.status_code == 303
    return verify.cookies[auth.SESSION_COOKIE]


def _create_org(client: TestClient, name: str) -> dict:
    resp = client.post("/api/orgs", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- pure logic (no HTTP, no mocking needed) -------------------------------------


def test_seat_limit_for_plan():
    assert billing.seat_limit_for_plan("free") == billing.free_plan_max_members()
    assert billing.seat_limit_for_plan("pro") is None


def test_handle_webhook_event_checkout_completed_upgrades_org():
    store = InMemoryAccountStore()
    user = store.create_or_get_user("alice@example.com")
    org = store.create_org("Acme", user["user_id"])
    assert org["billing_plan"] == "free"

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_1", "subscription": "sub_1", "metadata": {"org_id": org["org_id"]}}},
    }
    billing.handle_webhook_event(store, event)

    updated = store.get_org(org["org_id"])
    assert updated["billing_plan"] == "pro"
    assert updated["billing_status"] == "active"
    assert updated["billing_customer_id"] == "cus_1"
    assert updated["billing_subscription_id"] == "sub_1"


def test_handle_webhook_event_checkout_completed_ignores_missing_org_id():
    store = InMemoryAccountStore()
    event = {"type": "checkout.session.completed", "data": {"object": {"customer": "cus_1", "metadata": {}}}}
    billing.handle_webhook_event(store, event)  # must not raise


def test_handle_webhook_event_subscription_updated_syncs_status():
    store = InMemoryAccountStore()
    user = store.create_or_get_user("alice@example.com")
    org = store.create_org("Acme", user["user_id"])
    store.set_org_billing(org["org_id"], customer_id="cus_1", plan="pro", status="active")

    event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "past_due"}},
    }
    billing.handle_webhook_event(store, event)
    updated = store.get_org(org["org_id"])
    assert updated["billing_status"] == "past_due"
    assert updated["billing_plan"] == "free"  # not active/trialing anymore


def test_handle_webhook_event_subscription_deleted_downgrades_org():
    store = InMemoryAccountStore()
    user = store.create_or_get_user("alice@example.com")
    org = store.create_org("Acme", user["user_id"])
    store.set_org_billing(org["org_id"], customer_id="cus_1", subscription_id="sub_1", plan="pro", status="active")

    event = {"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_1"}}}
    billing.handle_webhook_event(store, event)
    updated = store.get_org(org["org_id"])
    assert updated["billing_plan"] == "free"
    assert updated["billing_status"] == "canceled"


def test_handle_webhook_event_unknown_customer_is_a_no_op():
    store = InMemoryAccountStore()
    event = {"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_does_not_exist"}}}
    billing.handle_webhook_event(store, event)  # must not raise


def test_handle_webhook_event_unrecognized_type_is_ignored():
    store = InMemoryAccountStore()
    event = {"type": "invoice.paid", "data": {"object": {}}}
    billing.handle_webhook_event(store, event)  # must not raise


# -- REST: billing status ----------------------------------------------------------


def test_billing_status_shows_disabled_by_default(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.get(f"/api/orgs/{org['org_id']}/billing")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["billing_enabled"] is False
    assert body["plan"] == "free"


def test_billing_status_reflects_plan_once_enabled(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.get(f"/api/orgs/{org['org_id']}/billing")
    body = resp.json()
    assert body["billing_enabled"] is True
    assert body["plan"] == "free"
    assert body["seat_limit"] == billing.free_plan_max_members()


# -- REST: checkout / portal (mocked at the billing module boundary) ------------


def test_checkout_requires_org_admin(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")

    member = _client()
    _sign_in(member, "bob@example.com")
    owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})

    resp = member.post(f"/api/orgs/{org['org_id']}/billing/checkout")
    assert resp.status_code == 403


def test_checkout_404s_when_billing_disabled(monkeypatch):
    _enable_accounts(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.post(f"/api/orgs/{org['org_id']}/billing/checkout")
    assert resp.status_code == 404


def test_checkout_returns_the_url_from_the_billing_module(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    monkeypatch.setattr(
        billing, "create_checkout_session",
        lambda store, org, success_url, cancel_url: "https://checkout.stripe.com/test/session123",
    )
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.post(f"/api/orgs/{org['org_id']}/billing/checkout")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/test/session123"


def test_checkout_surfaces_a_configuration_error_as_400(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch, price_id=None)  # no CRDT_CAD_STRIPE_PRICE_ID set

    def _raise(store, org, success_url, cancel_url):
        raise RuntimeError("CRDT_CAD_STRIPE_PRICE_ID is not set -- no plan configured to check out into.")

    monkeypatch.setattr(billing, "create_checkout_session", _raise)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.post(f"/api/orgs/{org['org_id']}/billing/checkout")
    assert resp.status_code == 400
    assert "PRICE_ID" in resp.json()["detail"]


def test_portal_returns_the_url_from_the_billing_module(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    monkeypatch.setattr(
        billing, "create_portal_session",
        lambda org, return_url: "https://billing.stripe.com/test/portal123",
    )
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.post(f"/api/orgs/{org['org_id']}/billing/portal")
    assert resp.status_code == 200, resp.text
    assert resp.json()["portal_url"] == "https://billing.stripe.com/test/portal123"


def test_portal_requires_an_existing_customer(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)

    def _raise(org, return_url):
        raise RuntimeError("this organization has no Stripe customer yet -- subscribe first.")

    monkeypatch.setattr(billing, "create_portal_session", _raise)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")
    resp = client.post(f"/api/orgs/{org['org_id']}/billing/portal")
    assert resp.status_code == 400
    assert "no Stripe customer" in resp.json()["detail"]


# -- REST: webhook ------------------------------------------------------------------


def test_webhook_404s_when_billing_disabled(monkeypatch):
    _enable_accounts(monkeypatch)
    resp = _client().post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "x"})
    assert resp.status_code == 404


def test_webhook_rejects_invalid_signature(monkeypatch):
    _enable_billing(monkeypatch)

    def _raise(payload, sig_header):
        raise ValueError("signature mismatch")

    monkeypatch.setattr(billing, "verify_webhook", _raise)
    resp = _client().post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "bad"})
    assert resp.status_code == 400


def test_webhook_end_to_end_upgrades_org(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    client = _client()
    _sign_in(client, "alice@example.com")
    org = _create_org(client, "Acme")

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {"customer": "cus_42", "subscription": "sub_42", "metadata": {"org_id": org["org_id"]}}},
    }
    monkeypatch.setattr(billing, "verify_webhook", lambda payload, sig_header: event)
    resp = client.post("/api/billing/webhook", content=b"{}", headers={"stripe-signature": "whatever"})
    assert resp.status_code == 200, resp.text

    updated = auth.get_account_store().get_org(org["org_id"])
    assert updated["billing_plan"] == "pro"
    assert updated["billing_customer_id"] == "cus_42"


# -- seat cap ------------------------------------------------------------------------


def test_seat_cap_blocks_invite_over_limit_on_free_plan(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_FREE_PLAN_MAX_MEMBERS", "1")
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")  # creator is already the 1 allowed member

    resp = owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert resp.status_code == 402
    assert "free plan" in resp.json()["detail"]


def test_seat_cap_does_not_apply_when_billing_disabled(monkeypatch):
    _enable_accounts(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_FREE_PLAN_MAX_MEMBERS", "1")
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")

    resp = owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert resp.status_code == 200, resp.text


def test_seat_cap_allows_upgrade_to_pro_to_add_more_members(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_FREE_PLAN_MAX_MEMBERS", "1")
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")

    auth.get_account_store().set_org_billing(org["org_id"], plan="pro", status="active")
    resp = owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert resp.status_code == 200, resp.text


def test_seat_cap_exempts_reinviting_an_existing_active_member(monkeypatch):
    _enable_accounts(monkeypatch)
    _enable_billing(monkeypatch)
    monkeypatch.setenv("CRDT_CAD_FREE_PLAN_MAX_MEMBERS", "2")
    owner = _client()
    _sign_in(owner, "alice@example.com")
    org = _create_org(owner, "Acme")
    resp1 = owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "member"})
    assert resp1.status_code == 200, resp1.text  # now at the 2-member cap

    # Re-inviting bob (e.g. to promote him) must not trip the cap even
    # though the roster is already at its limit.
    resp2 = owner.post(f"/api/orgs/{org['org_id']}/invite", json={"email": "bob@example.com", "role": "admin"})
    assert resp2.status_code == 200, resp2.text
