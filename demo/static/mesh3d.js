// 3D collaborative mesh demo built directly on the crdt_cad.crdt.mesh wire
// protocol (MeshCRDT / MeshOp). Mirrors the same "server is authoritative,
// client mints ops + renders optimistically" design as sketch.js -- see
// common.js for the shared rationale.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const actorId = getOrCreateActorId();
const actorName = getOrCreateActorName();
const actorColor = colorForActor(actorId);
const room = new URLSearchParams(location.search).get("room") || "demo-mesh";
document.getElementById("roomInput").value = room;
document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;

const clock = new LocalClock(actorId);
const rid = () => Math.random().toString(36).slice(2, 10);
const round = (n) => Math.round(n * 100) / 100;
const EDGE_SEP = "\x1f";
const canonicalEdgeKey = (a, b) => (a <= b ? `${a}${EDGE_SEP}${b}` : `${b}${EDGE_SEP}${a}`);
const decodeEdge = (key) => key.split(EDGE_SEP);

const state = {
  vertices: new Map(),      // id -> [x,y,z]
  edges: new Set(),         // canonical "a<sep>b" keys
  faceIndex: new Set(),
  faceNodes: new Map(),     // faceId -> [{id,o,v,db}] (already in document order)
  faceProps: new Map(),     // faceId -> {material, color, ...}
  presence: new Map(),
  invalidFaces: new Set(),  // face ids currently flagged by a validity_warning (see syncScene)
};

const ui = { tool: "vertex", selectedFace: null, opsCount: 0 };
let pendingFaceLoop = [];

// -- wire <-> local state -----------------------------------------------------

/** Bumps `clock` past every OpId found in a freshly-loaded snapshot, so this
 * replica's own next local edit is guaranteed to out-rank anything already in
 * the document -- otherwise a fresh page load of a room an AI generator (or
 * anyone else) already populated with high-counter ops would leave this
 * client's clock at 0, and its first edits would silently lose LWW
 * tie-breaks against that existing content. See LocalClock.observe(). */
function observeSnapshotCounters(doc) {
  let maxCounter = 0;
  const bump = (idPair) => { if (idPair && idPair[0] > maxCounter) maxCounter = idPair[0]; };
  const scanEntries = (lww) => { if (lww) for (const e of lww.entries) bump(e.id); };
  scanEntries(doc.vertices);
  scanEntries(doc.edges);
  scanEntries(doc.face_index);
  for (const rga of Object.values(doc.faces || {})) {
    for (const n of rga.nodes) { bump(n.id); bump(n.db); }
  }
  for (const m of Object.values(doc.face_props || {})) scanEntries(m);
  scanEntries(doc.presence);
  clock.observe(maxCounter);
}

function loadSnapshot(doc) {
  observeSnapshotCounters(doc);
  state.vertices.clear();
  for (const e of doc.vertices.entries) if (!e.d) state.vertices.set(e.k, e.v);
  state.edges = new Set(doc.edges.entries.filter((e) => !e.d).map((e) => e.k));
  state.faceIndex = new Set(doc.face_index.entries.filter((e) => !e.d).map((e) => e.k));
  state.faceNodes.clear();
  for (const [fid, rga] of Object.entries(doc.faces)) state.faceNodes.set(fid, rga.nodes.slice());
  state.faceProps.clear();
  if (doc.face_props) {
    for (const [fid, lww] of Object.entries(doc.face_props)) {
      const props = {};
      for (const e of lww.entries) if (!e.d) props[e.k] = e.v;
      state.faceProps.set(fid, props);
    }
  }
  state.presence.clear();
  if (doc.presence) for (const e of doc.presence.entries) if (!e.d) state.presence.set(e.k, e.v);
  syncScene();
}

function applyOp(op) {
  const p = op.payload;
  if (p && p.id) clock.observe(p.id[0]);
  if (op.target === "vertex") {
    if (!p.d) state.vertices.set(p.k, p.v); else state.vertices.delete(p.k);
  } else if (op.target === "edge") {
    if (!p.d) state.edges.add(p.k); else state.edges.delete(p.k);
  } else if (op.target === "face_index") {
    if (!p.d) state.faceIndex.add(p.k); else state.faceIndex.delete(p.k);
  } else if (op.target === "face_geom") {
    let nodes = state.faceNodes.get(op.face_id);
    if (!nodes) { nodes = []; state.faceNodes.set(op.face_id, nodes); }
    applyRgaOp(nodes, p);
  } else if (op.target === "face_prop") {
    let props = state.faceProps.get(op.face_id);
    if (!props) { props = {}; state.faceProps.set(op.face_id, props); }
    if (!p.d) props[p.k] = p.v; else delete props[p.k];
  } else if (op.target === "presence") {
    if (!p.d) {
      state.presence.set(p.k, p.v);
      p2p.maybeConnectTo(p.k);
    } else {
      state.presence.delete(p.k);
    }
  }
}

// -- op constructors (mirrors crdt_cad.crdt.mesh.MeshCRDT) -----------------------

function addVertexOp(vertexId, pos) {
  return { target: "vertex", payload: lwwOp(clock.tick(), vertexId, pos, false) };
}
function removeVertexOp(vertexId) {
  return { target: "vertex", payload: lwwOp(clock.tick(), vertexId, null, true) };
}
function addEdgeOp(a, b) {
  return { target: "edge", payload: lwwOp(clock.tick(), canonicalEdgeKey(a, b), true, false) };
}
function removeEdgeOp(a, b) {
  return { target: "edge", payload: lwwOp(clock.tick(), canonicalEdgeKey(a, b), null, true) };
}
function addFaceIndexOnlyOp(faceId) {
  return { target: "face_index", payload: lwwOp(clock.tick(), faceId, true, false) };
}
function addFaceOps(faceId, loop) {
  const ops = [addFaceIndexOnlyOp(faceId)];
  let prev = null;
  for (const vid of loop) {
    const insId = clock.tick();
    ops.push({ target: "face_geom", face_id: faceId, payload: rgaInsertOp(insId, prev, vid) });
    prev = insId;
  }
  return ops;
}
function removeFaceOp(faceId) {
  return { target: "face_index", payload: lwwOp(clock.tick(), faceId, null, true) };
}
function setFacePropOp(faceId, key, value) {
  return { target: "face_prop", face_id: faceId, payload: lwwOp(clock.tick(), key, value, false) };
}
function removeFacePropOp(faceId, key) {
  return { target: "face_prop", face_id: faceId, payload: lwwOp(clock.tick(), key, null, true) };
}

// -- undo / redo: fresh inverted ops each time, not snapshots ---------------------
// Mirrors crdt_cad.crdt.mesh.MeshCRDT's undo/redo (same entry "kind"s, same
// composite-bundling for multi-op actions like extrude) -- see that
// module's docstring for the full rationale. This is an independent
// client-side reimplementation of the same algorithm, not a call into the
// server: exactly the same relationship sketch.js's undo/redo has to
// DrawingDocument.undo()/redo().

const undoStack = [];
const redoStack = [];

function pushUndo(entry) {
  undoStack.push(entry);
  redoStack.length = 0;
}

function applyInverse(entry) {
  if (entry.kind === "composite") {
    const ops = [];
    for (let i = entry.entries.length - 1; i >= 0; i--) ops.push(...applyInverse(entry.entries[i]));
    return ops;
  }
  let op;
  if (entry.kind === "vertex_create") {
    op = removeVertexOp(entry.vertexId);
  } else if (entry.kind === "vertex_move" || entry.kind === "vertex_remove") {
    op = addVertexOp(entry.vertexId, entry.previous);
  } else if (entry.kind === "edge_add") {
    op = removeEdgeOp(entry.v1, entry.v2);
  } else if (entry.kind === "edge_remove") {
    op = addEdgeOp(entry.v1, entry.v2);
  } else if (entry.kind === "face_add") {
    op = removeFaceOp(entry.faceId);
  } else if (entry.kind === "face_remove") {
    op = addFaceIndexOnlyOp(entry.faceId); // re-flips membership only -- the RGA boundary was never touched by remove
  } else if (entry.kind === "face_prop_set") {
    op = entry.hadPrevious ? setFacePropOp(entry.faceId, entry.key, entry.previous) : removeFacePropOp(entry.faceId, entry.key);
  }
  applyOp(op);
  return [op];
}

function applyForward(entry) {
  if (entry.kind === "composite") {
    const ops = [];
    for (const sub of entry.entries) ops.push(...applyForward(sub));
    return ops;
  }
  let op;
  if (entry.kind === "vertex_create") {
    op = addVertexOp(entry.vertexId, entry.position);
  } else if (entry.kind === "vertex_move") {
    op = addVertexOp(entry.vertexId, entry.forward);
  } else if (entry.kind === "vertex_remove") {
    op = removeVertexOp(entry.vertexId);
  } else if (entry.kind === "edge_add") {
    op = addEdgeOp(entry.v1, entry.v2);
  } else if (entry.kind === "edge_remove") {
    op = removeEdgeOp(entry.v1, entry.v2);
  } else if (entry.kind === "face_add") {
    op = addFaceIndexOnlyOp(entry.faceId);
  } else if (entry.kind === "face_remove") {
    op = removeFaceOp(entry.faceId);
  } else if (entry.kind === "face_prop_set") {
    op = setFacePropOp(entry.faceId, entry.key, entry.forwardValue);
  }
  applyOp(op);
  return [op];
}

function undo() {
  const entry = undoStack.pop();
  if (!entry) return;
  const ops = applyInverse(entry);
  sendOps(ops);
  redoStack.push(entry);
  syncScene();
}

function redo() {
  const entry = redoStack.pop();
  if (!entry) return;
  const ops = applyForward(entry);
  sendOps(ops);
  undoStack.push(entry);
  syncScene();
}

// -- relay connection -----------------------------------------------------------

function applyIncomingOps(ops) {
  for (const op of ops) applyOp(op);
  ui.opsCount += ops.length;
  syncScene();
}

// `conn`/`p2p` are assigned inside the async bootstrap below (ensureRoomAccess
// awaits an /api/auth/required check, and possibly a passphrase prompt,
// before a token -- or null, on the zero-config default -- is available).
// Every reference elsewhere in this file is inside a function or event
// handler, so it only runs well after this has resolved.
let conn, p2p;

(async () => {
  const token = await ensureRoomAccess("mesh", room);
  const persistedOutbox = await loadPersistedOutbox("mesh", room, actorId);
  if (persistedOutbox.length) {
    showToast(`Recovered ${persistedOutbox.length} offline edit(s) from before this page loaded`, "info");
  }
  conn = new RelayConnection(`/ws/mesh/${encodeURIComponent(room)}`, actorId, {
    onSnapshot: (doc) => {
      loadSnapshot(doc);
      // See the identical comment in sketch.js: the server never echoes
      // ops back to the actor that sent them, so recovered-but-unsent
      // edits need to be replayed locally after the fresh snapshot loads.
      for (const op of persistedOutbox) applyOp(op);
      if (persistedOutbox.length) syncScene();
    },
    onDelta: (ops) => applyIncomingOps(ops),
    onOps: (ops) => applyIncomingOps(ops),
    onStatus: (status) => setStatus(status),
    onSaved: () => showToast("Saved", "success"),
    onMergePreview: (mine, theirs, proceed) => showMergePreviewModal(mine, theirs, describeMeshOps, proceed),
    onValidityWarning: (faces, problems) => applyValidityWarning(faces, problems),
    token,
    kind: "mesh",
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
    // See the identical comment in sketch.js -- the token we had (or lack
    // thereof) was rejected; clear it and re-prompt rather than let
    // RelayConnection retry forever with the same bad token. Also strip
    // any ?token= from the URL first: ensureRoomAccess() trusts a URL
    // token unconditionally (that's what makes invite links friction-free
    // for a legitimate recipient), so leaving a just-proven-bad one in
    // place would make it re-adopt the same bad token forever instead of
    // ever reaching the actual re-prompt.
    clearRoomToken("mesh", room);
    const url = new URL(location.href);
    url.searchParams.delete("token");
    history.replaceState({}, "", url);
    showToast("Incorrect or expired room secret -- please try again", "error");
    ensureRoomAccess("mesh", room).then((token) => {
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
    p2p.disconnectAll();
    updateP2pPill();
  }
};
document.getElementById("roomInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") location.search = `?room=${encodeURIComponent(e.target.value.trim() || "demo-mesh")}`;
});

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
  triggerDownload(withToken(`/api/mesh/${encodeURIComponent(room)}/export/json`, "mesh", room));
document.getElementById("downloadStlBtn").onclick = () =>
  triggerDownload(withToken(`/api/mesh/${encodeURIComponent(room)}/export/stl`, "mesh", room));

// Unlike the two above (which always succeed), STEP export needs the
// optional `build123d` dependency server-side and fails for an empty
// mesh -- a plain triggerDownload() would silently "download" the JSON
// error body as if it were a .step file, so this checks the response
// first and surfaces a real error via toast instead.
document.getElementById("downloadStepBtn").onclick = async () => {
  const url = withToken(`/api/mesh/${encodeURIComponent(room)}/export/step`, "mesh", room);
  let resp;
  try {
    resp = await fetch(url);
  } catch {
    showToast("Could not reach the server for STEP export", "error");
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    showToast(body.detail || "STEP export failed", "error");
    return;
  }
  const blob = await resp.blob();
  const blobUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = blobUrl;
  a.download = `${room}.step`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(blobUrl);
};

document.getElementById("shareBtn").onclick = async () => {
  let url = `${location.origin}/3d?room=${encodeURIComponent(room)}`;
  const token = roomTokenFor("mesh", room);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  try {
    await navigator.clipboard.writeText(url);
    showToast("Invite link copied to clipboard", "success");
  } catch (err) {
    showToast(url, "info");
  }
};

// -- AI text-to-3D generation -------------------------------------------------------
// The generated mesh arrives back over the *same* WebSocket ops broadcast as any
// other edit (see the server's Room.commit_ops_batched), so it renders through the
// normal onOps -> applyIncomingOps -> syncScene path with no special-case client code.

const genBtn = document.getElementById("genBtn");
const genPromptInput = document.getElementById("genPromptInput");
const genStatus = document.getElementById("genStatus");

async function generateMesh() {
  const prompt = genPromptInput.value.trim();
  if (!prompt) {
    showToast("Describe what to generate first", "error");
    return;
  }
  genBtn.disabled = true;
  genBtn.textContent = "Generating…";
  genStatus.textContent = "Generating mesh — this can take a few seconds…";
  try {
    const resp = await fetch(withToken(`/api/mesh/${encodeURIComponent(room)}/generate`, "mesh", room), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const result = await resp.json();
    const via = result.interpretation_source === "llm" ? "Claude Fable 5" : "the offline heuristic parser";
    // mesh_source (Phase 9, unverified against a live Meshy account --
    // see crdt_cad.ai.meshy_adapter's module docstring): "meshy" only
    // when MESHY_API_KEY was set *and* the hosted API actually returned
    // a mesh; any failure there silently falls back to "procedural",
    // same as it always was.
    const meshVia = result.mesh_source === "meshy" ? "Meshy" : "the procedural builder";
    showToast(`Generated ${result.vertex_count} vertices / ${result.face_count} faces via ${meshVia}`, "success");
    genStatus.textContent =
      `Last generation: ${result.spec.bedrooms} bedroom(s), ${result.spec.floors} floor(s), ` +
      `${escapeHtml(result.spec.floor_material)} floor, ${escapeHtml(result.spec.style)} style (interpreted via ${via}, ` +
      `mesh via ${meshVia}, ${result.batches} batch(es)).`;
  } catch (err) {
    showToast(`Generation failed: ${err.message}`, "error");
    genStatus.textContent = `Generation failed: ${err.message}`;
  } finally {
    genBtn.disabled = false;
    genBtn.textContent = "Generate";
  }
}
genBtn.onclick = generateMesh;
genPromptInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") generateMesh();
});

// -- high-level mutations ---------------------------------------------------------

function addVertex(pos) {
  const id = "v_" + rid();
  const op = addVertexOp(id, pos);
  applyOp(op);
  sendOps([op]);
  sendPresence(pos);
  pushUndo({ kind: "vertex_create", vertexId: id, position: pos });
  syncScene();
  return id;
}

function removeVertex(id) {
  const previous = state.vertices.get(id);
  const op = removeVertexOp(id);
  applyOp(op);
  sendOps([op]);
  pushUndo({ kind: "vertex_remove", vertexId: id, previous });
  syncScene();
}

function removeFace(id) {
  const op = removeFaceOp(id);
  applyOp(op);
  sendOps([op]);
  pushUndo({ kind: "face_remove", faceId: id });
  if (ui.selectedFace === id) ui.selectedFace = null;
  syncScene();
}

function setFaceProp(faceId, key, value) {
  const props = state.faceProps.get(faceId) || {};
  const hadPrevious = key in props;
  const previous = props[key];
  const op = setFacePropOp(faceId, key, value);
  applyOp(op);
  sendOps([op]);
  pushUndo({ kind: "face_prop_set", faceId, key, previous, hadPrevious, forwardValue: value });
  syncScene();
}

function finishFace() {
  if (pendingFaceLoop.length < 3) return;
  const faceId = "face_" + rid();
  const ops = addFaceOps(faceId, pendingFaceLoop);
  const subEntries = [{ kind: "face_add", faceId }];
  for (let i = 0; i < pendingFaceLoop.length; i++) {
    const a = pendingFaceLoop[i], b = pendingFaceLoop[(i + 1) % pendingFaceLoop.length];
    ops.push(addEdgeOp(a, b));
    subEntries.push({ kind: "edge_add", v1: a, v2: b });
  }
  for (const op of ops) applyOp(op);
  sendOps(ops);
  pushUndo({ kind: "composite", entries: subEntries });
  pendingFaceLoop = [];
  updatePendingFaceLine();
  syncScene();
}

function cancelFace() {
  pendingFaceLoop = [];
  updatePendingFaceLine();
  syncScene();
}

function extrudeFace(faceId, height) {
  const loop = liveValues(state.faceNodes.get(faceId));
  if (loop.length < 3) return;
  const ops = [];
  const subEntries = [];
  const newLoop = [];
  for (const vid of loop) {
    const p = state.vertices.get(vid);
    if (!p) continue;
    const newId = "v_" + rid();
    const pos = [p[0], p[1] + height, p[2]];
    const op = addVertexOp(newId, pos);
    applyOp(op); ops.push(op);
    subEntries.push({ kind: "vertex_create", vertexId: newId, position: pos });
    newLoop.push(newId);
  }
  const buildRing = (ring) => {
    for (let k = 0; k < ring.length; k++) {
      const a = ring[k], b = ring[(k + 1) % ring.length];
      const op = addEdgeOp(a, b);
      applyOp(op); ops.push(op);
      subEntries.push({ kind: "edge_add", v1: a, v2: b });
    }
  };
  for (let i = 0; i < loop.length; i++) {
    const j = (i + 1) % loop.length;
    const sideLoop = [loop[i], loop[j], newLoop[j], newLoop[i]];
    const sideId = "face_" + rid();
    for (const op of addFaceOps(sideId, sideLoop)) { applyOp(op); ops.push(op); }
    subEntries.push({ kind: "face_add", faceId: sideId });
    buildRing(sideLoop);
  }
  const topId = "face_" + rid();
  for (const op of addFaceOps(topId, newLoop)) { applyOp(op); ops.push(op); }
  subEntries.push({ kind: "face_add", faceId: topId });
  buildRing(newLoop);

  sendOps(ops);
  // One bundled undo entry -- a single Ctrl+Z/Undo click removes every
  // vertex, edge, and face this extrude created, in one step, regardless
  // of what a collaborator may have concurrently changed elsewhere (see
  // MeshCRDT.extrude_face's docstring and its concurrent-safety test).
  pushUndo({ kind: "composite", entries: subEntries });
  syncScene();
}

function sendPresence(pos) {
  const op = { target: "presence", payload: lwwOp(clock.tick(), actorId, { pos, name: actorName, color: actorColor }, false) };
  applyOp(op);
  sendOps([op]);
}

// -- three.js scene -----------------------------------------------------------------

const canvasWrap = document.querySelector(".canvas-wrap");
const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById("canvas3d"), antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1013);

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
camera.position.set(6, 6, 8);
camera.lookAt(0, 0, 0);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0);
controls.enableDamping = true;

scene.add(new THREE.AmbientLight(0xffffff, 0.65));
const dirLight = new THREE.DirectionalLight(0xffffff, 0.9);
dirLight.position.set(6, 12, 8);
scene.add(dirLight);

scene.add(new THREE.GridHelper(20, 20, 0x2e333d, 0x1c2028));

const groundGeo = new THREE.PlaneGeometry(200, 200).rotateX(-Math.PI / 2);
const ground = new THREE.Mesh(groundGeo, new THREE.MeshBasicMaterial({ visible: false }));
scene.add(ground);

const vertexGeo = new THREE.SphereGeometry(0.1, 16, 16);
const edgeMat = new THREE.LineBasicMaterial({ color: 0x4dabf7 });
let pendingLoopLine = null;

const vertexMeshes = new Map();
const edgeLines = new Map();
const faceMeshes = new Map();
const invalidFaceOutlines = new Map();

const FACE_PALETTE = [0x4dabf7, 0x69db7c, 0xffd43b, 0xda77f2, 0xff922b, 0x38d9a9, 0xf783ac];
function faceColor(faceId) {
  const props = state.faceProps.get(faceId);
  if (props && typeof props.color === "string" && /^#[0-9a-fA-F]{6}$/.test(props.color)) {
    return parseInt(props.color.slice(1), 16);
  }
  let hash = 0;
  for (let i = 0; i < faceId.length; i++) hash = (hash * 31 + faceId.charCodeAt(i)) >>> 0;
  return FACE_PALETTE[hash % FACE_PALETTE.length];
}

function buildFaceGeometry(loop) {
  const pts = loop.map((vid) => state.vertices.get(vid)).filter(Boolean);
  if (pts.length < 3) return null;
  const positions = [];
  for (const p of pts) positions.push(p[0], p[1], p[2]);
  const idxs = [];
  for (let i = 1; i < pts.length - 1; i++) idxs.push(0, i, i + 1);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geo.setIndex(idxs);
  geo.computeVertexNormals();
  return geo;
}

function vertexColor(id) {
  if (pendingFaceLoop.includes(id)) return 0xffd43b;
  return 0xd7dbe0;
}

function syncScene() {
  for (const [id, mesh] of [...vertexMeshes]) {
    if (!state.vertices.has(id)) { scene.remove(mesh); vertexMeshes.delete(id); }
  }
  for (const [id, pos] of state.vertices) {
    let mesh = vertexMeshes.get(id);
    if (!mesh) {
      mesh = new THREE.Mesh(vertexGeo, new THREE.MeshStandardMaterial({ color: vertexColor(id) }));
      mesh.userData.vertexId = id;
      scene.add(mesh);
      vertexMeshes.set(id, mesh);
    }
    mesh.position.set(pos[0], pos[1], pos[2]);
    mesh.material.color.set(vertexColor(id));
  }

  for (const [key, line] of [...edgeLines]) {
    if (!state.edges.has(key)) { scene.remove(line); edgeLines.delete(key); }
  }
  for (const key of state.edges) {
    const [a, b] = decodeEdge(key);
    const pa = state.vertices.get(a), pb = state.vertices.get(b);
    if (!pa || !pb) continue;
    let line = edgeLines.get(key);
    if (!line) {
      line = new THREE.Line(new THREE.BufferGeometry(), edgeMat);
      scene.add(line);
      edgeLines.set(key, line);
    }
    line.geometry.setFromPoints([new THREE.Vector3(...pa), new THREE.Vector3(...pb)]);
  }

  for (const [id, mesh] of [...faceMeshes]) {
    if (!state.faceIndex.has(id)) { scene.remove(mesh); mesh.geometry.dispose(); faceMeshes.delete(id); }
  }
  for (const id of state.faceIndex) {
    const loop = liveValues(state.faceNodes.get(id));
    const geo = buildFaceGeometry(loop);
    if (!geo) continue;
    let mesh = faceMeshes.get(id);
    if (!mesh) {
      mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: faceColor(id), side: THREE.DoubleSide, transparent: true, opacity: 0.82 }));
      mesh.userData.faceId = id;
      scene.add(mesh);
      faceMeshes.set(id, mesh);
    } else {
      mesh.geometry.dispose();
      mesh.geometry = geo;
    }
  }

  for (const [id, line] of [...invalidFaceOutlines]) {
    if (!state.invalidFaces.has(id) || !state.faceIndex.has(id)) { scene.remove(line); invalidFaceOutlines.delete(id); }
  }
  for (const id of state.invalidFaces) {
    if (!state.faceIndex.has(id)) continue;
    const loop = liveValues(state.faceNodes.get(id)).map((vid) => state.vertices.get(vid)).filter(Boolean);
    if (loop.length < 2) continue;
    let line = invalidFaceOutlines.get(id);
    if (!line) {
      // depthTest:false so the warning outline always reads clearly through
      // the semi-transparent, exactly-coplanar face fill it traces.
      line = new THREE.LineLoop(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xff2222, linewidth: 2, depthTest: false }));
      scene.add(line);
      invalidFaceOutlines.set(id, line);
    }
    line.geometry.setFromPoints(loop.map((p) => new THREE.Vector3(...p)));
  }

  renderPanels();
}

// -- cross-component mesh validity warnings (Phase 6 "Validation Fork") -----
// The server broadcasts `validity_warning` after a merge leaves face
// topology cross-component-inconsistent (e.g. a face boundary referencing a
// vertex a concurrent edit deleted -- see crdt_cad.geometry.mesh_validity).
// This is purely informational: the merge already happened and can't be
// rejected without breaking convergence, so all there is to do client-side
// is highlight the affected faces and let the user decide how to fix them.

let validityBannerEl = null;

function applyValidityWarning(faces, problems) {
  for (const id of faces) state.invalidFaces.add(id);
  syncScene();
  showValidityBanner(problems);
}

function clearValidityWarning() {
  state.invalidFaces.clear();
  syncScene();
  if (validityBannerEl) { validityBannerEl.remove(); validityBannerEl = null; }
}

function showValidityBanner(problems) {
  if (validityBannerEl) validityBannerEl.remove();
  const el = document.createElement("div");
  el.style.cssText =
    "position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:1500;" +
    "background:#3a1f22;border:1px solid #ff6b6b;border-radius:10px;padding:14px 18px;" +
    "max-width:520px;width:90%;color:#e7e9ee;font-size:13px;font-family:inherit;" +
    "box-shadow:0 4px 20px rgba(0,0,0,0.4);";
  const list = problems
    .map((p) => `<li>${p.problem} (face${p.faces.length > 1 ? "s" : ""} ${p.faces.join(", ")})</li>`)
    .join("");
  el.innerHTML = `
    <div style="display:flex;align-items:flex-start;gap:10px;">
      <div style="font-size:18px;line-height:1;">⚠️</div>
      <div style="flex:1;">
        <div style="font-weight:700;color:#ff6b6b;margin-bottom:4px;">Mesh validity warning</div>
        <div style="color:#9aa1ad;margin-bottom:8px;line-height:1.4;">
          A merge left the highlighted face(s) (outlined in red) in an inconsistent
          state. Nothing was rejected -- fix or delete the affected faces when convenient.
        </div>
        <ul style="margin:0 0 10px;padding-left:18px;">${list}</ul>
        <button id="validityDismissBtn" style="background:#ff6b6b;border:none;color:#06121a;font-weight:700;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;">Dismiss</button>
      </div>
    </div>
  `;
  document.body.appendChild(el);
  el.querySelector("#validityDismissBtn").onclick = () => clearValidityWarning();
  validityBannerEl = el;
}

function syncFacesTouching(vertexId) {
  for (const [fid, mesh] of faceMeshes) {
    const loop = liveValues(state.faceNodes.get(fid));
    if (!loop.includes(vertexId)) continue;
    const geo = buildFaceGeometry(loop);
    if (geo) { mesh.geometry.dispose(); mesh.geometry = geo; }
  }
  for (const [key, line] of edgeLines) {
    const [a, b] = decodeEdge(key);
    if (a !== vertexId && b !== vertexId) continue;
    const pa = state.vertices.get(a), pb = state.vertices.get(b);
    if (pa && pb) line.geometry.setFromPoints([new THREE.Vector3(...pa), new THREE.Vector3(...pb)]);
  }
}

function updatePendingFaceLine() {
  if (pendingFaceLoop.length < 2) {
    if (pendingLoopLine) { scene.remove(pendingLoopLine); pendingLoopLine = null; }
    return;
  }
  const pts = pendingFaceLoop.map((id) => state.vertices.get(id)).filter(Boolean).map((p) => new THREE.Vector3(...p));
  if (!pendingLoopLine) {
    pendingLoopLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xffd43b }));
    scene.add(pendingLoopLine);
  }
  pendingLoopLine.geometry.setFromPoints(pts);
}

// -- raycasting / tools -------------------------------------------------------------

const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();
function updateMouse(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
}
function raycastGround() {
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObject(ground);
  return hits.length ? hits[0].point : null;
}
function raycastVertices() {
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects([...vertexMeshes.values()]);
  return hits.length ? hits[0].object : null;
}
function raycastFaces() {
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects([...faceMeshes.values()]);
  return hits.length ? hits[0].object : null;
}

let dragState = null;
let lastMoveSent = 0;

/** Starts dragging an existing vertex. Plain drag moves it across the
 * horizontal plane at its current height (X/Z, the common case -- resizing
 * a footprint). Shift+drag instead moves it only vertically (Y), using a
 * plane through the vertex facing the camera so mouse-up/down maps to
 * world-up/down regardless of viewing angle -- there's no 3-axis gizmo
 * here, so a modifier key is the lightweight way to reach the third axis. */
function startVertexDrag(vid, vertical) {
  const pos = state.vertices.get(vid);
  const startPosition = pos.slice(); // captured once, for a single undo entry covering the whole drag
  if (vertical) {
    const camDir = new THREE.Vector3();
    camera.getWorldDirection(camDir);
    camDir.y = 0;
    if (camDir.lengthSq() < 1e-6) camDir.set(0, 0, 1);
    camDir.normalize();
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(camDir, new THREE.Vector3(...pos));
    dragState = { vertexId: vid, plane, vertical: true, fixedX: pos[0], fixedZ: pos[2], startPosition };
  } else {
    dragState = { vertexId: vid, plane: new THREE.Plane(new THREE.Vector3(0, 1, 0), -pos[1]), vertical: false, startPosition };
  }
  controls.enabled = false;
}

renderer.domElement.addEventListener("pointerdown", (e) => {
  updateMouse(e);
  if (ui.tool === "face") {
    const vmesh = raycastVertices();
    if (vmesh) {
      const vid = vmesh.userData.vertexId;
      if (pendingFaceLoop.length >= 3 && vid === pendingFaceLoop[0]) {
        finishFace();
      } else if (!pendingFaceLoop.includes(vid)) {
        pendingFaceLoop.push(vid);
        updatePendingFaceLine();
        syncScene();
      }
    }
    return;
  }

  // Vertex and Move tools both let you grab and drag an existing vertex --
  // previously this only worked in the Move tool, so a user on the default
  // Vertex tool had no way to reposition a point they'd just placed without
  // first discovering the separate Move tool.
  const vmesh = raycastVertices();
  if (vmesh) {
    startVertexDrag(vmesh.userData.vertexId, e.shiftKey);
    return;
  }

  if (ui.tool === "vertex") {
    const pt = raycastGround();
    if (pt) addVertex([round(pt.x), 0, round(pt.z)]);
  } else if (ui.tool === "move") {
    const fmesh = raycastFaces();
    ui.selectedFace = fmesh ? fmesh.userData.faceId : null;
    renderPanels();
  }
});

renderer.domElement.addEventListener("pointermove", (e) => {
  updateMouse(e);
  if (!dragState) return;
  raycaster.setFromCamera(mouse, camera);
  const hit = new THREE.Vector3();
  const ok = raycaster.ray.intersectPlane(dragState.plane, hit);
  if (!ok) return;
  const current = state.vertices.get(dragState.vertexId);
  const pos = dragState.vertical
    ? [dragState.fixedX, round(hit.y), dragState.fixedZ]
    : [round(hit.x), current[1], round(hit.z)];
  state.vertices.set(dragState.vertexId, pos);
  const mesh = vertexMeshes.get(dragState.vertexId);
  if (mesh) mesh.position.set(pos[0], pos[1], pos[2]);
  syncFacesTouching(dragState.vertexId);

  const now = performance.now();
  if (now - lastMoveSent > 80) {
    lastMoveSent = now;
    sendOps([addVertexOp(dragState.vertexId, pos)]);
  }
});

window.addEventListener("pointerup", () => {
  if (dragState) {
    const pos = state.vertices.get(dragState.vertexId);
    sendOps([addVertexOp(dragState.vertexId, pos)]);
    sendPresence(pos);
    const moved = pos.some((v, i) => v !== dragState.startPosition[i]);
    if (moved) {
      pushUndo({ kind: "vertex_move", vertexId: dragState.vertexId, previous: dragState.startPosition, forward: pos });
    }
    dragState = null;
    controls.enabled = true;
  }
});

// -- tool buttons -----------------------------------------------------------------

function setTool(tool) {
  ui.tool = tool;
  for (const [id, t] of [["toolVertex", "vertex"], ["toolFace", "face"], ["toolMove", "move"]]) {
    document.getElementById(id).classList.toggle("active", tool === t);
  }
  const hints = {
    vertex: "Click the ground grid to place a vertex, or drag an existing one to move it (hold Shift to move it up/down instead).",
    face: "Click 3+ vertices in order, then click the first one again (or use Finish) to create a face.",
    move: "Drag a vertex to move it (hold Shift to move it up/down; or type exact X/Y/Z below). Click empty space on a face to select it for extrusion, recoloring, or a material tag.",
  };
  document.getElementById("toolHint").textContent = hints[tool];
  if (tool !== "face" && pendingFaceLoop.length) cancelFace();
  renderPanels();
}
document.getElementById("toolVertex").onclick = () => setTool("vertex");
document.getElementById("toolFace").onclick = () => setTool("face");
document.getElementById("toolMove").onclick = () => setTool("move");

document.getElementById("undoBtn").onclick = undo;
document.getElementById("redoBtn").onclick = redo;
window.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA") return; // let native text-field undo/redo work instead
  if (e.key === "z" || e.key === "Z") {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
  } else if (e.key === "y" || e.key === "Y") {
    e.preventDefault();
    redo();
  }
});

// -- panels -------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderPanels() {
  renderToolHint();
  renderFacePanel();
  renderVertexList();
  renderFaceList();
  renderPresenceList();
}

function renderToolHint() {
  if (ui.tool !== "face") return;
  const hint = document.getElementById("toolHint");
  if (pendingFaceLoop.length === 0) {
    hint.textContent = "Click 3+ vertices in order, then click the first one again (or use Finish) to create a face.";
    return;
  }
  hint.innerHTML = `${pendingFaceLoop.length} vertex(es) selected. `;
  const finishBtn = document.createElement("button");
  finishBtn.textContent = "Finish face";
  finishBtn.disabled = pendingFaceLoop.length < 3;
  finishBtn.onclick = finishFace;
  finishBtn.style.marginRight = "6px";
  const cancelBtn = document.createElement("button");
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = cancelFace;
  hint.appendChild(finishBtn);
  hint.appendChild(cancelBtn);
}

function renderFacePanel() {
  const panel = document.getElementById("facePanel");
  // Don't clobber an in-progress color/material edit if a concurrent op
  // (another user's edit, or even our own color `input` events) triggers a
  // re-render while this panel still has focus -- a full innerHTML rebuild
  // would otherwise reset the field and drop keystrokes/cursor position.
  if (panel.contains(document.activeElement)) return;
  if (!ui.selectedFace || !state.faceIndex.has(ui.selectedFace)) {
    panel.innerHTML = '<div class="empty-hint">Select a face (Move tool, click its fill) to extrude or restyle it.</div>';
    return;
  }
  const faceId = ui.selectedFace;
  const props = state.faceProps.get(faceId) || {};
  const currentColor = /^#[0-9a-fA-F]{6}$/.test(props.color) ? props.color : `#${faceColor(faceId).toString(16).padStart(6, "0")}`;
  panel.innerHTML = `
    <div class="field-row"><label>Color</label><input id="faceColorInput" type="color" value="${currentColor}" style="width:56px;height:28px;padding:2px"/></div>
    <div class="field-row"><label>Material</label><input id="faceMaterialInput" type="text" value="${escapeHtml(props.material || "")}" placeholder="e.g. wood" style="width:130px"/></div>
    <div class="field-row"><label>Height</label><input id="extrudeHeight" type="number" step="0.25" value="1" style="width:70px"/></div>
    <button id="extrudeBtn" style="width:100%">Extrude</button>
    <button class="danger" id="deleteFaceBtn" style="width:100%;margin-top:6px">Delete face</button>
  `;
  document.getElementById("faceColorInput").addEventListener("input", (e) => setFaceProp(faceId, "color", e.target.value));
  document.getElementById("faceMaterialInput").addEventListener("change", (e) => setFaceProp(faceId, "material", e.target.value.trim()));
  document.getElementById("extrudeBtn").onclick = () => {
    const h = parseFloat(document.getElementById("extrudeHeight").value) || 1;
    extrudeFace(faceId, h);
  };
  document.getElementById("deleteFaceBtn").onclick = () => removeFace(faceId);
}

function renderVertexList() {
  const list = document.getElementById("vertexList");
  document.getElementById("vertexCount").textContent = state.vertices.size;
  // Same in-progress-edit guard as renderFacePanel -- typing a coordinate
  // shouldn't get wiped out by a re-render triggered elsewhere (e.g. a
  // collaborator's concurrent edit) before you've finished.
  if (list.contains(document.activeElement)) return;
  list.innerHTML = "";
  for (const [id, pos] of state.vertices) {
    const row = document.createElement("div");
    row.className = "path-row";
    row.innerHTML =
      `<span class="path-swatch" style="background:#d7dbe0"></span>` +
      `<span class="name">` +
      [0, 1, 2].map((axis) => `<input class="vertex-coord" data-axis="${axis}" type="number" step="0.1" value="${pos[axis].toFixed(2)}"/>`).join(" ") +
      `</span>` +
      `<button class="ghost-btn" data-act="del">✕</button>`;
    for (const input of row.querySelectorAll(".vertex-coord")) {
      input.addEventListener("change", (e) => {
        const current = state.vertices.get(id);
        if (!current) return;
        const next = current.slice();
        next[parseInt(e.target.dataset.axis, 10)] = parseFloat(e.target.value) || 0;
        const op = addVertexOp(id, next);
        applyOp(op);
        sendOps([op]);
        pushUndo({ kind: "vertex_move", vertexId: id, previous: current, forward: next });
        syncScene();
      });
    }
    row.querySelector('[data-act="del"]').onclick = () => removeVertex(id);
    list.appendChild(row);
  }
}

function renderFaceList() {
  const list = document.getElementById("faceList");
  document.getElementById("faceCount").textContent = state.faceIndex.size;
  list.innerHTML = "";
  for (const id of state.faceIndex) {
    const loop = liveValues(state.faceNodes.get(id));
    const material = state.faceProps.get(id)?.material;
    const label = material ? `${loop.length}-gon · ${escapeHtml(material)}` : `${loop.length}-gon`;
    const row = document.createElement("div");
    row.className = "path-row" + (id === ui.selectedFace ? " active" : "");
    row.style.cursor = "pointer";
    row.title = "Select this face to recolor, tag, extrude, or delete it";
    row.innerHTML = `<span class="path-swatch" style="background:#${faceColor(id).toString(16).padStart(6, "0")}"></span><span class="name">${label}</span><button class="ghost-btn" data-act="del">✕</button>`;
    // The whole row selects the face -- not just the text label -- since
    // clicking the color swatch itself is the most natural first thing to
    // try when looking for a color control, and that used to do nothing.
    row.addEventListener("click", (e) => {
      if (e.target.closest('[data-act="del"]')) return;
      ui.selectedFace = id;
      renderPanels();
    });
    row.querySelector('[data-act="del"]').onclick = () => removeFace(id);
    list.appendChild(row);
  }
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

function renderPresenceOverlay() {
  const layer = document.getElementById("cursorLayer");
  layer.innerHTML = "";
  const rect = renderer.domElement.getBoundingClientRect();
  for (const [actor, p] of state.presence) {
    if (actor === actorId || !p || !p.pos) continue;
    const v = new THREE.Vector3(p.pos[0], p.pos[1], p.pos[2]).project(camera);
    if (v.z > 1) continue;
    const x = (v.x * 0.5 + 0.5) * rect.width;
    const y = (-v.y * 0.5 + 0.5) * rect.height;
    const el = document.createElement("div");
    el.className = "cursor-label";
    el.style.left = x + "px";
    el.style.top = y + "px";
    el.style.background = p.color || "#4dabf7";
    el.textContent = p.name || actor;
    layer.appendChild(el);
  }
}

setInterval(() => {
  document.getElementById("opsCounter").textContent = `${ui.opsCount} ops relayed`;
  document.getElementById("offlineCounter").textContent = conn.outbox.length ? `${conn.outbox.length} queued offline` : "";
}, 400);

// -- resize & render loop -----------------------------------------------------------

function resizeRenderer() {
  const rect = canvasWrap.getBoundingClientRect();
  camera.aspect = rect.width / Math.max(rect.height, 1);
  camera.updateProjectionMatrix();
  renderer.setSize(rect.width, rect.height);
}
window.addEventListener("resize", resizeRenderer);
resizeRenderer();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderPresenceOverlay();
  renderer.render(scene, camera);
}
animate();

setTool("vertex");
renderPanels();
