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


def mint_room_token(kind: str, room_id: str) -> str:
    """Signs a token scoped to one specific ``(kind, room_id)`` pair -- a
    token minted for one room grants no access to any other room."""
    return _serializer().dumps({"kind": kind, "room_id": room_id})


def verify_room_token(token: Optional[str], kind: str, room_id: str) -> bool:
    """Always ``True`` when auth is disabled -- today's zero-config
    behavior for every caller of this function."""
    if not auth_enabled():
        return True
    if not token:
        return False
    try:
        payload = _serializer().loads(token, max_age=token_max_age_seconds())
    except (BadSignature, SignatureExpired):
        return False
    return payload.get("kind") == kind and payload.get("room_id") == room_id


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


class RoomLimitExceeded(Exception):
    """Raised by ``RoomManager.get_or_create`` when creating a brand new
    room would exceed :func:`max_rooms_per_server`. Never raised for an
    already-existing room -- the ceiling bounds distinct rooms, not access
    to rooms that already exist."""


class MessageTooLarge(Exception):
    """Raised when a raw incoming WebSocket frame exceeds
    :func:`max_ws_message_bytes`, before any JSON parsing is attempted."""
