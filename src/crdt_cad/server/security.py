"""Optional security hardening for the relay server: shared-secret room
tokens, CORS lockdown, and rate limiting / resource ceilings.

Everything here is **disabled or wide-open by default** and only tightens
when its governing environment variable is set, so the zero-config local
demo experience (``git clone && pip install -e . && uvicorn ...``, no
secrets to manage) is completely unchanged unless a deployer opts in.

Every knob reads its environment variable fresh on each call rather than
caching it into a module-level constant at import time. This is what lets
tests flip behavior with a plain ``monkeypatch.setenv(...)`` instead of
needing to reload the module -- the one exception is :func:`cors_origins`,
whose result is baked into ``CORSMiddleware`` once at app-startup time
(a limitation of how Starlette's CORS middleware works, not of this
module); see the docstring on that function.
"""

from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_TOKEN_SALT = "crdt-cad-room-token"


# -- shared-secret room tokens ------------------------------------------------


def auth_secret() -> Optional[str]:
    return os.environ.get("CRDT_CAD_SECRET") or None


def auth_enabled() -> bool:
    return auth_secret() is not None


def token_max_age_seconds() -> int:
    return int(os.environ.get("CRDT_CAD_TOKEN_MAX_AGE_SECONDS", str(24 * 3600)))


def _serializer() -> URLSafeTimedSerializer:
    secret = auth_secret()
    if secret is None:
        raise RuntimeError("CRDT_CAD_SECRET is not configured -- token minting/verification is disabled")
    return URLSafeTimedSerializer(secret, salt=_TOKEN_SALT)


def secret_matches(candidate: str) -> bool:
    """Constant-time comparison against the configured secret (avoids a
    timing side-channel on the one endpoint that checks it directly)."""
    secret = auth_secret()
    if secret is None:
        return False
    return hmac.compare_digest(candidate, secret)


def mint_room_token(kind: str, room_id: str, role: str = "editor") -> str:
    """Signs a token scoped to one specific ``(kind, room_id)`` pair -- a
    token minted for one room grants no access to any other room.

    ``role`` (Phase 17) is either ``"editor"`` (full read/write, the only
    role that ever existed before this) or ``"viewer"`` (read-only --
    receives snapshots/deltas same as an editor, but any ``ops`` message
    from it is rejected server-side, see ``_handle_message`` in
    ``app.py``). Omitting it keeps minting an editor token, so every
    pre-Phase-17 call site (the shared-secret join flow) is unaffected.
    """
    return _serializer().dumps({"kind": kind, "room_id": room_id, "role": role})


def verify_room_token(token: Optional[str], kind: str, room_id: str) -> bool:
    """Always ``True`` when auth is disabled -- today's zero-config
    behavior for every caller of this function. Does not distinguish
    editor from viewer -- both are "a valid token for this room" as far
    as this function is concerned; see :func:`token_role` for the role
    itself."""
    if not auth_enabled():
        return True
    if not token:
        return False
    try:
        payload = _serializer().loads(token, max_age=token_max_age_seconds())
    except (BadSignature, SignatureExpired):
        return False
    return payload.get("kind") == kind and payload.get("room_id") == room_id


def token_role(token: Optional[str], kind: str, room_id: str) -> Optional[str]:
    """Returns the role (``"editor"`` or ``"viewer"``) a token grants for
    this room, or ``None`` if the token doesn't verify at all. When auth
    is disabled entirely, always ``"editor"`` -- there is no separate
    permission model to restrict against, matching every other function
    here's "wide open by default" behavior. A token minted before roles
    existed has no ``"role"`` key at all and defaults to ``"editor"``, so
    it keeps exactly the full access it always had.
    """
    if not auth_enabled():
        return "editor"
    if not verify_room_token(token, kind, room_id):
        return None
    payload = _serializer().loads(token, max_age=token_max_age_seconds())
    return payload.get("role", "editor")


def cors_origins() -> list[str]:
    """Explicit origin list via ``CRDT_CAD_CORS_ORIGINS`` (comma-separated)
    always wins. Otherwise: wide open (``["*"]``) when auth is off, since
    that's today's behavior and there's no secret to protect; locked to
    same-origin only (``[]``) once a secret is configured -- CORS only
    governs *cross*-origin requests, so the demo pages served by this same
    process are never affected either way.

    Unlike every other function in this module, this one is only ever
    consulted once, at process startup, when ``CORSMiddleware`` is
    constructed -- Starlette bakes the allowed-origins list into the
    middleware instance and has no mechanism to reconsider it per request.
    Flipping ``CRDT_CAD_SECRET``/``CRDT_CAD_CORS_ORIGINS`` on a already-running
    process will not change CORS behavior; restart the process.
    """
    env = os.environ.get("CRDT_CAD_CORS_ORIGINS")
    if env:
        return [o.strip() for o in env.split(",") if o.strip()]
    return [] if auth_enabled() else ["*"]


# -- client IP resolution ------------------------------------------------------


def trust_proxy_headers() -> bool:
    """Off by default: trusting ``X-Forwarded-For`` when the process is
    *not* actually behind a reverse proxy lets any client bypass the
    per-IP rate limiter below just by sending a different fake header
    value on every request. Set ``CRDT_CAD_TRUST_PROXY_HEADERS=1`` only
    when a reverse proxy you control (Caddy -- see docker-compose.prod.yml
    -- or the Kubernetes ingress) is the *sole* way to reach this process,
    so every connection's ``X-Forwarded-For`` is proxy-set, not
    client-supplied."""
    return os.environ.get("CRDT_CAD_TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")


def client_ip(request) -> str:
    """The address the per-IP rate limiter (``/generate``, see app.py)
    should charge. Behind a reverse proxy, ``request.client.host`` is
    always the *proxy's* address -- every real client would share one
    rate-limit bucket, letting a single abusive client exhaust the whole
    deployment's quota. When trusted (see :func:`trust_proxy_headers`),
    take the last hop of ``X-Forwarded-For``: Caddy/nginx/ingress-nginx
    all *append* the real connecting peer's address as the final entry
    rather than trusting whatever a client already put there, so the
    last entry is the one this process's immediate (trusted) proxy hop
    actually observed."""
    if trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            last_hop = forwarded.split(",")[-1].strip()
            if last_hop:
                return last_hop
    return request.client.host if request.client else "unknown"


# -- rate limiting -------------------------------------------------------------


@dataclass
class TokenBucket:
    """Refills continuously at ``rate`` tokens/sec up to ``capacity``;
    :meth:`allow` spends tokens or refuses if there aren't enough. Used both
    per-connection (WS ops/sec) and per-room (ops/minute) and per-IP
    (``/generate``)."""

    rate: float
    capacity: float
    tokens: float = field(init=False)
    _last: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self._last = time.monotonic()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def remaining(self) -> float:
        """Peeks at the current token count *without* spending -- applies
        the same continuous-refill formula :meth:`allow` uses, computed
        fresh from ``now`` each call, so it's accurate as of "right now"
        without mutating ``self.tokens``/``self._last`` (a pure read, safe
        to call as often as the UI wants -- e.g. every keystroke -- with
        no side effect on the actual budget)."""
        elapsed = time.monotonic() - self._last
        return min(self.capacity, self.tokens + elapsed * self.rate)


class PerKeyRateLimiter:
    """A :class:`TokenBucket` per arbitrary string key (e.g. client IP),
    created lazily on first use. Used for endpoints where there's no
    longer-lived object (like a `Room`) to hang a persistent bucket off of."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(rate=self._rate, capacity=self._capacity)
            self._buckets[key] = bucket
        return bucket.allow(cost)

    def remaining(self, key: str) -> float:
        """Peeks at `key`'s current budget without spending -- a key with
        no bucket yet has never made a request, so it's at full capacity."""
        bucket = self._buckets.get(key)
        return bucket.remaining() if bucket is not None else self._capacity

    def capacity(self) -> float:
        return self._capacity


def ws_ops_per_second() -> float:
    return float(os.environ.get("CRDT_CAD_WS_OPS_PER_SECOND", "200"))


def ws_ops_burst() -> float:
    return float(os.environ.get("CRDT_CAD_WS_OPS_BURST", "400"))


def new_ws_ops_bucket() -> TokenBucket:
    return TokenBucket(rate=ws_ops_per_second(), capacity=ws_ops_burst())


def max_ops_per_room_per_minute() -> int:
    return int(os.environ.get("CRDT_CAD_MAX_OPS_PER_ROOM_PER_MINUTE", "20000"))


def new_room_ops_bucket() -> TokenBucket:
    cap = max_ops_per_room_per_minute()
    return TokenBucket(rate=cap / 60.0, capacity=float(cap))


def generate_per_minute() -> float:
    return float(os.environ.get("CRDT_CAD_GENERATE_PER_MINUTE", "6"))


def generate_burst() -> float:
    return float(os.environ.get("CRDT_CAD_GENERATE_BURST", "3"))


generate_rate_limiter = PerKeyRateLimiter(rate=generate_per_minute() / 60.0, capacity=generate_burst())
"""Module-level singleton (per-IP buckets persist for the process
lifetime, like Room state does). Rate values are read once at import,
matching the existing project pattern for genuinely process-lifetime
singletons (e.g. ``store`` in ``app.py``); tests that need different
rates construct their own :class:`PerKeyRateLimiter` instance instead of
relying on this one."""


# -- resource ceilings ----------------------------------------------------------


def max_ws_message_bytes() -> int:
    return int(os.environ.get("CRDT_CAD_MAX_WS_MESSAGE_BYTES", "2000000"))


def max_ops_per_message() -> int:
    return int(os.environ.get("CRDT_CAD_MAX_OPS_PER_MESSAGE", "2000"))


def max_rooms_per_server() -> int:
    return int(os.environ.get("CRDT_CAD_MAX_ROOMS_PER_SERVER", "500"))


def max_clients_per_room() -> int:
    return int(os.environ.get("CRDT_CAD_MAX_CLIENTS_PER_ROOM", "50"))


def max_versions_per_room() -> int:
    """Phase 17 (version history): how many checkpoint snapshots
    `Room.checkpoint_version` keeps per room before pruning the oldest --
    bounds `room_versions` table growth the same way every other ceiling
    here bounds some other unbounded resource."""
    return int(os.environ.get("CRDT_CAD_MAX_VERSIONS_PER_ROOM", "20"))


class RoomLimitExceeded(Exception):
    """Raised by ``RoomManager.get_or_create`` when creating a brand new
    room would exceed :func:`max_rooms_per_server`. Never raised for an
    already-existing room -- the ceiling bounds distinct rooms, not access
    to rooms that already exist."""


class MessageTooLarge(Exception):
    """Raised when a raw incoming WebSocket frame exceeds
    :func:`max_ws_message_bytes`, before any JSON parsing is attempted."""
