const rail = document.getElementById("rail");
const hideRail = document.getElementById("hide-rail");
const showRail = document.getElementById("show-rail");
const viewport = document.getElementById("viewport");
const world = document.getElementById("world");
const edgesEl = document.getElementById("edges");
const nodesEl = document.getElementById("nodes");
const recentsEl = document.getElementById("recents");
const finder = document.getElementById("finder");
const finderHits = document.getElementById("finder-hits");
const finderStatus = document.getElementById("finder-status");
const finderFilters = document.getElementById("finder-filters");
const queryField = document.getElementById("q");
const recentsLabel = document.getElementById("recents-label");
const strip = document.getElementById("strip");
const walkList = document.getElementById("walk-list");
const inspector = document.getElementById("inspector");
const searchStatus = finderStatus;
const admin = document.getElementById("admin");
const adminJob = document.getElementById("admin-job");
const adminIndex = document.getElementById("admin-index");
const adminGaps = document.getElementById("admin-gaps");
const adminConnectors = document.getElementById("admin-connectors");
const adminRecent = document.getElementById("admin-recent");
const adminProgress = document.getElementById("admin-progress");
const adminProgressFill = document.getElementById("admin-progress-fill");
const adminProgressLabel = document.getElementById("admin-progress-label");
const adminDlq = document.getElementById("admin-dlq");
const syncAll = document.getElementById("sync-all");
const adminRecentState = new Map();
let adminConnectorRows = [];
let chatPeek = null;

const CONNECTORS = [
  { id: "chat", label: "chat" },
  { id: "jira", label: "jira" },
  { id: "confluence", label: "confluence" },
];

let finderSource = "all";
let finderCatalog = { chat: [], jira: [], confluence: [] };
let finderPick = 0;
let searchTimer = 0;
let searchGen = 0;
let lastSearched = null;

function emptyCatalog() {
  return { chat: [], jira: [], confluence: [] };
}

let board = null;
let selectedId = null;
let selectedEdgeId = null;
let linkingFrom = null;
let cameraTimer = 0;
let adminTimer = 0;
let pan = null;
let drag = null;
let cursorWorld = null;
const ropes = new Map();
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function activeWalk() {
  return board.walks.find((w) => w.id === board.active_walk_id) || board.walks[0];
}

function stepOf(nodeId) {
  const ids = activeWalk()?.node_ids || [];
  const index = ids.indexOf(nodeId);
  return index >= 0 ? index + 1 : null;
}

function applyCamera() {
  const { x, y, zoom } = board.camera;
  world.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
}

function screenToWorld(clientX, clientY) {
  const rect = viewport.getBoundingClientRect();
  return {
    x: (clientX - rect.left - board.camera.x) / board.camera.zoom,
    y: (clientY - rect.top - board.camera.y) / board.camera.zoom,
  };
}

function anchors(edge) {
  const from = board.nodes.find((n) => n.id === edge.from_id);
  const to = board.nodes.find((n) => n.id === edge.to_id);
  if (!from || !to) return null;
  return {
    x1: from.x + 260,
    y1: from.y + 52,
    x2: to.x,
    y2: to.y + 52,
  };
}

const ROPE_SEGS = 14;
const ROPE_GRAVITY = 0.58;
const ROPE_DAMP = 0.985;

function restLength(a) {
  const dx = Math.abs(a.x2 - a.x1);
  const dy = Math.abs(a.y2 - a.y1);
  const dist = Math.hypot(dx, dy);
  const extra = 32 + dx * 0.4 + dy * 0.1;
  return (dist + extra) / ROPE_SEGS;
}

function sagFor(a) {
  const dx = Math.abs(a.x2 - a.x1);
  return 28 + dx * 0.22;
}

function seedRope(a) {
  const sag = sagFor(a);
  const pts = [];
  for (let i = 0; i <= ROPE_SEGS; i += 1) {
    const t = i / ROPE_SEGS;
    pts.push({
      x: a.x1 + (a.x2 - a.x1) * t,
      y: a.y1 + (a.y2 - a.y1) * t + Math.sin(t * Math.PI) * sag,
    });
  }
  return { pts, prev: pts.map((p) => ({ x: p.x, y: p.y })) };
}

function hangPath(a) {
  const sag = sagFor(a);
  return `M ${a.x1} ${a.y1} Q ${(a.x1 + a.x2) / 2} ${(a.y1 + a.y2) / 2 + sag} ${a.x2} ${a.y2}`;
}

function ropePath(pts) {
  if (pts.length < 2) return "";
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i += 1) {
    const mx = (pts[i].x + pts[i + 1].x) / 2;
    const my = (pts[i].y + pts[i + 1].y) / 2;
    d += ` Q ${pts[i].x} ${pts[i].y} ${mx} ${my}`;
  }
  const last = pts[pts.length - 1];
  d += ` T ${last.x} ${last.y}`;
  return d;
}

function pinEnds(rope, a) {
  const n = rope.pts.length - 1;
  rope.pts[0].x = a.x1;
  rope.pts[0].y = a.y1;
  rope.prev[0].x = a.x1;
  rope.prev[0].y = a.y1;
  rope.pts[n].x = a.x2;
  rope.pts[n].y = a.y2;
  rope.prev[n].x = a.x2;
  rope.prev[n].y = a.y2;
}

function stepRope(rope, a) {
  pinEnds(rope, a);
  if (reduceMotion.matches) {
    const seeded = seedRope(a);
    rope.pts = seeded.pts;
    rope.prev = seeded.prev;
    return;
  }
  const rest = restLength(a);
  for (let i = 1; i < rope.pts.length - 1; i += 1) {
    const p = rope.pts[i];
    const q = rope.prev[i];
    const vx = (p.x - q.x) * ROPE_DAMP;
    const vy = (p.y - q.y) * ROPE_DAMP;
    q.x = p.x;
    q.y = p.y;
    p.x += vx;
    p.y += vy + ROPE_GRAVITY;
  }
  for (let iter = 0; iter < 16; iter += 1) {
    for (let i = 0; i < rope.pts.length - 1; i += 1) {
      const aPt = rope.pts[i];
      const bPt = rope.pts[i + 1];
      const dx = bPt.x - aPt.x;
      const dy = bPt.y - aPt.y;
      const d = Math.hypot(dx, dy) || 1;
      const corr = (d - rest) / d;
      if (i === 0) {
        bPt.x -= dx * corr;
        bPt.y -= dy * corr;
      } else if (i === rope.pts.length - 2) {
        aPt.x += dx * corr;
        aPt.y += dy * corr;
      } else {
        aPt.x += dx * corr * 0.5;
        aPt.y += dy * corr * 0.5;
        bPt.x -= dx * corr * 0.5;
        bPt.y -= dy * corr * 0.5;
      }
    }
    pinEnds(rope, a);
  }
}

function lowestPoint(pts) {
  return pts.reduce((low, p) => (p.y > low.y ? p : low), pts[0]);
}

function stepRopes() {
  if (!board) return;
  const seen = new Set();
  for (const edge of board.edges) {
    const a = anchors(edge);
    if (!a) continue;
    seen.add(edge.id);
    let rope = ropes.get(edge.id);
    if (!rope || rope.pts.length !== ROPE_SEGS + 1) {
      rope = seedRope(a);
      ropes.set(edge.id, rope);
    }
    stepRope(rope, a);
  }
  for (const id of [...ropes.keys()]) {
    if (!seen.has(id)) ropes.delete(id);
  }
}

function drawRopes() {
  if (!board) return;
  const walkId = board.active_walk_id;
  const parts = [];
  for (const edge of board.edges) {
    const a = anchors(edge);
    if (!a) continue;
    const rope = ropes.get(edge.id);
    const d = rope ? ropePath(rope.pts) : hangPath(a);
    const active = edge.walk_id === walkId;
    const selected = edge.id === selectedEdgeId;
    const color = selected ? "#e07a3a" : active ? "#c45c26" : "#8a8680";
    const width = selected ? 3.6 : active ? 2.8 : 2.2;
    parts.push(
      `<path class="rope-hit" data-edge="${edge.id}" d="${d}" fill="none" stroke="transparent" stroke-width="18" stroke-linecap="round"/>`,
      `<path class="rope" data-edge="${edge.id}" d="${d}" fill="none" stroke="${color}" stroke-width="${width}" stroke-dasharray="11 9" stroke-linecap="round" stroke-linejoin="round"/>`,
    );
    if (selected && rope) {
      const dip = lowestPoint(rope.pts);
      parts.push(
        `<g class="rope-cut" data-edge="${edge.id}" transform="translate(${dip.x} ${dip.y + 18})">
            <circle r="11" fill="#faf9f6" stroke="#c45c26" stroke-width="1.6"/>
            <text text-anchor="middle" dy="4" fill="#c45c26" font-size="14" font-family="IBM Plex Mono, monospace">×</text>
          </g>`,
      );
    }
  }
  if (linkingFrom && cursorWorld) {
    const from = board.nodes.find((n) => n.id === linkingFrom);
    if (from) {
      const a = {
        x1: from.x + 260,
        y1: from.y + 52,
        x2: cursorWorld.x,
        y2: cursorWorld.y,
      };
      parts.push(
        `<path d="${hangPath(a)}" fill="none" stroke="#3b82c4" stroke-width="2.6" stroke-dasharray="11 9" stroke-linecap="round"/>`,
      );
    }
  }
  edgesEl.innerHTML = parts.join("");
}

function tick() {
  stepRopes();
  drawRopes();
  requestAnimationFrame(tick);
}

function renderNodes() {
  nodesEl.innerHTML = "";
  for (const node of board.nodes) {
    const el = document.createElement("article");
    el.className = `node ${node.source}${node.id === selectedId ? " selected" : ""}${
      node.unread ? " unread" : ""
    }`;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
    el.dataset.id = node.id;
    const step = stepOf(node.id);
    el.innerHTML = `
      <header class="node-head">
        ${step ? `<span class="step">${step}</span>` : ""}
        <span class="kind">${node.source}</span>
      </header>
      <h3></h3>
      <p></p>
      ${node.unread ? `<span class="node-badge" aria-label="Novidade"></span>` : ""}
      <button type="button" class="drop-card" aria-label="Tirar do canvas">×</button>
      <button type="button" class="port in" aria-hidden="true"></button>
      <button type="button" class="port out" aria-label="Arrasta até outro card para ligar" title="Arrasta até outro card para ligar"></button>
    `;
    el.querySelector("h3").textContent = node.title;
    el.querySelector("p").textContent = node.excerpt.slice(0, 140);
    if (node.unread) {
      el.setAttribute("aria-label", `${node.title}, novidade`);
    }
    el.addEventListener("pointerdown", onNodeDown);
    el.querySelector(".drop-card").addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
    el.querySelector(".drop-card").addEventListener("click", (event) => {
      event.stopPropagation();
      unpinNode(node.id);
    });
    el.querySelector(".port.out").addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      event.preventDefault();
      linkingFrom = node.id;
      event.currentTarget.setPointerCapture(event.pointerId);
    });
    el.addEventListener("dblclick", () => openInspector(node));
    nodesEl.appendChild(el);
  }
}

function renderStrip() {
  const walk = activeWalk();
  strip.innerHTML = "";
  if (!walk) return;
  walk.node_ids.forEach((id, index) => {
    const node = board.nodes.find((n) => n.id === id);
    if (!node) return;
    if (index) {
      const sep = document.createElement("span");
      sep.textContent = "→";
      sep.style.color = "#c45c26";
      strip.appendChild(sep);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = `${index + 1}  ${node.title}`;
    if (id === selectedId) btn.className = "now";
    btn.addEventListener("click", () => focusNode(id));
    strip.appendChild(btn);
  });
}

function renderWalks() {
  walkList.innerHTML = "";
  for (const walk of board.walks) {
    const li = document.createElement("li");
    if (walk.id === board.active_walk_id) li.className = "active";
    li.innerHTML = `<span></span><small>${walk.node_ids.length}</small><button type="button" class="drop-walk" aria-label="Apagar ${walk.name}">×</button>`;
    li.querySelector("span").textContent = walk.name;
    li.addEventListener("click", () => {
      post("/api/board/walk/active", { id: walk.id }).then(setBoard);
    });
    li.querySelector(".drop-walk").addEventListener("click", (event) => {
      event.stopPropagation();
      post("/api/board/walk/delete", { id: walk.id }).then(setBoard);
    });
    walkList.appendChild(li);
  }
}

function render() {
  applyCamera();
  renderNodes();
  renderStrip();
  renderWalks();
  drawRopes();
}

function setBoard(next) {
  board = next;
  render();
}

function focusNode(id) {
  selectedId = id;
  const node = board.nodes.find((n) => n.id === id);
  if (node) {
    board.camera.x = viewport.clientWidth / 2 - (node.x + 130) * board.camera.zoom;
    board.camera.y = viewport.clientHeight / 2 - (node.y + 50) * board.camera.zoom;
    scheduleCamera();
  }
  render();
}

async function openInspector(node) {
  selectedId = node.id;
  if (node.unread) {
    node.unread = false;
    renderNodes();
  }
  post("/api/board/seen", { id: node.id }).then((next) => {
    if (next?.nodes) setBoard(next);
  });
  inspector.hidden = false;
  document.getElementById("insp-source").textContent = node.source;
  document.getElementById("insp-title").textContent = node.title;
  document.getElementById("insp-excerpt").textContent = node.excerpt;
  const link = document.getElementById("insp-url");
  if (node.url) {
    link.hidden = false;
    link.href = node.url;
  } else {
    link.hidden = true;
  }
  const body = document.getElementById("insp-body");
  body.innerHTML = "";
  const kind = node.source === "jira" ? "issue" : node.source === "confluence" ? "page" : "thread";
  const ref = node.source === "jira" ? node.parent_id || node.native_id || node.ref : node.ref;
  const data = await fetch(`/api/expand?kind=${kind}&ref=${encodeURIComponent(ref)}`).then((r) => r.json());
  for (const item of data.items || []) {
    const p = document.createElement("p");
    p.textContent = item.text;
    body.appendChild(p);
  }
  render();
}

function scheduleCamera() {
  applyCamera();
  clearTimeout(cameraTimer);
  cameraTimer = setTimeout(() => {
    post("/api/board/camera", board.camera);
  }, 400);
}

async function post(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json();
}

function onNodeDown(event) {
  if (event.target.closest(".port, .drop-card")) return;
  event.preventDefault();
  const el = event.currentTarget;
  const id = el.dataset.id;
  selectedId = id;
  selectedEdgeId = null;
  for (const nodeEl of nodesEl.children) {
    nodeEl.classList.toggle("selected", nodeEl.dataset.id === id);
  }
  const node = board.nodes.find((n) => n.id === id);
  const start = {
    x: event.clientX,
    y: event.clientY,
    nx: node.x,
    ny: node.y,
  };
  let done = false;
  const apply = (ev) => {
    node.x = start.nx + (ev.clientX - start.x) / board.camera.zoom;
    node.y = start.ny + (ev.clientY - start.y) / board.camera.zoom;
    el.style.left = `${node.x}px`;
    el.style.top = `${node.y}px`;
  };
  const up = (ev) => {
    if (done) return;
    done = true;
    document.removeEventListener("pointermove", apply);
    document.removeEventListener("mousemove", apply);
    document.removeEventListener("pointerup", up);
    document.removeEventListener("mouseup", up);
    apply(ev);
    drag = null;
    post("/api/board/node", { id, x: node.x, y: node.y }).then(setBoard);
  };
  document.addEventListener("pointermove", apply);
  document.addEventListener("mousemove", apply);
  document.addEventListener("pointerup", up);
  document.addEventListener("mouseup", up);
}

viewport.addEventListener("pointerdown", (event) => {
  if (event.target.closest(".node") || event.target.closest("[data-edge]")) return;
  selectedEdgeId = null;
  pan = {
    x: event.clientX - board.camera.x,
    y: event.clientY - board.camera.y,
  };
  viewport.classList.add("panning");
  viewport.setPointerCapture(event.pointerId);
});

viewport.addEventListener("pointermove", (event) => {
  if (!pan) return;
  board.camera.x = event.clientX - pan.x;
  board.camera.y = event.clientY - pan.y;
  applyCamera();
});

document.addEventListener("pointermove", (event) => {
  if (!board) return;
  cursorWorld = screenToWorld(event.clientX, event.clientY);
});

viewport.addEventListener("pointerup", () => {
  if (!pan) return;
  pan = null;
  viewport.classList.remove("panning");
  scheduleCamera();
});

document.addEventListener("pointerup", (event) => {
  if (!linkingFrom) return;
  const target = document.elementFromPoint(event.clientX, event.clientY);
  const nodeEl = target?.closest?.(".node");
  const toId = nodeEl?.dataset.id;
  if (toId && toId !== linkingFrom) {
    post("/api/board/link", { from_id: linkingFrom, to_id: toId }).then(setBoard);
  }
  linkingFrom = null;
});

viewport.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const point = screenToWorld(event.clientX, event.clientY);
    const factor = event.deltaY < 0 ? 1.08 : 0.92;
    const next = Math.min(2.4, Math.max(0.28, board.camera.zoom * factor));
    board.camera.zoom = next;
    const rect = viewport.getBoundingClientRect();
    board.camera.x = event.clientX - rect.left - point.x * next;
    board.camera.y = event.clientY - rect.top - point.y * next;
    scheduleCamera();
  },
  { passive: false },
);

function dropPoint() {
  const rect = viewport.getBoundingClientRect();
  const origin = screenToWorld(rect.left + rect.width * 0.38, rect.top + rect.height * 0.38);
  const n = board.nodes.length;
  return {
    x: origin.x + (n % 5) * 32,
    y: origin.y + (n % 5) * 24,
  };
}

function pinHit(hit) {
  const ids = new Set(board.nodes.map((n) => n.id));
  const at = dropPoint();
  post("/api/board/pin", { hit, x: at.x, y: at.y }).then((next) => {
    setBoard(next);
    const added = next.nodes.find((n) => !ids.has(n.id));
    selectedId = added?.id || selectedId;
    if (selectedId) focusNode(selectedId);
  });
}

function firstSentence(text, max = 88) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (!compact) return "";
  if (compact.length <= max) return compact;
  const window = compact.slice(0, max + 1);
  const punct = [...window.matchAll(/[.!?](?=\s|$)/g)];
  if (punct.length && punct[0].index + 1 >= 24) {
    return compact.slice(0, punct[0].index + 1).trim();
  }
  const space = window.lastIndexOf(" ");
  return (space >= 24 ? compact.slice(0, space) : compact.slice(0, max)).trim();
}

function hitKind(hit) {
  const bits = [hit.source];
  if (hit.channel_or_space) bits.push(hit.channel_or_space);
  if (
    hit.source === "jira" &&
    hit.native_id &&
    hit.native_id !== hit.title &&
    !bits.includes(hit.native_id)
  ) {
    bits.push(hit.native_id);
  }
  return bits.filter(Boolean).join(" · ");
}

function hitTitle(hit) {
  const title = (hit.title || "").trim();
  const channel = (hit.channel_or_space || "").trim();
  if (title && title !== channel) return title;
  const text = (hit.text || "").trim();
  if (text) return firstSentence(text);
  return title || hit.native_id || "sem título";
}

function hitPreview(hit, limit) {
  const storedTitle = (hit.title || "").trim();
  const channel = (hit.channel_or_space || "").trim();
  let text = (hit.text || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  if (storedTitle && storedTitle !== channel && text.startsWith(storedTitle)) {
    text = text.slice(storedTitle.length).replace(/^[\s:.\-–—/]+/, "").trim();
  }
  if (!text) return "";
  const shown = hitTitle(hit);
  if (text === shown) return "";
  if (text.startsWith(shown)) {
    const rest = text.slice(shown.length).replace(/^[\s:.\-–—/]+/, "").trim();
    if (rest) text = rest;
  }
  return text.length > limit ? `${text.slice(0, limit).trim()}…` : text;
}

function renderHit(hit, { size = "compact", onPick } = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `${size === "sheet" ? "finder-hit" : "hit"} ${hit.source || ""}`;
  const title = hitTitle(hit);
  btn.setAttribute("aria-label", `Soltar ${title} no canvas`);
  const kind = document.createElement("small");
  kind.textContent = hitKind(hit);
  const heading = document.createElement("b");
  heading.textContent = title;
  const excerpt = document.createElement("span");
  if (size === "sheet") excerpt.className = "preview";
  excerpt.textContent = hitPreview(hit, size === "sheet" ? 280 : 110);
  btn.append(kind, heading);
  if (excerpt.textContent) btn.append(excerpt);
  btn.addEventListener("click", () => {
    pinHit(hit);
    searchStatus.textContent = "solto — arrasta o cobre para ligar";
    onPick?.(hit);
  });
  return btn;
}

function renderFeed(el, hits, empty, onPick, size = "compact") {
  el.innerHTML = "";
  if (!hits.length) {
    const note = document.createElement("p");
    note.className = "status";
    note.textContent = empty;
    el.appendChild(note);
    return;
  }
  for (const hit of hits) {
    el.appendChild(renderHit(hit, { size, onPick }));
  }
}

function finderHitButtons() {
  return [...finderHits.querySelectorAll(".finder-hit")];
}

function selectFinderHit(index) {
  const buttons = finderHitButtons();
  if (!buttons.length) {
    finderPick = 0;
    return;
  }
  finderPick = ((index % buttons.length) + buttons.length) % buttons.length;
  buttons.forEach((btn, i) => {
    btn.classList.toggle("on", i === finderPick);
    btn.setAttribute("aria-selected", i === finderPick ? "true" : "false");
  });
  buttons[finderPick].scrollIntoView({ block: "nearest" });
}

function closeFinder() {
  finder.hidden = true;
  document.body.classList.remove("finder-open");
  clearTimeout(searchTimer);
}

function openFinder() {
  inspector.hidden = true;
  finder.hidden = false;
  document.body.classList.add("finder-open");
}

function finderChordLabel() {
  return /Mac|iPhone|iPad/.test(navigator.userAgent) ? "⌘K" : "Ctrl+K";
}

function summonFinder() {
  if (!finder.hidden) {
    queryField.focus();
    return;
  }
  if (admin && !admin.hidden) closeAdmin();
  openFinder();
  queryField.focus();
  const q = queryField.value.trim();
  if (q) runSearch();
  else browseIndex();
}

function catalogCount(source) {
  if (source === "all") {
    return CONNECTORS.reduce((sum, conn) => sum + finderCatalog[conn.id].length, 0);
  }
  return finderCatalog[source]?.length || 0;
}

function renderFinderFilters() {
  finderFilters.innerHTML = "";
  const options = [{ id: "all", label: "Todos" }, ...CONNECTORS];
  for (const option of options) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.source = option.id;
    btn.className = option.id;
    if (option.id === finderSource) btn.classList.add("on");
    const count = catalogCount(option.id);
    btn.textContent = count ? `${option.label} ${count}` : option.label;
    btn.setAttribute("aria-pressed", option.id === finderSource ? "true" : "false");
    btn.addEventListener("click", () => {
      if (finderSource === option.id) return;
      finderSource = option.id;
      renderFinderFilters();
      renderCatalog();
    });
    finderFilters.appendChild(btn);
  }
}

function renderCatalog() {
  finderHits.innerHTML = "";
  const total = catalogCount("all");
  if (!total) {
    renderFeed(finderHits, [], "nada neste conector ainda", undefined, "sheet");
    finderStatus.textContent = finderSource === "all" ? "nada no índice ainda" : `nada em ${finderSource}`;
    return;
  }
  if (finderSource !== "all") {
    const hits = finderCatalog[finderSource] || [];
    finderStatus.textContent = hits.length
      ? `${hits.length} em ${finderSource} — clique ou Enter para soltar`
      : `nada em ${finderSource}`;
    renderFeed(finderHits, hits, `nada em ${finderSource}`, closeFinder, "sheet");
    selectFinderHit(0);
    return;
  }
  finderStatus.textContent = `${total} no índice — filtre por conector`;
  for (const conn of CONNECTORS) {
    const hits = finderCatalog[conn.id];
    if (!hits.length) continue;
    const heading = document.createElement("p");
    heading.className = `finder-group ${conn.id}`;
    heading.textContent = conn.label;
    finderHits.appendChild(heading);
    for (const hit of hits) {
      finderHits.appendChild(renderHit(hit, { size: "sheet", onPick: closeFinder }));
    }
  }
  selectFinderHit(0);
}

async function loadCatalog() {
  const catalog = emptyCatalog();
  const results = await Promise.all(
    CONNECTORS.map((conn) =>
      fetch(`/api/recent?source=${encodeURIComponent(conn.id)}&k=32`).then((r) =>
        r.json(),
      ),
    ),
  );
  let ok = false;
  CONNECTORS.forEach((conn, index) => {
    const data = results[index];
    if (data?.ok) {
      ok = true;
      catalog[conn.id] = data.hits || [];
    }
  });
  return { ok, catalog };
}

async function browseIndex() {
  const gen = ++searchGen;
  lastSearched = "";
  finderSource = "all";
  finderCatalog = emptyCatalog();
  openFinder();
  finderFilters.hidden = false;
  renderFinderFilters();
  searchStatus.textContent = "olhando o índice…";
  finderHits.innerHTML = "";
  const { ok, catalog } = await loadCatalog();
  if (gen !== searchGen) return;
  finderCatalog = catalog;
  renderFinderFilters();
  if (!ok) {
    finderFilters.hidden = true;
    searchStatus.textContent = "índice fora — Soltar nota põe no canvas";
    finderHits.innerHTML = "";
    return;
  }
  searchStatus.textContent = catalogCount("all")
    ? `${catalogCount("all")} no índice`
    : "nada no índice ainda";
  renderCatalog();
}

async function loadRecents() {
  recentsLabel.hidden = false;
  recentsEl.hidden = false;
  const data = await fetch("/api/recent").then((r) => r.json());
  if (!data.ok) {
    renderFeed(recentsEl, [], "nada no índice ainda — Buscar solta uma nota");
    return;
  }
  renderFeed(recentsEl, data.hits, "nada recente no índice ainda");
}

function dropTyped() {
  const text = queryField.value.trim();
  if (!text) return;
  const ref = `local:${Date.now()}`;
  pinHit({
    source: "chat",
    title: text,
    text,
    native_id: ref,
    id: ref,
  });
  queryField.value = "";
  lastSearched = null;
  closeFinder();
}

async function runSearch() {
  const q = queryField.value.trim();
  if (!q) {
    await browseIndex();
    return;
  }
  const gen = ++searchGen;
  lastSearched = q;
  finderFilters.hidden = true;
  openFinder();
  searchStatus.textContent = "buscando…";
  finderHits.innerHTML = "";
  const data = await fetch(`/api/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
  if (gen !== searchGen) return;
  if (!data.ok) {
    searchStatus.textContent = "índice fora — Soltar nota põe no canvas";
    finderHits.innerHTML = "";
    return;
  }
  const msg = data.hits.length
    ? `${data.hits.length} achados — Enter solta o marcado`
    : "nada encontrado — Enter solta uma nota";
  searchStatus.textContent = msg;
  renderFeed(finderHits, data.hits, "nada encontrado — Enter solta uma nota", closeFinder, "sheet");
  selectFinderHit(0);
}

async function commitFinder() {
  const q = queryField.value.trim();
  if (q !== lastSearched) {
    await runSearch();
  }
  const buttons = finderHitButtons();
  if (buttons.length) {
    (buttons[finderPick] || buttons[0]).click();
    return;
  }
  dropTyped();
}

const RAIL_KEY = "ragtone.rail-collapsed";

function setRailCollapsed(collapsed) {
  document.body.classList.toggle("rail-collapsed", collapsed);
  rail.inert = collapsed;
  hideRail.setAttribute("aria-expanded", collapsed ? "false" : "true");
  showRail.setAttribute("aria-expanded", collapsed ? "false" : "true");
  try {
    localStorage.setItem(RAIL_KEY, collapsed ? "1" : "0");
  } catch (err) {}
}

hideRail.addEventListener("click", () => {
  setRailCollapsed(true);
  showRail.focus();
});
showRail.addEventListener("click", () => {
  setRailCollapsed(false);
  hideRail.focus();
});
setRailCollapsed(document.body.classList.contains("rail-collapsed"));

document.getElementById("open-finder").addEventListener("click", summonFinder);
document.getElementById("open-finder-float").addEventListener("click", summonFinder);
document.getElementById("drop").addEventListener("click", dropTyped);
document.getElementById("close-finder").addEventListener("click", closeFinder);
finder.addEventListener("click", (event) => {
  if (event.target === finder) closeFinder();
});

queryField.addEventListener("input", () => {
  if (finder.hidden) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 280);
});

queryField.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    selectFinderHit(finderPick + 1);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    selectFinderHit(finderPick - 1);
    return;
  }
  if (event.key !== "Enter") return;
  event.preventDefault();
  commitFinder();
});

for (const el of document.querySelectorAll("[data-finder-chord]")) {
  el.textContent = finderChordLabel();
}

function unpinNode(id) {
  if (selectedId === id) selectedId = null;
  inspector.hidden = true;
  post("/api/board/unpin", { id }).then(setBoard);
}

function unlinkEdge(id) {
  selectedEdgeId = null;
  post("/api/board/unlink", { id }).then(setBoard);
}

edgesEl.addEventListener("pointerdown", (event) => {
  const hit = event.target.closest("[data-edge]");
  if (!hit) return;
  event.stopPropagation();
  event.preventDefault();
  const id = hit.dataset.edge;
  if (event.target.closest(".rope-cut")) {
    unlinkEdge(id);
    return;
  }
  selectedEdgeId = id;
  selectedId = null;
  for (const nodeEl of nodesEl.children) {
    nodeEl.classList.remove("selected");
  }
});

edgesEl.addEventListener("dblclick", (event) => {
  const hit = event.target.closest("[data-edge]");
  if (!hit) return;
  event.stopPropagation();
  unlinkEdge(hit.dataset.edge);
});

document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (!finder.hidden) closeFinder();
    else summonFinder();
    return;
  }
  if (event.key === "Escape" && finder && !finder.hidden) {
    closeFinder();
    event.preventDefault();
    return;
  }
  if (event.target.closest("input, textarea, select")) return;
  if (event.key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    summonFinder();
    return;
  }
  if (event.key === "Escape" && admin && !admin.hidden) {
    closeAdmin();
    event.preventDefault();
    return;
  }
  if (admin && !admin.hidden) return;
  if (finder && !finder.hidden) return;
  if (event.key !== "Backspace" && event.key !== "Delete") return;
  event.preventDefault();
  if (selectedEdgeId) {
    unlinkEdge(selectedEdgeId);
    return;
  }
  if (selectedId) unpinNode(selectedId);
});

document.getElementById("new-walk").addEventListener("click", () => {
  const name = prompt("Nome da trilha", `Trilha ${board.walks.length + 1}`);
  if (!name) return;
  post("/api/board/walk", { name }).then(setBoard);
});

document.getElementById("close-inspector").addEventListener("click", () => {
  inspector.hidden = true;
});

const GAP_COPY = {
  es_down: () => "Elasticsearch fora do ar — o índice não recebe nada",
  disabled: (name) => `${name} desligado em ragtone.yaml`,
  mcp_missing: (name) => `${name} precisa do MCP configurado em foundation_mcps`,
  chat_no_channels: () => "chat sem canais para olhar",
  jira_no_boards: () => "jira sem projetos/boards para olhar",
  confluence_no_docs: () => "confluence sem espaços ou páginas para olhar",
  never_ran: (name) => `${name} nunca rodou — force uma atualização`,
  zero_chunks: (name) => `${name} não tem chunks no índice`,
};

const WATCH_UI = {
  chat: { title: "Canais do Slack", placeholder: "cola o link do canal" },
  jira: { title: "Boards / projetos", placeholder: "ABC" },
  confluence: { title: "Docs / espaços", placeholder: "ENG ou 123456" },
};

const CHANNEL_RANGES = [
  { days: 7, label: "7 dias" },
  { days: 30, label: "30 dias" },
  { days: 90, label: "90 dias" },
  { days: 180, label: "6 meses" },
  { days: 365, label: "1 ano" },
  { days: 0, label: "tudo" },
];

function asTargets(row) {
  const raw = row.targets || row.watching || [];
  return raw.map((item) => (typeof item === "string" ? { id: item } : item));
}

function rangeLabel(days) {
  if (days === 0) return "tudo";
  if (days == null) return "";
  const found = CHANNEL_RANGES.find((item) => item.days === days);
  return found ? found.label : `${days}d`;
}

function closeAdmin() {
  admin.hidden = true;
  clearTimeout(adminTimer);
  adminRecentState.clear();
  adminRecent.innerHTML = "";
  chatPeek = null;
}

function jobLine(job) {
  if (!job || job.status === "idle") return "";
  if (job.status === "running") {
    const counts = jobCounts(job);
    return `Atualizando ${job.connector}…${counts}`;
  }
  if (job.status === "ok") {
    const extra = job.backfill ? " (desde o começo)" : "";
    const dead = job.dlq ? ` · ${job.dlq} na DLQ` : "";
    return `Pronto — ${job.chunks} chunks em ${job.connector}${extra}${dead}`;
  }
  return job.error || "atualização falhou";
}

function jobCounts(job) {
  if (job.total != null) return ` ${job.indexed}/${job.total}`;
  if (job.discovered) return ` ${job.indexed}/${job.discovered}`;
  return "";
}

function renderQueue(job) {
  const running = job?.status === "running";
  const percent = job?.percent;
  if (adminProgress) {
    adminProgress.hidden = !running && percent == null;
  }
  if (adminProgressFill) {
    adminProgressFill.style.width = `${percent == null ? 0 : percent}%`;
  }
  if (adminProgressLabel) {
    if (percent != null) {
      adminProgressLabel.textContent = `${percent}%${jobCounts(job)}`;
    } else if (running) {
      adminProgressLabel.textContent = job.discovered
        ? `${job.indexed} indexados · ${job.discovered} descobertos`
        : "paginando…";
    } else {
      adminProgressLabel.textContent = "";
    }
  }
  renderDlq(job);
}

function renderDlq(job) {
  if (!adminDlq) return;
  adminDlq.innerHTML = "";
  const letters = job?.dead_letters || [];
  if (!letters.length) return;
  const list = document.createElement("ul");
  list.className = "admin-dlq";
  for (const item of letters) {
    const row = document.createElement("li");
    const copy = document.createElement("p");
    const title = document.createElement("b");
    title.textContent = `${item.connector} · ${item.ref}`;
    const err = document.createElement("span");
    err.textContent = item.error || "falhou";
    copy.append(title, err);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Tentar de novo";
    btn.addEventListener("click", () => retryDlq({ id: item.id }));
    row.append(copy, btn);
    list.appendChild(row);
  }
  adminDlq.appendChild(list);
  if (letters.length > 1) {
    const all = document.createElement("button");
    all.type = "button";
    all.className = "admin-dlq-all";
    all.textContent = "Tentar todas de novo";
    all.addEventListener("click", () => retryDlq({ all: true }));
    adminDlq.appendChild(all);
  }
}

async function retryDlq(payload) {
  const res = await fetch("/api/admin/dlq/retry", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    adminJob.textContent = data.error || "falhou";
    return;
  }
  renderAdmin(data);
}

function indexLine(index) {
  if (!index?.ok) return "Elasticsearch fora";
  const parts = Object.entries(index.by_source || {}).map(
    ([name, count]) => `${name} ${count}`,
  );
  const detail = parts.length ? ` · ${parts.join(" · ")}` : "";
  return `Elasticsearch ok · ${index.total} chunks${detail}`;
}

function connectorMeta(row) {
  const bits = [
    row.enabled ? "ligado" : "desligado",
    row.mcp_configured ? `MCP ${row.mcp}` : `MCP ${row.mcp} faltando`,
    `${row.chunks} chunks`,
    row.checkpoint ? `checkpoint ${row.checkpoint}` : "nunca rodou",
  ];
  return bits.join(" · ");
}

function watchEditor(row) {
  const spec = WATCH_UI[row.name] || { title: "Onde olhar", placeholder: "" };
  const wrap = document.createElement("form");
  wrap.className = "admin-watch";
  wrap.dataset.source = row.name;
  const label = document.createElement("p");
  label.className = "search-label";
  label.textContent = spec.title;
  const chips = document.createElement("ul");
  chips.className = "watch-chips";
  const items = asTargets(row);
  for (const item of items) {
    const li = document.createElement("li");
    const range = row.name === "chat" ? rangeLabel(item.backfill_days) : "";
    li.textContent = range ? `${item.id} · ${range}` : item.id;
    const drop = document.createElement("button");
    drop.type = "button";
    drop.setAttribute("aria-label", `Remover ${item.id}`);
    drop.textContent = "×";
    drop.addEventListener("click", () => {
      saveWatches(
        row.name,
        items.filter((value) => value.id !== item.id),
      );
    });
    li.appendChild(drop);
    chips.appendChild(li);
  }
  if (!items.length) {
    const empty = document.createElement("li");
    empty.textContent = "nada selecionado";
    empty.style.background = "transparent";
    empty.style.fontWeight = "400";
    chips.appendChild(empty);
  }
  const add = document.createElement("div");
  add.className = row.name === "chat" ? "watch-add chat" : "watch-add";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = spec.placeholder;
  input.autocomplete = "off";
  const submit = document.createElement("button");
  submit.type = "submit";
  if (row.name === "chat") {
    submit.textContent = "Olhar";
    submit.dataset.peek = "1";
    add.append(input, submit);
    const peek = document.createElement("div");
    peek.className = "watch-peek";
    peek.hidden = true;
    input.addEventListener("input", () => {
      if (chatPeek && input.value.trim() !== chatPeek.raw) {
        chatPeek = null;
        paintChatPeek(wrap);
      }
    });
    wrap.addEventListener("submit", (event) => {
      event.preventDefault();
      lookAtChat(input.value);
    });
    wrap.append(label, chips, add, peek);
    paintChatPeek(wrap);
    return wrap;
  }
  submit.textContent = "Adicionar";
  add.append(input, submit);
  wrap.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    saveWatches(row.name, items.concat({ id: value }));
  });
  wrap.append(label, chips, add);
  return wrap;
}

function paintChatPeek(wrap) {
  const panel = wrap.querySelector(".watch-peek");
  const input = wrap.querySelector(".watch-add input");
  const look = wrap.querySelector("[data-peek]");
  if (!panel) return;
  if (chatPeek?.raw && input && input.value.trim() === "") {
    input.value = chatPeek.raw;
  }
  if (look) look.disabled = Boolean(chatPeek?.loading);
  if (!chatPeek) {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  panel.hidden = false;
  panel.replaceChildren();
  const head = document.createElement("p");
  head.className = "search-label";
  if (chatPeek.loading) head.textContent = "olhando o canal…";
  else if (chatPeek.title && chatPeek.title !== chatPeek.id) {
    head.textContent = `${chatPeek.title} · ${chatPeek.id}`;
  } else {
    head.textContent = chatPeek.id ? `canal ${chatPeek.id}` : "canal";
  }
  panel.appendChild(head);
  if (chatPeek.error) {
    const note = document.createElement("p");
    note.className = "status";
    note.textContent = chatPeek.error;
    panel.appendChild(note);
  }
  if (chatPeek.messages?.length) {
    const list = document.createElement("ol");
    list.className = "watch-preview";
    for (const message of chatPeek.messages) {
      const item = document.createElement("li");
      const who = document.createElement("b");
      who.textContent = message.author || "msg";
      const body = document.createElement("span");
      body.textContent = message.text;
      item.append(who, body);
      list.appendChild(item);
    }
    panel.appendChild(list);
  }
  if (!chatPeek.loading && chatPeek.id) {
    const confirm = document.createElement("div");
    confirm.className = "watch-confirm";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Quanto tempo para trás");
    for (const option of CHANNEL_RANGES) {
      const node = document.createElement("option");
      node.value = String(option.days);
      node.textContent = option.label;
      if (option.days === (chatPeek.days ?? 90)) node.selected = true;
      select.appendChild(node);
    }
    select.addEventListener("change", () => {
      chatPeek.days = Number(select.value);
    });
    const add = document.createElement("button");
    add.type = "button";
    add.textContent = "Adicionar";
    add.addEventListener("click", () => {
      const items = asTargets(
        adminConnectorRows.find((row) => row.name === "chat") || { watching: [] },
      );
      saveWatches("chat", items.concat({
        id: chatPeek.id,
        backfill_days: Number(select.value),
      }));
    });
    confirm.append(select, add);
    panel.appendChild(confirm);
  }
}

async function lookAtChat(raw) {
  const value = String(raw || "").trim();
  if (!value) return;
  chatPeek = { raw: value, loading: true, id: null, title: null, messages: [], error: null, days: 90 };
  const wrap = document.querySelector('.admin-watch[data-source="chat"]');
  if (wrap) paintChatPeek(wrap);
  try {
    const res = await fetch("/api/admin/peek", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "chat", ref: value }),
    });
    const data = await res.json();
    if (!chatPeek || chatPeek.raw !== value) return;
    chatPeek = {
      raw: value,
      loading: false,
      id: data.id || null,
      title: data.title || data.id || null,
      messages: data.messages || [],
      error: data.error || (!res.ok ? "não deu para olhar o canal" : null),
      days: 90,
    };
  } catch {
    if (!chatPeek || chatPeek.raw !== value) return;
    chatPeek = {
      raw: value,
      loading: false,
      id: null,
      title: null,
      messages: [],
      error: "não deu para olhar o canal",
      days: 90,
    };
  }
  const next = document.querySelector('.admin-watch[data-source="chat"]');
  if (next) paintChatPeek(next);
}

function recentDrawerState(name) {
  if (!adminRecentState.has(name)) {
    adminRecentState.set(name, { open: false, loading: false, hits: null, ok: true });
  }
  return adminRecentState.get(name);
}

function makeRecentDrawer(row) {
  const card = document.createElement("article");
  card.className = "admin-recent-source";
  card.dataset.source = row.name;
  const head = document.createElement("header");
  const copy = document.createElement("div");
  const title = document.createElement("h4");
  title.textContent = row.name;
  const meta = document.createElement("p");
  meta.className = "admin-recent-meta";
  copy.append(title, meta);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.addEventListener("click", () => toggleAdminRecent(row.name));
  const hits = document.createElement("div");
  hits.className = "admin-recent-hits hits";
  hits.id = `admin-recent-${row.name}`;
  hits.hidden = true;
  head.append(copy, btn);
  card.append(head, hits);
  return card;
}

function paintRecentDrawer(row) {
  const card = adminRecent.querySelector(`[data-source="${row.name}"]`);
  if (!card) return;
  const state = recentDrawerState(row.name);
  const meta = card.querySelector(".admin-recent-meta");
  const btn = card.querySelector("header button");
  const hits = card.querySelector(".admin-recent-hits");
  meta.textContent = `${row.chunks} chunks`;
  btn.disabled = state.loading;
  btn.setAttribute("aria-expanded", state.open ? "true" : "false");
  btn.setAttribute("aria-controls", hits.id);
  if (state.loading) btn.textContent = "Abrindo…";
  else if (state.open) btn.textContent = "Fechar";
  else btn.textContent = "Abrir";
  card.classList.toggle("is-open", state.open);
  hits.hidden = !state.open;
  if (!state.open) {
    hits.innerHTML = "";
    return;
  }
  if (state.loading) {
    renderFeed(hits, [], "abrindo…");
    return;
  }
  if (!state.ok) {
    renderFeed(hits, [], "índice fora");
    return;
  }
  renderFeed(hits, state.hits || [], "nada recente neste conector", closeAdmin);
}

function renderAdminRecents(connectors) {
  adminConnectorRows = connectors;
  const names = connectors.map((row) => row.name);
  const shown = [...adminRecent.querySelectorAll("[data-source]")].map(
    (el) => el.dataset.source,
  );
  if (shown.join("\0") !== names.join("\0")) {
    adminRecent.innerHTML = "";
    for (const row of connectors) {
      adminRecent.appendChild(makeRecentDrawer(row));
    }
  }
  for (const row of connectors) {
    paintRecentDrawer(row);
  }
}

async function toggleAdminRecent(name) {
  const row = adminConnectorRows.find((item) => item.name === name) || {
    name,
    chunks: 0,
  };
  const state = recentDrawerState(name);
  if (state.open) {
    state.open = false;
    state.loading = false;
    paintRecentDrawer(row);
    return;
  }
  state.open = true;
  state.loading = true;
  paintRecentDrawer(row);
  try {
    const data = await fetch(`/api/recent?source=${encodeURIComponent(name)}`).then(
      (r) => r.json(),
    );
    if (!state.open) return;
    state.ok = Boolean(data.ok);
    state.hits = data.hits || [];
  } catch {
    if (!state.open) return;
    state.ok = false;
    state.hits = [];
  } finally {
    state.loading = false;
  }
  if (state.open) paintRecentDrawer(row);
}

function renderConnectors(data) {
  const running = data.job?.status === "running";
  const ready = (data.connectors || []).filter((row) => row.can_sync);
  syncAll.disabled = running || !ready.length;
  adminConnectors.innerHTML = "";
  for (const row of data.connectors || []) {
    const card = document.createElement("article");
    card.className = "admin-connector";
    const copy = document.createElement("div");
    const title = document.createElement("h4");
    title.textContent = row.name;
    const meta = document.createElement("p");
    meta.textContent = connectorMeta(row);
    copy.append(title, meta);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Atualizar";
    btn.disabled = running || !row.can_sync;
    btn.addEventListener("click", () => requestSync(row.name));
    card.append(copy, btn, watchEditor(row));
    adminConnectors.appendChild(card);
  }
}

let lastJobStatus = "";

function renderAdmin(data, { forceConnectors = false } = {}) {
  const jobStatus = data.job?.status || "idle";
  if (lastJobStatus === "running" && jobStatus !== "running") {
    refreshUnreads();
    loadRecents();
  }
  lastJobStatus = jobStatus;
  adminJob.textContent = jobLine(data.job);
  renderQueue(data.job);
  adminIndex.textContent = indexLine(data.index);
  adminGaps.innerHTML = "";
  const gaps = data.gaps || [];
  if (!gaps.length) {
    const item = document.createElement("li");
    item.textContent = "Nada óbvio faltando.";
    adminGaps.appendChild(item);
  } else {
    for (const gap of gaps) {
      const item = document.createElement("li");
      const copy = GAP_COPY[gap.code];
      item.textContent = copy ? copy(gap.connector) : gap.code;
      adminGaps.appendChild(item);
    }
  }
  const running = data.job?.status === "running";
  const focused = document.activeElement;
  const typing = Boolean(focused && focused.closest(".admin-watch"));
  if (!typing || forceConnectors) {
    renderConnectors(data);
  } else {
    syncAll.disabled = running || !(data.connectors || []).some((row) => row.can_sync);
  }
  renderAdminRecents(data.connectors || []);
  if (running) {
    clearTimeout(adminTimer);
    adminTimer = setTimeout(refreshAdmin, 2000);
  }
}

async function refreshAdmin() {
  if (admin.hidden) return;
  const data = await fetch("/api/admin").then((r) => r.json());
  renderAdmin(data);
}

async function requestSync(name) {
  const backfill = document.getElementById("admin-backfill").checked;
  const res = await fetch("/api/admin/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, backfill }),
  });
  const data = await res.json();
  if (!res.ok) {
    adminJob.textContent = data.error || "falhou";
    return;
  }
  renderAdmin(data);
}

async function saveWatches(name, items) {
  const res = await fetch("/api/admin/watches", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, items }),
  });
  const data = await res.json();
  if (!res.ok) {
    adminJob.textContent = data.error || "não deu para salvar";
    return;
  }
  if (name === "chat") chatPeek = null;
  renderAdmin(data, { forceConnectors: true });
}

function openAdmin() {
  inspector.hidden = true;
  closeFinder();
  admin.hidden = false;
  refreshAdmin();
}

document.getElementById("open-admin").addEventListener("click", openAdmin);
document.getElementById("close-admin").addEventListener("click", closeAdmin);
syncAll.addEventListener("click", () => requestSync("all"));

function paintUnreads(nodes) {
  if (!board || !nodes) return;
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let changed = false;
  for (const node of board.nodes) {
    const fresh = byId.get(node.id);
    if (!fresh || Boolean(fresh.unread) === Boolean(node.unread)) continue;
    node.unread = Boolean(fresh.unread);
    changed = true;
  }
  if (changed) renderNodes();
}

async function refreshUnreads() {
  if (!board || drag || pan || linkingFrom) return;
  const next = await fetch("/api/board").then((r) => r.json());
  paintUnreads(next.nodes);
}

fetch("/api/board")
  .then((r) => r.json())
  .then((next) => {
    setBoard(next);
    requestAnimationFrame(tick);
    loadRecents();
  });

setInterval(() => {
  if (document.hidden || !board) return;
  refreshUnreads();
}, 20000);
