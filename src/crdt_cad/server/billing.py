"""Org subscriptions via Stripe Checkout + the Stripe billing portal
(Part 6, Phase P6).

Opt-in and layered on top of everything else, same discipline as every
other Part 6 phase: :func:`billing_enabled` is False whenever
``CRDT_CAD_STRIPE_SECRET_KEY`` is unset, and every route in
``app.py`` checks it before doing anything -- a deployment that never
sets it is byte-for-byte unaffected, including the free-plan seat cap
(:func:`seat_limit_for_plan` is only ever consulted when billing is
actually enabled, so a deployment that never touches this phase keeps
today's unlimited org membership).

**Test-mode only in this repo**: there is no live Stripe account
reachable from this sandbox to verify against, so this is
config/gating tested (unit tests mock :class:`stripe.StripeClient`
itself at this module's ``_client()`` boundary) rather than
live-verified against a real Stripe test-mode account -- the same
honesty rule this project already applies to the Meshy AI tier and the
GHCR release image (see README).

Data model: four columns on ``orgs`` (``billing_customer_id``,
``billing_subscription_id``, ``billing_plan`` -- ``"free"`` or
``"pro"``, ``billing_status`` -- the raw Stripe subscription status,
e.g. ``"active"``/``"trialing"``/``"past_due"``/``"canceled"``), kept
in sync by :func:`handle_webhook_event`, not by the checkout/portal
calls themselves (a checkout redirect can be abandoned; the webhook is
the only source of truth for whether money actually changed hands).
"""

from __future__ import annotations

import os
from typing import Optional

from crdt_cad.persistence.accounts import AccountStore


def billing_enabled() -> bool:
    return bool(os.environ.get("CRDT_CAD_STRIPE_SECRET_KEY"))


def price_id() -> Optional[str]:
    return os.environ.get("CRDT_CAD_STRIPE_PRICE_ID")


def free_plan_max_members() -> int:
    return int(os.environ.get("CRDT_CAD_FREE_PLAN_MAX_MEMBERS", "3"))


def seat_limit_for_plan(plan: str) -> Optional[int]:
    """None means unlimited. Only the free plan is capped -- any other
    plan value (currently just ``"pro"``) is unlimited. A deliberately
    simple two-tier model, not a general plan-configuration system."""
    return free_plan_max_members() if plan == "free" else None


def _import_stripe():
    try:
        import stripe
    except ImportError as exc:
        raise RuntimeError(
            "Billing needs the stripe package -- install with `pip install crdt-cad[billing]`."
        ) from exc
    return stripe


def _client():
    stripe = _import_stripe()
    secret = os.environ.get("CRDT_CAD_STRIPE_SECRET_KEY")
    if not secret:
        raise RuntimeError("CRDT_CAD_STRIPE_SECRET_KEY is not set -- billing is disabled on this server.")
    return stripe.StripeClient(secret)


def _ensure_customer(store: AccountStore, org: dict) -> str:
    """Reuses the org's existing Stripe customer if it already has one
    (so a canceled-then-resubscribed org doesn't accumulate duplicate
    Stripe customers), otherwise creates one."""
    if org.get("billing_customer_id"):
        return org["billing_customer_id"]
    stripe = _import_stripe()
    client = _client()
    try:
        customer = client.v1.customers.create({"name": org["name"], "metadata": {"org_id": org["org_id"]}})
    except stripe.StripeError as exc:
        raise RuntimeError(f"Stripe rejected the request: {exc.user_message or str(exc)}") from exc
    store.set_org_billing(org["org_id"], customer_id=customer["id"])
    return customer["id"]


def create_checkout_session(store: AccountStore, org: dict, success_url: str, cancel_url: str) -> str:
    """Returns the Stripe-hosted Checkout URL to redirect the browser
    to. The org's plan/status are deliberately NOT updated here -- a
    checkout redirect can be abandoned before payment completes; only
    the ``checkout.session.completed`` webhook (see
    :func:`handle_webhook_event`) is trusted to mean money changed
    hands."""
    pid = price_id()
    if not pid:
        raise RuntimeError("CRDT_CAD_STRIPE_PRICE_ID is not set -- no plan configured to check out into.")
    stripe = _import_stripe()
    client = _client()
    customer_id = _ensure_customer(store, org)
    try:
        session = client.v1.checkout.sessions.create({
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": pid, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"org_id": org["org_id"]},
        })
    except stripe.StripeError as exc:
        raise RuntimeError(f"Stripe rejected the request: {exc.user_message or str(exc)}") from exc
    return session["url"]


def create_portal_session(org: dict, return_url: str) -> str:
    """Returns the Stripe-hosted billing-portal URL -- an admin manages
    payment methods, invoices, and cancellation there directly; this
    app never touches card details itself."""
    if not org.get("billing_customer_id"):
        raise RuntimeError("this organization has no Stripe customer yet -- subscribe first.")
    stripe = _import_stripe()
    client = _client()
    try:
        session = client.v1.billing_portal.sessions.create({
            "customer": org["billing_customer_id"],
            "return_url": return_url,
        })
    except stripe.StripeError as exc:
        raise RuntimeError(f"Stripe rejected the request: {exc.user_message or str(exc)}") from exc
    return session["url"]


def verify_webhook(payload: bytes, sig_header: str):
    """Raises (via the real ``stripe`` package) if the signature doesn't
    verify against ``CRDT_CAD_STRIPE_WEBHOOK_SECRET`` -- the only
    thing standing between this endpoint and an attacker POSTing a
    forged "you're subscribed now" event."""
    try:
        import stripe
    except ImportError as exc:
        raise RuntimeError(
            "Billing needs the stripe package -- install with `pip install crdt-cad[billing]`."
        ) from exc
    secret = os.environ.get("CRDT_CAD_STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("CRDT_CAD_STRIPE_WEBHOOK_SECRET is not set -- cannot verify webhook signatures.")
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def handle_webhook_event(store: AccountStore, event) -> None:
    """Dispatches a verified Stripe event, syncing an org's plan/status.
    Unknown event types are ignored -- a real deployment only
    subscribes specific event types in the Stripe dashboard, so this is
    just a defensive no-op for anything unexpected that slips through."""
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        org_id = (obj.get("metadata") or {}).get("org_id")
        if not org_id:
            return
        store.set_org_billing(
            org_id, customer_id=obj.get("customer"), subscription_id=obj.get("subscription"),
            plan="pro", status="active",
        )
    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        customer_id = obj.get("customer")
        if not customer_id:
            return
        org = store.get_org_by_billing_customer_id(customer_id)
        if org is None:
            return
        status = obj.get("status")
        plan = "pro" if status in ("active", "trialing") else "free"
        store.set_org_billing(org["org_id"], subscription_id=obj.get("id"), plan=plan, status=status)
    elif event_type == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if not customer_id:
            return
        org = store.get_org_by_billing_customer_id(customer_id)
        if org is None:
            return
        store.set_org_billing(org["org_id"], plan="free", status="canceled")
