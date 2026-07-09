// 3D collaborative mesh demo built directly on the crdt_cad.crdt.mesh wire
// protocol (MeshCRDT / MeshOp). Mirrors the same "server is authoritative,
// client mints ops + renders optimistically" design as sketch.js -- see
// common.js for the shared rationale.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

initTooltips();
initPanelCollapse();
initCommandPalette(buildCommands);

const actorId = getOrCreateActorId();
let actorName = getOrCreateActorName();
const actorColor = colorForActor(actorId);
const room = new URLSearchParams(location.search).get("room") || "demo-mesh";
document.getElementById("roomInput").value = room;
document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;

// Phase 17 read-only share links: true once the server's own snapshot/delta
// reply says this connection is a "viewer" (see RelayConnection's onRole) --
// gates the canvas pointerdown handler further down so a viewer can orbit/
// zoom/pan but never start an edit gesture.
let viewerMode = false;

// Phase D5: mirrors sketch.js's identical declaration -- see its comment.
let currentConnStatus = "connecting";
initStatusCluster();

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

// -- parametric primitives (Phase 16) -- typed dimensions, then click to place ----
const PRIMITIVE_FIELD_DEFS = {
  box: [["width", "Width"], ["height", "Height"], ["depth", "Depth"]],
  cylinder: [["radius", "Radius"], ["height", "Height"], ["segments", "Segments"]],
  pyramid: [["radius", "Base radius"], ["height", "Height"], ["segments", "Segments"]],
  plane: [["width", "Width"], ["depth", "Depth"]],
};
const PRIMITIVE_DEFAULTS = {
  box: { width: 2, height: 2, depth: 2 },
  cylinder: { radius: 1, height: 2, segments: 16 },
  pyramid: { radius: 1, height: 2, segments: 4 },
  plane: { width: 2, depth: 2 },
};
function isPrimitiveTool(tool) {
  return tool === "box" || tool === "cylinder" || tool === "pyramid" || tool === "plane";
}
// Currently-typed dimensions for the active primitive tool, or null when
// no primitive tool is selected -- reseeded from PRIMITIVE_DEFAULTS every
// time a primitive tool is (re-)selected (see setTool).
let primitiveFields = null;

// -- 3D snapping (Phase 16) -- toggle mirrors the 2D demo's Snap button ----------
let snapEnabled3D = false;
const GRID_SNAP_STEP = 1; // matches the GridHelper(20, 20) below -- 1-unit cells
const VERTEX_SNAP_THRESHOLD = 0.3; // world units; existing-vertex snap wins over grid snap when both are in range

/** Snaps a candidate world position to the nearest existing vertex
 * (within a small threshold) or, failing that, the nearest grid
 * intersection -- shared by new-vertex placement and vertex dragging,
 * a no-op unless the Snap toggle is on. `excludeVertexId` keeps a
 * vertex from snapping to its own (pre-drag) position. */
function snapPosition3D(pos, excludeVertexId) {
  if (!snapEnabled3D) return pos;
  let best = null, bestDist = VERTEX_SNAP_THRESHOLD;
  for (const [vid, vpos] of state.vertices) {
    if (vid === excludeVertexId) continue;
    const d = Math.hypot(pos[0] - vpos[0], pos[1] - vpos[1], pos[2] - vpos[2]);
    if (d < bestDist) { bestDist = d; best = vpos; }
  }
  if (best) return best.slice();
  return pos.map((v) => Math.round(v / GRID_SNAP_STEP) * GRID_SNAP_STEP);
}

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

function applyIncomingOps(ops, fromActor) {
  for (const op of ops) applyOp(op);
  ui.opsCount += ops.length;
  syncScene();
  // Phase D7: drives the AI-generation progress line off the same real
  // batches this fetch's own WS connection is already receiving -- see
  // noteGenerationBatch's own comment for why this is genuinely
  // arrival-driven, not a fake timer.
  if (fromActor === AI_GENERATOR_ACTOR_ID) noteGenerationBatch(ops);
  // Phase D6: mirrors sketch.js's identical remote-edit flash -- see its
  // comment. Mesh ops key their touched geometry differently: a vertex
  // move/create carries the vertex id as `scope`; a face op carries the
  // face id. Both get flashed the same way in syncScene's own render.
  if (fromActor && fromActor !== actorId) {
    const color = (state.presence.get(fromActor) || {}).color;
    if (color) {
      for (const op of ops) {
        if (op.target === "vertex" && state.vertices.has(op.payload.k)) flashRemoteEdit(op.payload.k, color);
        else if (op.target === "face_index" && state.faceIndex.has(op.payload.k)) flashRemoteEdit(op.payload.k, color);
        else if (op.target === "face_geom" && state.faceIndex.has(op.face_id)) flashRemoteEdit(op.face_id, color);
      }
    }
  }
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
    onOps: (ops, from) => applyIncomingOps(ops, from),
    onStatus: (status) => setStatus(status),
    onSaved: (at) => { setSaveState("saved", at); showToast("Saved", "success"); },
    onMergePreview: (mine, theirs, proceed) => showMergePreviewModal(mine, theirs, describeMeshOps, proceed),
    onValidityWarning: (faces, problems) => applyValidityWarning(faces, problems),
    onRole: (role) => {
      viewerMode = applyViewerModeUI(role);
      // Phase D6: unlike the 2D demo (which sends presence continuously
      // on mousemove), 3D only ever sends it at discrete commit points
      // (placing/dragging a vertex) -- without this, a collaborator who
      // joins and just looks around stays completely invisible to
      // everyone else's avatar stack/cursor layer until their first
      // edit. One ping right after connecting (not gated on viewerMode
      // -- a read-only viewer is still a real participant worth seeing)
      // fixes that; every subsequent real interaction still updates it.
      sendPresence([controls.target.x, controls.target.y, controls.target.z]);
    },
    token,
    kind: "mesh",
    room,
    initialOutbox: persistedOutbox,
  });

  p2p = new P2PManager(conn, actorId, {
    onPeerData: (peerActorId, ops) => applyIncomingOps(ops, peerActorId),
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
  if (viewerMode) {
    // See the identical guard (and its full rationale) in sketch.js:
    // this is the one chokepoint every mutating code path here already
    // funnels through, so gating it here -- rather than at each of
    // Vertex/Face/Move/the primitive tools' own entry points -- is what
    // guarantees a viewer's optimistic local edit never leaks out over
    // either the WS or the direct P2P channel.
    console.warn("sendOps() called while connected as a read-only viewer -- dropped, not sent");
    return;
  }
  ui.opsCount += ops.length;
  conn.send(ops);
  if (!conn.userWantsOffline) p2p.broadcastOps(ops);
}

function setStatus(status) {
  currentConnStatus = status;
  document.getElementById("statusText").textContent = status;
  updateStatusCluster(status, conn ? conn.outbox.length : 0);
  document.getElementById("offlineToggle").textContent = status === "offline" ? "Reconnect" : "Go offline";
  if (status === "online") setSaveState("saved");
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

document.getElementById("saveBtn").onclick = () => { setSaveState("saving"); conn.save(); };

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

document.getElementById("shareViewOnlyBtn").onclick = async () => {
  // See the identical handler (and its full rationale) in sketch.js.
  try {
    const resp = await fetch(withToken(`/api/mesh/${encodeURIComponent(room)}/share-link`, "mesh", room), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "viewer" }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }
    const { token } = await resp.json();
    const url = `${location.origin}/3d?room=${encodeURIComponent(room)}&token=${encodeURIComponent(token)}`;
    try {
      await navigator.clipboard.writeText(url);
      showToast("Read-only invite link copied to clipboard", "success");
    } catch {
      showToast(url, "info");
    }
  } catch (err) {
    showToast(`Could not create a view-only link: ${err.message}`, "error");
  }
};

document.getElementById("renameActorBtn").onclick = () => {
  const next = window.prompt("Your display name (shown to collaborators):", actorName);
  const updated = setActorName(next);
  if (!updated) return;
  actorName = updated;
  document.getElementById("actorLabel").textContent = `${actorName} (${actorId})`;
};

// -- document name (Phase D2 top bar; reuses Phase 17's rename endpoint) -------

const docNameBtn = document.getElementById("docNameBtn");

async function refreshDocName() {
  try {
    const resp = await fetch("/api/workspace/rooms");
    const rows = await resp.json();
    const row = rows.find((r) => r.kind === "mesh" && r.room_id === room);
    docNameBtn.textContent = (row && row.display_name) || room;
  } catch {
    docNameBtn.textContent = room;
  }
}
refreshDocName();

docNameBtn.onclick = async () => {
  const current = docNameBtn.textContent;
  const next = window.prompt("Rename this room:", current);
  if (next === null || !next.trim() || next.trim() === current) return;
  try {
    await renameRoom("mesh", room, next.trim(), conn);
    docNameBtn.textContent = next.trim();
    showToast("Renamed", "success");
  } catch (err) {
    showToast(`Rename failed: ${err.message}`, "error");
  }
};

// -- AI text-to-3D generation -------------------------------------------------------
// The generated mesh arrives back over the *same* WebSocket ops broadcast as any
// other edit (see the server's Room.commit_ops_batched), so it renders through the
// normal onOps -> applyIncomingOps -> syncScene path with no special-case client code.

const genBtn = document.getElementById("genBtn");
const genPromptInput = document.getElementById("genPromptInput");
const genStatus = document.getElementById("genStatus");

// -- AI generation staging (Phase D7 art direction) ------------------------------
//
// The ops really do stream in batches over the WS relay the requesting
// tab is already connected to (Room.commit_ops_batched, server/app.py --
// broadcast() there has no `exclude`, so the requester gets its own
// batches too, while its own fetch() is still pending), so the progress
// line below is driven by genuinely arriving `ops` messages, not a
// fake timer. There is no per-batch "this batch is the floor/walls/
// roof" tag server-side, though (generate_mesh_ops flattens every
// floor's vertices, then every floor's faces, into one flat op list
// before commit_ops_batched ever chunks it by raw size) -- so the
// stage names below are derived honestly from each batch's own
// face_prop "material" values (procedural_house.py tags every face
// with one: the user's chosen floor material, "roof"/"concrete", or
// "exterior_wall"/"interior_wall") rather than invented outright, and
// accumulate in whatever order they're actually seen (floor-then-roof-
// then-walls in practice, not necessarily the brief's illustrative
// "floor... walls... roof..." wording).
const AI_GENERATOR_ACTOR_ID = "ai_generator_bot";
let generationInFlight = false;
const generationStagesSeen = new Set();
// Phase G2: a scene's ops carry a "scene_object" face_prop (the index of
// the object each face belongs to) on every face, forced into its own
// batch per object server-side (Room.commit_ops_grouped_batched) -- so
// counting distinct values seen so far is an honest "objects placed so
// far" progress count, not a fake timer.
const generationSceneObjectsSeen = new Set();

// House-specific staging (Phase D7): "walls"/"roof"/"floor" only make
// sense for the house generator's own material vocabulary
// (exterior_wall/interior_wall/roof/concrete/<floor material>). Phase
// G1 added generators whose materials are just "wood"/"stone"/"metal"/
// etc -- defaulting those to "floor" would be actively misleading (a
// table has no floor being built), so anything outside the house
// vocabulary surfaces as its own material name instead.
function stageForMaterial(material) {
  if (typeof material !== "string") return null;
  if (material.includes("wall")) return "walls";
  if (material === "roof" || material === "concrete") return "roof";
  if (["wood", "marble", "tile", "carpet", "stone"].includes(material)) return "floor";
  return material || null;
}

/** Called from applyIncomingOps for every batch while a generation is
 * in flight -- the first call also flips the prompt box from the
 * "thinking" shimmer to the progress line, since the first ops batch
 * arriving is the honest signal that interpretation finished and
 * building started (not a fixed timer guessing at it). */
function noteGenerationBatch(ops) {
  if (!generationInFlight) return;
  if (genPromptInput.classList.contains("ai-thinking")) {
    genPromptInput.classList.remove("ai-thinking");
  }
  for (const op of ops) {
    if (op.target === "face_prop" && op.payload.k === "material") {
      const stage = stageForMaterial(op.payload.v);
      if (stage) generationStagesSeen.add(stage);
    }
    if (op.target === "face_prop" && op.payload.k === "scene_object") {
      generationSceneObjectsSeen.add(op.payload.v);
    }
  }
  if (generationSceneObjectsSeen.size > 0) {
    const stages = [...generationStagesSeen].join(", ");
    genStatus.textContent = `Placing object ${generationSceneObjectsSeen.size}${stages ? ` (${stages})` : ""}...`;
  } else {
    genStatus.textContent = `Building ${[...generationStagesSeen].join(", ")}...`;
  }
}

/** ~15 degree orbit around the current camera-to-target radius while
 * geometry lands, skipped entirely under prefers-reduced-motion (a
 * Three.js camera move isn't a CSS animation/transition, so the global
 * reduced-motion rule in tokens.css can't neutralize it the way it does
 * everywhere else motion is used in this app -- this has to check the
 * media query itself). Runs on a fixed ~4s tween rather than being tied
 * to exact batch timing, holding its final position once done. Returns
 * a cleanup function that stops it early (called if generation fails
 * fast, before the tween would otherwise finish on its own). */
function orbitCameraDuringGeneration() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return () => {};
  const totalRadians = (15 * Math.PI) / 180;
  const startAngle = Math.atan2(camera.position.z, camera.position.x);
  const radius = Math.hypot(camera.position.x, camera.position.z);
  const startTime = performance.now();
  const DURATION_MS = 4000;
  let active = true;
  function tick() {
    if (!active) return;
    const t = Math.min((performance.now() - startTime) / DURATION_MS, 1);
    const angle = startAngle + totalRadians * t;
    camera.position.x = radius * Math.cos(angle);
    camera.position.z = radius * Math.sin(angle);
    camera.lookAt(controls.target);
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return () => { active = false; };
}

// Phase G1: generation now dispatches across a whole registry of
// generators (house, table, chair, box, ...), not just the one house
// archetype, so the status line can't hardcode HouseSpec's own field
// names anymore -- a per-generator summary, with a reasonable generic
// fallback (first few numeric dimension fields) for anything not
// special-cased here.
function describeGeneratedSpec(generatorName, spec) {
  if (generatorName === "house") {
    return `${spec.bedrooms} bedroom(s), ${spec.floors} floor(s), ${escapeHtml(spec.floor_material)} floor, ${escapeHtml(spec.style)} style`;
  }
  if (generatorName === "scene") {
    const counts = new Map();
    for (const obj of spec.objects || []) {
      counts.set(obj.generator, (counts.get(obj.generator) || 0) + (obj.count || 1));
    }
    const parts = [...counts.entries()].map(([name, count]) => (count > 1 ? `${count}x ${escapeHtml(name)}` : escapeHtml(name)));
    return `${(spec.objects || []).length} object group(s): ${parts.join(", ")}`;
  }
  const dims = Object.entries(spec)
    .filter(([key, value]) => typeof value === "number" && key.endsWith("_m"))
    .slice(0, 4)
    .map(([key, value]) => `${key.replace(/_m$/, "")}: ${value}m`);
  return dims.length ? dims.join(", ") : "default dimensions";
}

async function generateMesh() {
  const prompt = genPromptInput.value.trim();
  if (!prompt) {
    showToast("Describe what to generate first", "error");
    return;
  }
  genBtn.disabled = true;
  genBtn.textContent = "Generating…";
  generationInFlight = true;
  generationStagesSeen.clear();
  generationSceneObjectsSeen.clear();
  genPromptInput.classList.add("ai-thinking");
  genStatus.textContent = "Interpreting your prompt...";
  const stopOrbit = orbitCameraDuringGeneration();
  try {
    const resp = await fetch(withToken(`/api/mesh/${encodeURIComponent(room)}/generate`, "mesh", room), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      // `detail` is usually a plain string, but a pre-commit validation
      // failure (GenerationValidationError, Phase G1 rule 1) returns a
      // structured object instead ({message, errors, watertight, ...})
      // so the report's own fields are inspectable, not just a joined
      // string -- new Error(object) would otherwise stringify to the
      // useless "[object Object]".
      const detail = err.detail;
      const message =
        typeof detail === "string" ? detail
        : detail && typeof detail === "object" ? `${detail.message}: ${(detail.errors || []).join("; ")}`
        : `HTTP ${resp.status}`;
      throw new Error(message);
    }
    const result = await resp.json();
    const via = result.interpretation_source === "llm" ? "Claude Fable 5" : "the offline heuristic dispatcher";
    // mesh_source (Phase 9, unverified against a live Meshy account --
    // see crdt_cad.ai.meshy_adapter's module docstring): "meshy" only
    // when MESHY_API_KEY was set *and* the hosted API actually returned
    // a mesh; any failure there silently falls back to "procedural",
    // same as it always was.
    const meshVia = result.mesh_source === "meshy" ? "Meshy" : "the procedural builder";
    const validity = result.watertight && result.manifold ? "watertight" : "not fully watertight (see below)";
    showToast(`Built by ${result.actor} -- ${result.vertex_count} vertices, ${result.face_count} faces`, "success");
    genStatus.textContent =
      `Last generation: ${escapeHtml(result.generator)} (${describeGeneratedSpec(result.generator, result.spec)}), ` +
      `${validity}, interpreted via ${via}, mesh via ${meshVia}, ${result.batches} batch(es).`;
  } catch (err) {
    // Danger toast with the server's own reason, an inline Retry (the
    // prompt box is never cleared on failure, so Retry just re-submits
    // exactly what's already there), per the brief.
    showToast(`Generation failed: ${err.message}`, "error", { actionLabel: "Retry", onAction: () => generateMesh() });
    genStatus.textContent = `Generation failed: ${err.message}`;
  } finally {
    genBtn.disabled = false;
    genBtn.textContent = "Generate";
    generationInFlight = false;
    genPromptInput.classList.remove("ai-thinking");
    stopOrbit();
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
  showUndoToast("Vertex deleted", undo);
}

function removeFace(id) {
  const op = removeFaceOp(id);
  applyOp(op);
  sendOps([op]);
  pushUndo({ kind: "face_remove", faceId: id });
  if (ui.selectedFace === id) ui.selectedFace = null;
  syncScene();
  showUndoToast("Face deleted", undo);
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

// -- parametric primitives (Phase 16) ---------------------------------------------
// Box/Cylinder/Pyramid/Plane generate a whole vertex+edge+face set as one
// batch of client-minted ops, following exactly the same pattern
// extrudeFace/finishFace already use above (and the same pattern
// crdt_cad.ai.generator.generate_mesh_ops uses server-side to build a
// whole AI-generated mesh): mint each op via the existing op
// constructors, apply it locally, collect it into one flat array, then
// ONE sendOps(ops) call and ONE pushUndo({kind:"composite", ...}) --
// never one op (or one undo entry) per vertex/face. A primitive well
// under a few hundred ops stays far under the WS message's op/byte
// ceilings (security.max_ops_per_message/max_ws_message_bytes), so no
// server-side chunking (Room.commit_ops_batched, which is a REST-only,
// AI-generation-specific helper) is needed here.

function pushVertexOp(ops, subEntries, id, pos) {
  const op = addVertexOp(id, pos);
  ops.push(op);
  subEntries.push({ kind: "vertex_create", vertexId: id, position: pos });
}
function pushFaceOps(ops, subEntries, faceId, loop) {
  for (const op of addFaceOps(faceId, loop)) ops.push(op);
  subEntries.push({ kind: "face_add", faceId });
}
function pushEdgeOp(ops, subEntries, a, b) {
  ops.push(addEdgeOp(a, b));
  subEntries.push({ kind: "edge_add", v1: a, v2: b });
}
function pushRingEdges(ops, subEntries, ring) {
  for (let i = 0; i < ring.length; i++) pushEdgeOp(ops, subEntries, ring[i], ring[(i + 1) % ring.length]);
}

/** Applies every op in a builder's result locally, sends them as one
 * batch, and records one composite undo entry -- shared by all four
 * primitive builders below, mirroring extrudeFace's own tail exactly. */
function commitPrimitive({ ops, subEntries }) {
  for (const op of ops) applyOp(op);
  sendOps(ops);
  pushUndo({ kind: "composite", entries: subEntries });
  syncScene();
}

/** `segments` evenly-spaced points around a horizontal circle of the
 * given `radius`, centered at (center[0], y, center[2]) -- shared by
 * Cylinder and Pyramid, which both start from a base ring. */
function ringPositions(center, radius, segments, y) {
  const [cx, , cz] = center;
  const pts = [];
  for (let i = 0; i < segments; i++) {
    const theta = (2 * Math.PI * i) / segments;
    pts.push([cx + radius * Math.cos(theta), y, cz + radius * Math.sin(theta)]);
  }
  return pts;
}

/** `center` is the box's base center (its lowest, middle point) --
 * matches where the ground-click placement below anchors every
 * primitive, so a Box "sits on" the point the user clicked the same way
 * a Cylinder/Pyramid/Plane do. */
function buildBoxOps(center, w, h, d) {
  const ops = [], subEntries = [];
  const [cx, cy, cz] = center;
  const hw = w / 2, hd = d / 2;
  const corners = {
    b0: [cx - hw, cy, cz - hd], b1: [cx + hw, cy, cz - hd], b2: [cx + hw, cy, cz + hd], b3: [cx - hw, cy, cz + hd],
    t0: [cx - hw, cy + h, cz - hd], t1: [cx + hw, cy + h, cz - hd], t2: [cx + hw, cy + h, cz + hd], t3: [cx - hw, cy + h, cz + hd],
  };
  const ids = {};
  for (const [key, pos] of Object.entries(corners)) {
    ids[key] = "v_" + rid();
    pushVertexOp(ops, subEntries, ids[key], pos);
  }
  const rings = [
    [ids.b0, ids.b3, ids.b2, ids.b1], // bottom
    [ids.t0, ids.t1, ids.t2, ids.t3], // top
    [ids.b0, ids.b1, ids.t1, ids.t0], // sides
    [ids.b1, ids.b2, ids.t2, ids.t1],
    [ids.b2, ids.b3, ids.t3, ids.t2],
    [ids.b3, ids.b0, ids.t0, ids.t3],
  ];
  for (const ring of rings) {
    pushFaceOps(ops, subEntries, "face_" + rid(), ring);
    pushRingEdges(ops, subEntries, ring);
  }
  return { ops, subEntries };
}

function buildCylinderOps(center, radius, height, segments) {
  const ops = [], subEntries = [];
  const cy = center[1];
  const bottomIds = ringPositions(center, radius, segments, cy).map((p) => {
    const id = "v_" + rid();
    pushVertexOp(ops, subEntries, id, p);
    return id;
  });
  const topIds = ringPositions(center, radius, segments, cy + height).map((p) => {
    const id = "v_" + rid();
    pushVertexOp(ops, subEntries, id, p);
    return id;
  });
  for (let i = 0; i < segments; i++) {
    const j = (i + 1) % segments;
    const ring = [bottomIds[i], bottomIds[j], topIds[j], topIds[i]];
    pushFaceOps(ops, subEntries, "face_" + rid(), ring);
    pushRingEdges(ops, subEntries, ring);
  }
  pushFaceOps(ops, subEntries, "face_" + rid(), [...bottomIds].reverse());
  pushRingEdges(ops, subEntries, bottomIds);
  pushFaceOps(ops, subEntries, "face_" + rid(), topIds);
  pushRingEdges(ops, subEntries, topIds);
  return { ops, subEntries };
}

function buildPyramidOps(center, radius, height, segments) {
  const ops = [], subEntries = [];
  const [cx, cy, cz] = center;
  const baseIds = ringPositions(center, radius, segments, cy).map((p) => {
    const id = "v_" + rid();
    pushVertexOp(ops, subEntries, id, p);
    return id;
  });
  const apexId = "v_" + rid();
  pushVertexOp(ops, subEntries, apexId, [cx, cy + height, cz]);
  for (let i = 0; i < segments; i++) {
    const j = (i + 1) % segments;
    const ring = [baseIds[i], baseIds[j], apexId];
    pushFaceOps(ops, subEntries, "face_" + rid(), ring);
    pushRingEdges(ops, subEntries, ring);
  }
  pushFaceOps(ops, subEntries, "face_" + rid(), [...baseIds].reverse());
  pushRingEdges(ops, subEntries, baseIds);
  return { ops, subEntries };
}

function buildPlaneOps(center, w, d) {
  const ops = [], subEntries = [];
  const [cx, cy, cz] = center;
  const hw = w / 2, hd = d / 2;
  const corners = [[cx - hw, cy, cz - hd], [cx + hw, cy, cz - hd], [cx + hw, cy, cz + hd], [cx - hw, cy, cz + hd]];
  const ids = corners.map((p) => {
    const id = "v_" + rid();
    pushVertexOp(ops, subEntries, id, p);
    return id;
  });
  pushFaceOps(ops, subEntries, "face_" + rid(), ids);
  pushRingEdges(ops, subEntries, ids);
  return { ops, subEntries };
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
renderer.domElement.style.cursor = "crosshair"; // matches ui.tool's "vertex" default -- see setTool()

const scene = new THREE.Scene();
scene.background = new THREE.Color(canvasColor("--bg-canvas"));

const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 1000);
camera.position.set(6, 6, 8);
camera.lookAt(0, 0, 0);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0);
controls.enableDamping = true;
// Phase D6 follow mode: "any manual pan/zoom exits" -- OrbitControls
// fires its own "start" event only for a genuine user-driven pointer/
// wheel gesture, never for the programmatic controls.target.set() the
// follow-mode tick itself does, so this can't immediately undo itself.
controls.addEventListener("start", exitFollow);

// -- orthographic-ish view buttons (Phase 16) ------------------------------------
// Repositions the *existing* perspective camera to standard axis-aligned
// views rather than swapping in a real THREE.OrthographicCamera: `camera`
// is referenced directly by name throughout this file (raycasting,
// resize, the presence overlay, the render loop), so a true second
// camera would mean threading a "current camera" indirection through
// every one of those call sites. An axis-aligned perspective view reads
// as "Top/Front/Right" for a CAD sketch at this scale and is a much
// smaller, safer change -- an honest, deliberate scope reduction from a
// literal parallel-projection camera, not a silent one.
const DEFAULT_CAMERA_POSITION = [6, 6, 8];
const VIEW_DISTANCE = 12;
function setCameraView(view) {
  exitFollow(); // Phase D6: a manual view change exits follow mode
  controls.target.set(0, 0, 0);
  if (view === "top") {
    // Looking straight down -Y with the default up=(0,1,0) leaves the
    // camera's up vector parallel to its view direction (undefined
    // orientation) -- point "up" along -Z instead for a top-down view.
    camera.up.set(0, 0, -1);
    camera.position.set(0, VIEW_DISTANCE, 0);
  } else {
    camera.up.set(0, 1, 0);
    if (view === "front") camera.position.set(0, 0, VIEW_DISTANCE);
    else if (view === "right") camera.position.set(VIEW_DISTANCE, 0, 0);
    else camera.position.set(...DEFAULT_CAMERA_POSITION); // "perspective" -- the original default view
  }
  camera.lookAt(controls.target);
  controls.update();
  for (const [id, v] of [["viewTop", "top"], ["viewFront", "front"], ["viewRight", "right"], ["viewPerspective", "perspective"]]) {
    document.getElementById(id).classList.toggle("active", v === view);
  }
}
document.getElementById("viewTop").onclick = () => setCameraView("top");
document.getElementById("viewFront").onclick = () => setCameraView("front");
document.getElementById("viewRight").onclick = () => setCameraView("right");
document.getElementById("viewPerspective").onclick = () => setCameraView("perspective");

document.getElementById("snapToggleBtn3d").onclick = (e) => {
  snapEnabled3D = !snapEnabled3D;
  e.target.classList.toggle("active", snapEnabled3D);
};

scene.add(new THREE.AmbientLight(0xffffff, 0.65));
const dirLight = new THREE.DirectionalLight(0xffffff, 0.9);
dirLight.position.set(6, 12, 8);
scene.add(dirLight);

// Colored from the active theme's --border at construction time -- unlike
// scene.background (a plain property, trivial to re-apply on toggle, see
// applyThreeJsTheme below), GridHelper bakes its two colors into a
// per-vertex color buffer attribute at construction, so a live theme
// toggle while this page is open won't retint an already-built grid.
// Documented gap, not silently missed: acceptable for D1 (the important
// promise -- a *fresh* load in light theme looks right -- holds), a
// rebuild-the-helper-on-toggle fix is a candidate for D3/D8 polish.
const gridColor = canvasColor("--border");
scene.add(new THREE.GridHelper(20, 20, gridColor, gridColor));

const groundGeo = new THREE.PlaneGeometry(200, 200).rotateX(-Math.PI / 2);
const ground = new THREE.Mesh(groundGeo, new THREE.MeshBasicMaterial({ visible: false }));
scene.add(ground);

// Re-applies the parts of the 3D scene's theme that a CSS custom
// property alone can't reach (scene.background isn't driven by the DOM
// at all) -- called once immediately by initThemeToggle (redundant with
// the construction-time value above, but keeps this the single source
// of truth) and again on every toggle.
function applyThreeJsTheme() {
  scene.background = new THREE.Color(canvasColor("--bg-canvas"));
}
initThemeToggle(applyThreeJsTheme);

const vertexGeo = new THREE.SphereGeometry(0.1, 16, 16);
const edgeMat = new THREE.LineBasicMaterial({ color: 0x4dabf7 });
let pendingLoopLine = null;

const vertexMeshes = new Map();
const edgeLines = new Map();
const faceMeshes = new Map();
const invalidFaceOutlines = new Map();

const FACE_PALETTE = [0x4dabf7, 0x69db7c, 0xffd43b, 0xda77f2, 0xff922b, 0x38d9a9, 0xf783ac];

// Phase D6: id (vertex or face) -> a Three.js numeric color, briefly, for
// "a just-arrived remote edit flashes its color" -- see flashRemoteEdit
// and its onOps call site. vertexColor/faceColor both check this first
// so the existing per-frame syncScene() material assignment is all that's
// needed to show (and, once the entry expires, stop showing) it.
const remoteEditFlashes = new Map();
function flashRemoteEdit(id, cssColor) {
  remoteEditFlashes.set(id, parseInt(cssColor.slice(1), 16));
  syncScene();
  setTimeout(() => { remoteEditFlashes.delete(id); syncScene(); }, 600);
}

function faceColor(faceId) {
  if (remoteEditFlashes.has(faceId)) return remoteEditFlashes.get(faceId);
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
  if (remoteEditFlashes.has(id)) return remoteEditFlashes.get(id);
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
      // Phase D6: faceColor() was previously only ever read at mesh
      // creation time -- an existing face's material color never
      // updated afterward, which would have made flashRemoteEdit a
      // silent no-op for any face that already had a mesh.
      mesh.material.color.set(faceColor(id));
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
  el.className = "validity-banner";
  const list = problems
    .map((p) => `<li>${p.problem} (face${p.faces.length > 1 ? "s" : ""} ${p.faces.join(", ")})</li>`)
    .join("");
  el.innerHTML = `
    <div class="validity-banner-icon">${iconHtml("warning")}</div>
    <div class="validity-banner-body">
      <div class="validity-banner-title">Mesh validity warning</div>
      <div class="validity-banner-desc">
        A merge left the highlighted face(s) (outlined in red) in an inconsistent
        state. Nothing was rejected -- fix or delete the affected faces when convenient.
      </div>
      <ul>${list}</ul>
      <button id="validityDismissBtn" class="danger-btn">Dismiss</button>
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
  if (viewerMode) return; // Phase 17: a read-only viewer can still orbit/pan/zoom (OrbitControls, separate listeners) but never start an edit gesture
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
    if (pt) addVertex(snapPosition3D([round(pt.x), 0, round(pt.z)], null));
  } else if (ui.tool === "move") {
    const fmesh = raycastFaces();
    ui.selectedFace = fmesh ? fmesh.userData.faceId : null;
    renderPanels();
  } else if (isPrimitiveTool(ui.tool)) {
    const pt = raycastGround();
    if (!pt) return;
    // Every primitive is placed with its base center at the clicked
    // (snapped) ground point, y=0, so it sits on the grid the same way
    // a plain vertex does -- consistent anchor across all four kinds.
    const center = snapPosition3D([round(pt.x), 0, round(pt.z)], null);
    const f = primitiveFields || PRIMITIVE_DEFAULTS[ui.tool];
    if (ui.tool === "box") commitPrimitive(buildBoxOps(center, f.width, f.height, f.depth));
    else if (ui.tool === "cylinder") commitPrimitive(buildCylinderOps(center, f.radius, f.height, Math.max(3, Math.round(f.segments))));
    else if (ui.tool === "pyramid") commitPrimitive(buildPyramidOps(center, f.radius, f.height, Math.max(3, Math.round(f.segments))));
    else if (ui.tool === "plane") commitPrimitive(buildPlaneOps(center, f.width, f.depth));
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
  const rawPos = dragState.vertical
    ? [dragState.fixedX, round(hit.y), dragState.fixedZ]
    : [round(hit.x), current[1], round(hit.z)];
  const pos = snapPosition3D(rawPos, dragState.vertexId);
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
    // A real bug caught while verifying Phase 16's snapping: pointermove
    // only calls syncFacesTouching() (a lightweight 3D-scene-only patch,
    // for live feedback during the drag) -- nothing here ever refreshed
    // the side panels afterward, so the Vertices list's coordinate
    // inputs kept showing the *pre*-drag position even though the 3D
    // view was already correct. renderPanels() is cheap (it's already
    // called after every other mutation via syncScene()) and only needs
    // to run once, here, when the drag actually ends.
    renderPanels();
  }
});

// -- tool buttons -----------------------------------------------------------------

const TOOL_BUTTON_IDS_3D = {
  vertex: "toolVertex", face: "toolFace", move: "toolMove",
  box: "toolBox", cylinder: "toolCylinder", pyramid: "toolPyramid", plane: "toolPlane",
};
function setTool(tool) {
  ui.tool = tool;
  for (const [t, id] of Object.entries(TOOL_BUTTON_IDS_3D)) {
    document.getElementById(id).classList.toggle("active", tool === t);
  }
  const hints = {
    vertex: "Click the ground grid to place a vertex, or drag an existing one to move it (hold Shift to move it up/down instead).",
    face: "Click 3+ vertices in order, then click the first one again (or use Finish) to create a face.",
    move: "Drag a vertex to move it (hold Shift to move it up/down; or type exact X/Y/Z below). Click empty space on a face to select it for extrusion, recoloring, or a material tag.",
    box: "Type dimensions below, then click the ground to place the box.",
    cylinder: "Type dimensions below, then click the ground to place the cylinder.",
    pyramid: "Type dimensions below, then click the ground to place the pyramid.",
    plane: "Type dimensions below, then click the ground to place the plane.",
  };
  document.getElementById("toolHint").textContent = hints[tool];
  if (tool !== "face" && pendingFaceLoop.length) cancelFace();
  if (isPrimitiveTool(tool)) primitiveFields = { ...PRIMITIVE_DEFAULTS[tool] };
  // Phase D3: crosshair for tools that place brand-new geometry (a
  // vertex or a primitive), default arrow for tools that only interact
  // with what's already there (Face's click-existing-vertices loop,
  // Move's drag). Orbiting/panning the camera is OrbitControls' own
  // concern, not this canvas's cursor.
  renderer.domElement.style.cursor = tool === "face" || tool === "move" ? "default" : "crosshair";
  renderPrimitivePanel();
  renderPanels();
}
document.getElementById("toolVertex").onclick = () => setTool("vertex");
document.getElementById("toolFace").onclick = () => setTool("face");
document.getElementById("toolMove").onclick = () => setTool("move");
document.getElementById("toolBox").onclick = () => setTool("box");
document.getElementById("toolCylinder").onclick = () => setTool("cylinder");
document.getElementById("toolPyramid").onclick = () => setTool("pyramid");
document.getElementById("toolPlane").onclick = () => setTool("plane");

/** Mirrors sketch.js's buildCommands() -- see its comment for the
 * viewer-mode filtering rationale (dropped entirely for tools/editing,
 * clickCmd() additionally skips anything the .viewer-mode CSS has
 * currently disabled). Rebuilt fresh every time the palette opens. */
function buildCommands() {
  function cmd(label, group, shortcut, icon, run) {
    return { label, group, shortcut, icon, run };
  }
  function clickCmd(id, label, group, shortcut, icon) {
    const el = document.getElementById(id);
    if (!el) return null;
    if (getComputedStyle(el).pointerEvents === "none") return null;
    return cmd(label, group, shortcut, icon, () => el.click());
  }
  function toolCmd(tool, label, key, icon) {
    return viewerMode ? null : cmd(label, "Tools", key.toUpperCase(), icon, () => setTool(tool));
  }

  return [
    toolCmd("vertex", "Vertex", "v", "vertex"),
    toolCmd("face", "Face", "f", "triangle"),
    toolCmd("move", "Move", "m", "cursor"),
    toolCmd("box", "Box", "b", "box"),
    toolCmd("cylinder", "Cylinder", "c", "cylinder"),
    toolCmd("pyramid", "Pyramid", "p", "pyramid"),
    toolCmd("plane", "Plane", "l", "plane"),

    !viewerMode && cmd("Undo", "Edit", "Ctrl+Z", "undo", undo),
    !viewerMode && cmd("Redo", "Edit", "Ctrl+Shift+Z", "redo", redo),

    clickCmd("viewTop", "View: Top", "View", "", "maximize"),
    clickCmd("viewFront", "View: Front", "View", "", "maximize"),
    clickCmd("viewRight", "View: Right", "View", "", "maximize"),
    clickCmd("viewPerspective", "View: Perspective", "View", "", "maximize"),
    clickCmd("snapToggleBtn3d", "Toggle snap to grid/vertices", "View", "", "magnet"),
    clickCmd("toggleSecondaryPanelBtn", "Toggle tools & files panel", "View", "\\", "chevron-left"),
    clickCmd("toggleRightPanelBtn", "Toggle inspector panel", "View", "\\", "chevron-right"),

    clickCmd("saveBtn", "Save", "File", "", "save"),
    clickCmd("downloadJsonBtn", "Export .json", "File", "", "file"),
    clickCmd("downloadStlBtn", "Export .stl", "File", "", "file"),
    clickCmd("downloadStepBtn", "Export .step", "File", "", "file"),

    (() => {
      const el = document.getElementById("genPromptInput");
      if (!el || getComputedStyle(el).pointerEvents === "none") return null;
      return cmd("Focus AI Generate prompt", "AI Generate", "", "sparkles", () => el.focus());
    })(),
    clickCmd("genBtn", "Generate", "AI Generate", "", "sparkles"),

    clickCmd("shareBtn", "Copy full-access invite link", "Room", "", "link"),
    clickCmd("shareViewOnlyBtn", "Copy view-only invite link", "Room", "", "eye"),
    clickCmd("docNameBtn", "Rename this room", "Room", "", "pen"),
    clickCmd("renameActorBtn", "Change your display name", "Room", "", "pen"),
    clickCmd("offlineToggle", document.getElementById("offlineToggle")?.textContent || "Go offline", "Room", "", "plug-off"),

    clickCmd("themeToggleBtn", "Toggle light/dark theme", "General", "", "sun"),
    cmd("Keyboard shortcuts", "General", "?", "search", () => showShortcutOverlay(SHORTCUT_GROUPS_3D)),
    cmd("Open 2D sketch demo", "General", "", "chevron-right", () => { location.href = "/2d"; }),
    cmd("Back to workspace home", "General", "", "home", () => { location.href = "/"; }),
  ].filter(Boolean);
}

/** Numeric dimension fields for the active primitive tool -- "type
 * dimensions, then click to place" per the brief, mirroring the 2D
 * demo's shape numeric panel rather than a drag-to-size gesture (3D
 * primitives don't have an obvious 2-point drag the way a 2D rect
 * does). Empty when no primitive tool is active. */
function renderPrimitivePanel() {
  const panel = document.getElementById("primitivePanel");
  if (!panel) return;
  if (!isPrimitiveTool(ui.tool)) { panel.innerHTML = ""; return; }
  const defs = PRIMITIVE_FIELD_DEFS[ui.tool];
  panel.innerHTML = defs
    .map(([key, label]) => {
      const isInt = key === "segments";
      return `<div class="field-row"><label>${label}</label><input class="primField" data-key="${key}" type="number" step="${isInt ? 1 : 0.1}" min="${isInt ? 3 : 0.01}" value="${primitiveFields[key]}" style="width:70px"/></div>`;
    })
    .join("");
  for (const inp of panel.querySelectorAll(".primField")) {
    inp.addEventListener("change", (e) => {
      const key = e.target.dataset.key;
      primitiveFields[key] = parseFloat(e.target.value) || PRIMITIVE_DEFAULTS[ui.tool][key];
    });
  }
}

document.getElementById("undoBtn").onclick = undo;
document.getElementById("redoBtn").onclick = redo;

// Phase D4 single-key tool shortcuts -- same rationale as sketch.js's
// KEY_TO_TOOL: gated on !viewerMode because a read-only viewer already
// can't reach these same tool buttons via mouse (.viewer-mode CSS).
const KEY_TO_TOOL_3D = { v: "vertex", f: "face", m: "move", b: "box", c: "cylinder", p: "pyramid", l: "plane" };

const SHORTCUT_GROUPS_3D = [
  {
    title: "Tools",
    rows: [
      ["V", "Vertex"], ["F", "Face"], ["M", "Move"], ["B", "Box"],
      ["C", "Cylinder"], ["P", "Pyramid"], ["L", "Plane"],
    ],
  },
  {
    title: "Editing",
    rows: [["Ctrl/Cmd+Z", "Undo"], ["Ctrl/Cmd+Shift+Z", "Redo"]],
  },
  {
    title: "View",
    rows: [
      ["Drag", "Orbit"], ["Scroll wheel", "Zoom"], ["Right-drag", "Pan"],
      ["\\", "Toggle both side panels"],
    ],
  },
  {
    title: "General",
    rows: [["Ctrl/Cmd+K", "Open the command palette"], ["?", "Toggle this overlay"]],
  },
];

window.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && (e.key === "z" || e.key === "Z")) {
    e.preventDefault();
    if (!viewerMode) { if (e.shiftKey) redo(); else undo(); }
  } else if (mod && (e.key === "y" || e.key === "Y")) {
    e.preventDefault();
    if (!viewerMode) redo();
  } else if (e.key === "?") {
    // see the identical comment in sketch.js -- without preventDefault,
    // this same keystroke's default action types "?" into the overlay's
    // own just-focused search input, self-filtering the list.
    e.preventDefault();
    showShortcutOverlay(SHORTCUT_GROUPS_3D);
  } else if (!mod && !e.altKey && !viewerMode && KEY_TO_TOOL_3D[e.key.toLowerCase()]) {
    e.preventDefault();
    setTool(KEY_TO_TOOL_3D[e.key.toLowerCase()]);
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
      `<button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>`;
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
    row.innerHTML = `<span class="path-swatch" style="background:#${faceColor(id).toString(16).padStart(6, "0")}"></span><span class="name">${label}</span><button class="ghost-btn" data-act="del" title="Delete" aria-label="Delete">${iconHtml("x")}</button>`;
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
  renderAvatarStack(actorName, actorColor, others, toggleFollow, followingActorId);
}

// Phase D6: mirrors sketch.js's tickPresenceCursors -- eased position in
// 3D *world* space (not screen space, so the ease stays correct as the
// camera itself moves), idle-fade after 3s. actorId -> {pos, targetPos,
// lastMovedAt, el}.
const CURSOR_IDLE_FADE_MS = 3000;
const remoteCursorState = new Map();

function renderPresenceOverlay() {
  const layer = document.getElementById("cursorLayer");
  const rect = renderer.domElement.getBoundingClientRect();
  const now = performance.now();
  const seen = new Set();
  for (const [actor, p] of state.presence) {
    if (actor === actorId || !p || !p.pos) continue;
    seen.add(actor);
    let cs = remoteCursorState.get(actor);
    if (!cs) {
      cs = { pos: [...p.pos], targetPos: p.pos, lastMovedAt: now, el: null };
      remoteCursorState.set(actor, cs);
    }
    if (cs.targetPos[0] !== p.pos[0] || cs.targetPos[1] !== p.pos[1] || cs.targetPos[2] !== p.pos[2]) {
      cs.targetPos = p.pos;
      cs.lastMovedAt = now;
    }
    const t = 1 - Math.exp(-16 / 80); // same ~80ms ease as the 2D demo
    for (let i = 0; i < 3; i++) cs.pos[i] += (cs.targetPos[i] - cs.pos[i]) * t;

    const v = new THREE.Vector3(...cs.pos).project(camera);
    if (v.z > 1) { cs.el?.remove(); cs.el = null; continue; }
    if (!cs.el) {
      cs.el = document.createElement("div");
      cs.el.className = "cursor-label";
      cs.el.innerHTML = '<div class="cursor-dot"></div><div class="cursor-name"></div>';
      layer.appendChild(cs.el);
    }
    cs.el.style.left = (v.x * 0.5 + 0.5) * rect.width + "px";
    cs.el.style.top = (-v.y * 0.5 + 0.5) * rect.height + "px";
    cs.el.querySelector(".cursor-dot").style.background = p.color || "#4dabf7";
    cs.el.querySelector(".cursor-name").style.background = p.color || "#4dabf7";
    cs.el.querySelector(".cursor-name").textContent = p.name || actor;
    cs.el.classList.toggle("idle", now - cs.lastMovedAt > CURSOR_IDLE_FADE_MS);
  }
  for (const [actor, cs] of remoteCursorState) {
    if (!seen.has(actor)) {
      cs.el?.remove();
      remoteCursorState.delete(actor);
    }
  }
  // Follow mode (Phase D6 stretch goal): move the OrbitControls target
  // to whoever's being followed, every frame -- their own camera
  // position/orientation is never broadcast (client-local-only, per the
  // brief), so this can only mean "keep orbiting around where they are,"
  // not a full camera-pose sync.
  if (followingActorId) {
    const followed = remoteCursorState.get(followingActorId);
    if (followed) controls.target.set(...followed.pos);
  }
}

// -- follow mode (Phase D6 stretch goal) -----------------------------------------

let followingActorId = null;

function toggleFollow(id) {
  followingActorId = followingActorId === id ? null : id;
  renderPresenceList();
}

function exitFollow() {
  if (followingActorId) followingActorId = null;
}

setInterval(() => {
  document.getElementById("opsCounter").textContent = `${ui.opsCount} ops relayed`;
  // `conn` is assigned inside an async bootstrap (it awaits
  // ensureRoomAccess() first) -- this 400ms interval can fire before
  // that resolves, so `conn` may briefly still be undefined here. A
  // real, pre-existing (not Phase-16-specific) uncaught exception
  // caught live: "Cannot read properties of undefined (reading
  // 'outbox')" on an early tick.
  updateStatusCluster(currentConnStatus, conn ? conn.outbox.length : 0);
  const hint = document.getElementById("emptyCanvasHint");
  if (hint) hint.style.display = state.vertices.size === 0 ? "block" : "none";
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
