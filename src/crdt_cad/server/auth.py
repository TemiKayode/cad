"""User accounts: magic-link + OAuth sign-in, server-side sessions
(Part 6, Phase P1).

Opt-in and layered *on top of* the existing room-token model, never
replacing it: ``CRDT_CAD_AUTH_MODE`` defaults to ``tokens``, in which
this module registers its routes but every one of them answers
"accounts are not enabled here" and **no account schema is ever
created**. The zero-config `docker compose up` experience is
byte-for-byte identical to before this module existed.

Sign-in methods (``CRDT_CAD_AUTH_MODE=accounts``):

- **Magic links** (always available in accounts mode): POST an e-mail
  address, receive a time-limited signed link, following it sets a
  session cookie. No passwords are ever stored or accepted. Links are
  signed with ``CRDT_CAD_SECRET`` (the same deployment secret the room
  tokens use -- accounts mode refuses to start without one).
- **OAuth** (optional, per provider): Google and GitHub, active only
  when that provider's client id/secret env vars are set. Implemented
  with ``authlib`` (the ``accounts`` extra) -- standard authorization-
  code flow, state kept in a signed cookie session; identity resolves
  to the provider-verified e-mail and lands in the same ``users`` row a
  magic link for that address would.

Sessions are server-side: the cookie holds a random token whose
**SHA-256 hash** is what's stored (a leaked accounts database cannot
mint working cookies), so "sign out" deletes a row and "sign out
everywhere" is a real operation.

Mail: real SMTP when ``CRDT_CAD_SMTP_HOST`` is configured; otherwise
the link is echoed to the server log (dev convenience). The link is
additionally returned in the API response **only** when
``CRDT_CAD_AUTH_DEV_ECHO=1`` -- never by default, because a deployment
that forgot SMTP must not hand any visitor a working sign-in link for
any address they type.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import smtplib
import time
from email.message import EmailMessage
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from crdt_cad.persistence.accounts import (
    AccountStore,
    PostgresAccountStore,
    SQLiteAccountStore,
)

logger = logging.getLogger("crdt_cad.server.auth")

SESSION_COOKIE = "crdt_cad_session"
_MAGIC_SALT = "crdt-cad-magic-link-v1"


# -- configuration -------------------------------------------------------------


def auth_mode() -> str:
    """``tokens`` (default -- accounts entirely inert) or ``accounts``."""
    return os.environ.get("CRDT_CAD_AUTH_MODE", "tokens").strip().lower()


def accounts_enabled() -> bool:
    return auth_mode() == "accounts"


def _secret() -> str:
    secret = os.environ.get("CRDT_CAD_SECRET", "")
    if not secret:
        # Reached only in accounts mode (call sites gate on accounts_enabled):
        # magic links must verify across restarts and replicas, so a random
        # per-process fallback would be silently broken -- fail loudly instead.
        raise HTTPException(
            status_code=503,
            detail="accounts mode requires CRDT_CAD_SECRET to be set (magic links are signed with it)",
        )
    return secret


def magic_link_max_age_seconds() -> int:
    return int(os.environ.get("CRDT_CAD_MAGIC_LINK_MAX_AGE_SECONDS", "900"))  # 15 min


def session_max_age_seconds() -> int:
    return int(os.environ.get("CRDT_CAD_SESSION_MAX_AGE_SECONDS", str(30 * 86400)))  # 30 days


def dev_echo_enabled() -> bool:
    return os.environ.get("CRDT_CAD_AUTH_DEV_ECHO", "").lower() in ("1", "true", "yes")


def oauth_providers_configured() -> dict[str, dict]:
    """Provider name -> client credentials, for every provider whose env
    vars are both present. Empty dict when none are (OAuth buttons simply
    don't render client-side)."""
    providers = {}
    for name in ("google", "github"):
        cid = os.environ.get(f"CRDT_CAD_OAUTH_{name.upper()}_CLIENT_ID")
        csecret = os.environ.get(f"CRDT_CAD_OAUTH_{name.upper()}_CLIENT_SECRET")
        if cid and csecret:
            providers[name] = {"client_id": cid, "client_secret": csecret}
    return providers


def platform_admin_emails() -> set[str]:
    """The operator's own bootstrap mechanism for the first admin(s):
    a comma-separated allowlist of e-mail addresses, never a database
    flag -- there is no chicken-and-egg "who grants the first admin"
    problem to solve, since the deployer already controls the process
    environment. Case-insensitive."""
    raw = os.environ.get("CRDT_CAD_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_platform_admin(user: Optional[dict]) -> bool:
    return bool(user) and user["email"].strip().lower() in platform_admin_emails()


# -- store selection -------------------------------------------------------------

_account_store: Optional[AccountStore] = None


def get_account_store() -> AccountStore:
    """Lazy singleton: the schema is created on first *use* in accounts
    mode, never merely by importing this module -- tokens-mode deployments
    keep a byte-for-byte identical database file."""
    global _account_store
    if _account_store is None:
        dsn = os.environ.get("CRDT_CAD_DATABASE_URL")
        if dsn:
            _account_store = PostgresAccountStore(dsn)
        else:
            from crdt_cad.server import app as app_module  # late: avoid import cycle

            _account_store = SQLiteAccountStore(app_module.DB_PATH)
    return _account_store


def reset_account_store_for_tests() -> None:
    global _account_store
    _account_store = None


# -- sessions -------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: str) -> str:
    """Mints a session, stores its hash, returns the raw cookie value.
    The only two call sites (magic-link verify, OAuth callback) are both
    a genuine "this account just signed in" moment, so this is also
    where Part 6 P3's pending org invites are accepted -- an invite sent
    to an address with no account yet is stored "pending" until the
    person it names actually shows up and proves it by signing in."""
    token = secrets.token_urlsafe(32)
    get_account_store().create_session(
        _hash_token(token), user_id, expires_at=time.time() + session_max_age_seconds()
    )
    get_account_store().activate_pending_memberships(user_id)
    return token


def current_user(request: Request) -> Optional[dict]:
    """The signed-in user for this request, or None. Never raises --
    endpoints that *require* a user do their own 401. Part 6 P4: a
    disabled account resolves to None here too -- disabling takes effect
    on the account's *existing* sessions immediately, not just future
    sign-in attempts, without needing any session-invalidation machinery
    of its own."""
    if not accounts_enabled():
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    store = get_account_store()
    sess = store.get_session(_hash_token(token))
    if not sess:
        return None
    store.touch_session(_hash_token(token))
    user = store.get_user(sess["user_id"])
    if user is None or user.get("disabled"):
        return None
    return user


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=session_max_age_seconds(),
        httponly=True,
        samesite="lax",
        # Secure behind TLS. request.url.scheme is "http" behind Caddy
        # unless proxy headers are honored -- uvicorn does honor
        # X-Forwarded-Proto with --proxy-headers (the Docker default CMD),
        # and localhost dev legitimately runs plain http.
        secure=request.url.scheme == "https",
        path="/",
    )


# -- magic links -------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=_MAGIC_SALT)


def mint_magic_token(email: str) -> str:
    return _serializer().dumps({"email": email.strip().lower()})


def verify_magic_token(token: str) -> str:
    """Returns the e-mail a valid token was minted for, or raises 400/410."""
    try:
        payload = _serializer().loads(token, max_age=magic_link_max_age_seconds())
    except SignatureExpired as exc:
        raise HTTPException(status_code=410, detail="this sign-in link has expired -- request a new one") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail="invalid sign-in link") from exc
    return payload["email"]


def _send_magic_link(email: str, link: str) -> bool:
    """Returns True if real mail was sent, False if only console-echoed."""
    host = os.environ.get("CRDT_CAD_SMTP_HOST")
    if not host:
        logger.info("magic link for %s (no SMTP configured, dev echo): %s", email, link)
        return False
    port = int(os.environ.get("CRDT_CAD_SMTP_PORT", "587"))
    user = os.environ.get("CRDT_CAD_SMTP_USER")
    password = os.environ.get("CRDT_CAD_SMTP_PASSWORD")
    sender = os.environ.get("CRDT_CAD_SMTP_FROM", user or f"crdt-cad@{host}")
    msg = EmailMessage()
    msg["Subject"] = "Your crdt-cad sign-in link"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(
        "Follow this link to sign in to crdt-cad:\n\n"
        f"    {link}\n\n"
        f"It expires in {magic_link_max_age_seconds() // 60} minutes. "
        "If you didn't request it, ignore this e-mail -- nothing happens without the link."
    )
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if os.environ.get("CRDT_CAD_SMTP_STARTTLS", "1").lower() in ("1", "true", "yes"):
            smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)
    return True


# -- routes -------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


# Deliberately not pydantic's EmailStr: that type requires the
# `email-validator` package -- a new hard dependency for what is, here,
# only a sanity gate (the real verification is that the magic link
# arrives at the address and gets clicked).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RequestLinkBody(BaseModel):
    email: str


def _require_accounts_mode() -> None:
    if not accounts_enabled():
        raise HTTPException(status_code=404, detail="accounts are not enabled on this deployment")


@router.get("/me")
async def me(request: Request) -> dict:
    """Always answers (both modes) -- the client uses this one call to
    decide whether to render any account UI at all."""
    user = current_user(request)
    return {
        "mode": auth_mode(),
        "signed_in": user is not None,
        "user": {
            "user_id": user["user_id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "avatar_color": user["avatar_color"],
        }
        if user
        else None,
        "oauth_providers": sorted(oauth_providers_configured()) if accounts_enabled() else [],
        "is_platform_admin": is_platform_admin(user),
    }


def _domain_requires_sso(email: str) -> Optional[dict]:
    """Part 6 P4 domain capture: an org can require every sign-in from
    its own e-mail domain to go through its configured SSO, not a magic
    link or generic OAuth. Returns that org, or None if no org captures
    this address's domain (the overwhelmingly common case -- most
    deployments never configure this at all)."""
    domain = email.rsplit("@", 1)[-1].lower()
    return get_account_store().get_org_by_sso_domain(domain)


def _reject_for_sso(org: dict) -> None:
    raise HTTPException(
        status_code=403,
        detail={
            "error": "sso_required",
            "org_id": org["org_id"],
            "org_name": org["name"],
            "sso_start_url": f"/api/auth/sso/{org['org_id']}/start",
        },
    )


@router.post("/request-link")
async def request_link(body: RequestLinkBody, request: Request) -> dict:
    _require_accounts_mode()
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="that does not look like an e-mail address")
    captured_by = _domain_requires_sso(email)
    if captured_by is not None:
        _reject_for_sso(captured_by)
    token = mint_magic_token(email)
    link = str(request.base_url).rstrip("/") + f"/api/auth/verify?token={token}"
    sent = await asyncio.to_thread(_send_magic_link, email, link)
    result: dict = {"sent": sent}
    if not sent and dev_echo_enabled():
        # Explicit opt-in only -- see module docstring for why this must
        # never be the no-SMTP default.
        result["dev_link"] = link
    return result


@router.get("/verify")
async def verify(token: str, request: Request) -> RedirectResponse:
    _require_accounts_mode()
    email = verify_magic_token(token)
    captured_by = _domain_requires_sso(email)
    if captured_by is not None:
        _reject_for_sso(captured_by)
    user = get_account_store().create_or_get_user(email)
    if user.get("disabled"):
        raise HTTPException(status_code=403, detail="this account has been disabled")
    session_token = create_session(user["user_id"])
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, session_token, request)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    _require_accounts_mode()
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        get_account_store().delete_session(_hash_token(token))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/logout-everywhere")
async def logout_everywhere(request: Request) -> dict:
    _require_accounts_mode()
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    count = get_account_store().delete_user_sessions(user["user_id"])
    return {"sessions_removed": count}


class ProfileBody(BaseModel):
    display_name: Optional[str] = None
    avatar_color: Optional[str] = None


@router.patch("/profile")
async def update_profile(body: ProfileBody, request: Request) -> dict:
    _require_accounts_mode()
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="not signed in")
    name = body.display_name.strip()[:60] if body.display_name else None
    get_account_store().update_profile(user["user_id"], display_name=name, avatar_color=body.avatar_color)
    return {"ok": True}


# -- OAuth (optional, env-gated, needs the `accounts` extra) --------------------

_oauth_registry = None


def _get_oauth():
    """Lazy authlib registry over the configured providers. Import errors
    surface as a clear 503 naming the extra, mirroring PostgresStore's
    missing-asyncpg message."""
    global _oauth_registry
    if _oauth_registry is not None:
        return _oauth_registry
    try:
        from authlib.integrations.starlette_client import OAuth
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail="OAuth needs authlib -- install with `pip install crdt-cad[accounts]`",
        ) from exc
    oauth = OAuth()
    providers = oauth_providers_configured()
    if "google" in providers:
        oauth.register(
            name="google",
            client_id=providers["google"]["client_id"],
            client_secret=providers["google"]["client_secret"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    if "github" in providers:
        oauth.register(
            name="github",
            client_id=providers["github"]["client_id"],
            client_secret=providers["github"]["client_secret"],
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    _oauth_registry = oauth
    return oauth


def _require_provider(provider: str):
    if provider not in oauth_providers_configured():
        raise HTTPException(status_code=404, detail=f"OAuth provider {provider!r} is not configured here")
    client = getattr(_get_oauth(), provider, None)
    if client is None:
        raise HTTPException(status_code=404, detail=f"OAuth provider {provider!r} is not configured here")
    return client


@router.get("/oauth/{provider}/start")
async def oauth_start(provider: str, request: Request):
    _require_accounts_mode()
    client = _require_provider(provider)
    redirect_uri = str(request.base_url).rstrip("/") + f"/api/auth/oauth/{provider}/callback"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/oauth/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> RedirectResponse:
    _require_accounts_mode()
    client = _require_provider(provider)
    token = await client.authorize_access_token(request)

    email: Optional[str] = None
    name: Optional[str] = None
    if provider == "google":
        info = token.get("userinfo") or {}
        if info.get("email_verified"):
            email = info.get("email")
        name = info.get("name")
    elif provider == "github":
        profile = (await client.get("user", token=token)).json()
        name = profile.get("name") or profile.get("login")
        emails = (await client.get("user/emails", token=token)).json()
        for entry in emails if isinstance(emails, list) else []:
            if entry.get("primary") and entry.get("verified"):
                email = entry.get("email")
                break
    if not email:
        raise HTTPException(status_code=403, detail=f"{provider} did not supply a verified e-mail address")
    captured_by = _domain_requires_sso(email)
    if captured_by is not None:
        # An org that's captured this domain wants identity to flow
        # through *its own* SSO, not a generic Google/GitHub sign-in --
        # even one that happens to use the same address.
        _reject_for_sso(captured_by)

    user = get_account_store().create_or_get_user(email, display_name=name)
    if user.get("disabled"):
        raise HTTPException(status_code=403, detail="this account has been disabled")
    session_token = create_session(user["user_id"])
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, session_token, request)
    return response


# -- OIDC SSO (optional, per-org, needs the `accounts` extra) -------------------
#
# Distinct from the fixed Google/GitHub providers above: any org can
# configure its *own* issuer/client (Okta, Entra, Google Workspace --
# anything that speaks standard OIDC discovery), via
# POST /api/orgs/{org_id}/sso (see app.py, org-admin only; the client
# secret is stored but never echoed back once set). SAML is explicitly
# out of scope: OIDC covers the realistic self-host audience, and this
# project doesn't claim SAML support it has no real IdP to verify
# against -- see the README.

_org_oidc_clients: dict[str, object] = {}
_org_oidc_registry = None


def _get_org_oidc_client(org_id: str, org: dict):
    """Lazily registers (once per org_id) and returns an authlib client
    for that org's own OIDC issuer -- the same lazy-registry pattern
    ``_get_oauth`` uses for Google/GitHub, just keyed dynamically by org
    instead of a fixed small set of provider names. Cached so a repeat
    sign-in doesn't re-fetch the issuer's discovery document every time."""
    global _org_oidc_registry
    if org_id in _org_oidc_clients:
        return _org_oidc_clients[org_id]
    if _org_oidc_registry is None:
        try:
            from authlib.integrations.starlette_client import OAuth
        except ImportError as exc:
            raise HTTPException(
                status_code=503,
                detail="SSO needs authlib -- install with `pip install crdt-cad[accounts]`",
            ) from exc
        _org_oidc_registry = OAuth()
    name = f"org_{org_id}"
    _org_oidc_registry.register(
        name=name,
        client_id=org["sso_client_id"],
        client_secret=org["sso_client_secret"],
        server_metadata_url=org["sso_issuer"].rstrip("/") + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    client = getattr(_org_oidc_registry, name)
    _org_oidc_clients[org_id] = client
    return client


def forget_org_oidc_client(org_id: str) -> None:
    """Drops a cached authlib client so the next sign-in re-reads the
    org's current SSO config, instead of a stale one from before an
    admin changed or cleared it."""
    _org_oidc_clients.pop(org_id, None)


def _require_org_sso(org_id: str) -> dict:
    org = get_account_store().get_org(org_id)
    if org is None or not (org.get("sso_issuer") and org.get("sso_client_id") and org.get("sso_client_secret")):
        raise HTTPException(status_code=404, detail="this organization has no SSO configured")
    return org


@router.get("/sso/{org_id}/start")
async def sso_start(org_id: str, request: Request):
    _require_accounts_mode()
    org = _require_org_sso(org_id)
    client = _get_org_oidc_client(org_id, org)
    redirect_uri = str(request.base_url).rstrip("/") + f"/api/auth/sso/{org_id}/callback"
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/sso/{org_id}/callback")
async def sso_callback(org_id: str, request: Request) -> RedirectResponse:
    _require_accounts_mode()
    org = _require_org_sso(org_id)
    client = _get_org_oidc_client(org_id, org)
    token = await client.authorize_access_token(request)
    info = token.get("userinfo") or {}
    # Unlike the Google path above, a missing email_verified claim isn't
    # treated as untrusted here: the org's own admin deliberately chose
    # this issuer for their organization, a materially different trust
    # model than "any visitor signing up with a Google account."
    if not info.get("email") or info.get("email_verified") is False:
        raise HTTPException(status_code=403, detail="the identity provider did not supply a verified e-mail address")
    email = info["email"].strip().lower()
    name = info.get("name")

    # Defense in depth: if a *different* org has captured this domain,
    # this org's own SSO shouldn't be a backdoor around that rule.
    captured_by = _domain_requires_sso(email)
    if captured_by is not None and captured_by["org_id"] != org_id:
        _reject_for_sso(captured_by)

    store = get_account_store()
    user = store.create_or_get_user(email, display_name=name)
    if user.get("disabled"):
        raise HTTPException(status_code=403, detail="this account has been disabled")

    # Signing in through an org's own SSO implies membership: default to
    # "member" the first time, leave an existing role (e.g. admin, or a
    # membership from before SSO was configured) untouched on repeat
    # sign-ins.
    if store.get_org_membership(org_id, user["user_id"]) is None:
        store.invite_org_member(org_id, email, role="member")
        store.activate_pending_memberships(user["user_id"])

    session_token = create_session(user["user_id"])
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, session_token, request)
    return response
