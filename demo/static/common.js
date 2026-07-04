// Shared browser-side plumbing used by both the 2D sketch demo and the 3D
// mesh demo: actor identity, a small WebSocket relay client implementing
// the hello/snapshot/delta/ops protocol from crdt_cad.server.app, and a
// lightweight "frontier" (vector clock) tracker.
//
// Design note -- why the browser doesn't run the CRDT merge algorithm:
// the server (Python, the same crdt_cad.crdt package that ships with
// Hypothesis-verified convergence tests) is the single authoritative
// merge point. This client only ever needs to (a) mint new ops with a
// locally-unique, monotonically increasing OpId, (b) render its own
// edits optimistically (always safe: a single actor's own edits are
// never concurrent with themselves), and (c) render whatever the server
// confirms. Real conflict resolution -- e.g. two offline users both
// splitting the same path/face boundary -- is resolved server-side and
// arrives at the client as an ordinary snapshot/delta to render, exactly
// like a late joiner would see it. Periodic snapshots (from the server,
// every 30s, plus on every reconnect) are the correctness backstop that
// makes this safe even if an individual incremental "ops" broadcast is
// ever missed or reordered in transit.

const ACTOR_COLORS = [
  "#ff6b6b", "#4dabf7", "#69db7c", "#ffd43b", "#da77f2",
  "#ff922b", "#38d9a9", "#f783ac", "#748ffc", "#94d82d",
];

function getOrCreateActorId() {
  let id = sessionStorage.getItem("crdt_cad_actor_id");
  if (!id) {
    id = "u_" + Math.random().toString(36).slice(2, 10);
    sessionStorage.setItem("crdt_cad_actor_id", id);
  }
  return id;
}

function getOrCreateActorName() {
  let name = localStorage.getItem("crdt_cad_actor_name");
  if (!name) {
    name = "Guest " + Math.floor(Math.random() * 900 + 100);
    localStorage.setItem("crdt_cad_actor_name", name);
  }
  return name;
}

function colorForActor(actorId) {
  let hash = 0;
  for (let i = 0; i < actorId.length; i++) hash = (hash * 31 + actorId.charCodeAt(i)) >>> 0;
  return ACTOR_COLORS[hash % ACTOR_COLORS.length];
}

// -- optional shared-secret room auth ------------------------------------------
//
// Mirrors crdt_cad.server.security: entirely opt-in. GET /api/auth/required
// reports whether the server has CRDT_CAD_SECRET configured at all; when it
// doesn't (the zero-config default), ensureRoomAccess() resolves to null
// immediately and nothing below ever runs. When it does, a room-scoped
// signed token is required for both the WS "hello" handshake and every
// REST call that touches a room -- see the Share button wiring in
// sketch.js/mesh3d.js for how a token gets embedded in an invite link so
// the recipient skips re-entering the secret.

function _tokenStorageKey(kind, room) {
  return `crdt_cad_token:${kind}:${room}`;
}

function roomTokenFor(kind, room) {
  return localStorage.getItem(_tokenStorageKey(kind, room));
}

function clearRoomToken(kind, room) {
  localStorage.removeItem(_tokenStorageKey(kind, room));
}

/** Appends `?token=`/`&token=` to a REST URL if this room has one stored --
 * a no-op (returns `url` unchanged) when auth isn't configured, since
 * roomTokenFor() then has nothing stored to return. Every REST call the
 * demos make against a `{room_id}`-scoped endpoint needs this, mirroring
 * require_room_access() on the server side. */
function withToken(url, kind, room) {
  const token = roomTokenFor(kind, room);
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** Resolves to a valid room token, or null if this server has no secret
 * configured. Checks (in order): a `?token=` query param (invite link),
 * a previously-stored token for this room, then -- only if the server
 * actually requires one -- prompts for the shared secret and exchanges it
 * for a token via POST /api/auth/token, retrying on a wrong guess. */
async function ensureRoomAccess(kind, room) {
  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token");
  if (urlToken) {
    localStorage.setItem(_tokenStorageKey(kind, room), urlToken);
    return urlToken;
  }

  const stored = roomTokenFor(kind, room);
  if (stored) return stored;

  let required = false;
  try {
    const resp = await fetch("/api/auth/required");
    required = (await resp.json()).required;
  } catch (err) {
    return null; // fail open to "no auth" -- matches the server's own zero-config default
  }
  if (!required) return null;

  for (;;) {
    const secret = window.prompt("This room requires a shared secret to join. Enter it:");
    if (secret === null) continue; // no usable "cancel" once auth is actually required -- keep asking
    try {
      const resp = await fetch("/api/auth/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secret, kind, room_id: room }),
      });
      if (resp.ok) {
        const { token } = await resp.json();
        localStorage.setItem(_tokenStorageKey(kind, room), token);
        return token;
      }
      window.alert("Incorrect secret -- try again.");
    } catch (err) {
      window.alert(`Could not reach the server: ${err.message}`);
    }
  }
}

/** Poor-man's vector clock: actor -> highest op counter seen. Just a
 * pointwise-max tracker (the same one-line rule VectorClock.record uses
 * server-side) -- not a merge algorithm, so it's safe to keep this tiny. */
class FrontierTracker {
  constructor() {
    this.counters = {};
  }
  record(opId) {
    if (!opId) return;
    const [counter, actor] = opId;
    if (!this.counters[actor] || this.counters[actor] < counter) {
      this.counters[actor] = counter;
    }
  }
  recordAll(dict) {
    for (const [actor, counter] of Object.entries(dict || {})) {
      if (!this.counters[actor] || this.counters[actor] < counter) {
        this.counters[actor] = counter;
      }
    }
  }
  /** True if `otherDict` (typically a server-broadcast frontier) has any
   * actor/counter pair this tracker hasn't caught up to yet -- i.e. there's
   * something out there this replica hasn't seen. */
  isBehind(otherDict) {
    for (const [actor, counter] of Object.entries(otherDict || {})) {
      if (!this.counters[actor] || this.counters[actor] < counter) return true;
    }
    return false;
  }
  toDict() {
    return { ...this.counters };
  }
}

// -- offline outbox durability (IndexedDB) -------------------------------------
//
// The in-memory outbox (RelayConnection.outbox) already survives a dropped
// WebSocket -- edits keep queuing locally and flush on reconnect. What it
// doesn't survive is a hard refresh or closed tab: the queue lives only in
// JS memory. This persists just the queued ops (not the frontier -- see
// below) to IndexedDB, keyed per room+actor, so a reload while offline
// recovers the queue instead of silently dropping it. Not a CRDT of its
// own -- just durability for the same op list that was always going to be
// sent once reconnected.
//
// Deliberately does NOT also persist/restore the frontier (the vector
// clock RelayConnection uses to ask the server for a `delta` instead of a
// full `snapshot`) even though that was the first thing tried here. The
// reasoning doesn't carry over from "network dropped, JS memory intact"
// to "hard reload, JS memory wiped": a `delta` reply only contains ops
// the server thinks this frontier hasn't seen yet, which is correct when
// local `state` already has everything up to that frontier in memory --
// but after a reload, local `state` starts from nothing. Restoring a
// stale frontier makes the server send an (correctly) near-empty delta,
// and the client ends up with an empty room in view despite the server
// having everything. Always requesting a full `snapshot` after a reload
// (by never claiming a `known_frontier`) is what actually rebuilds state
// correctly; the recovered outbox is then replayed *locally* on top of
// that snapshot (see loadSnapshot's caller in sketch.js/mesh3d.js) before
// being flushed to the server for real.

const OFFLINE_DB_NAME = "crdt_cad_offline";
const OFFLINE_STORE = "state";

function _offlineDbKey(kind, room, actorId) {
  return `${kind}:${room}:${actorId}`;
}

function _openOfflineDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OFFLINE_DB_NAME, 1);
    req.onupgradeneeded = () => { req.result.createObjectStore(OFFLINE_STORE); };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** Returns the persisted outbox (a list of ops), or `[]` if nothing was
 * persisted (the common case) or IndexedDB is unavailable. */
async function loadPersistedOutbox(kind, room, actorId) {
  try {
    const db = await _openOfflineDb();
    const value = await new Promise((resolve, reject) => {
      const tx = db.transaction(OFFLINE_STORE, "readonly");
      const req = tx.objectStore(OFFLINE_STORE).get(_offlineDbKey(kind, room, actorId));
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
    db.close();
    return value || [];
  } catch (err) {
    console.warn("crdt-cad: could not read persisted offline outbox from IndexedDB", err);
    return [];
  }
}

/** Persists (or, once empty again, deletes) the current offline outbox.
 * Best-effort: a failure here degrades to today's in-memory-only
 * behavior, never blocks sending. */
async function persistOutbox(kind, room, actorId, outbox) {
  try {
    const db = await _openOfflineDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(OFFLINE_STORE, "readwrite");
      if (outbox.length) {
        tx.objectStore(OFFLINE_STORE).put(outbox, _offlineDbKey(kind, room, actorId));
      } else {
        tx.objectStore(OFFLINE_STORE).delete(_offlineDbKey(kind, room, actorId));
      }
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    db.close();
  } catch (err) {
    console.warn("crdt-cad: could not persist offline outbox to IndexedDB", err);
  }
}

/**
 * Handles the hello/snapshot/delta/ops WebSocket protocol against a
 * crdt_cad relay room, including offline queueing and auto-reconnect.
 *
 * Callbacks:
 *   onSnapshot(doc)             -- full authoritative state (new client)
 *   onDelta(ops)                -- ops missed while offline (reconnect)
 *   onOps(ops, fromActor)       -- live broadcast from another client
 *   onStatus(status)            -- "connecting" | "online" | "offline"
 *   onRejected(reason, op)      -- server refused one op (validity gate)
 *   onSignal(fromActor, data)   -- WebRTC signaling payload from a peer
 *   onSaved(at)                 -- explicit save confirmed durable
 *   onValidityWarning(faces, problems) -- mesh rooms only (see
 *     crdt_cad.geometry.mesh_validity): a merge just produced
 *     cross-component-inconsistent geometry (e.g. a face boundary
 *     referencing a vertex a concurrent edit deleted). A warning, not a
 *     rejection -- the ops already applied and converged; this is purely
 *     "something here needs a human to look at it."
 *   onMergePreview(mine, theirs, proceed) -- "Time-Travel Merge": called
 *     instead of auto-applying a reconnect delta when *both* sides
 *     changed something while we were apart. Call proceed() to continue
 *     (apply their ops, then flush ours) whenever the caller is ready --
 *     the CRDT converges either way, this is purely a review step.
 *
 * Options:
 *   token               -- room auth token, or null (see security.py)
 *   kind, room           -- identify the offline-outbox IndexedDB row;
 *     omit both to disable outbox persistence (falls back to the old
 *     in-memory-only behavior)
 *   initialOutbox        -- ops loaded via loadPersistedOutbox() *before*
 *     constructing this connection, so a client that went offline,
 *     queued edits, and was hard-refreshed (or had its tab closed)
 *     resumes with its queued ops intact. This connection always
 *     requests a fresh full `snapshot` on the *first* connect of its
 *     lifetime (a fresh page load never claims a `known_frontier`) so
 *     local state rebuilds correctly regardless of what's recovered --
 *     see the long comment above loadPersistedOutbox() for why a
 *     restored frontier would be unsound here. The caller is
 *     responsible for re-applying `initialOutbox` to its own local
 *     state after `onSnapshot` fires (this class only resends it to the
 *     server; it has no rendering of its own to update).
 */
// WebSocket close codes the server uses for its optional security hardening
// (crdt_cad.server.app, WS_CLOSE_* constants) -- mirrored here so the client
// can react appropriately instead of treating them as an ordinary drop.
const WS_CLOSE_UNAUTHORIZED = 4401;

class RelayConnection {
  constructor(
    wsPath,
    actorId,
    {
      onSnapshot, onDelta, onOps, onStatus, onRejected, onSignal, onSaved, onValidityWarning, onMergePreview,
      token, kind, room, initialOutbox,
    },
  ) {
    this.wsPath = wsPath;
    this.actorId = actorId;
    this.token = token || null;
    // `kind`/`room` identify the offline-outbox IndexedDB row; omit them
    // (both null) to opt out of persistence entirely and get the old
    // in-memory-only behavior.
    this.kind = kind || null;
    this.room = room || null;
    this.onSnapshot = onSnapshot || (() => {});
    this.onDelta = onDelta || (() => {});
    this.onOps = onOps || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onRejected = onRejected || (() => {});
    this.onSignal = onSignal || (() => {});
    this.onSaved = onSaved || (() => {});
    this.onValidityWarning = onValidityWarning || (() => {});
    this.onMergePreview = onMergePreview || null;
    this.frontier = new FrontierTracker();
    this.ws = null;
    this.outbox = initialOutbox ? initialOutbox.slice() : [];
    this.userWantsOffline = false;
    this._reconnectDelay = 1000;
    this._connect();
  }

  _persistOutbox() {
    if (!this.kind || !this.room) return;
    persistOutbox(this.kind, this.room, this.actorId, this.outbox);
  }

  _wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${this.wsPath}`;
  }

  _connect() {
    if (this.userWantsOffline) return;
    this.onStatus("connecting");
    const ws = new WebSocket(this._wsUrl());
    this.ws = ws;
    ws.onopen = () => {
      this._reconnectDelay = 1000;
      ws.send(JSON.stringify({
        type: "hello",
        actor: this.actorId,
        token: this.token,
        known_frontier: Object.keys(this.frontier.counters).length ? this.frontier.toDict() : null,
      }));
    };
    ws.onmessage = (event) => this._handleMessage(JSON.parse(event.data));
    ws.onclose = (event) => {
      if (event.code === WS_CLOSE_UNAUTHORIZED) {
        // The room's shared secret changed, our token expired, or it was
        // never valid -- retrying with the same bad token forever would
        // just spam reconnect attempts that can never succeed. Surface a
        // distinct status so the UI can prompt for a fresh secret instead.
        this.onStatus("unauthorized");
        return;
      }
      this.onStatus("offline");
      if (!this.userWantsOffline) {
        setTimeout(() => this._connect(), this._reconnectDelay);
        this._reconnectDelay = Math.min(this._reconnectDelay * 1.5, 10000);
      }
    };
    ws.onerror = () => ws.close();
  }

  _handleMessage(msg) {
    if (msg.type === "snapshot") {
      this.frontier.recordAll(msg.frontier);
      this.onSnapshot(msg.doc);
      this.onStatus("online");
      this._flushOutbox();
    } else if (msg.type === "delta") {
      this.frontier.recordAll(msg.frontier);
      const offlineOps = this.outbox.slice();
      const proceed = () => {
        this.onDelta(msg.ops);
        this.onStatus("online");
        this._flushOutbox();
      };
      if (offlineOps.length && msg.ops.length && this.onMergePreview) {
        this.onMergePreview(offlineOps, msg.ops, proceed);
      } else {
        proceed();
      }
    } else if (msg.type === "ops") {
      for (const op of msg.ops) this._recordOpFrontier(op);
      this.onOps(msg.ops, msg.from);
    } else if (msg.type === "frontier") {
      // The lightweight periodic ping (see Room._snapshot_loop): if it's
      // ahead of what this replica has recorded, ask for a catch-up.
      // Reuses the exact same "resync" -> "delta"/"snapshot" round trip a
      // reconnect already uses, so it's handled by the branches above with
      // no special-casing -- an online client's outbox is normally empty,
      // so this never triggers a Time-Travel Merge preview in the common
      // case, only in the (correct) rare one where it isn't.
      if (this.frontier.isBehind(msg.frontier) && this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          type: "resync",
          known_frontier: Object.keys(this.frontier.counters).length ? this.frontier.toDict() : null,
        }));
      }
    } else if (msg.type === "rejected") {
      this.onRejected(msg.reason, msg.op);
    } else if (msg.type === "signal") {
      this.onSignal(msg.from, msg.data);
    } else if (msg.type === "saved") {
      this.onSaved(msg.at);
    } else if (msg.type === "validity_warning") {
      this.onValidityWarning(msg.faces, msg.problems);
    }
  }

  _recordOpFrontier(op) {
    const payload = op.payload || {};
    if (payload.id) this.frontier.record(payload.id);
  }

  _flushOutbox() {
    if (!this.outbox.length || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "ops", ops: this.outbox }));
    this.outbox = [];
    this._persistOutbox(); // clears the now-empty persisted queue
  }

  /** Send (or, if offline, queue) one or more ops that were already
   * applied optimistically to local state by the caller. */
  send(ops) {
    for (const op of ops) this._recordOpFrontier(op);
    if (this.userWantsOffline || !this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.outbox.push(...ops);
      this._persistOutbox(); // survives a hard refresh/closed tab while offline
      return;
    }
    this.ws.send(JSON.stringify({ type: "ops", ops }));
  }

  goOffline() {
    this.userWantsOffline = true;
    this.onStatus("offline");
    if (this.ws) this.ws.close();
  }

  goOnline() {
    if (!this.userWantsOffline) return;
    this.userWantsOffline = false;
    this._connect();
  }

  /** Send a WebRTC signaling payload (SDP offer/answer/ICE candidate) to
   * one specific peer in the room; the server relays it verbatim. */
  signal(toActor, data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "signal", to: toActor, data }));
    }
  }

  /** Force an immediate durable snapshot (beyond the automatic
   * persist-on-every-accepted-op the server already does); replies via
   * onSaved so the UI can show a confirmation. */
  save() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "save" }));
    }
  }
}

/** Monotonically increasing local Lamport counter used to mint OpIds
 * client-side, exactly matching the [counter, actor] wire format the
 * Python LamportClock produces. */
class LocalClock {
  constructor(actor) {
    this.actor = actor;
    this.counter = 0;
  }
  tick() {
    this.counter += 1;
    return [this.counter, this.actor];
  }
  /** Lamport clock rule: bump past any counter this replica has observed,
   * so a later local edit is guaranteed a higher OpId than anything it has
   * already seen (including a big batch from another actor, e.g. an AI
   * generator or an import) and doesn't lose an LWW tie-break it should
   * win just because this replica's own counter started fresh at 0.
   * Mirrors LamportClock.observe() on the Python server side. */
  observe(counter) {
    if (counter > this.counter) this.counter = counter;
  }
}

function lwwOp(id, key, value, deleted = false) {
  return { id, k: key, v: deleted ? null : value, d: deleted };
}

function rgaInsertOp(id, origin, value) {
  return { t: "ins", id, o: origin, v: value };
}

function rgaDeleteOp(target, id) {
  return { t: "del", target, id };
}

function idEq(a, b) {
  return a && b && a[0] === b[0] && a[1] === b[1];
}

/** Applies one RGA insert/delete payload to a plain array of
 * {id, o, v, db} nodes already in document order. Shared by the 2D
 * (path points) and 3D (face vertex loops) demos -- both are just
 * "ordered sequence of values", so the same tiny applier works for
 * either. Safe against duplicate/out-of-order delivery: inserts are
 * deduped by id before splicing. */
function applyRgaOp(nodes, payload) {
  if (payload.t === "ins") {
    if (nodes.some((n) => idEq(n.id, payload.id))) return;
    const node = { id: payload.id, o: payload.o, v: payload.v, db: null };
    if (payload.o == null) {
      nodes.unshift(node);
    } else {
      const idx = nodes.findIndex((n) => idEq(n.id, payload.o));
      nodes.splice(idx === -1 ? nodes.length : idx + 1, 0, node);
    }
  } else if (payload.t === "del") {
    const node = nodes.find((n) => idEq(n.id, payload.target));
    if (node) node.db = payload.id;
  }
}

function liveValues(nodes) {
  return (nodes || []).filter((n) => !n.db).map((n) => n.v);
}

/** Same as liveValues, but keeps each node's `id` alongside its value --
 * needed wherever a per-node id-keyed prop (e.g. sketch.js's curve
 * segments, see curvePropKey) has to be looked up while walking a live
 * sequence, not just the plain value. */
function liveEntries(nodes) {
  return (nodes || []).filter((n) => !n.db);
}

/** Mirrors crdt_cad.crdt.clock.OpId.__str__ ("{counter}@{actor}") so a
 * curve prop key built client-side (curvePropKey in sketch.js) matches
 * the key the server/Python side stores it under exactly. */
function opIdKey(id) {
  return `${id[0]}@${id[1]}`;
}

// -- shared UI feedback: toasts + the Time-Travel Merge preview modal --------------

function showToast(message, kind = "info") {
  let container = document.getElementById("toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.style.cssText =
      "position:fixed;bottom:40px;left:50%;transform:translateX(-50%);z-index:1000;" +
      "display:flex;flex-direction:column;gap:6px;align-items:center;";
    document.body.appendChild(container);
  }
  const colors = { info: "#4dabf7", success: "#38d9a9", error: "#ff6b6b" };
  const el = document.createElement("div");
  el.textContent = message;
  el.style.cssText =
    `padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;color:#06121a;` +
    `background:${colors[kind] || colors.info};box-shadow:0 4px 16px rgba(0,0,0,0.35);` +
    "transition:opacity 0.3s;";
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, 2800);
}

/** Summarizes a batch of DrawingDocument ops into short human-readable
 * bullet points for the merge-preview modal. */
function describeDocOps(ops) {
  const counts = {};
  for (const op of ops) {
    const p = op.payload;
    let label;
    if (op.target === "layer") label = p.d ? "removed a layer" : "added a layer";
    else if (op.target === "path_index") label = p.d ? "removed a path" : "added a path";
    else if (op.target === "path_prop") label = `changed "${p.k}" on a path`;
    else if (op.target === "path_geom") label = p.t === "ins" ? "extended a path" : "removed a point";
    else if (op.target === "comment") label = p.d ? "removed a comment" : "added a comment";
    else continue; // presence etc. -- not meaningful for a merge review
    counts[label] = (counts[label] || 0) + 1;
  }
  return Object.entries(counts).map(([label, n]) => (n > 1 ? `${label} (×${n})` : label));
}

/** Same idea for MeshCRDT ops. */
function describeMeshOps(ops) {
  const counts = {};
  for (const op of ops) {
    const p = op.payload;
    let label;
    if (op.target === "vertex") label = p.d ? "removed a vertex" : "added/moved a vertex";
    else if (op.target === "edge") label = p.d ? "removed an edge" : "added an edge";
    else if (op.target === "face_index") label = p.d ? "removed a face" : "added a face";
    else if (op.target === "face_geom") label = "edited a face boundary";
    else continue;
    counts[label] = (counts[label] || 0) + 1;
  }
  return Object.entries(counts).map(([label, n]) => (n > 1 ? `${label} (×${n})` : label));
}

/** The interactive "Time-Travel Merge" panel: shown when reconnecting
 * after an offline stretch during which *both* sides changed something.
 * This is a review step, not a manual conflict-resolution step -- the
 * CRDT has already guaranteed a consistent merged result either way;
 * this just shows the user what happened on each branch before applying. */
function showMergePreviewModal(offlineOps, remoteOps, describeOps, onProceed) {
  const overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(6,8,12,0.72);z-index:2000;" +
    "display:flex;align-items:center;justify-content:center;";
  const box = document.createElement("div");
  box.style.cssText =
    "background:#1c1f26;border:1px solid #2e333d;border-radius:10px;padding:20px;" +
    "max-width:520px;width:90%;color:#e7e9ee;font-size:13px;font-family:inherit;";
  const mine = describeOps(offlineOps);
  const theirs = describeOps(remoteOps);
  const list = (items) =>
    items.length ? `<ul style="margin:0;padding-left:18px;">${items.map((s) => `<li>${s}</li>`).join("")}</ul>` : '<div style="color:#9aa1ad">(nothing visible-- ephemeral only)</div>';
  box.innerHTML = `
    <h3 style="margin:0 0 8px;font-size:15px;">Reconnected -- Time-Travel Merge</h3>
    <p style="color:#9aa1ad;margin:0 0 14px;line-height:1.5;">
      You edited while offline. Here's what changed on each branch -- the CRDT
      engine merges both automatically once you continue, with nothing lost
      and no conflicts to resolve by hand.
    </p>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px;">
      <div>
        <div style="font-weight:700;color:#4dabf7;margin-bottom:4px;">Your offline branch</div>
        <div style="color:#9aa1ad;font-size:11px;margin-bottom:6px;">${offlineOps.length} op(s)</div>
        ${list(mine)}
      </div>
      <div>
        <div style="font-weight:700;color:#38d9a9;margin-bottom:4px;">Their branch (while you were away)</div>
        <div style="color:#9aa1ad;font-size:11px;margin-bottom:6px;">${remoteOps.length} op(s)</div>
        ${list(theirs)}
      </div>
    </div>
    <button id="mergeProceedBtn" style="width:100%;background:#4dabf7;border:none;color:#06121a;font-weight:700;padding:9px;border-radius:6px;cursor:pointer;font-size:13px;">Merge now</button>
  `;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  box.querySelector("#mergeProceedBtn").onclick = () => {
    overlay.remove();
    onProceed();
  };
}

// -- WebRTC P2P data-channel sync, signaling relayed over the WS relay -----------

const STUN_SERVERS = [{ urls: "stun:stun.l.google.com:19302" }];

/**
 * Direct client-to-client op sync over a WebRTC DataChannel, falling
 * back to (and always running alongside) the server relay -- per the
 * brief: "Integrate WebRTC for direct client-to-client sync when
 * possible... Fall back to server relay." The server relay is never
 * bypassed for real (it's still what persists state and serves late
 * joiners); this is a latency/decentralization optimization layered on
 * top, using the browser's native RTCPeerConnection (no extra library
 * needed client-side -- aiortc is a *Python* WebRTC peer implementation,
 * useful if a Python process ever needs to join as a peer, not for
 * relaying signaling messages between two browsers, which is just
 * message-passing the existing relay already does generically).
 */
class P2PManager {
  constructor(relayConnection, actorId, { onPeerData, onPeerStatus } = {}) {
    this.relay = relayConnection;
    this.actorId = actorId;
    this.onPeerData = onPeerData || (() => {});
    this.onPeerStatus = onPeerStatus || (() => {});
    this.peers = new Map(); // peerActorId -> { pc, channel }
    this.relay.onSignal = (from, data) => this._handleSignal(from, data);
  }

  get connectedCount() {
    let n = 0;
    for (const entry of this.peers.values()) {
      if (entry.channel && entry.channel.readyState === "open") n++;
    }
    return n;
  }

  /** Deterministic initiator rule (avoids both sides racing to send an
   * offer at once): only the lexically-greater actor id calls out. */
  maybeConnectTo(peerActorId) {
    if (peerActorId === this.actorId || this.peers.has(peerActorId)) return;
    if (this.relay.userWantsOffline) return; // don't form new P2P links while "offline"
    if (this.actorId > peerActorId) this._connectTo(peerActorId);
  }

  /** Tears down every peer connection. Must be called when the user goes
   * "offline" -- otherwise an already-established WebRTC data channel
   * would keep carrying ops directly between peers, silently defeating
   * the offline simulation (the WS relay being closed wouldn't be the
   * only path ops travel over). */
  disconnectAll() {
    for (const entry of this.peers.values()) {
      try {
        if (entry.channel) entry.channel.close();
        if (entry.pc) entry.pc.close();
      } catch (err) {
        // already closed/errored -- fine, we're tearing down anyway
      }
    }
    this.peers.clear();
    this.onPeerStatus(null, "all-disconnected");
  }

  async _connectTo(peerActorId) {
    if (typeof RTCPeerConnection === "undefined") return;
    const pc = new RTCPeerConnection({ iceServers: STUN_SERVERS });
    const channel = pc.createDataChannel("crdt-ops");
    this._wireChannel(peerActorId, pc, channel);
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      this.relay.signal(peerActorId, { kind: "offer", sdp: pc.localDescription });
    } catch (err) {
      // WebRTC unsupported/blocked in this environment -- server relay still works fine.
    }
  }

  async _handleSignal(fromActor, data) {
    if (typeof RTCPeerConnection === "undefined") return;
    let entry = this.peers.get(fromActor);
    try {
      if (data.kind === "offer") {
        const pc = new RTCPeerConnection({ iceServers: STUN_SERVERS });
        entry = { pc, channel: null };
        this.peers.set(fromActor, entry);
        pc.ondatachannel = (e) => this._wireChannel(fromActor, pc, e.channel);
        pc.onicecandidate = (e) => {
          if (e.candidate) this.relay.signal(fromActor, { kind: "ice", candidate: e.candidate });
        };
        await pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        this.relay.signal(fromActor, { kind: "answer", sdp: pc.localDescription });
      } else if (data.kind === "answer" && entry) {
        await entry.pc.setRemoteDescription(new RTCSessionDescription(data.sdp));
      } else if (data.kind === "ice" && entry) {
        await entry.pc.addIceCandidate(data.candidate);
      }
    } catch (err) {
      // Signaling races/unsupported browsers shouldn't break the app -- relay path is unaffected.
    }
  }

  _wireChannel(peerActorId, pc, channel) {
    const entry = this.peers.get(peerActorId) || { pc, channel: null };
    entry.pc = pc;
    entry.channel = channel;
    this.peers.set(peerActorId, entry);
    if (!pc.onicecandidate) {
      pc.onicecandidate = (e) => {
        if (e.candidate) this.relay.signal(peerActorId, { kind: "ice", candidate: e.candidate });
      };
    }
    channel.onopen = () => this.onPeerStatus(peerActorId, "connected");
    channel.onclose = () => {
      this.onPeerStatus(peerActorId, "disconnected");
      this.peers.delete(peerActorId);
    };
    channel.onerror = () => {};
    channel.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "ops") this.onPeerData(peerActorId, msg.ops);
      } catch (err) {
        // ignore malformed peer payloads
      }
    };
  }

  /** Best-effort direct broadcast to every connected peer; the caller
   * always ALSO sends via the server relay -- this only shaves latency
   * for peers with an open data channel, never replaces the relay. */
  broadcastOps(ops) {
    const payload = JSON.stringify({ type: "ops", ops });
    for (const entry of this.peers.values()) {
      if (entry.channel && entry.channel.readyState === "open") {
        try {
          entry.channel.send(payload);
        } catch (err) {
          // ignore -- relay path already carries these ops
        }
      }
    }
  }
}
