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
let lastMousePt = null;

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

const conn = new RelayConnection(`/ws/${encodeURIComponent(room)}`, actorId, {
  onSnapshot: (doc) => { loadSnapshot(doc); },
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
});

const p2p = new P2PManager(conn, actorId, {
  onPeerData: (_peerActorId, ops) => applyIncomingOps(ops),
  onPeerStatus: () => updateP2pPill(),
});

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
  triggerDownload(`/api/rooms/${encodeURIComponent(room)}/export/json`);
document.getElementById("downloadSvgBtn").onclick = () =>
  triggerDownload(`/api/rooms/${encodeURIComponent(room)}/export/svg`);
document.getElementById("downloadDxfBtn").onclick = () =>
  triggerDownload(`/api/rooms/${encodeURIComponent(room)}/export/dxf`);

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
    const resp = await fetch(`/api/rooms/${encodeURIComponent(room)}/import/${ext}`, { method: "POST", body });
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
  const url = `${location.origin}/?room=${encodeURIComponent(room)}`;
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

function canvasPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return [Math.round(e.clientX - rect.left), Math.round(e.clientY - rect.top)];
}

let drawing = null;

canvas.addEventListener("pointerdown", (e) => {
  if (ui.tool !== "pen") return;
  if (!ui.activeLayer) { ui.activeLayer = addLayer("Layer 1"); renderLayerList(); }
  const pt = canvasPoint(e);
  const { id, lastPointId } = addPath(ui.activeLayer, [pt], actorColor, 2.5);
  drawing = { pathId: id, lastPointId, lastPt: pt };
  ui.selectedPath = id;
  renderAll();
});

canvas.addEventListener("pointermove", (e) => {
  const pt = canvasPoint(e);
  sendPresence(pt[0], pt[1]);
  lastMousePt = pt;
  if (drawing) {
    const dist = Math.hypot(pt[0] - drawing.lastPt[0], pt[1] - drawing.lastPt[1]);
    if (dist > 2.5) {
      drawing.lastPointId = appendPoint(drawing.pathId, drawing.lastPointId, pt);
      drawing.lastPt = pt;
      render();
    }
  } else if (ui.tool === "polygon" && pendingPolygon.length) {
    render();
  }
});

window.addEventListener("pointerup", () => { drawing = null; });

canvas.addEventListener("click", (e) => {
  const pt = canvasPoint(e);
  if (ui.tool === "select") {
    ui.selectedPath = hitTestPath(pt);
    renderAll();
  } else if (ui.tool === "polygon") {
    if (pendingPolygon.length >= 3 && Math.hypot(pt[0] - pendingPolygon[0][0], pt[1] - pendingPolygon[0][1]) < 10) {
      finishPolygon();
    } else {
      pendingPolygon.push(pt);
      render();
      renderToolHint();
    }
  }
});

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ui.tool === "polygon") cancelPolygon();
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

function hitTestPath(pt) {
  let best = null, bestDist = 8;
  for (const pathId of state.pathIndex) {
    if (ui.hiddenLayers.has((state.pathProps.get(pathId) || {}).layer_id)) continue;
    const pts = pathPoints(pathId);
    for (let i = 0; i < pts.length - 1; i++) {
      const d = distToSegment(pt, pts[i], pts[i + 1]);
      if (d < bestDist) { bestDist = d; best = pathId; }
    }
  }
  return best;
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
function setTool(tool) {
  if (ui.tool === "polygon" && tool !== "polygon") cancelPolygon();
  ui.tool = tool;
  document.getElementById("toolPen").classList.toggle("active", tool === "pen");
  document.getElementById("toolSelect").classList.toggle("active", tool === "select");
  document.getElementById("toolPolygon").classList.toggle("active", tool === "polygon");
  canvas.style.cursor = tool === "select" ? "default" : "crosshair";
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
  } else {
    hint.textContent = "Click a path to select it.";
  }
}

document.getElementById("addLayerBtn").onclick = () => {
  const id = addLayer(`Layer ${state.layerOrder.length + 1}`);
  ui.activeLayer = id;
  renderAll();
};

// -- rendering ------------------------------------------------------------------

function render() {
  const rect = canvasWrap.getBoundingClientRect();
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.save();
  ctx.strokeStyle = "#1c2028";
  ctx.lineWidth = 1;
  for (let x = 0; x < rect.width; x += 32) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, rect.height); ctx.stroke(); }
  for (let y = 0; y < rect.height; y += 32) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(rect.width, y); ctx.stroke(); }
  ctx.restore();

  for (const pathId of state.pathIndex) {
    const props = state.pathProps.get(pathId) || {};
    if (ui.hiddenLayers.has(props.layer_id)) continue;
    const pts = pathPoints(pathId);
    if (pts.length === 0) continue;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
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
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(pendingPolygon[0][0], pendingPolygon[0][1]);
    for (let i = 1; i < pendingPolygon.length; i++) ctx.lineTo(pendingPolygon[i][0], pendingPolygon[i][1]);
    if (lastMousePt) ctx.lineTo(lastMousePt[0], lastMousePt[1]);
    ctx.stroke();
    ctx.setLineDash([]);
    for (const [i, pt] of pendingPolygon.entries()) {
      ctx.beginPath();
      ctx.arc(pt[0], pt[1], i === 0 ? 6 : 4, 0, Math.PI * 2);
      ctx.fillStyle = i === 0 ? "#ffd43b" : "#ffe89b";
      ctx.fill();
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
    el.style.left = p.x + "px";
    el.style.top = p.y + "px";
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
  renderPresenceList();
}

setInterval(() => {
  document.getElementById("opsCounter").textContent = `${ui.opsCount} ops relayed`;
  document.getElementById("offlineCounter").textContent = conn.outbox.length ? `${conn.outbox.length} queued offline` : "";
}, 400);

resizeCanvas();
setTool("pen");
