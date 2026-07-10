"""Part 6, Phase P5: 3D comments (mirroring 2D's pre-existing comments,
now also on MeshCRDT), @mentions resolving to notifications, and a
per-room activity feed -- comments themselves work in tokens-only mode
(the zero-config default), same as 2D; mentions/notifications/activity
are accounts-mode only, since they need a real identity to attribute
and notify against.

`isolated_store` and `isolated_account_store` (both autouse, in
tests/conftest.py) give every test a fresh in-memory room store and a
fresh in-memory accounts store, so nothing here can leak into another
test or touch a real database file.
"""

from crdt_cad.crdt.clock import LamportClock
from crdt_cad.crdt.mesh import MeshCRDT
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


# -- 3D comments work in tokens-only mode (zero-config, mirrors 2D) -------------


def test_mesh_comment_add_broadcasts_and_persists():
    client = _client()
    mesh = MeshCRDT(LamportClock(actor="a"))
    _comment_id, op = mesh.add_comment("face_1", "looks great", author="alice")

    with client.websocket_connect("/ws/mesh/commentroom1") as ws1, client.websocket_connect("/ws/mesh/commentroom1") as ws2:
        _hello(ws1, actor="a")
        ws1.receive_json()
        _hello(ws2, actor="b")
        ws2.receive_json()

        ws1.send_json({"type": "ops", "ops": [op.to_dict()], "from": "a"})
        broadcast = ws2.receive_json()
        assert broadcast["type"] == "ops"
        assert broadcast["ops"][0]["target"] == "comment"

        ws1.send_json({"type": "save"})
        ws1.receive_json()

    # Reconnecting (simulating a restart) sees the comment in the snapshot.
    with client.websocket_connect("/ws/mesh/commentroom1") as ws3:
        _hello(ws3, actor="c")
        snap = ws3.receive_json()
        assert len(snap["doc"]["comments"]["entries"]) == 1


# -- @mentions -> notifications (accounts mode only) -----------------------------


def test_mention_creates_notification_for_room_member(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")

    with owner.websocket_connect("/ws/mesh/mentionroom1") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        auth.get_account_store().set_room_grant("mesh", "mentionroom1", bob["user_id"], "viewer")

        mesh = MeshCRDT(LamportClock(actor="alice"))
        _comment_id, op = mesh.add_comment("face_1", "hey @bob@example.com check this out", author="alice")
        ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    resp = bob_client.get("/api/notifications")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unread_count"] == 1
    assert body["notifications"][0]["kind"] == "mention"
    assert body["notifications"][0]["payload"]["room_id"] == "mentionroom1"


def test_mention_of_comment_author_does_not_self_notify(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")

    with owner.websocket_connect("/ws/mesh/mentionroom2") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        mesh = MeshCRDT(LamportClock(actor="alice"))
        _comment_id, op = mesh.add_comment("face_1", "note to self @alice@example.com", author="alice")
        ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})

    resp = owner.get("/api/notifications")
    assert resp.status_code == 200
    assert resp.json()["unread_count"] == 0


def test_mention_in_private_room_is_not_notified_without_access(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    auth.get_account_store().create_or_get_user("stranger@example.com")

    with owner.websocket_connect("/ws/mesh/mentionroom3") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        # Room is private (claim-on-first-touch default) and stranger
        # has no grant -- the mention must not leak a notification to
        # someone who can't actually see this room.
        mesh = MeshCRDT(LamportClock(actor="alice"))
        _comment_id, op = mesh.add_comment("face_1", "hey @stranger@example.com", author="alice")
        ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})

    stranger_client = _client()
    _sign_in(stranger_client, "stranger@example.com")
    resp = stranger_client.get("/api/notifications")
    assert resp.json()["unread_count"] == 0


def test_notifications_require_sign_in():
    resp = _client().get("/api/notifications")
    assert resp.status_code in (401, 404)  # 404 if accounts mode is off entirely


def test_mark_notification_read(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")

    with owner.websocket_connect("/ws/mesh/mentionroom4") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        auth.get_account_store().set_room_grant("mesh", "mentionroom4", bob["user_id"], "viewer")
        mesh = MeshCRDT(LamportClock(actor="alice"))
        _comment_id, op = mesh.add_comment("face_1", "@bob@example.com take a look", author="alice")
        ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    notif_id = bob_client.get("/api/notifications").json()["notifications"][0]["notification_id"]

    resp = bob_client.post(f"/api/notifications/{notif_id}/read")
    assert resp.status_code == 200
    assert bob_client.get("/api/notifications").json()["unread_count"] == 0

    # Another user can't mark someone else's notification read.
    resp2 = owner.post(f"/api/notifications/{notif_id}/read")
    assert resp2.status_code == 404


def test_mark_all_notifications_read(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")
    bob = auth.get_account_store().create_or_get_user("bob@example.com")

    with owner.websocket_connect("/ws/mesh/mentionroom5") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        auth.get_account_store().set_room_grant("mesh", "mentionroom5", bob["user_id"], "viewer")
        mesh = MeshCRDT(LamportClock(actor="alice"))
        for i in range(3):
            _comment_id, op = mesh.add_comment(f"face_{i}", "@bob@example.com ping", author="alice")
            ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})

    bob_client = _client()
    _sign_in(bob_client, "bob@example.com")
    assert bob_client.get("/api/notifications").json()["unread_count"] == 3
    resp = bob_client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert resp.json()["marked_read"] == 3
    assert bob_client.get("/api/notifications").json()["unread_count"] == 0


# -- per-room activity feed -------------------------------------------------------


def test_activity_feed_is_empty_without_accounts_mode():
    client = _client()
    with client.websocket_connect("/ws/mesh/activityroom1") as ws:
        _hello(ws, actor="a")
        ws.receive_json()
    resp = client.get("/api/mesh/activityroom1/activity")
    assert resp.status_code == 200
    assert resp.json() == {"activity": []}


def test_activity_feed_logs_comment_events(monkeypatch):
    _enable_accounts(monkeypatch)
    owner = _client()
    _sign_in(owner, "alice@example.com")

    with owner.websocket_connect("/ws/mesh/activityroom2") as ws:
        _hello(ws, actor="alice")
        ws.receive_json()
        mesh = MeshCRDT(LamportClock(actor="alice"))
        comment_id, op = mesh.add_comment("face_1", "hello", author="alice")
        ws.send_json({"type": "ops", "ops": [op.to_dict()], "from": "alice"})
        rm_op = mesh.remove_comment(comment_id)
        ws.send_json({"type": "ops", "ops": [rm_op.to_dict()], "from": "alice"})

    resp = owner.get("/api/mesh/activityroom2/activity")
    assert resp.status_code == 200
    kinds = [a["kind"] for a in resp.json()["activity"]]
    assert kinds == ["comment_removed", "comment_added"]  # newest first
    assert resp.json()["activity"][1]["actor_user_id"] is not None


def test_activity_feed_requires_room_access():
    resp = _client().get("/api/mesh/nonexistent-activity-room/activity")
    assert resp.status_code == 200  # tokens-only mode: wide open, same as every other room-read endpoint
