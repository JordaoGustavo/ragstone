const viewport = document.getElementById("viewport");
const world = document.getElementById("world");
const edgesEl = document.getElementById("edges");
const nodesEl = document.getElementById("nodes");
const hitsEl = document.getElementById("hits");
const recentsEl = document.getElementById("recents");
const recentsLabel = document.getElementById("recents-label");
const strip = document.getElementById("strip");
const walkList = document.getElementById("walk-list");
const inspector = document.getElementById("inspector");
const searchStatus = document.getElementById("search-status");
const admin = document.getElementById("admin");
const adminJob = document.getElementById("admin-job");
const adminIndex = document.getElementById("admin-index");
const adminGaps = document.getElementById("admin-gaps");
const adminConnectors = document.getElementById("admin-connectors");
const adminRecent = document.getElementById("admin-recent");
const syncAll = document.getElementById("sync-all");

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
    el.className = `node ${node.source}${node.id === selectedId ? " selected" : ""}`;
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
      <button type="button" class="drop-card" aria-label="Tirar do canvas">×</button>
      <button type="button" class="port in" aria-hidden="true"></button>
      <button type="button" class="port out" aria-label="Arrasta até outro card para ligar" title="Arrasta até outro card para ligar"></button>
    `;
    el.querySelector("h3").textContent = node.title;
    el.querySelector("p").textContent = node.excerpt.slice(0, 140);
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

function renderFeed(el, hits, empty, onPick) {
  el.innerHTML = "";
  if (!hits.length) {
    const note = document.createElement("p");
    note.className = "status";
    note.textContent = empty;
    el.appendChild(note);
    return;
  }
  for (const hit of hits) {
    const btn = document.createElement("button");
    btn.className = "hit";
    const kind = document.createElement("small");
    const channel = hit.channel_or_space ? ` · ${hit.channel_or_space}` : "";
    kind.textContent = `${hit.source}${channel}`;
    const title = document.createElement("b");
    title.textContent = (hit.text || hit.title || hit.native_id).slice(0, 80);
    const excerpt = document.createElement("span");
    excerpt.textContent = hit.title && hit.text && hit.title !== hit.text ? hit.title : (hit.text || "").slice(80, 180);
    btn.append(kind, title, excerpt);
    btn.addEventListener("click", () => {
      pinHit(hit);
      searchStatus.textContent = "solto — arrasta o cobre para ligar";
      onPick?.(hit);
    });
    el.appendChild(btn);
  }
}

async function loadRecents() {
  recentsLabel.hidden = false;
  recentsEl.hidden = false;
  const data = await fetch("/api/recent").then((r) => r.json());
  if (!data.ok) {
    renderFeed(recentsEl, [], "nada no índice ainda — Soltar põe uma nota");
    return;
  }
  renderFeed(recentsEl, data.hits, "nada recente no índice ainda");
}

function dropTyped() {
  const field = document.getElementById("q");
  const text = field.value.trim();
  if (!text) return;
  const ref = `local:${Date.now()}`;
  pinHit({
    source: "chat",
    title: text,
    text,
    native_id: ref,
    id: ref,
  });
  field.value = "";
  hitsEl.innerHTML = "";
  hitsEl.hidden = true;
  searchStatus.textContent = "solto — arrasta o cobre para ligar";
}

async function runSearch() {
  const field = document.getElementById("q");
  const q = field.value.trim();
  if (!q) return;
  searchStatus.textContent = "buscando…";
  const data = await fetch(`/api/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
  hitsEl.hidden = false;
  if (!data.ok) {
    searchStatus.textContent = "índice fora — Soltar põe uma nota";
    hitsEl.innerHTML = "";
    hitsEl.hidden = true;
    return;
  }
  searchStatus.textContent = data.hits.length ? `${data.hits.length} achados` : "nada encontrado";
  renderFeed(hitsEl, data.hits, "nada encontrado");
}

document.getElementById("q").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  runSearch();
});

document.getElementById("drop").addEventListener("click", dropTyped);

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
  if (event.key === "Escape" && admin && !admin.hidden) {
    closeAdmin();
    event.preventDefault();
    return;
  }
  if (event.target.closest("input, textarea")) return;
  if (admin && !admin.hidden) return;
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
  chat_no_channels: () => "chat sem canais na config",
  never_ran: (name) => `${name} nunca rodou — force uma atualização`,
  zero_chunks: (name) => `${name} não tem chunks no índice`,
};

function closeAdmin() {
  admin.hidden = true;
  clearTimeout(adminTimer);
}

function jobLine(job) {
  if (!job || job.status === "idle") return "";
  if (job.status === "running") {
    return `Atualizando ${job.connector}…`;
  }
  if (job.status === "ok") {
    const extra = job.backfill ? " (desde o começo)" : "";
    return `Pronto — ${job.chunks} chunks em ${job.connector}${extra}`;
  }
  return job.error || "atualização falhou";
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
  if (row.name === "chat") {
    bits.push(row.channels?.length ? `canais ${row.channels.join(", ")}` : "sem canais");
  }
  return bits.join(" · ");
}

function renderAdmin(data) {
  adminJob.textContent = jobLine(data.job);
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
    card.append(copy, btn);
    adminConnectors.appendChild(card);
  }
  renderFeed(adminRecent, data.recent || [], "nada no índice ainda", closeAdmin);
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

function openAdmin() {
  inspector.hidden = true;
  admin.hidden = false;
  refreshAdmin();
}

document.getElementById("open-admin").addEventListener("click", openAdmin);
document.getElementById("close-admin").addEventListener("click", closeAdmin);
syncAll.addEventListener("click", () => requestSync("all"));

fetch("/api/board")
  .then((r) => r.json())
  .then((next) => {
    setBoard(next);
    requestAnimationFrame(tick);
    loadRecents();
  });
