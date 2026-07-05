// 2D collaborative sketch demo built directly on the crdt_cad.crdt.document
// wire protocol. See common.js for the WebSocket relay client and the
// design note on why merge logic stays server-side.

const actorId = getOrCreateActorId();
let actorName = getOrCreateActorName();
const actorColor = colorForActor(actorId);
const room = new URLSearchParams(location.search).get("room") || "demo";
document.getElementById("roomInput").value = room;
document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;

const clock = new LocalClock(actorId);
const rid = () => Math.random().toString(36).slice(2, 10);

const state = {
  layers: new Map(),      // id -> {name, ...}
  layerOrder: [],
  pathIndex: new Set(),
  pathProps: new Map(),   // id -> {layer_id, color, stroke_width}
  pathNodes: new Map(),   // id -> [{id,o,v,db}]  (already in document order)
  comments: new Map(),
  presence: new Map(),
};

const ui = {
  tool: "pen",
  activeLayer: null,
  hiddenLayers: new Set(),
  selectedPath: null,
  opsCount: 0,
};
const undoStack = [];
const redoStack = [];
let pendingPolygon = [];
// -- interactive constraint solver UI (Phase 9) -----------------------------
// Up to 2 {pathId, nodeId, pos} entries -- see the "constrain" tool's click
// handler and applyConstraint() below. The already-tested `/api/solve`
// endpoint (crdt_cad.geometry.constraints) does the actual solving; this
// is purely a client-side workflow on top of it, same as every other
// edit here -- points move via the exact same delete+insert path_geom
// ops any other point move would use (see movePathPoint), broadcast the
// normal way so everyone in the room sees it.
let constraintSelection = [];
let lastMousePt = null; // world coordinates -- see the view transform section below

// -- view transform (Phase 10) ------------------------------------------------
// Pan (panX/panY, screen pixels the world origin is offset by) + zoom are
// purely client-local UI state -- never synced, never touching the CRDT --
// per the brief: "the view transform is client-local state -- it is not
// CRDT data and must not sync." All stored/sent geometry (path points,
// presence cursors) is in *world* coordinates; only rendering and input
// mapping go through this transform. A fresh view (panX=0, panY=0, zoom=1)
// maps world 1:1 to screen pixels, so every pre-existing room's pixel-space
// data renders exactly as it always did -- world space is a strict superset.
const view = { panX: 0, panY: 0, zoom: 1 };
let snapToGridEnabled = false;

function screenToWorld(sx, sy) {
  return [(sx - view.panX) / view.zoom, (sy - view.panY) / view.zoom];
}
function worldToScreen(wx, wy) {
  return [wx * view.zoom + view.panX, wy * view.zoom + view.panY];
}

/** Picks a "nice" world-space grid step (1/2/5 x10^n) so its on-screen
 * spacing stays in a comfortable, zoom-independent pixel range -- used for
 * both grid rendering and snap-to-grid, so snapping always matches
 * whatever grid is currently visible. */
function pickGridStep(zoom) {
  const targetScreenPx = 60;
  const rawStep = targetScreenPx / zoom;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const residual = rawStep / magnitude;
  let nice;
  if (residual < 1.5) nice = 1;
  else if (residual < 3.5) nice = 2;
  else if (residual < 7.5) nice = 5;
  else nice = 10;
  return nice * magnitude;
}

function snapWorldPoint([wx, wy]) {
  if (!snapToGridEnabled) return [wx, wy];
  const step = pickGridStep(view.zoom);
  return [Math.round(wx / step) * step, Math.round(wy / step) * step];
}

// -- wire <-> local state -----------------------------------------------------

/** Bumps `clock` past every OpId found in a freshly-loaded snapshot -- see
 * the identical helper (and the LWW tie-break bug it fixes) in mesh3d.js /
 * LocalClock.observe() in common.js for the full rationale. Without this, a
 * fresh client's first edit to something another actor already touched at a
 * higher counter (e.g. a bulk SVG/DXF import) would silently lose. */
function observeSnapshotCounters(doc) {
  let maxCounter = 0;
  const bump = (idPair) => { if (idPair && idPair[0] > maxCounter) maxCounter = idPair[0]; };
  const scanEntries = (lww) => { if (lww) for (const e of lww.entries) bump(e.id); };
  scanEntries(doc.layers);
  for (const m of Object.values(doc.layer_props || {})) scanEntries(m);
  scanEntries(doc.path_index);
  for (const m of Object.values(doc.path_props || {})) scanEntries(m);
  for (const rga of Object.values(doc.paths || {})) {
    for (const n of rga.nodes) { bump(n.id); bump(n.db); }
  }
  scanEntries(doc.comments);
  scanEntries(doc.presence);
  clock.observe(maxCounter);
}

function loadSnapshot(doc) {
  observeSnapshotCounters(doc);
  state.layers.clear();
  state.layerOrder = [];
  for (const e of doc.layers.entries) {
    if (!e.d) {
      if (!state.layers.has(e.k)) state.layerOrder.push(e.k);
      state.layers.set(e.k, {});
    }
  }
  for (const [lid, m] of Object.entries(doc.layer_props)) {
    if (!state.layers.has(lid)) continue;
    const props = {};
    for (const e of m.entries) if (!e.d) props[e.k] = e.v;
    state.layers.set(lid, props);
  }
  state.pathIndex = new Set(doc.path_index.entries.filter((e) => !e.d).map((e) => e.k));
  state.pathProps.clear();
  for (const [pid, m] of Object.entries(doc.path_props)) {
    const props = {};
    for (const e of m.entries) if (!e.d) props[e.k] = e.v;
    state.pathProps.set(pid, props);
  }
  state.pathNodes.clear();
  for (const [pid, rga] of Object.entries(doc.paths)) {
    state.pathNodes.set(pid, rga.nodes.slice());
  }
  state.comments.clear();
  for (const e of doc.comments.entries) if (!e.d) state.comments.set(e.k, e.v);
  state.presence.clear();
  for (const e of doc.presence.entries) if (!e.d) state.presence.set(e.k, e.v);

  if (!ui.activeLayer || !state.layers.has(ui.activeLayer)) {
    ui.activeLayer = state.layerOrder[0] || null;
  }
  renderAll();
}

function applyOp(op) {
  const p = op.payload;
  if (p && p.id) clock.observe(p.id[0]);
  if (op.target === "layer") {
    if (!p.d) {
      if (!state.layers.has(p.k)) {
        state.layerOrder.push(p.k);
        state.layers.set(p.k, {});
        if (!ui.activeLayer) ui.activeLayer = p.k;
      }
    } else {
      state.layers.delete(p.k);
      state.layerOrder = state.layerOrder.filter((id) => id !== p.k);
    }
  } else if (op.target === "layer_prop") {
    const props = state.layers.get(op.scope) || {};
    if (!p.d) props[p.k] = p.v; else delete props[p.k];
    state.layers.set(op.scope, props);
  } else if (op.target === "path_index") {
    if (!p.d) state.pathIndex.add(p.k); else state.pathIndex.delete(p.k);
  } else if (op.target === "path_prop") {
    const props = state.pathProps.get(op.scope) || {};
    if (!p.d) props[p.k] = p.v; else delete props[p.k];
    state.pathProps.set(op.scope, props);
  } else if (op.target === "path_geom") {
    applyGeomOp(op.scope, p);
  } else if (op.target === "comment") {
    if (!p.d) state.comments.set(p.k, p.v); else state.comments.delete(p.k);
  } else if (op.target === "presence") {
    if (!p.d) {
      state.presence.set(p.k, p.v);
      p2p.maybeConnectTo(p.k);
    } else {
      state.presence.delete(p.k);
    }
  }
}

function applyGeomOp(pathId, payload) {
  let nodes = state.pathNodes.get(pathId);
  if (!nodes) { nodes = []; state.pathNodes.set(pathId, nodes); }
  applyRgaOp(nodes, payload);
}

function pathPoints(pathId) {
  return liveValues(state.pathNodes.get(pathId));
}

/** Mirrors crdt_cad.crdt.document.curve_prop_key -- the path_prop key a
 * curve segment (Phase 8) arriving at this anchor point is stored under,
 * server-side and here alike. See render()'s path-drawing loop. */
function curvePropKey(nodeId) {
  return `curve:${opIdKey(nodeId)}`;
}

/** Undoes a locally-optimistic apply for an op the server refused (the
 * geometry validity gate rejected it). Only path_geom inserts can be
 * rejected today -- see _validate_op server-side. */
function revertRejectedOp(op) {
  if (op.target !== "path_geom" || op.payload.t !== "ins") return;
  const nodes = state.pathNodes.get(op.scope);
  if (!nodes) return;
  const idx = nodes.findIndex((n) => idEq(n.id, op.payload.id));
  if (idx !== -1) nodes.splice(idx, 1);
}

function applyIncomingOps(ops) {
  for (const op of ops) applyOp(op);
  ui.opsCount += ops.length;
  renderAll();
}

// -- relay connection -----------------------------------------------------------

// `conn`/`p2p` are assigned inside the async bootstrap below (ensureRoomAccess
// awaits an /api/auth/required check, and possibly a passphrase prompt,
// before a token -- or null, on the zero-config default -- is available).
// Every reference elsewhere in this file is inside a function or event
// handler, so it only runs well after this has resolved.
let conn, p2p;

(async () => {
  const token = await ensureRoomAccess("drawing", room);
  const persistedOutbox = await loadPersistedOutbox("drawing", room, actorId);
  if (persistedOutbox.length) {
    showToast(`Recovered ${persistedOutbox.length} offline edit(s) from before this page loaded`, "info");
  }
  conn = new RelayConnection(`/ws/${encodeURIComponent(room)}`, actorId, {
    onSnapshot: (doc) => {
      loadSnapshot(doc);
      // Replay recovered-but-unsent edits on top of the fresh snapshot --
      // the server never echoes ops back to the actor that sent them, so
      // without this, edits recovered from before a hard refresh would be
      // silently missing from view even though they get flushed and
      // correctly persisted server-side.
      for (const op of persistedOutbox) applyOp(op);
      if (persistedOutbox.length) renderAll();
    },
    onDelta: (ops) => applyIncomingOps(ops),
    onOps: (ops) => applyIncomingOps(ops),
    onStatus: (status) => setStatus(status),
    onRejected: (reason, op) => {
      revertRejectedOp(op);
      renderAll();
      showToast(`Rejected: ${reason}`, "error");
    },
    onSaved: () => showToast("Saved", "success"),
    onMergePreview: (mine, theirs, proceed) => showMergePreviewModal(mine, theirs, describeDocOps, proceed),
    token,
    kind: "drawing",
    room,
    initialOutbox: persistedOutbox,
  });

  p2p = new P2PManager(conn, actorId, {
    onPeerData: (_peerActorId, ops) => applyIncomingOps(ops),
    onPeerStatus: () => updateP2pPill(),
  });
})();

function updateP2pPill() {
  const pill = document.getElementById("p2pPill");
  const count = p2p.connectedCount;
  if (count > 0) {
    pill.style.display = "";
    pill.className = "status-pill online";
    document.getElementById("p2pText").textContent = `P2P ×${count}`;
  } else {
    pill.style.display = "none";
  }
}

function sendOps(ops) {
  ui.opsCount += ops.length;
  conn.send(ops);
  if (!conn.userWantsOffline) p2p.broadcastOps(ops);
}

function setStatus(status) {
  const pill = document.getElementById("statusPill");
  pill.className = `status-pill ${status}`;
  document.getElementById("statusText").textContent = status;
  document.getElementById("offlineToggle").textContent = status === "offline" ? "Reconnect" : "Go offline";
  if (status === "unauthorized") {
    // The token we had (or lack thereof) was rejected -- clear it and
    // re-prompt rather than let RelayConnection keep retrying with the
    // same bad token forever (see the WS_CLOSE_UNAUTHORIZED handling in
    // common.js). Also strip any ?token= from the URL first: otherwise
    // ensureRoomAccess() would just re-adopt the same just-proven-bad
    // token from the URL again instead of ever reaching the re-prompt.
    clearRoomToken("drawing", room);
    const url = new URL(location.href);
    url.searchParams.delete("token");
    history.replaceState({}, "", url);
    showToast("Incorrect or expired room secret -- please try again", "error");
    ensureRoomAccess("drawing", room).then((token) => {
      conn.token = token;
      conn._connect();
    });
  }
}

document.getElementById("offlineToggle").onclick = () => {
  if (conn.userWantsOffline) {
    conn.goOnline();
  } else {
    conn.goOffline();
    p2p.disconnectAll(); // otherwise an already-open P2P data channel would keep syncing behind the WS's back
    updateP2pPill();
  }
};

document.getElementById("roomInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    const v = e.target.value.trim() || "demo";
    location.search = `?room=${encodeURIComponent(v)}`;
  }
});

// -- save / download / import / share --------------------------------------------

document.getElementById("saveBtn").onclick = () => conn.save();

function triggerDownload(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
document.getElementById("downloadJsonBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/json`, "drawing", room));
document.getElementById("downloadSvgBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/svg`, "drawing", room));
document.getElementById("downloadDxfBtn").onclick = () =>
  triggerDownload(withToken(`/api/rooms/${encodeURIComponent(room)}/export/dxf`, "drawing", room));

document.getElementById("importBtn").onclick = () => document.getElementById("importFileInput").click();
document.getElementById("importFileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const ext = file.name.split(".").pop().toLowerCase();
  if (ext !== "svg" && ext !== "dxf") {
    showToast("Only .svg or .dxf files are supported", "error");
    e.target.value = "";
    return;
  }
  const body = ext === "svg" ? await file.text() : await file.arrayBuffer();
  try {
    const resp = await fetch(withToken(`/api/rooms/${encodeURIComponent(room)}/import/${ext}`, "drawing", room), { method: "POST", body });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    showToast(`Imported ${result.path_count} path(s)`, "success");
  } catch (err) {
    showToast(`Import failed: ${err.message}`, "error");
  }
  e.target.value = "";
});

document.getElementById("shareBtn").onclick = async () => {
  let url = `${location.origin}/?room=${encodeURIComponent(room)}`;
  const token = roomTokenFor("drawing", room);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  try {
    await navigator.clipboard.writeText(url);
    showToast("Invite link copied to clipboard", "success");
  } catch (err) {
    showToast(url, "info");
  }
};

// -- local mutation helpers (mirrors crdt_cad.crdt.document.DrawingDocument) ---

function addLayer(name) {
  const id = "layer_" + rid();
  const ops = [];
  let op = { target: "layer", payload: lwwOp(clock.tick(), id, true, false) };
  applyOp(op); ops.push(op);
  op = { target: "layer_prop", scope: id, payload: lwwOp(clock.tick(), "name", name, false) };
  applyOp(op); ops.push(op);
  sendOps(ops);
  return id;
}

function addPath(layerId, points, color, width, extraProps = {}) {
  const id = "path_" + rid();
  const ops = [];
  let op = { target: "path_index", payload: lwwOp(clock.tick(), id, true, false) };
  applyOp(op); ops.push(op);
  const propEntries = [["layer_id", layerId], ["color", color], ["stroke_width", width], ...Object.entries(extraProps)];
  for (const [k, v] of propEntries) {
    op = { target: "path_prop", scope: id, payload: lwwOp(clock.tick(), k, v, false) };
    applyOp(op); ops.push(op);
  }
  let prevId = null;
  for (const pt of points) {
    const insId = clock.tick();
    op = { target: "path_geom", scope: id, payload: rgaInsertOp(insId, prevId, pt) };
    applyOp(op); ops.push(op);
    prevId = insId;
  }
  sendOps(ops);
  undoStack.push({ kind: "path_add", pathId: id });
  redoStack.length = 0;
  return { id, lastPointId: prevId };
}

function appendPoint(pathId, lastPointId, pt) {
  const insId = clock.tick();
  const op = { target: "path_geom", scope: pathId, payload: rgaInsertOp(insId, lastPointId, pt) };
  applyOp(op);
  sendOps([op]);
  return insId;
}

function removePath(pathId) {
  const op = { target: "path_index", payload: lwwOp(clock.tick(), pathId, null, true) };
  applyOp(op);
  sendOps([op]);
  undoStack.push({ kind: "path_remove", pathId });
  redoStack.length = 0;
  if (ui.selectedPath === pathId) ui.selectedPath = null;
}

function setPathProp(pathId, key, value) {
  const props = state.pathProps.get(pathId) || {};
  const hadPrevious = key in props;
  const previous = props[key];
  const op = { target: "path_prop", scope: pathId, payload: lwwOp(clock.tick(), key, value, false) };
  applyOp(op);
  sendOps([op]);
  undoStack.push({ kind: "prop_set", pathId, key, previous, hadPrevious, forwardValue: value });
  redoStack.length = 0;
}

function addComment(pathId, text) {
  const id = "comment_" + rid();
  const op = { target: "comment", payload: lwwOp(clock.tick(), id, { path_id: pathId, point_index: 0, text, author: actorName }, false) };
  applyOp(op);
  sendOps([op]);
}

function removeComment(id) {
  const op = { target: "comment", payload: lwwOp(clock.tick(), id, null, true) };
  applyOp(op);
  sendOps([op]);
}

// -- undo / redo: fresh inverted ops each time, not snapshots --------------------

function undo() {
  const entry = undoStack.pop();
  if (!entry) return;
  let op;
  if (entry.kind === "path_add") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, null, true) };
  } else if (entry.kind === "path_remove") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, true, false) };
  } else if (entry.kind === "prop_set") {
    op = { target: "path_prop", scope: entry.pathId, payload: entry.hadPrevious ? lwwOp(clock.tick(), entry.key, entry.previous, false) : lwwOp(clock.tick(), entry.key, null, true) };
  }
  applyOp(op);
  sendOps([op]);
  redoStack.push(entry);
  renderAll();
}

function redo() {
  const entry = redoStack.pop();
  if (!entry) return;
  let op;
  if (entry.kind === "path_add") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, true, false) };
  } else if (entry.kind === "path_remove") {
    op = { target: "path_index", payload: lwwOp(clock.tick(), entry.pathId, null, true) };
  } else if (entry.kind === "prop_set") {
    op = { target: "path_prop", scope: entry.pathId, payload: lwwOp(clock.tick(), entry.key, entry.forwardValue, false) };
  }
  applyOp(op);
  sendOps([op]);
  undoStack.push(entry);
  renderAll();
}

document.getElementById("undoBtn").onclick = undo;
document.getElementById("redoBtn").onclick = redo;

// -- presence ---------------------------------------------------------------------

let lastPresenceSent = 0;
function sendPresence(x, y) {
  const now = performance.now();
  if (now - lastPresenceSent < 60) return;
  lastPresenceSent = now;
  const op = { target: "presence", payload: lwwOp(clock.tick(), actorId, { x, y, name: actorName, color: actorColor }, false) };
  applyOp(op);
  sendOps([op]);
}

// -- canvas & tools -------------------------------------------------------------

const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const canvasWrap = document.querySelector(".canvas-wrap");

function resizeCanvas() {
  const rect = canvasWrap.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  render();
}
window.addEventListener("resize", resizeCanvas);

/** Raw canvas-relative screen pixels (unaffected by pan/zoom) -- used for
 * hit-test thresholds and pan-drag deltas, where a constant on-screen
 * distance is what actually feels right at any zoom level. */
function canvasPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return [Math.round(e.clientX - rect.left), Math.round(e.clientY - rect.top)];
}

/** The world-space point under the cursor -- what actually gets stored/
 * sent as geometry, snapped to the current grid if snap-to-grid is on. */
function worldPoint(e) {
  const [sx, sy] = canvasPoint(e);
  return snapWorldPoint(screenToWorld(sx, sy));
}

let drawing = null;
let spacePressed = false;
let panState = null; // {startSx, startSy, panX0, panY0}

function isPanGesture(e) {
  return e.button === 1 || (e.button === 0 && spacePressed);
}

canvas.addEventListener("pointerdown", (e) => {
  if (isPanGesture(e)) {
    e.preventDefault();
    const [sx, sy] = canvasPoint(e);
    panState = { startSx: sx, startSy: sy, panX0: view.panX, panY0: view.panY };
    canvas.style.cursor = "grabbing";
    return;
  }
  if (ui.tool !== "pen") return;
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const pt = worldPoint(e);
  const { id, lastPointId } = addPath(ui.activeLayer, [pt], actorColor, 2.5);
  drawing = { pathId: id, lastPointId, lastScreenPt: canvasPoint(e) };
  ui.selectedPath = id;
  renderAll();
});

canvas.addEventListener("pointermove", (e) => {
  if (panState) {
    const [sx, sy] = canvasPoint(e);
    view.panX = panState.panX0 + (sx - panState.startSx);
    view.panY = panState.panY0 + (sy - panState.startSy);
    render();
    return;
  }
  const screenPt = canvasPoint(e);
  const pt = worldPoint(e);
  sendPresence(pt[0], pt[1]);
  lastMousePt = pt;
  updateCursorReadout(pt);
  if (drawing) {
    // The "how far before adding a new point" feel should stay constant
    // on screen regardless of zoom, so this threshold compares screen
    // positions -- but the stored point itself is world-space.
    const dist = Math.hypot(screenPt[0] - drawing.lastScreenPt[0], screenPt[1] - drawing.lastScreenPt[1]);
    if (dist > 2.5) {
      drawing.lastPointId = appendPoint(drawing.pathId, drawing.lastPointId, pt);
      drawing.lastScreenPt = screenPt;
      render();
    }
  } else if (ui.tool === "polygon" && pendingPolygon.length) {
    render();
  }
});

let justPanned = false;
window.addEventListener("pointerup", () => {
  if (panState) {
    panState = null;
    justPanned = true;
    canvas.style.cursor = ui.tool === "select" ? "default" : "crosshair";
    return;
  }
  drawing = null;
});

canvas.addEventListener("click", (e) => {
  // A drag-to-pan (middle-button, or Space+left-button) still fires a
  // `click` on release -- suppress exactly that one, not real clicks.
  if (justPanned) { justPanned = false; return; }
  const screenPt = canvasPoint(e);
  const pt = worldPoint(e);
  if (ui.tool === "select") {
    ui.selectedPath = hitTestPath(screenPt);
    renderAll();
  } else if (ui.tool === "polygon") {
    if (pendingPolygon.length >= 3 && Math.hypot(...subtract(worldToScreen(...pendingPolygon[0]), screenPt)) < 10) {
      finishPolygon();
    } else {
      pendingPolygon.push(pt);
      render();
      renderToolHint();
    }
  } else if (ui.tool === "constrain") {
    const hit = hitTestPoint(screenPt);
    if (!hit) return;
    const already = constraintSelection.findIndex((s) => s.pathId === hit.pathId && idEq(s.nodeId, hit.nodeId));
    if (already !== -1) {
      constraintSelection.splice(already, 1);
    } else if (constraintSelection.length >= 2) {
      constraintSelection = [hit];
    } else {
      constraintSelection.push(hit);
    }
    render();
    renderConstraintPanel();
  }
});

function subtract(a, b) {
  return [a[0] - b[0], a[1] - b[1]];
}

canvas.addEventListener(
  "wheel",
  (e) => {
    e.preventDefault();
    const [sx, sy] = canvasPoint(e);
    const [wxBefore, wyBefore] = screenToWorld(sx, sy);
    const factor = Math.exp(-e.deltaY * 0.001);
    view.zoom = Math.max(0.05, Math.min(20, view.zoom * factor));
    // Re-anchor pan so the world point under the cursor doesn't jump --
    // "zoom centered on cursor" per the brief.
    view.panX = sx - wxBefore * view.zoom;
    view.panY = sy - wyBefore * view.zoom;
    render();
    updateZoomIndicator();
  },
  { passive: false }
);

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ui.tool === "polygon") cancelPolygon();
  if (e.code === "Space" && !["INPUT", "TEXTAREA"].includes(e.target.tagName)) {
    spacePressed = true;
    canvas.style.cursor = "grab";
    e.preventDefault(); // don't let the page scroll on spacebar
  }
});
window.addEventListener("keyup", (e) => {
  if (e.code === "Space") {
    spacePressed = false;
    canvas.style.cursor = ui.tool === "select" ? "default" : "crosshair";
  }
});

function finishPolygon() {
  if (pendingPolygon.length < 3) { cancelPolygon(); return; }
  if (!ui.activeLayer) ui.activeLayer = addLayer("Layer 1");
  const closed = [...pendingPolygon, pendingPolygon[0]];
  const { id } = addPath(ui.activeLayer, closed, "#ffd43b", 2.5, { strict: true });
  ui.selectedPath = id;
  pendingPolygon = [];
  renderAll();
}

function cancelPolygon() {
  pendingPolygon = [];
  render();
  renderToolHint();
}

/** `screenPt` is in raw canvas pixels (canvasPoint(e)) -- stored path
 * points are world-space, so each candidate is projected to screen via
 * worldToScreen before comparing, keeping the hit-test radius a constant
 * on-screen size regardless of zoom. */
function hitTestPath(screenPt) {
  let best = null, bestDist = 8;
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    const pts = pathPoints(pathId).map(([wx, wy]) => worldToScreen(wx, wy));
    for (let i = 0; i < pts.length - 1; i++) {
      const d = distToSegment(screenPt, pts[i], pts[i + 1]);
      if (d < bestDist) { bestDist = d; best = pathId; }
    }
  }
  return best;
}

/** Finds the closest *individual point* (not segment) across every
 * visible path, within a small on-screen pixel radius -- the constrain
 * tool relates specific points, not whole paths. `screenPt` is raw
 * canvas pixels; `pos` on the returned hit is world-space. */
function hitTestPoint(screenPt) {
  let best = null, bestDist = 10;
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    for (const entry of liveEntries(state.pathNodes.get(pathId))) {
      const [sx, sy] = worldToScreen(entry.v[0], entry.v[1]);
      const d = Math.hypot(screenPt[0] - sx, screenPt[1] - sy);
      if (d < bestDist) { bestDist = d; best = { pathId, nodeId: entry.id, pos: entry.v }; }
    }
  }
  return best;
}

function livePosOf(pathId, nodeId) {
  const entry = liveEntries(state.pathNodes.get(pathId)).find((n) => idEq(n.id, nodeId));
  return entry ? entry.v : null;
}

/** Parallel/perpendicular constraints relate two *lines*, not two bare
 * points -- for a point the user actually clicked, its "line" is the
 * segment to whichever live neighbor it has (the next point if there is
 * one, else the previous one). Returns null for a single-point path,
 * which has no line to define. */
function findAdjacentPoint(pathId, nodeId) {
  const entries = liveEntries(state.pathNodes.get(pathId));
  const idx = entries.findIndex((e) => idEq(e.id, nodeId));
  if (idx === -1) return null;
  if (idx + 1 < entries.length) return { pathId, nodeId: entries[idx + 1].id, pos: entries[idx + 1].v };
  if (idx - 1 >= 0) return { pathId, nodeId: entries[idx - 1].id, pos: entries[idx - 1].v };
  return null;
}

/** Moves an existing path point to a new position. RGA values are
 * immutable once inserted (no in-place "set" the way MeshCRDT's vertex
 * LWWMap has), so this is CRDT-safe delete-then-reinsert at the same
 * slot -- the point's node id changes as a result. If a curve segment
 * (Phase 8) was attached to the old id, it's orphaned (harmless dead
 * weight in path_props, but the visual effect is that segment reverts
 * to a straight line) -- an accepted trade-off for the common case this
 * solver targets (straight-line CAD-style sketches), not attempted to
 * be avoided here. */
function movePathPoint(pathId, oldNodeId, newPos) {
  const entries = liveEntries(state.pathNodes.get(pathId));
  const idx = entries.findIndex((e) => idEq(e.id, oldNodeId));
  if (idx === -1) return;
  const prevId = idx > 0 ? entries[idx - 1].id : null;
  const delOp = { target: "path_geom", scope: pathId, payload: rgaDeleteOp(oldNodeId, clock.tick()) };
  applyOp(delOp);
  const insOp = { target: "path_geom", scope: pathId, payload: rgaInsertOp(clock.tick(), prevId, newPos) };
  applyOp(insOp);
  sendOps([delOp, insOp]);
}

/** Solves the chosen constraint against the two currently-selected
 * points (see the "constrain" tool's click handler) via the existing,
 * already-tested `/api/solve` endpoint, then moves whichever points it
 * returned a changed position for. Parallel/perpendicular need two
 * *lines*, inferred from each selected point's neighbor (see
 * findAdjacentPoint) -- coincident/fixed_distance work directly on the
 * two selected points. */
async function applyConstraint(kind, param) {
  if (constraintSelection.length !== 2) return;
  const [sel1, sel2] = constraintSelection;
  const points = {};
  const refs = {};
  let pointIds;
  if (kind === "parallel" || kind === "perpendicular") {
    const adj1 = findAdjacentPoint(sel1.pathId, sel1.nodeId);
    const adj2 = findAdjacentPoint(sel2.pathId, sel2.nodeId);
    if (!adj1 || !adj2) {
      showToast("Both selected points need a neighboring point to define a line", "error");
      return;
    }
    refs.p1a = sel1; refs.p1b = adj1; refs.p2a = sel2; refs.p2b = adj2;
    for (const [key, ref] of Object.entries(refs)) points[key] = livePosOf(ref.pathId, ref.nodeId);
    pointIds = ["p1a", "p1b", "p2a", "p2b"];
  } else {
    refs.p1 = sel1; refs.p2 = sel2;
    for (const [key, ref] of Object.entries(refs)) points[key] = livePosOf(ref.pathId, ref.nodeId);
    pointIds = ["p1", "p2"];
  }
  if (Object.values(points).some((p) => !p)) {
    showToast("A selected point no longer exists (concurrent edit) -- try again", "error");
    constraintSelection = [];
    renderAll();
    return;
  }

  let resp;
  try {
    resp = await fetch("/api/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ points, constraints: [{ kind, point_ids: pointIds, param: param || 0 }] }),
    });
  } catch {
    showToast("Could not reach the solver", "error");
    return;
  }
  if (!resp.ok) { showToast("Solve request failed", "error"); return; }
  const result = await resp.json();
  if (!result.converged) {
    showToast("Solver did not converge -- no changes applied", "error");
    return;
  }
  for (const [key, ref] of Object.entries(refs)) {
    const [nx, ny] = result.positions[key];
    const [ox, oy] = points[key];
    if (Math.abs(nx - ox) > 1e-6 || Math.abs(ny - oy) > 1e-6) {
      movePathPoint(ref.pathId, ref.nodeId, [nx, ny]);
    }
  }
  constraintSelection = [];
  renderAll();
  showToast("Constraint applied", "success");
}

function renderConstraintPanel() {
  const panel = document.getElementById("constraintPanel");
  if (!panel) return;
  if (constraintSelection.length < 2) {
    const remaining = 2 - constraintSelection.length;
    panel.innerHTML = `<div class="empty-hint">Constrain tool: click ${remaining} more point(s) to relate them.</div>`;
    return;
  }
  const [a, b] = constraintSelection;
  const dist = Math.hypot(a.pos[0] - b.pos[0], a.pos[1] - b.pos[1]);
  panel.innerHTML = `
    <div class="field-row"><label>Distance</label><input id="constraintDistance" type="number" step="0.1" value="${dist.toFixed(2)}" style="width:70px"/></div>
    <button id="constrainCoincident" style="width:100%;margin-bottom:4px">Coincident</button>
    <button id="constrainParallel" style="width:100%;margin-bottom:4px">Parallel</button>
    <button id="constrainPerpendicular" style="width:100%;margin-bottom:4px">Perpendicular</button>
    <button id="constrainFixedDistance" style="width:100%">Fixed distance</button>
  `;
  document.getElementById("constrainCoincident").onclick = () => applyConstraint("coincident");
  document.getElementById("constrainParallel").onclick = () => applyConstraint("parallel");
  document.getElementById("constrainPerpendicular").onclick = () => applyConstraint("perpendicular");
  document.getElementById("constrainFixedDistance").onclick = () =>
    applyConstraint("fixed_distance", parseFloat(document.getElementById("constraintDistance").value) || 0);
}

function distToSegment(p, a, b) {
  const [px, py] = p, [ax, ay] = a, [bx, by] = b;
  const dx = bx - ax, dy = by - ay;
  const lenSq = dx * dx + dy * dy;
  let t = lenSq === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));
  const cx = ax + t * dx, cy = ay + t * dy;
  return Math.hypot(px - cx, py - cy);
}

document.getElementById("toolPen").onclick = () => setTool("pen");
document.getElementById("toolSelect").onclick = () => setTool("select");
document.getElementById("toolPolygon").onclick = () => setTool("polygon");
document.getElementById("toolConstrain").onclick = () => setTool("constrain");
function setTool(tool) {
  if (ui.tool === "polygon" && tool !== "polygon") cancelPolygon();
  if (ui.tool === "constrain" && tool !== "constrain") { constraintSelection = []; renderConstraintPanel(); }
  ui.tool = tool;
  document.getElementById("toolPen").classList.toggle("active", tool === "pen");
  document.getElementById("toolSelect").classList.toggle("active", tool === "select");
  document.getElementById("toolPolygon").classList.toggle("active", tool === "polygon");
  document.getElementById("toolConstrain").classList.toggle("active", tool === "constrain");
  canvas.style.cursor = tool === "select" ? "default" : "crosshair";
  render();
  renderToolHint();
}

function renderToolHint() {
  const hint = document.getElementById("toolHint");
  if (!hint) return;
  if (ui.tool === "polygon") {
    hint.textContent = pendingPolygon.length
      ? `${pendingPolygon.length} vertex(es) -- click the first vertex again to close (rejects self-intersecting shapes), or Esc to cancel.`
      : "Click to place vertices of a strict polygon. Self-intersecting or zero-length edges are rejected by the server.";
  } else if (ui.tool === "pen") {
    hint.textContent = "Drag to draw a freehand stroke.";
  } else if (ui.tool === "constrain") {
    hint.textContent = "Click two points (any paths) to relate them -- coincident, parallel, perpendicular, or a fixed distance apart.";
  } else {
    hint.textContent = "Click a path to select it.";
  }
}

// -- view controls: zoom indicator, cursor readout, fit-to-content, snap ----------

function updateZoomIndicator() {
  document.getElementById("zoomIndicator").textContent = `${Math.round(view.zoom * 100)}%`;
}

function updateCursorReadout([wx, wy]) {
  document.getElementById("cursorCoords").textContent = `${wx.toFixed(1)}, ${wy.toFixed(1)}`;
}

/** Fits all visible (non-hidden-layer) geometry into view with some
 * padding -- an empty document resets to the identity view (zoom 1,
 * centered on the world origin) rather than leaving a stale pan/zoom
 * from before everything was deleted. */
function fitToContent() {
  const allPts = [];
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    allPts.push(...pathPoints(pathId));
  }
  const rect = canvasWrap.getBoundingClientRect();
  if (allPts.length === 0) {
    view.zoom = 1;
    view.panX = 0;
    view.panY = 0;
    render();
    updateZoomIndicator();
    return;
  }
  const xs = allPts.map((p) => p[0]), ys = allPts.map((p) => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 40;
  const contentW = Math.max(maxX - minX, 1e-6), contentH = Math.max(maxY - minY, 1e-6);
  const zoomX = (rect.width - pad * 2) / contentW;
  const zoomY = (rect.height - pad * 2) / contentH;
  view.zoom = Math.max(0.05, Math.min(20, Math.min(zoomX, zoomY)));
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  view.panX = rect.width / 2 - cx * view.zoom;
  view.panY = rect.height / 2 - cy * view.zoom;
  render();
  updateZoomIndicator();
}
document.getElementById("fitToContentBtn").onclick = fitToContent;

document.getElementById("snapToggleBtn").onclick = (e) => {
  snapToGridEnabled = !snapToGridEnabled;
  e.target.classList.toggle("active", snapToGridEnabled);
};

document.getElementById("addLayerBtn").onclick = () => {
  const id = addLayer(`Layer ${state.layerOrder.length + 1}`);
  ui.activeLayer = id;
  renderAll();
};

// -- rendering ------------------------------------------------------------------

/** Draws an adaptive grid: a minor line every `pickGridStep(zoom)` world
 * units (faded out as its on-screen spacing compresses below a readable
 * threshold -- "fade minor lines out as they compress" per the brief),
 * and a major line every 5x that. Screen-space (drawn before the world
 * transform is applied), computed from the world-space viewport bounds. */
function drawGrid(rect) {
  const step = pickGridStep(view.zoom);
  const majorStep = step * 5;
  const onScreenMinorSpacing = step * view.zoom;
  const minorAlpha = Math.max(0, Math.min(1, (onScreenMinorSpacing - 8) / (40 - 8)));

  const [wx0, wy0] = screenToWorld(0, 0);
  const [wx1, wy1] = screenToWorld(rect.width, rect.height);

  ctx.save();
  if (minorAlpha > 0.02) {
    ctx.strokeStyle = "#1c2028";
    ctx.globalAlpha = minorAlpha;
    ctx.lineWidth = 1;
    for (let wx = Math.floor(wx0 / step) * step; wx <= wx1; wx += step) {
      const [sx] = worldToScreen(wx, 0);
      ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, rect.height); ctx.stroke();
    }
    for (let wy = Math.floor(wy0 / step) * step; wy <= wy1; wy += step) {
      const [, sy] = worldToScreen(0, wy);
      ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(rect.width, sy); ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = "#2e333d";
  ctx.lineWidth = 1;
  for (let wx = Math.floor(wx0 / majorStep) * majorStep; wx <= wx1; wx += majorStep) {
    const [sx] = worldToScreen(wx, 0);
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, rect.height); ctx.stroke();
  }
  for (let wy = Math.floor(wy0 / majorStep) * majorStep; wy <= wy1; wy += majorStep) {
    const [, sy] = worldToScreen(0, wy);
    ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(rect.width, sy); ctx.stroke();
  }
  ctx.restore();
}

function render() {
  const rect = canvasWrap.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);
  drawGrid(rect);

  // Everything from here down is drawn directly in world coordinates --
  // the canvas transform (not per-point math) handles the screen mapping,
  // so a path's geometry, and its stroke_width, correctly scale with zoom
  // the same way real-world ink would.
  ctx.save();
  ctx.translate(view.panX, view.panY);
  ctx.scale(view.zoom, view.zoom);

  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    if (ui.hiddenLayers.has(props.layer_id)) continue;
    const entries = liveEntries(state.pathNodes.get(pathId));
    const pts = entries.map((n) => n.v);
    if (pts.length === 0) continue;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) {
      // A curve segment (Phase 8) is stored as a path_prop keyed by the
      // arriving anchor's own node id -- see curve_prop_key's docstring
      // in document.py. Absent (every point in a path created before
      // this existed, or any point reached via a plain L) means a
      // straight line, same as always.
      const seg = props[curvePropKey(entries[i].id)];
      if (seg && seg.kind === "cubic") {
        ctx.bezierCurveTo(seg.c1[0], seg.c1[1], seg.c2[0], seg.c2[1], pts[i][0], pts[i][1]);
      } else if (seg && seg.kind === "quad") {
        ctx.quadraticCurveTo(seg.c[0], seg.c[1], pts[i][0], pts[i][1]);
      } else {
        ctx.lineTo(pts[i][0], pts[i][1]);
      }
    }
    ctx.strokeStyle = props.color || "#e7e9ee";
    ctx.lineWidth = props.stroke_width || 2.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
    if (pathId === ui.selectedPath) {
      ctx.save();
      ctx.strokeStyle = "#4dabf7";
      ctx.lineWidth = (props.stroke_width || 2.5) + 4;
      ctx.globalAlpha = 0.25;
      ctx.stroke();
      ctx.restore();
    }
    if (pts.length === 1) {
      ctx.beginPath();
      ctx.arc(pts[0][0], pts[0][1], (props.stroke_width || 2.5), 0, Math.PI * 2);
      ctx.fillStyle = props.color || "#e7e9ee";
      ctx.fill();
    }
  }

  if (ui.tool === "polygon" && pendingPolygon.length) {
    ctx.save();
    ctx.strokeStyle = "#ffd43b";
    ctx.lineWidth = 2 / view.zoom; // constant on-screen thickness for this transient preview
    ctx.setLineDash([6 / view.zoom, 4 / view.zoom]);
    ctx.beginPath();
    ctx.moveTo(pendingPolygon[0][0], pendingPolygon[0][1]);
    for (let i = 1; i < pendingPolygon.length; i++) ctx.lineTo(pendingPolygon[i][0], pendingPolygon[i][1]);
    if (lastMousePt) ctx.lineTo(lastMousePt[0], lastMousePt[1]);
    ctx.stroke();
    ctx.restore();
  }

  ctx.restore();

  // Screen-space overlays from here down: constant on-screen size
  // regardless of zoom, which is what actually feels right for
  // selection/vertex handles (drawn after ctx.restore(), so no
  // world transform is active).
  if (ui.tool === "polygon" && pendingPolygon.length) {
    ctx.save();
    for (const [i, pt] of pendingPolygon.entries()) {
      const [sx, sy] = worldToScreen(pt[0], pt[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, i === 0 ? 6 : 4, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#ffd43b" : "#ffe89b";
      ctx.fill();
    }
    ctx.restore();
  }

  if (ui.tool === "constrain" && constraintSelection.length) {
    ctx.save();
    ctx.strokeStyle = "#ffd43b";
    ctx.lineWidth = 2;
    for (const sel of constraintSelection) {
      const pos = livePosOf(sel.pathId, sel.nodeId);
      if (!pos) continue;
      const [sx, sy] = worldToScreen(pos[0], pos[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, 7, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
  }

  renderPresence();
}

function renderPresence() {
  const layer = document.getElementById("cursorLayer");
  layer.innerHTML = "";
  for (const [actor, p] of state.presence) {
    if (actor === actorId || !p) continue;
    const el = document.createElement("div");
    el.className = "cursor-label";
    // Presence positions are stored/sent in world coordinates (Phase 10)
    // -- this DOM overlay isn't inside the canvas's transform, so it
    // needs its own worldToScreen conversion to land in the right place.
    const [sx, sy] = worldToScreen(p.x, p.y);
    el.style.left = sx + "px";
    el.style.top = sy + "px";
    el.style.background = p.color || "#4dabf7";
    el.textContent = p.name || actor;
    layer.appendChild(el);
  }
}

function renderLayerList() {
  const list = document.getElementById("layerList");
  list.innerHTML = "";
  for (const lid of state.layerOrder) {
    const props = state.layers.get(lid) || {};
    const row = document.createElement("div");
    row.className = "layer-row" + (lid === ui.activeLayer ? " active" : "");
    row.innerHTML = `
      <span class="layer-swatch" style="background:${ui.hiddenLayers.has(lid) ? '#444' : '#4dabf7'}"></span>
      <span class="name">${escapeHtml(props.name || lid)}</span>
      <button class="ghost-btn" data-act="vis">${ui.hiddenLayers.has(lid) ? "🙈" : "👁"}</button>
    `;
    row.querySelector(".name").onclick = () => { ui.activeLayer = lid; renderLayerList(); };
    row.querySelector('[data-act="vis"]').onclick = (e) => {
      e.stopPropagation();
      if (ui.hiddenLayers.has(lid)) ui.hiddenLayers.delete(lid); else ui.hiddenLayers.add(lid);
      renderLayerList(); render();
    };
    list.appendChild(row);
  }
}

function renderPathList() {
  const list = document.getElementById("pathList");
  list.innerHTML = "";
  if (state.pathIndex.size === 0) {
    list.innerHTML = '<div class="empty-hint">Draw with the pen tool to create your first path.</div>';
    return;
  }
  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    const row = document.createElement("div");
    row.className = "path-row" + (pathId === ui.selectedPath ? " active" : "");
    row.innerHTML = `
      <span class="path-swatch" style="background:${props.color || "#eee"}"></span>
      <span class="name">${pathPoints(pathId).length} pts · ${escapeHtml((state.layers.get(props.layer_id) || {}).name || "?")}</span>
      <button class="ghost-btn" data-act="del">✕</button>
    `;
    row.querySelector(".name").onclick = () => { ui.selectedPath = pathId; renderAll(); };
    row.querySelector('[data-act="del"]').onclick = (e) => { e.stopPropagation(); removePath(pathId); renderAll(); };
    list.appendChild(row);
  }
}

function renderSelectionPanel() {
  const panel = document.getElementById("selectionPanel");
  const commentPanel = document.getElementById("commentList");
  if (!ui.selectedPath || !state.pathProps.has(ui.selectedPath)) {
    panel.innerHTML = '<div class="empty-hint">Select a path to edit its color, stroke width, or leave a comment.</div>';
    commentPanel.innerHTML = '<div class="empty-hint">No path selected.</div>';
    return;
  }
  const pathId = ui.selectedPath;
  const props = state.pathProps.get(pathId) || {};
  panel.innerHTML = `
    <div class="field-row"><label>Color</label><input id="selColor" type="text" value="${props.color || "#ffffff"}" style="width:90px"/></div>
    <div class="field-row"><label>Width</label><input id="selWidth" type="number" min="1" max="20" step="0.5" value="${props.stroke_width || 2.5}" style="width:70px"/></div>
    <button class="danger" id="selDelete" style="width:100%;margin-top:6px">Delete path</button>
  `;
  document.getElementById("selColor").onchange = (e) => setPathProp(pathId, "color", e.target.value);
  document.getElementById("selWidth").onchange = (e) => setPathProp(pathId, "stroke_width", parseFloat(e.target.value));
  document.getElementById("selDelete").onclick = () => { removePath(pathId); renderAll(); };

  commentPanel.innerHTML = "";
  const commentsForPath = [...state.comments.entries()].filter(([, c]) => c && c.path_id === pathId);
  if (commentsForPath.length === 0) {
    commentPanel.innerHTML = '<div class="empty-hint">No comments yet.</div>';
  } else {
    for (const [cid, c] of commentsForPath) {
      const row = document.createElement("div");
      row.className = "comment-row";
      row.innerHTML = `<div style="flex:1"><b>${escapeHtml(c.author)}</b>: ${escapeHtml(c.text)}</div><button class="ghost-btn" data-act="del">✕</button>`;
      row.querySelector('[data-act="del"]').onclick = () => { removeComment(cid); renderAll(); };
      commentPanel.appendChild(row);
    }
  }
  const addRow = document.createElement("div");
  addRow.style.marginTop = "8px";
  addRow.innerHTML = `<textarea id="commentText" rows="2" placeholder="Add a comment…"></textarea><button id="commentAdd" style="width:100%;margin-top:4px">Comment</button>`;
  commentPanel.appendChild(addRow);
  document.getElementById("commentAdd").onclick = () => {
    const text = document.getElementById("commentText").value.trim();
    if (text) { addComment(pathId, text); renderAll(); }
  };
}

function renderPresenceList() {
  const list = document.getElementById("presenceList");
  list.innerHTML = "";
  const others = [...state.presence.entries()].filter(([a]) => a !== actorId);
  document.getElementById("presenceCount").textContent = others.length + 1;
  const me = document.createElement("div");
  me.className = "presence-row";
  me.innerHTML = `<span class="path-swatch" style="background:${actorColor}"></span><span class="name">${escapeHtml(actorName)} (you)</span>`;
  list.appendChild(me);
  for (const [, p] of others) {
    if (!p) continue;
    const row = document.createElement("div");
    row.className = "presence-row";
    row.innerHTML = `<span class="path-swatch" style="background:${p.color || "#4dabf7"}"></span><span class="name">${escapeHtml(p.name || "?")}</span>`;
    list.appendChild(row);
  }
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderAll() {
  render();
  renderLayerList();
  renderPathList();
  renderSelectionPanel();
  renderConstraintPanel();
  renderPresenceList();
}

setInterval(() => {
  document.getElementById("opsCounter").textContent = `${ui.opsCount} ops relayed`;
  document.getElementById("offlineCounter").textContent = conn.outbox.length ? `${conn.outbox.length} queued offline` : "";
}, 400);

resizeCanvas();
setTool("pen");
