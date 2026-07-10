"""Durable storage for user accounts and sign-in sessions (Part 6, P1).

Deliberately a *separate* store from :mod:`crdt_cad.persistence.store`
(room snapshots): accounts are optional -- the zero-config deployment
never creates this schema at all (``CRDT_CAD_AUTH_MODE`` defaults to
``tokens``, see :mod:`crdt_cad.server.auth`) -- and they have a
different lifecycle (a user outlives any one room; deleting a user must
never delete a room's CRDT history, only anonymize attribution).

Same backend split as room snapshots, for the same reasons: SQLite by
default (zero infrastructure), Postgres via ``asyncpg`` when several
server processes must share one user base (the k8s Mode B deployment),
and an in-memory implementation for tests.

What is stored, and what deliberately is not:

- ``users``: id, e-mail (unique, lowercased), display name, avatar
  color, created_at. **No passwords, ever** -- sign-in is magic links
  and OAuth only (see the brief's "never store what you can't protect").
- ``sessions``: a **hash** of the session token (never the token itself
  -- a leaked database must not mint working cookies), the user it
  belongs to, expiry, and last-seen. Sessions are server-side so
  "sign out everywhere" is a real operation, not a client-side hope.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


def _user_row_to_dict(row) -> dict:
    return {
        "user_id": row[0],
        "email": row[1],
        "display_name": row[2],
        "avatar_color": row[3],
        "created_at": row[4],
        "disabled": bool(row[5]) if len(row) > 5 else False,
    }


class AccountStore:
    """Interface every accounts backend implements. All methods are
    synchronous (callers route through ``asyncio.to_thread`` where it
    matters), mirroring :class:`crdt_cad.persistence.store.DocumentStore`."""

    # -- users ------------------------------------------------------------

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        """Returns the existing user for ``email`` or creates one. E-mail
        is the identity key (magic links and OAuth both resolve to a
        verified e-mail), normalized to lowercase."""
        raise NotImplementedError

    def get_user(self, user_id: str) -> Optional[dict]:
        raise NotImplementedError

    def get_user_by_email(self, email: str) -> Optional[dict]:
        raise NotImplementedError

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        """Updates only the fields given (None = leave unchanged).
        Returns False if the user doesn't exist."""
        raise NotImplementedError

    def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        """Part 6 P4: a disabled account can't sign in (checked at every
        sign-in completion point) and an existing session stops resolving
        immediately (checked in ``current_user``). Returns False if the
        user doesn't exist."""
        raise NotImplementedError

    def list_all_users(self) -> list[dict]:
        """Every user, for the admin panel."""
        raise NotImplementedError

    # -- sessions ---------------------------------------------------------

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        raise NotImplementedError

    def get_session(self, token_hash: str) -> Optional[dict]:
        """Returns ``{"user_id", "expires_at", "created_at", "last_seen"}``
        or None. Expired sessions are treated as absent (and reaped)."""
        raise NotImplementedError

    def touch_session(self, token_hash: str) -> None:
        raise NotImplementedError

    def delete_session(self, token_hash: str) -> None:
        raise NotImplementedError

    def delete_user_sessions(self, user_id: str) -> int:
        """Sign out everywhere. Returns how many sessions were removed."""
        raise NotImplementedError

    # -- room ownership & per-user grants (Part 6, P2) ---------------------

    def claim_room(self, kind: str, room_id: str, owner_user_id: str, visibility: str = "private") -> bool:
        """Registers ``owner_user_id`` as this room's owner -- called once,
        the first time a signed-in user opens a brand-new room (no
        persisted CRDT content yet and no existing ownership row; see
        app.py's claim-on-first-touch in ``_serve_room``). A no-op if the
        room already has an owner (first claim wins; returns False so a
        raced double claim can't silently steal ownership from whoever
        got there first). Pre-existing rooms opened before accounts mode
        existed are never retroactively claimed this way -- they stay
        ownerless-public until an admin tool claims them (P4, not yet
        built) -- see :meth:`get_room_ownership`."""
        raise NotImplementedError

    def get_room_ownership(self, kind: str, room_id: str) -> Optional[dict]:
        """Returns ``{"owner_user_id", "visibility", "owner_org_id"}``
        (the last one None unless :meth:`transfer_room_to_org` has been
        called -- Part 6 P3), or None if this room has no owner at all --
        true for every room predating this phase, and for any room never
        opened by a signed-in user. An unowned room is treated as fully
        public, identical to today's behavior."""
        raise NotImplementedError

    def set_room_visibility(self, kind: str, room_id: str, visibility: str) -> bool:
        """Returns False if the room has no owner yet (nothing to set)."""
        raise NotImplementedError

    def set_room_grant(self, kind: str, room_id: str, user_id: str, role: str) -> None:
        """Grants (or updates) one user's role on one room. Idempotent."""
        raise NotImplementedError

    def revoke_room_grant(self, kind: str, room_id: str, user_id: str) -> None:
        raise NotImplementedError

    def get_room_grant(self, kind: str, room_id: str, user_id: str) -> Optional[str]:
        raise NotImplementedError

    def list_room_grants(self, kind: str, room_id: str) -> list[dict]:
        """``[{"user_id", "email", "display_name", "role"}, ...]`` for a
        sharing UI -- joined against ``users`` so the owner sees who
        they've invited by name, not by opaque id."""
        raise NotImplementedError

    def list_owned_rooms(self, user_id: str) -> list[dict]:
        """``[{"kind", "room_id", "visibility"}, ...]`` -- the home page's
        "your documents"."""
        raise NotImplementedError

    def list_granted_rooms(self, user_id: str) -> list[dict]:
        """``[{"kind", "room_id", "role"}, ...]`` -- the home page's
        "shared with you"."""
        raise NotImplementedError

    # -- organizations & teams (Part 6, P3) ---------------------------------

    def create_org(self, name: str, created_by_user_id: str) -> dict:
        """Creates an org and adds its creator as an active admin member
        in one step -- there is no "orgless" moment. Returns
        ``{"org_id", "name", "created_by", "created_at",
        "default_visibility", "allowed_share_link_roles"}``."""
        raise NotImplementedError

    def get_org(self, org_id: str) -> Optional[dict]:
        raise NotImplementedError

    def set_org_defaults(
        self, org_id: str, default_visibility: Optional[str] = None,
        allowed_share_link_roles: Optional[list[str]] = None,
    ) -> bool:
        """Updates only the fields given. Returns False if the org doesn't
        exist."""
        raise NotImplementedError

    def list_orgs_for_user(self, user_id: str) -> list[dict]:
        """Active memberships only -- ``[{"org_id", "name", "role"}, ...]``."""
        raise NotImplementedError

    def invite_org_member(self, org_id: str, email: str, role: str = "member") -> tuple[dict, str]:
        """Creates (or reuses) the invitee's account and adds them as a
        member -- ``"active"`` immediately if they already had an account
        (they're a known, reachable person), ``"pending"`` if this invite
        is what created their account (the brief's "pending-invite
        state": consumed automatically the first time they actually sign
        in -- see :meth:`activate_pending_memberships`). Returns
        ``(user, status)``. Idempotent: re-inviting an existing member
        updates their role instead of erroring."""
        raise NotImplementedError

    def activate_pending_memberships(self, user_id: str) -> None:
        """Flips every ``"pending"`` membership for this user to
        ``"active"`` -- called once, right after their first successful
        sign-in (magic-link verify / OAuth callback), the moment a
        pending invite is genuinely accepted."""
        raise NotImplementedError

    def set_org_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        raise NotImplementedError

    def remove_org_member(self, org_id: str, user_id: str) -> None:
        raise NotImplementedError

    def get_org_membership(self, org_id: str, user_id: str) -> Optional[dict]:
        """``{"role", "status"}`` or None."""
        raise NotImplementedError

    def list_org_members(self, org_id: str) -> list[dict]:
        """``[{"user_id", "email", "display_name", "role", "status"}, ...]``
        for the org settings page."""
        raise NotImplementedError

    def count_org_admins(self, org_id: str) -> int:
        """Active admins only -- used to refuse the last admin leaving or
        demoting themself, so an org can never end up ownerless."""
        raise NotImplementedError

    def transfer_room_to_org(self, kind: str, room_id: str, org_id: str) -> bool:
        """Reassigns an already-claimed room's ``owner_org_id``. Returns
        False if the room has no owner yet (nothing to transfer)."""
        raise NotImplementedError

    # -- SSO (Part 6, P4) -----------------------------------------------------

    def set_org_sso(
        self, org_id: str, issuer: Optional[str], client_id: Optional[str],
        client_secret: Optional[str], domain: Optional[str],
    ) -> bool:
        """Passing all four as None clears SSO configuration entirely.
        Returns False if the org doesn't exist."""
        raise NotImplementedError

    def get_org_by_sso_domain(self, domain: str) -> Optional[dict]:
        """The org whose ``sso_domain`` matches (case-insensitive) and has
        a fully configured issuer/client, or None -- used to enforce
        domain capture: an e-mail at a captured domain must sign in
        through that org's own SSO, not a magic link or generic OAuth."""
        raise NotImplementedError

    # -- per-account quotas (Part 6, P4) ---------------------------------------

    def increment_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        """Increments and returns the new usage count for
        ``(user_id, kind, day)`` -- e.g. ``kind="generation"``,
        ``day="2026-07-10"``. The caller decides what counts as "a day";
        this just atomically bumps a counter and returns the new total."""
        raise NotImplementedError

    def get_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        raise NotImplementedError

    # -- admin panel (Part 6, P4) -----------------------------------------------

    def list_all_orgs(self) -> list[dict]:
        """Every org, for the admin panel."""
        raise NotImplementedError

    def list_all_owned_rooms(self) -> list[dict]:
        """Every room with an owner (personal or org), for the admin
        panel -- ``[{"kind","room_id","owner_user_id","owner_org_id",
        "visibility"}]``. Ownerless rooms aren't tracked here at all (see
        the room store's own listing for those)."""
        raise NotImplementedError

    def admin_claim_room(self, kind: str, room_id: str, owner_user_id: str) -> bool:
        """Unlike :meth:`claim_room` (first-touch only, private by
        default), an admin can claim any currently-ownerless room
        explicitly, defaulting it to ``public`` visibility (an admin
        claiming an abandoned room isn't making it private by surprise).
        Returns False if the room already has an owner."""
        raise NotImplementedError

    def delete_room_ownership(self, kind: str, room_id: str) -> None:
        """Removes ownership and every grant for a room -- called when
        an admin deletes the room's content outright."""
        raise NotImplementedError

    # -- notifications (Part 6, P5) ----------------------------------------------

    def create_notification(self, user_id: str, kind: str, payload: dict) -> dict:
        """``kind`` is e.g. ``"mention"`` or ``"room_shared"``; ``payload``
        is whatever that kind needs to render itself (room id/kind,
        who triggered it, a snippet of text) -- opaque to the store,
        round-tripped as-is. Returns the created row, including its new
        ``notification_id`` and ``created_at``."""
        raise NotImplementedError

    def list_notifications(self, user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        """Newest first. Each row: ``{"notification_id", "user_id",
        "kind", "payload", "created_at", "read"}``."""
        raise NotImplementedError

    def mark_notification_read(self, notification_id: str, user_id: str) -> bool:
        """``user_id`` must match the notification's own owner -- one
        user can never mark another's notification read. Returns False
        if no matching, unowned-by-someone-else row exists."""
        raise NotImplementedError

    def mark_all_notifications_read(self, user_id: str) -> int:
        """Returns how many were newly marked read."""
        raise NotImplementedError

    def count_unread_notifications(self, user_id: str) -> int:
        raise NotImplementedError

    # -- per-room activity feed (Part 6, P5) --------------------------------------

    def log_activity(self, room_kind: str, room_id: str, actor_user_id: Optional[str], kind: str, payload: dict) -> None:
        """Appends one entry to a room's activity feed -- e.g.
        ``kind="comment_added"``, ``"visibility_changed"``,
        ``"room_shared"``, ``"transferred_to_org"``,
        ``"generation_completed"``. ``actor_user_id`` is None when the
        actor isn't signed in (a comment from a guest actor in
        tokens-only mode still writes a comment, just with no stable
        identity to log against)."""
        raise NotImplementedError

    def list_activity(self, room_kind: str, room_id: str, limit: int = 50) -> list[dict]:
        """Newest first. Each row: ``{"activity_id", "room_kind",
        "room_id", "actor_user_id", "kind", "payload", "created_at"}``."""
        raise NotImplementedError


class InMemoryAccountStore(AccountStore):
    """Non-durable accounts store used in tests."""

    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._by_email: dict[str, str] = {}
        self._sessions: dict[str, dict] = {}
        self._ownership: dict[tuple[str, str], dict] = {}
        self._grants: dict[tuple[str, str, str], str] = {}
        self._orgs: dict[str, dict] = {}
        self._org_members: dict[tuple[str, str], dict] = {}
        self._quota_usage: dict[tuple[str, str, str], int] = {}
        self._notifications: dict[str, dict] = {}
        self._activity: list[dict] = []

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()
        if email in self._by_email:
            return dict(self._users[self._by_email[email]])
        user = {
            "user_id": uuid.uuid4().hex,
            "email": email,
            "display_name": display_name or email.split("@")[0],
            "avatar_color": None,
            "created_at": time.time(),
            "disabled": False,
        }
        self._users[user["user_id"]] = user
        self._by_email[email] = user["user_id"]
        return dict(user)

    def get_user(self, user_id: str) -> Optional[dict]:
        user = self._users.get(user_id)
        return dict(user) if user else None

    def get_user_by_email(self, email: str) -> Optional[dict]:
        user_id = self._by_email.get(email.strip().lower())
        return self.get_user(user_id) if user_id else None

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        if display_name is not None:
            user["display_name"] = display_name
        if avatar_color is not None:
            user["avatar_color"] = avatar_color
        return True

    def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        user = self._users.get(user_id)
        if not user:
            return False
        user["disabled"] = disabled
        return True

    def list_all_users(self) -> list[dict]:
        return [dict(u) for u in self._users.values()]

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        now = time.time()
        self._sessions[token_hash] = {
            "user_id": user_id,
            "expires_at": expires_at,
            "created_at": now,
            "last_seen": now,
        }

    def get_session(self, token_hash: str) -> Optional[dict]:
        sess = self._sessions.get(token_hash)
        if not sess:
            return None
        if sess["expires_at"] < time.time():
            del self._sessions[token_hash]
            return None
        return dict(sess)

    def touch_session(self, token_hash: str) -> None:
        if token_hash in self._sessions:
            self._sessions[token_hash]["last_seen"] = time.time()

    def delete_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id: str) -> int:
        doomed = [h for h, s in self._sessions.items() if s["user_id"] == user_id]
        for h in doomed:
            del self._sessions[h]
        return len(doomed)

    # -- room ownership & per-user grants -----------------------------------

    def claim_room(self, kind: str, room_id: str, owner_user_id: str, visibility: str = "private") -> bool:
        key = (kind, room_id)
        if key in self._ownership:
            return False
        self._ownership[key] = {"owner_user_id": owner_user_id, "visibility": visibility, "owner_org_id": None}
        return True

    def get_room_ownership(self, kind: str, room_id: str) -> Optional[dict]:
        row = self._ownership.get((kind, room_id))
        return dict(row) if row else None

    def set_room_visibility(self, kind: str, room_id: str, visibility: str) -> bool:
        row = self._ownership.get((kind, room_id))
        if not row:
            return False
        row["visibility"] = visibility
        return True

    def set_room_grant(self, kind: str, room_id: str, user_id: str, role: str) -> None:
        self._grants[(kind, room_id, user_id)] = role

    def revoke_room_grant(self, kind: str, room_id: str, user_id: str) -> None:
        self._grants.pop((kind, room_id, user_id), None)

    def get_room_grant(self, kind: str, room_id: str, user_id: str) -> Optional[str]:
        return self._grants.get((kind, room_id, user_id))

    def list_room_grants(self, kind: str, room_id: str) -> list[dict]:
        out = []
        for (k, r, user_id), role in self._grants.items():
            if k == kind and r == room_id:
                user = self.get_user(user_id) or {}
                out.append({
                    "user_id": user_id, "email": user.get("email"),
                    "display_name": user.get("display_name"), "role": role,
                })
        return out

    def list_owned_rooms(self, user_id: str) -> list[dict]:
        return [
            {"kind": k, "room_id": r, "visibility": row["visibility"]}
            for (k, r), row in self._ownership.items() if row["owner_user_id"] == user_id
        ]

    def list_granted_rooms(self, user_id: str) -> list[dict]:
        return [
            {"kind": k, "room_id": r, "role": role}
            for (k, r, u), role in self._grants.items() if u == user_id
        ]

    # -- organizations & teams -----------------------------------------------

    def create_org(self, name: str, created_by_user_id: str) -> dict:
        org_id = uuid.uuid4().hex
        org = {
            "org_id": org_id, "name": name, "created_by": created_by_user_id, "created_at": time.time(),
            "default_visibility": "private", "allowed_share_link_roles": ["viewer", "editor"],
            "sso_issuer": None, "sso_client_id": None, "sso_client_secret": None, "sso_domain": None,
        }
        self._orgs[org_id] = org
        self._org_members[(org_id, created_by_user_id)] = {"role": "admin", "status": "active"}
        return dict(org)

    def get_org(self, org_id: str) -> Optional[dict]:
        org = self._orgs.get(org_id)
        return dict(org) if org else None

    def set_org_defaults(
        self, org_id: str, default_visibility: Optional[str] = None,
        allowed_share_link_roles: Optional[list[str]] = None,
    ) -> bool:
        org = self._orgs.get(org_id)
        if not org:
            return False
        if default_visibility is not None:
            org["default_visibility"] = default_visibility
        if allowed_share_link_roles is not None:
            org["allowed_share_link_roles"] = list(allowed_share_link_roles)
        return True

    def list_orgs_for_user(self, user_id: str) -> list[dict]:
        out = []
        for (org_id, uid), m in self._org_members.items():
            if uid == user_id and m["status"] == "active":
                org = self._orgs.get(org_id)
                if org:
                    out.append({"org_id": org_id, "name": org["name"], "role": m["role"]})
        return out

    def invite_org_member(self, org_id: str, email: str, role: str = "member") -> tuple[dict, str]:
        existing = self.get_user_by_email(email)
        user = self.create_or_get_user(email)
        key = (org_id, user["user_id"])
        if key in self._org_members:
            self._org_members[key]["role"] = role
            return user, self._org_members[key]["status"]
        status = "active" if existing else "pending"
        self._org_members[key] = {"role": role, "status": status}
        return user, status

    def activate_pending_memberships(self, user_id: str) -> None:
        for key, m in self._org_members.items():
            if key[1] == user_id and m["status"] == "pending":
                m["status"] = "active"

    def set_org_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        key = (org_id, user_id)
        if key not in self._org_members:
            return False
        self._org_members[key]["role"] = role
        return True

    def remove_org_member(self, org_id: str, user_id: str) -> None:
        self._org_members.pop((org_id, user_id), None)

    def get_org_membership(self, org_id: str, user_id: str) -> Optional[dict]:
        m = self._org_members.get((org_id, user_id))
        return dict(m) if m else None

    def list_org_members(self, org_id: str) -> list[dict]:
        out = []
        for (oid, user_id), m in self._org_members.items():
            if oid == org_id:
                user = self.get_user(user_id) or {}
                out.append({
                    "user_id": user_id, "email": user.get("email"), "display_name": user.get("display_name"),
                    "role": m["role"], "status": m["status"],
                })
        return out

    def count_org_admins(self, org_id: str) -> int:
        return sum(
            1 for (oid, _), m in self._org_members.items()
            if oid == org_id and m["role"] == "admin" and m["status"] == "active"
        )

    def transfer_room_to_org(self, kind: str, room_id: str, org_id: str) -> bool:
        row = self._ownership.get((kind, room_id))
        if not row:
            return False
        row["owner_org_id"] = org_id
        return True

    # -- SSO ------------------------------------------------------------------

    def set_org_sso(
        self, org_id: str, issuer: Optional[str], client_id: Optional[str],
        client_secret: Optional[str], domain: Optional[str],
    ) -> bool:
        org = self._orgs.get(org_id)
        if not org:
            return False
        org["sso_issuer"] = issuer
        org["sso_client_id"] = client_id
        org["sso_client_secret"] = client_secret
        org["sso_domain"] = domain.strip().lower() if domain else None
        return True

    def get_org_by_sso_domain(self, domain: str) -> Optional[dict]:
        domain = domain.strip().lower()
        for org in self._orgs.values():
            if (
                org.get("sso_domain") == domain
                and org.get("sso_issuer") and org.get("sso_client_id") and org.get("sso_client_secret")
            ):
                return dict(org)
        return None

    # -- per-account quotas -----------------------------------------------------

    def increment_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        key = (user_id, kind, day)
        self._quota_usage[key] = self._quota_usage.get(key, 0) + 1
        return self._quota_usage[key]

    def get_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        return self._quota_usage.get((user_id, kind, day), 0)

    # -- admin panel --------------------------------------------------------

    def list_all_orgs(self) -> list[dict]:
        return [dict(o) for o in self._orgs.values()]

    def list_all_owned_rooms(self) -> list[dict]:
        return [
            {"kind": k, "room_id": r, "owner_user_id": row["owner_user_id"],
             "owner_org_id": row.get("owner_org_id"), "visibility": row["visibility"]}
            for (k, r), row in self._ownership.items()
        ]

    def admin_claim_room(self, kind: str, room_id: str, owner_user_id: str) -> bool:
        key = (kind, room_id)
        if key in self._ownership:
            return False
        self._ownership[key] = {"owner_user_id": owner_user_id, "visibility": "public", "owner_org_id": None}
        return True

    def delete_room_ownership(self, kind: str, room_id: str) -> None:
        self._ownership.pop((kind, room_id), None)
        for key in [k for k in self._grants if k[0] == kind and k[1] == room_id]:
            del self._grants[key]

    # -- notifications --------------------------------------------------------

    def create_notification(self, user_id: str, kind: str, payload: dict) -> dict:
        row = {
            "notification_id": uuid.uuid4().hex,
            "user_id": user_id,
            "kind": kind,
            "payload": dict(payload),
            "created_at": time.time(),
            "read": False,
        }
        self._notifications[row["notification_id"]] = row
        return dict(row)

    def list_notifications(self, user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        rows = [
            dict(r) for r in self._notifications.values()
            if r["user_id"] == user_id and (not unread_only or not r["read"])
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def mark_notification_read(self, notification_id: str, user_id: str) -> bool:
        row = self._notifications.get(notification_id)
        if row is None or row["user_id"] != user_id:
            return False
        row["read"] = True
        return True

    def mark_all_notifications_read(self, user_id: str) -> int:
        count = 0
        for row in self._notifications.values():
            if row["user_id"] == user_id and not row["read"]:
                row["read"] = True
                count += 1
        return count

    def count_unread_notifications(self, user_id: str) -> int:
        return sum(1 for r in self._notifications.values() if r["user_id"] == user_id and not r["read"])

    # -- per-room activity feed -------------------------------------------------

    def log_activity(self, room_kind: str, room_id: str, actor_user_id: Optional[str], kind: str, payload: dict) -> None:
        self._activity.append({
            "activity_id": uuid.uuid4().hex,
            "room_kind": room_kind,
            "room_id": room_id,
            "actor_user_id": actor_user_id,
            "kind": kind,
            "payload": dict(payload),
            "created_at": time.time(),
        })

    def list_activity(self, room_kind: str, room_id: str, limit: int = 50) -> list[dict]:
        rows = [
            dict(r) for r in self._activity
            if r["room_kind"] == room_kind and r["room_id"] == room_id
        ]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]


_USERS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    avatar_color TEXT,
    created_at REAL NOT NULL
)
"""

_SESSIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
)
"""

_ROOM_OWNERSHIP_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS room_ownership (
    kind TEXT NOT NULL,
    room_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'private',
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, room_id)
)
"""

_ROOM_GRANTS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS room_grants (
    kind TEXT NOT NULL,
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    granted_at REAL NOT NULL,
    PRIMARY KEY (kind, room_id, user_id)
)
"""

_ORGS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS orgs (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    default_visibility TEXT NOT NULL DEFAULT 'private',
    allowed_share_link_roles TEXT NOT NULL DEFAULT 'viewer,editor'
)
"""

_ORG_MEMBERS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS org_members (
    org_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (org_id, user_id)
)
"""

_QUOTA_USAGE_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS quota_usage (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, kind, day)
)
"""

_NOTIFICATIONS_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
)
"""

_ACTIVITY_LOG_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS activity_log (
    activity_id TEXT PRIMARY KEY,
    room_kind TEXT NOT NULL,
    room_id TEXT NOT NULL,
    actor_user_id TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
)
"""


class SQLiteAccountStore(AccountStore):
    """Accounts in a SQLite file -- by default the same file as room
    snapshots (different tables, one file to back up). Idempotent DDL on
    init, exactly like :class:`SQLiteStore` -- there is no separate
    migration step to run or forget."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_USERS_DDL_SQLITE)
            try:
                # Part 6 P4 added this column after P1-P3 shipped -- same
                # idempotent try/except-ALTER dance as owner_org_id below.
                conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # already migrated
            conn.execute(_SESSIONS_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            conn.execute(_ROOM_OWNERSHIP_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_room_ownership_owner ON room_ownership(owner_user_id)")
            try:
                # A fresh CREATE TABLE above doesn't include this column
                # (Part 6 P3 added it after P2 shipped) -- ALTER ADD
                # COLUMN has no IF NOT EXISTS in sqlite3, so a database
                # that already ran P2's DDL needs this explicit try/except
                # dance, same pattern as store.py's display_name migration.
                conn.execute("ALTER TABLE room_ownership ADD COLUMN owner_org_id TEXT")
            except sqlite3.OperationalError:
                pass  # already migrated
            conn.execute(_ROOM_GRANTS_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_room_grants_user ON room_grants(user_id)")
            conn.execute(_ORGS_DDL_SQLITE)
            for column in ("sso_issuer", "sso_client_id", "sso_client_secret", "sso_domain"):
                try:
                    conn.execute(f"ALTER TABLE orgs ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:
                    pass  # already migrated
            conn.execute(_ORG_MEMBERS_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id)")
            conn.execute(_QUOTA_USAGE_DDL_SQLITE)
            conn.execute(_NOTIFICATIONS_DDL_SQLITE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read)")
            conn.execute(_ACTIVITY_LOG_DDL_SQLITE)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_room ON activity_log(room_kind, room_id, created_at)"
            )

    @contextmanager
    def _connect(self):
        # Same close-guaranteeing wrapper as SQLiteStore._connect -- see
        # the connection-leak note there (bites hard on Windows).
        conn = sqlite3.connect(self._path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE email = ?",
                (email,),
            ).fetchone()
            if row:
                return _user_row_to_dict(row)
            user_id = uuid.uuid4().hex
            created = time.time()
            name = display_name or email.split("@")[0]
            conn.execute(
                "INSERT INTO users (user_id, email, display_name, avatar_color, created_at) VALUES (?, ?, ?, NULL, ?)",
                (user_id, email, name, created),
            )
            return {
                "user_id": user_id,
                "email": email,
                "display_name": name,
                "avatar_color": None,
                "created_at": created,
                "disabled": False,
            }

    def get_user(self, user_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return _user_row_to_dict(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
            return _user_row_to_dict(row) if row else None

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        sets, params = [], []
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if avatar_color is not None:
            sets.append("avatar_color = ?")
            params.append(avatar_color)
        if not sets:
            return self.get_user(user_id) is not None
        params.append(user_id)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", params)
            return cur.rowcount > 0

    def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE users SET disabled = ? WHERE user_id = ?", (1 if disabled else 0, user_id))
            return cur.rowcount > 0

    def list_all_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users"
            ).fetchall()
            return [_user_row_to_dict(r) for r in rows]

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, expires_at, now, now),
            )

    def get_session(self, token_hash: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, expires_at, created_at, last_seen FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not row:
                return None
            if row[1] < time.time():
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            return {"user_id": row[0], "expires_at": row[1], "created_at": row[2], "last_seen": row[3]}

    def touch_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (time.time(), token_hash))

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, user_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return cur.rowcount

    # -- room ownership & per-user grants -----------------------------------

    def claim_room(self, kind: str, room_id: str, owner_user_id: str, visibility: str = "private") -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO room_ownership (kind, room_id, owner_user_id, visibility, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, room_id, owner_user_id, visibility, time.time()),
            )
            return cur.rowcount > 0

    def get_room_ownership(self, kind: str, room_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_user_id, visibility, owner_org_id FROM room_ownership WHERE kind = ? AND room_id = ?",
                (kind, room_id),
            ).fetchone()
            return {"owner_user_id": row[0], "visibility": row[1], "owner_org_id": row[2]} if row else None

    def set_room_visibility(self, kind: str, room_id: str, visibility: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE room_ownership SET visibility = ? WHERE kind = ? AND room_id = ?",
                (visibility, kind, room_id),
            )
            return cur.rowcount > 0

    def set_room_grant(self, kind: str, room_id: str, user_id: str, role: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO room_grants (kind, room_id, user_id, role, granted_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (kind, room_id, user_id) DO UPDATE SET role = excluded.role",
                (kind, room_id, user_id, role, time.time()),
            )

    def revoke_room_grant(self, kind: str, room_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM room_grants WHERE kind = ? AND room_id = ? AND user_id = ?", (kind, room_id, user_id)
            )

    def get_room_grant(self, kind: str, room_id: str, user_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT role FROM room_grants WHERE kind = ? AND room_id = ? AND user_id = ?",
                (kind, room_id, user_id),
            ).fetchone()
            return row[0] if row else None

    def list_room_grants(self, kind: str, room_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT g.user_id, u.email, u.display_name, g.role FROM room_grants g "
                "JOIN users u ON u.user_id = g.user_id WHERE g.kind = ? AND g.room_id = ?",
                (kind, room_id),
            ).fetchall()
            return [{"user_id": r[0], "email": r[1], "display_name": r[2], "role": r[3]} for r in rows]

    def list_owned_rooms(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, room_id, visibility FROM room_ownership WHERE owner_user_id = ?", (user_id,)
            ).fetchall()
            return [{"kind": r[0], "room_id": r[1], "visibility": r[2]} for r in rows]

    def list_granted_rooms(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, room_id, role FROM room_grants WHERE user_id = ?", (user_id,)
            ).fetchall()
            return [{"kind": r[0], "room_id": r[1], "role": r[2]} for r in rows]

    # -- organizations & teams -----------------------------------------------

    @staticmethod
    def _org_row_to_dict(row) -> dict:
        return {
            "org_id": row[0], "name": row[1], "created_by": row[2], "created_at": row[3],
            "default_visibility": row[4], "allowed_share_link_roles": row[5].split(",") if row[5] else [],
            "sso_issuer": row[6] if len(row) > 6 else None,
            "sso_client_id": row[7] if len(row) > 7 else None,
            "sso_client_secret": row[8] if len(row) > 8 else None,
            "sso_domain": row[9] if len(row) > 9 else None,
        }

    def create_org(self, name: str, created_by_user_id: str) -> dict:
        org_id = uuid.uuid4().hex
        created = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO orgs (org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles) "
                "VALUES (?, ?, ?, ?, 'private', 'viewer,editor')",
                (org_id, name, created_by_user_id, created),
            )
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role, status) VALUES (?, ?, 'admin', 'active')",
                (org_id, created_by_user_id),
            )
        return {
            "org_id": org_id, "name": name, "created_by": created_by_user_id, "created_at": created,
            "default_visibility": "private", "allowed_share_link_roles": ["viewer", "editor"],
            "sso_issuer": None, "sso_client_id": None, "sso_client_secret": None, "sso_domain": None,
        }

    def get_org(self, org_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs WHERE org_id = ?",
                (org_id,),
            ).fetchone()
            return self._org_row_to_dict(row) if row else None

    def set_org_defaults(
        self, org_id: str, default_visibility: Optional[str] = None,
        allowed_share_link_roles: Optional[list[str]] = None,
    ) -> bool:
        sets, params = [], []
        if default_visibility is not None:
            sets.append("default_visibility = ?")
            params.append(default_visibility)
        if allowed_share_link_roles is not None:
            sets.append("allowed_share_link_roles = ?")
            params.append(",".join(allowed_share_link_roles))
        if not sets:
            return self.get_org(org_id) is not None
        params.append(org_id)
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE org_id = ?", params)
            return cur.rowcount > 0

    def list_orgs_for_user(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT o.org_id, o.name, m.role FROM org_members m JOIN orgs o ON o.org_id = m.org_id "
                "WHERE m.user_id = ? AND m.status = 'active'",
                (user_id,),
            ).fetchall()
            return [{"org_id": r[0], "name": r[1], "role": r[2]} for r in rows]

    def invite_org_member(self, org_id: str, email: str, role: str = "member") -> tuple[dict, str]:
        existing = self.get_user_by_email(email)
        user = self.create_or_get_user(email)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user["user_id"])
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE org_members SET role = ? WHERE org_id = ? AND user_id = ?",
                    (role, org_id, user["user_id"]),
                )
                return user, row[0]
            status = "active" if existing else "pending"
            conn.execute(
                "INSERT INTO org_members (org_id, user_id, role, status) VALUES (?, ?, ?, ?)",
                (org_id, user["user_id"], role, status),
            )
            return user, status

    def activate_pending_memberships(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE org_members SET status = 'active' WHERE user_id = ? AND status = 'pending'", (user_id,)
            )

    def set_org_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE org_members SET role = ? WHERE org_id = ? AND user_id = ?", (role, org_id, user_id)
            )
            return cur.rowcount > 0

    def remove_org_member(self, org_id: str, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user_id))

    def get_org_membership(self, org_id: str, user_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT role, status FROM org_members WHERE org_id = ? AND user_id = ?", (org_id, user_id)
            ).fetchone()
            return {"role": row[0], "status": row[1]} if row else None

    def list_org_members(self, org_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.user_id, u.email, u.display_name, m.role, m.status FROM org_members m "
                "JOIN users u ON u.user_id = m.user_id WHERE m.org_id = ?",
                (org_id,),
            ).fetchall()
            return [{"user_id": r[0], "email": r[1], "display_name": r[2], "role": r[3], "status": r[4]} for r in rows]

    def count_org_admins(self, org_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND role = 'admin' AND status = 'active'",
                (org_id,),
            ).fetchone()
            return row[0]

    def transfer_room_to_org(self, kind: str, room_id: str, org_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE room_ownership SET owner_org_id = ? WHERE kind = ? AND room_id = ?",
                (org_id, kind, room_id),
            )
            return cur.rowcount > 0

    # -- SSO ------------------------------------------------------------------

    def set_org_sso(
        self, org_id: str, issuer: Optional[str], client_id: Optional[str],
        client_secret: Optional[str], domain: Optional[str],
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE orgs SET sso_issuer = ?, sso_client_id = ?, sso_client_secret = ?, sso_domain = ? "
                "WHERE org_id = ?",
                (issuer, client_id, client_secret, domain.strip().lower() if domain else None, org_id),
            )
            return cur.rowcount > 0

    def get_org_by_sso_domain(self, domain: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs "
                "WHERE sso_domain = ? AND sso_issuer IS NOT NULL AND sso_client_id IS NOT NULL "
                "AND sso_client_secret IS NOT NULL",
                (domain.strip().lower(),),
            ).fetchone()
            return self._org_row_to_dict(row) if row else None

    # -- per-account quotas -----------------------------------------------------

    def increment_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO quota_usage (user_id, kind, day, count) VALUES (?, ?, ?, 1) "
                "ON CONFLICT (user_id, kind, day) DO UPDATE SET count = count + 1",
                (user_id, kind, day),
            )
            row = conn.execute(
                "SELECT count FROM quota_usage WHERE user_id = ? AND kind = ? AND day = ?", (user_id, kind, day)
            ).fetchone()
            return row[0]

    def get_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count FROM quota_usage WHERE user_id = ? AND kind = ? AND day = ?", (user_id, kind, day)
            ).fetchone()
            return row[0] if row else 0

    # -- admin panel --------------------------------------------------------

    def list_all_orgs(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs"
            ).fetchall()
            return [self._org_row_to_dict(r) for r in rows]

    def list_all_owned_rooms(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, room_id, owner_user_id, owner_org_id, visibility FROM room_ownership"
            ).fetchall()
            return [
                {"kind": r[0], "room_id": r[1], "owner_user_id": r[2], "owner_org_id": r[3], "visibility": r[4]}
                for r in rows
            ]

    def admin_claim_room(self, kind: str, room_id: str, owner_user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO room_ownership (kind, room_id, owner_user_id, visibility, created_at) "
                "VALUES (?, ?, ?, 'public', ?)",
                (kind, room_id, owner_user_id, time.time()),
            )
            return cur.rowcount > 0

    def delete_room_ownership(self, kind: str, room_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM room_ownership WHERE kind = ? AND room_id = ?", (kind, room_id))
            conn.execute("DELETE FROM room_grants WHERE kind = ? AND room_id = ?", (kind, room_id))

    # -- notifications --------------------------------------------------------

    def create_notification(self, user_id: str, kind: str, payload: dict) -> dict:
        notification_id = uuid.uuid4().hex
        created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO notifications (notification_id, user_id, kind, payload, created_at, read) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (notification_id, user_id, kind, json.dumps(payload), created_at),
            )
        return {
            "notification_id": notification_id, "user_id": user_id, "kind": kind,
            "payload": dict(payload), "created_at": created_at, "read": False,
        }

    def list_notifications(self, user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        query = "SELECT notification_id, user_id, kind, payload, created_at, read FROM notifications WHERE user_id = ?"
        params: list = [user_id]
        if unread_only:
            query += " AND read = 0"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "notification_id": r[0], "user_id": r[1], "kind": r[2],
                "payload": json.loads(r[3]), "created_at": r[4], "read": bool(r[5]),
            }
            for r in rows
        ]

    def mark_notification_read(self, notification_id: str, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notifications SET read = 1 WHERE notification_id = ? AND user_id = ?",
                (notification_id, user_id),
            )
            return cur.rowcount > 0

    def mark_all_notifications_read(self, user_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("UPDATE notifications SET read = 1 WHERE user_id = ? AND read = 0", (user_id,))
            return cur.rowcount

    def count_unread_notifications(self, user_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0", (user_id,)
            ).fetchone()
            return row[0] if row else 0

    # -- per-room activity feed -------------------------------------------------

    def log_activity(self, room_kind: str, room_id: str, actor_user_id: Optional[str], kind: str, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO activity_log (activity_id, room_kind, room_id, actor_user_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, room_kind, room_id, actor_user_id, kind, json.dumps(payload), time.time()),
            )

    def list_activity(self, room_kind: str, room_id: str, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT activity_id, room_kind, room_id, actor_user_id, kind, payload, created_at "
                "FROM activity_log WHERE room_kind = ? AND room_id = ? ORDER BY created_at DESC LIMIT ?",
                (room_kind, room_id, limit),
            ).fetchall()
        return [
            {
                "activity_id": r[0], "room_kind": r[1], "room_id": r[2], "actor_user_id": r[3],
                "kind": r[4], "payload": json.loads(r[5]), "created_at": r[6],
            }
            for r in rows
        ]


class PostgresAccountStore(AccountStore):
    """Accounts in Postgres, for multi-process deployments (k8s Mode B).
    Same asyncpg-behind-a-sync-interface bridge as
    :class:`crdt_cad.persistence.store.PostgresStore` -- a dedicated
    background thread runs its own event loop; every call blocks the
    calling thread until the query completes. See that class's docstring
    for why this trade-off is acceptable (and already made) here."""

    _SCHEMA_LOCK_KEY = 0x63AD_ACC7  # distinct from PostgresStore's lock key

    def __init__(self, dsn: str) -> None:
        try:
            import asyncpg
        except ImportError as exc:
            raise ImportError(
                "PostgresAccountStore needs asyncpg -- install with `pip install crdt-cad[postgres]`, "
                "or unset CRDT_CAD_DATABASE_URL to keep accounts in SQLite."
            ) from exc
        self._asyncpg = asyncpg
        self._dsn = dsn
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="postgres-accounts-loop")
        self._thread.start()
        self._pool = self._call(self._init_pool())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _init_pool(self):
        pool = await self._asyncpg.create_pool(self._dsn)
        async with pool.acquire() as conn:
            # Same replicas-race-on-first-boot consideration as
            # PostgresStore._init_pool (verified there against a real
            # 3-replica kind deployment) -- serialize one-time DDL.
            await conn.execute("SELECT pg_advisory_lock($1)", self._SCHEMA_LOCK_KEY)
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        display_name TEXT,
                        avatar_color TEXT,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                # Part 6 P4 added this column after P1-P3 shipped.
                await conn.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT FALSE"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        expires_at DOUBLE PRECISION NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        last_seen DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_ownership (
                        kind TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        owner_user_id TEXT NOT NULL,
                        visibility TEXT NOT NULL DEFAULT 'private',
                        created_at DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (kind, room_id)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_ownership_owner ON room_ownership(owner_user_id)"
                )
                # A fresh CREATE TABLE above doesn't include this column
                # (Part 6 P3 added it after P2 shipped) -- ADD COLUMN IF
                # NOT EXISTS is a real Postgres feature, so a database
                # that already ran P2's DDL just gets it added here.
                await conn.execute("ALTER TABLE room_ownership ADD COLUMN IF NOT EXISTS owner_org_id TEXT")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_grants (
                        kind TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        granted_at DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (kind, room_id, user_id)
                    )
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_room_grants_user ON room_grants(user_id)")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orgs (
                        org_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_by TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        default_visibility TEXT NOT NULL DEFAULT 'private',
                        allowed_share_link_roles TEXT NOT NULL DEFAULT 'viewer,editor'
                    )
                    """
                )
                # Part 6 P4 added these columns after P3 shipped.
                for column in ("sso_issuer", "sso_client_id", "sso_client_secret", "sso_domain"):
                    await conn.execute(f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {column} TEXT")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS org_members (
                        org_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        PRIMARY KEY (org_id, user_id)
                    )
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id)")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quota_usage (
                        user_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        day TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (user_id, kind, day)
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS notifications (
                        notification_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        read BOOLEAN NOT NULL DEFAULT FALSE
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read)"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity_log (
                        activity_id TEXT PRIMARY KEY,
                        room_kind TEXT NOT NULL,
                        room_id TEXT NOT NULL,
                        actor_user_id TEXT,
                        kind TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_activity_room ON activity_log(room_kind, room_id, created_at)"
                )
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", self._SCHEMA_LOCK_KEY)
        return pool

    def create_or_get_user(self, email: str, display_name: Optional[str] = None) -> dict:
        email = email.strip().lower()

        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE email = $1",
                    email,
                )
                if row:
                    return _user_row_to_dict(row)
                user_id = uuid.uuid4().hex
                created = time.time()
                name = display_name or email.split("@")[0]
                # ON CONFLICT: two processes racing to create the same
                # user must converge on one row, not error.
                row = await conn.fetchrow(
                    """
                    INSERT INTO users (user_id, email, display_name, avatar_color, created_at)
                    VALUES ($1, $2, $3, NULL, $4)
                    ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                    RETURNING user_id, email, display_name, avatar_color, created_at, disabled
                    """,
                    user_id, email, name, created,
                )
                return _user_row_to_dict(row)

        return self._call(_go())

    def get_user(self, user_id: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE user_id = $1",
                    user_id,
                )
                return _user_row_to_dict(row) if row else None

        return self._call(_go())

    def get_user_by_email(self, email: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users WHERE email = $1",
                    email.strip().lower(),
                )
                return _user_row_to_dict(row) if row else None

        return self._call(_go())

    def update_profile(
        self, user_id: str, display_name: Optional[str] = None, avatar_color: Optional[str] = None
    ) -> bool:
        async def _go():
            sets, params = [], []
            if display_name is not None:
                params.append(display_name)
                sets.append(f"display_name = ${len(params)}")
            if avatar_color is not None:
                params.append(avatar_color)
                sets.append(f"avatar_color = ${len(params)}")
            async with self._pool.acquire() as conn:
                if not sets:
                    row = await conn.fetchrow("SELECT 1 FROM users WHERE user_id = $1", user_id)
                    return row is not None
                params.append(user_id)
                result = await conn.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE user_id = ${len(params)}", *params
                )
                return result.endswith("1")

        return self._call(_go())

    def set_user_disabled(self, user_id: str, disabled: bool) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute("UPDATE users SET disabled = $1 WHERE user_id = $2", disabled, user_id)
                return not result.endswith(" 0")

        return self._call(_go())

    def list_all_users(self) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id, email, display_name, avatar_color, created_at, disabled FROM users"
                )
                return [_user_row_to_dict(r) for r in rows]

        return self._call(_go())

    def create_session(self, token_hash: str, user_id: str, expires_at: float) -> None:
        async def _go():
            now = time.time()
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO sessions (token_hash, user_id, expires_at, created_at, last_seen) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    token_hash, user_id, expires_at, now, now,
                )

        self._call(_go())

    def get_session(self, token_hash: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT user_id, expires_at, created_at, last_seen FROM sessions WHERE token_hash = $1",
                    token_hash,
                )
                if not row:
                    return None
                if row["expires_at"] < time.time():
                    await conn.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)
                    return None
                return dict(row)

        return self._call(_go())

    def touch_session(self, token_hash: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE sessions SET last_seen = $1 WHERE token_hash = $2", time.time(), token_hash
                )

        self._call(_go())

    def delete_session(self, token_hash: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash)

        self._call(_go())

    def delete_user_sessions(self, user_id: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)
                return int(result.rsplit(" ", 1)[1])

        return self._call(_go())

    # -- room ownership & per-user grants -----------------------------------

    def claim_room(self, kind: str, room_id: str, owner_user_id: str, visibility: str = "private") -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "INSERT INTO room_ownership (kind, room_id, owner_user_id, visibility, created_at) "
                    "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (kind, room_id) DO NOTHING",
                    kind, room_id, owner_user_id, visibility, time.time(),
                )
                return result.endswith("1")

        return self._call(_go())

    def get_room_ownership(self, kind: str, room_id: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT owner_user_id, visibility, owner_org_id FROM room_ownership "
                    "WHERE kind = $1 AND room_id = $2",
                    kind, room_id,
                )
                return dict(row) if row else None

        return self._call(_go())

    def set_room_visibility(self, kind: str, room_id: str, visibility: str) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE room_ownership SET visibility = $1 WHERE kind = $2 AND room_id = $3",
                    visibility, kind, room_id,
                )
                return not result.endswith(" 0")

        return self._call(_go())

    def set_room_grant(self, kind: str, room_id: str, user_id: str, role: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO room_grants (kind, room_id, user_id, role, granted_at) VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (kind, room_id, user_id) DO UPDATE SET role = EXCLUDED.role",
                    kind, room_id, user_id, role, time.time(),
                )

        self._call(_go())

    def revoke_room_grant(self, kind: str, room_id: str, user_id: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM room_grants WHERE kind = $1 AND room_id = $2 AND user_id = $3",
                    kind, room_id, user_id,
                )

        self._call(_go())

    def get_room_grant(self, kind: str, room_id: str, user_id: str) -> Optional[str]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT role FROM room_grants WHERE kind = $1 AND room_id = $2 AND user_id = $3",
                    kind, room_id, user_id,
                )
                return row["role"] if row else None

        return self._call(_go())

    def list_room_grants(self, kind: str, room_id: str) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT g.user_id, u.email, u.display_name, g.role FROM room_grants g "
                    "JOIN users u ON u.user_id = g.user_id WHERE g.kind = $1 AND g.room_id = $2",
                    kind, room_id,
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    def list_owned_rooms(self, user_id: str) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT kind, room_id, visibility FROM room_ownership WHERE owner_user_id = $1", user_id
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    def list_granted_rooms(self, user_id: str) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT kind, room_id, role FROM room_grants WHERE user_id = $1", user_id
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    # -- organizations & teams -----------------------------------------------

    @staticmethod
    def _org_row_to_dict(row) -> dict:
        d = dict(row)
        roles = d.pop("allowed_share_link_roles", "")
        d["allowed_share_link_roles"] = roles.split(",") if roles else []
        return d

    def create_org(self, name: str, created_by_user_id: str) -> dict:
        org_id = uuid.uuid4().hex
        created = time.time()

        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO orgs (org_id, name, created_by, created_at, default_visibility, "
                    "allowed_share_link_roles) VALUES ($1, $2, $3, $4, 'private', 'viewer,editor')",
                    org_id, name, created_by_user_id, created,
                )
                await conn.execute(
                    "INSERT INTO org_members (org_id, user_id, role, status) VALUES ($1, $2, 'admin', 'active')",
                    org_id, created_by_user_id,
                )

        self._call(_go())
        return {
            "org_id": org_id, "name": name, "created_by": created_by_user_id, "created_at": created,
            "default_visibility": "private", "allowed_share_link_roles": ["viewer", "editor"],
            "sso_issuer": None, "sso_client_id": None, "sso_client_secret": None, "sso_domain": None,
        }

    def get_org(self, org_id: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                    "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs WHERE org_id = $1",
                    org_id,
                )
                return self._org_row_to_dict(row) if row else None

        return self._call(_go())

    def set_org_defaults(
        self, org_id: str, default_visibility: Optional[str] = None,
        allowed_share_link_roles: Optional[list[str]] = None,
    ) -> bool:
        async def _go():
            sets, params = [], []
            if default_visibility is not None:
                params.append(default_visibility)
                sets.append(f"default_visibility = ${len(params)}")
            if allowed_share_link_roles is not None:
                params.append(",".join(allowed_share_link_roles))
                sets.append(f"allowed_share_link_roles = ${len(params)}")
            async with self._pool.acquire() as conn:
                if not sets:
                    row = await conn.fetchrow("SELECT 1 FROM orgs WHERE org_id = $1", org_id)
                    return row is not None
                params.append(org_id)
                result = await conn.execute(
                    f"UPDATE orgs SET {', '.join(sets)} WHERE org_id = ${len(params)}", *params
                )
                return not result.endswith(" 0")

        return self._call(_go())

    def list_orgs_for_user(self, user_id: str) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT o.org_id, o.name, m.role FROM org_members m JOIN orgs o ON o.org_id = m.org_id "
                    "WHERE m.user_id = $1 AND m.status = 'active'",
                    user_id,
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    def invite_org_member(self, org_id: str, email: str, role: str = "member") -> tuple[dict, str]:
        existing = self.get_user_by_email(email)
        user = self.create_or_get_user(email)

        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM org_members WHERE org_id = $1 AND user_id = $2", org_id, user["user_id"]
                )
                if row:
                    await conn.execute(
                        "UPDATE org_members SET role = $1 WHERE org_id = $2 AND user_id = $3",
                        role, org_id, user["user_id"],
                    )
                    return row["status"]
                status = "active" if existing else "pending"
                await conn.execute(
                    "INSERT INTO org_members (org_id, user_id, role, status) VALUES ($1, $2, $3, $4)",
                    org_id, user["user_id"], role, status,
                )
                return status

        status = self._call(_go())
        return user, status

    def activate_pending_memberships(self, user_id: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE org_members SET status = 'active' WHERE user_id = $1 AND status = 'pending'", user_id
                )

        self._call(_go())

    def set_org_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE org_members SET role = $1 WHERE org_id = $2 AND user_id = $3", role, org_id, user_id
                )
                return not result.endswith(" 0")

        return self._call(_go())

    def remove_org_member(self, org_id: str, user_id: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM org_members WHERE org_id = $1 AND user_id = $2", org_id, user_id)

        self._call(_go())

    def get_org_membership(self, org_id: str, user_id: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT role, status FROM org_members WHERE org_id = $1 AND user_id = $2", org_id, user_id
                )
                return dict(row) if row else None

        return self._call(_go())

    def list_org_members(self, org_id: str) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT m.user_id, u.email, u.display_name, m.role, m.status FROM org_members m "
                    "JOIN users u ON u.user_id = m.user_id WHERE m.org_id = $1",
                    org_id,
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    def count_org_admins(self, org_id: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n FROM org_members WHERE org_id = $1 AND role = 'admin' AND status = 'active'",
                    org_id,
                )
                return row["n"]

        return self._call(_go())

    def transfer_room_to_org(self, kind: str, room_id: str, org_id: str) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE room_ownership SET owner_org_id = $1 WHERE kind = $2 AND room_id = $3",
                    org_id, kind, room_id,
                )
                return not result.endswith(" 0")

        return self._call(_go())

    # -- SSO ------------------------------------------------------------------

    def set_org_sso(
        self, org_id: str, issuer: Optional[str], client_id: Optional[str],
        client_secret: Optional[str], domain: Optional[str],
    ) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE orgs SET sso_issuer = $1, sso_client_id = $2, sso_client_secret = $3, "
                    "sso_domain = $4 WHERE org_id = $5",
                    issuer, client_id, client_secret, domain.strip().lower() if domain else None, org_id,
                )
                return not result.endswith(" 0")

        return self._call(_go())

    def get_org_by_sso_domain(self, domain: str) -> Optional[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                    "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs "
                    "WHERE sso_domain = $1 AND sso_issuer IS NOT NULL AND sso_client_id IS NOT NULL "
                    "AND sso_client_secret IS NOT NULL",
                    domain.strip().lower(),
                )
                return self._org_row_to_dict(row) if row else None

        return self._call(_go())

    # -- per-account quotas -----------------------------------------------------

    def increment_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO quota_usage (user_id, kind, day, count) VALUES ($1, $2, $3, 1)
                    ON CONFLICT (user_id, kind, day) DO UPDATE SET count = quota_usage.count + 1
                    RETURNING count
                    """,
                    user_id, kind, day,
                )
                return row["count"]

        return self._call(_go())

    def get_quota_usage(self, user_id: str, kind: str, day: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT count FROM quota_usage WHERE user_id = $1 AND kind = $2 AND day = $3",
                    user_id, kind, day,
                )
                return row["count"] if row else 0

        return self._call(_go())

    # -- admin panel --------------------------------------------------------

    def list_all_orgs(self) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT org_id, name, created_by, created_at, default_visibility, allowed_share_link_roles, "
                    "sso_issuer, sso_client_id, sso_client_secret, sso_domain FROM orgs"
                )
                return [self._org_row_to_dict(r) for r in rows]

        return self._call(_go())

    def list_all_owned_rooms(self) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT kind, room_id, owner_user_id, owner_org_id, visibility FROM room_ownership"
                )
                return [dict(r) for r in rows]

        return self._call(_go())

    def admin_claim_room(self, kind: str, room_id: str, owner_user_id: str) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "INSERT INTO room_ownership (kind, room_id, owner_user_id, visibility, created_at) "
                    "VALUES ($1, $2, $3, 'public', $4) ON CONFLICT (kind, room_id) DO NOTHING",
                    kind, room_id, owner_user_id, time.time(),
                )
                return result.endswith("1")

        return self._call(_go())

    def delete_room_ownership(self, kind: str, room_id: str) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM room_ownership WHERE kind = $1 AND room_id = $2", kind, room_id)
                await conn.execute("DELETE FROM room_grants WHERE kind = $1 AND room_id = $2", kind, room_id)

        self._call(_go())

    # -- notifications --------------------------------------------------------

    def create_notification(self, user_id: str, kind: str, payload: dict) -> dict:
        notification_id = uuid.uuid4().hex
        created_at = time.time()

        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO notifications (notification_id, user_id, kind, payload, created_at, read) "
                    "VALUES ($1, $2, $3, $4, $5, FALSE)",
                    notification_id, user_id, kind, json.dumps(payload), created_at,
                )

        self._call(_go())
        return {
            "notification_id": notification_id, "user_id": user_id, "kind": kind,
            "payload": dict(payload), "created_at": created_at, "read": False,
        }

    def list_notifications(self, user_id: str, unread_only: bool = False, limit: int = 50) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                if unread_only:
                    rows = await conn.fetch(
                        "SELECT notification_id, user_id, kind, payload, created_at, read FROM notifications "
                        "WHERE user_id = $1 AND read = FALSE ORDER BY created_at DESC LIMIT $2",
                        user_id, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT notification_id, user_id, kind, payload, created_at, read FROM notifications "
                        "WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
                        user_id, limit,
                    )
                return [
                    {
                        "notification_id": r["notification_id"], "user_id": r["user_id"], "kind": r["kind"],
                        "payload": json.loads(r["payload"]), "created_at": r["created_at"], "read": r["read"],
                    }
                    for r in rows
                ]

        return self._call(_go())

    def mark_notification_read(self, notification_id: str, user_id: str) -> bool:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE notifications SET read = TRUE WHERE notification_id = $1 AND user_id = $2",
                    notification_id, user_id,
                )
                return not result.endswith(" 0")

        return self._call(_go())

    def mark_all_notifications_read(self, user_id: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE notifications SET read = TRUE WHERE user_id = $1 AND read = FALSE", user_id,
                )
                return int(result.split(" ")[-1])

        return self._call(_go())

    def count_unread_notifications(self, user_id: str) -> int:
        async def _go():
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS c FROM notifications WHERE user_id = $1 AND read = FALSE", user_id,
                )
                return row["c"] if row else 0

        return self._call(_go())

    # -- per-room activity feed -------------------------------------------------

    def log_activity(self, room_kind: str, room_id: str, actor_user_id: Optional[str], kind: str, payload: dict) -> None:
        async def _go():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO activity_log (activity_id, room_kind, room_id, actor_user_id, kind, payload, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    uuid.uuid4().hex, room_kind, room_id, actor_user_id, kind, json.dumps(payload), time.time(),
                )

        self._call(_go())

    def list_activity(self, room_kind: str, room_id: str, limit: int = 50) -> list[dict]:
        async def _go():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT activity_id, room_kind, room_id, actor_user_id, kind, payload, created_at "
                    "FROM activity_log WHERE room_kind = $1 AND room_id = $2 ORDER BY created_at DESC LIMIT $3",
                    room_kind, room_id, limit,
                )
                return [
                    {
                        "activity_id": r["activity_id"], "room_kind": r["room_kind"], "room_id": r["room_id"],
                        "actor_user_id": r["actor_user_id"], "kind": r["kind"],
                        "payload": json.loads(r["payload"]), "created_at": r["created_at"],
                    }
                    for r in rows
                ]

        return self._call(_go())
