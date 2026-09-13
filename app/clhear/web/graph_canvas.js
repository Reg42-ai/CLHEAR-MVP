/* CLHEAR graph canvas (HLD v2 §5 "the graph explorer renders the Blueprint as a
   constellation"). An Obsidian-style force-directed view drawn on a 2D canvas:
   zoom and pan, hover isolates a node and its neighbours, labels appear as you
   zoom in, drag pins while dragging, groups carry their member count and open on
   double-click, a node re-focuses on double-click. Framework-agnostic: pages mount
   it into an element and feed it the /graph/subgraph response.

   Served at /static/graph-canvas.js. Colours come from theme.css (--l1…--l8,
   --accent, --bad, --muted, --ink) so canvas, badges and legend always agree. */
import { forceSimulation, forceLink, forceManyBody, forceX, forceY, forceCollide } from "https://esm.sh/d3-force@3.0.0";
import { zoom as d3zoom, zoomIdentity } from "https://esm.sh/d3-zoom@3.0.0";
import { select, pointer } from "https://esm.sh/d3-selection@3.0.0";
import { drag as d3drag } from "https://esm.sh/d3-drag@3.0.0";

export const DEFAULT_FORCES = { center: 0.05, repel: 140, linkStrength: 0.6, linkDistance: 60 };
export const DEFAULT_DISPLAY = { labels: 1, arrows: false, sizeByDegree: true };
const A11Y_CAP = 200;
const DIM = 0.12;

const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function readTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
  const layers = {};
  for (let i = 0; i <= 8; i++) layers[`L${i}`] = v(`--l${i}`, "#9c9fc6");
  return { layers, accent: v("--accent", "#818cf8"), bad: v("--bad", "#fb7185"), muted: v("--muted", "#9c9fc6"),
           ink: v("--ink", "#eef0ff"), canvas: v("--canvas", "#07071a"), edge: v("--edge-bright", "rgba(129,140,248,0.55)") };
}

export function layerColor(layer, theme) {
  const t = theme || readTheme();
  return t.layers[layer] || t.layers.L0;
}

/** Radius: Obsidian sizes by links; groups by how many members they fold. */
export function nodeRadius(n, display, compact) {
  const base = compact ? 3 : 4;
  if (n.group || n.members) return Math.min(26, base + 2 * Math.sqrt(n.members || 1) + 2);
  if (!display.sizeByDegree) return base + 2;
  return Math.min(22, base + 2 * Math.sqrt(n.degree || 0));
}

function truncate(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

/**
 * Mount a graph canvas into `root`.
 * callbacks: onSelect(node|null), onFocus(node), onExpand(groupNode), onHover(node|null)
 * options:   compact (bool), forces, display
 * returns:   { setData, setOptions, select, highlight, fit, resetView, destroy, nodes }
 */
export function createGraphCanvas(root, { onSelect, onFocus, onExpand, onHover, compact = false, forces = {}, display = {} } = {}) {
  root.classList.add("gc-root");
  if (compact) root.classList.add("gc-compact");
  const canvas = document.createElement("canvas");
  canvas.className = "gc-canvas";
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "Graph: empty");
  canvas.tabIndex = -1;
  const list = document.createElement("div");
  list.className = "gc-a11y";
  const listTitle = document.createElement("p");
  listTitle.className = "gc-a11y-title";
  listTitle.textContent = "Nodes in this graph (keyboard: arrow keys move, Enter selects, F focuses, Escape clears)";
  const ol = document.createElement("ol");
  ol.setAttribute("aria-label", "Nodes in this graph");
  list.append(listTitle, ol);
  root.append(canvas, list);

  const ctx = canvas.getContext("2d");
  const state = {
    nodes: [], links: [], byId: new Map(), neighbours: new Map(), focus: null,
    hovered: null, selected: null, highlighted: null, transform: zoomIdentity,
    forces: { ...DEFAULT_FORCES, ...forces }, display: { ...DEFAULT_DISPLAY, ...display },
    theme: readTheme(), width: 0, height: 0, dpr: 1, sim: null, frame: 0, destroyed: false,
    autoFit: false, // follow the first layout until it settles or the reader takes the wheel
  };

  // ---------------------------------------------------------------- sizing / theme
  const resize = () => {
    const rect = root.getBoundingClientRect();
    state.width = Math.max(200, Math.floor(rect.width));
    state.height = Math.max(160, Math.floor(rect.height));
    state.dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = state.width * state.dpr; canvas.height = state.height * state.dpr;
    canvas.style.width = state.width + "px"; canvas.style.height = state.height + "px";
    draw();
  };
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  ro?.observe(root);
  const themeObserver = new MutationObserver(() => { state.theme = readTheme(); draw(); });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // ---------------------------------------------------------------- simulation
  const applyForces = (sim) => {
    const f = state.forces;
    sim.force("link").distance(f.linkDistance).strength(f.linkStrength);
    sim.force("charge").strength(-Math.abs(f.repel));
    sim.force("x").strength(f.center); sim.force("y").strength(f.center);
    sim.force("collide").radius((d) => nodeRadius(d, state.display, compact) + 1.5);
  };
  const settle = (sim) => {
    if (reducedMotion()) { sim.stop(); sim.tick(Math.min(300, 60 + state.nodes.length)); draw(); }  // one settled frame, no motion
    else sim.alpha(1).restart();
  };
  const buildSim = () => {
    state.sim?.stop();
    const sim = forceSimulation(state.nodes)
      .force("link", forceLink(state.links).id((d) => d.id))
      .force("charge", forceManyBody().distanceMax(600))
      .force("x", forceX(0)).force("y", forceY(0))
      .force("collide", forceCollide())
      .alphaDecay(0.035).velocityDecay(0.35)
      .on("tick", () => { draw(); if (state.autoFit) fit(); })
      .on("end", () => { if (state.autoFit) { fit(); state.autoFit = false; } draw(); });
    applyForces(sim);
    state.sim = sim;
    settle(sim);
  };

  // ---------------------------------------------------------------- data
  function setData(data) {
    const prev = state.byId;
    const nodes = (data.nodes || []).map((n) => {
      const old = prev.get(n.id);
      return old ? Object.assign(old, n) : { ...n };
    });
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const links = (data.edges || []).filter((e) => byId.has(e.from) && byId.has(e.to))
      .map((e) => ({ ...e, source: e.from, target: e.to }));
    const neighbours = new Map(nodes.map((n) => [n.id, new Set()]));
    for (const l of links) { neighbours.get(l.from).add(l.to); neighbours.get(l.to).add(l.from); }
    // new nodes appear beside a neighbour that already had a place (an expanded group's members
    // spill out where the group was) instead of from the origin
    for (const n of nodes) {
      if (n.x !== undefined) continue;
      const anchor = [...(neighbours.get(n.id) || [])].map((id) => byId.get(id)).find((m) => m && m.x !== undefined);
      const a = Math.random() * Math.PI * 2, r = 20 + Math.random() * 30;
      n.x = (anchor ? anchor.x : 0) + Math.cos(a) * r; n.y = (anchor ? anchor.y : 0) + Math.sin(a) * r;
    }
    const fresh = nodes.filter((n) => !prev.has(n.id)).length;
    const refocused = (data.focus || null) !== state.focus;
    state.nodes = nodes; state.links = links; state.byId = byId; state.neighbours = neighbours;
    state.focus = data.focus || null;
    if (state.selected && !byId.has(state.selected.id)) state.selected = null;
    if (state.hovered && !byId.has(state.hovered.id)) state.hovered = null;
    const focusLabel = state.focus && byId.get(state.focus) ? byId.get(state.focus).label : null;
    canvas.setAttribute("aria-label", `Graph: ${nodes.length} nodes, ${links.length} connections` +
      (focusLabel ? `, around ${focusLabel}` : "") + (data.truncated ? " (truncated to the busiest nodes)" : ""));
    renderList();
    // a new view (first load, new focus, mostly new nodes) is fitted as it settles; opening
    // one group keeps the reader's viewport
    state.autoFit = !prev.size || refocused || fresh > nodes.length / 2;
    buildSim();
    if (reducedMotion()) { fit(); state.autoFit = false; }
  }

  function setOptions(opts = {}) {
    if (opts.forces) state.forces = { ...state.forces, ...opts.forces };
    if (opts.display) state.display = { ...state.display, ...opts.display };
    if (state.sim) { applyForces(state.sim); if (opts.forces) { if (reducedMotion()) { state.sim.stop(); state.sim.tick(120); } else state.sim.alpha(0.5).restart(); } }
    draw();
  }

  // ---------------------------------------------------------------- geometry helpers
  const toGraph = (px, py) => state.transform.invert([px, py]);
  const hit = (px, py) => {
    if (!state.sim) return null;
    const [gx, gy] = toGraph(px, py);
    const n = state.sim.find(gx, gy, 14 / state.transform.k + 8);
    if (!n) return null;
    const r = nodeRadius(n, state.display, compact);
    return Math.hypot(n.x - gx, n.y - gy) <= r + 6 / state.transform.k ? n : null;
  };

  // ---------------------------------------------------------------- drawing
  function draw() {
    if (state.destroyed || state.frame) return;
    state.frame = requestAnimationFrame(() => { state.frame = 0; paint(); });
  }
  function paint() {
    const { width, height, dpr, transform: t, theme, display } = state;
    if (!width) return;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.translate(t.x, t.y); ctx.scale(t.k, t.k);

    const active = state.hovered || state.selected;
    const activeSet = active ? new Set([active.id, ...(state.neighbours.get(active.id) || [])]) : null;
    const hl = state.highlighted;
    const alphaFor = (id) => {
      if (hl && hl.size) return hl.has(id) ? 1 : DIM;
      if (activeSet) return activeSet.has(id) ? 1 : DIM;
      return 1;
    };

    // edges
    ctx.lineCap = "round";
    for (const l of state.links) {
      const a = l.source, b = l.target;
      if (a.x === undefined || b.x === undefined) continue;
      const isActive = activeSet && (a.id === active.id || b.id === active.id);
      const dim = hl && hl.size ? (hl.has(a.id) && hl.has(b.id) ? 1 : DIM) : activeSet ? (isActive ? 1 : DIM) : 0.55;
      ctx.globalAlpha = dim;
      ctx.strokeStyle = isActive ? theme.accent : l.rel === "gap" ? theme.bad : theme.muted;
      ctx.lineWidth = (l.rel === "gap" ? 1.4 : 0.6 + Math.min(4, Math.log2(l.weight || 1))) / Math.sqrt(t.k);
      if (l.rel === "gap") ctx.setLineDash([3 / t.k, 3 / t.k]); else ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      if (display.arrows) {
        const rb = nodeRadius(b, display, compact);
        const ang = Math.atan2(b.y - a.y, b.x - a.x), len = 6 / Math.sqrt(t.k);
        const tx = b.x - Math.cos(ang) * (rb + 1), ty = b.y - Math.sin(ang) * (rb + 1);
        ctx.fillStyle = ctx.strokeStyle; ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(tx, ty);
        ctx.lineTo(tx - Math.cos(ang - 0.45) * len, ty - Math.sin(ang - 0.45) * len);
        ctx.lineTo(tx - Math.cos(ang + 0.45) * len, ty - Math.sin(ang + 0.45) * len);
        ctx.closePath(); ctx.fill();
      }
    }
    ctx.setLineDash([]);

    // nodes
    for (const n of state.nodes) {
      if (n.x === undefined) continue;
      const r = nodeRadius(n, display, compact);
      ctx.globalAlpha = alphaFor(n.id);
      ctx.fillStyle = layerColor(n.layer, theme);
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
      if (n.group) { // hollow centre marks a folded group
        ctx.fillStyle = theme.canvas; ctx.globalAlpha = Math.min(ctx.globalAlpha, 0.55);
        ctx.beginPath(); ctx.arc(n.x, n.y, r * 0.45, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = alphaFor(n.id);
      }
      if (n.id === state.focus) {
        ctx.strokeStyle = theme.accent; ctx.lineWidth = 2.5 / t.k;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 4 / t.k, 0, Math.PI * 2); ctx.stroke();
      }
      if (n.gap) {
        ctx.strokeStyle = theme.bad; ctx.lineWidth = 1.5 / t.k; ctx.setLineDash([3 / t.k, 2 / t.k]);
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 3 / t.k, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      }
      if (state.selected && n.id === state.selected.id) {
        ctx.strokeStyle = theme.ink; ctx.lineWidth = 1.5 / t.k;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 2 / t.k, 0, Math.PI * 2); ctx.stroke();
      }
      if (hl && hl.has(n.id)) {
        ctx.strokeStyle = theme.accent; ctx.lineWidth = 2 / t.k;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 5 / t.k, 0, Math.PI * 2); ctx.stroke();
      }
    }

    // labels: appear as you zoom in (density), always for the active node and its neighbours
    // a label appears once the node is big enough on screen (radius × zoom); density moves the
    // bar: 0 → only hover / focus / groups, 1 → the busiest nodes at fit, 2 → nearly everything
    const threshold = 26 - 10 * display.labels;
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    for (const n of state.nodes) {
      if (n.x === undefined) continue;
      const r = nodeRadius(n, display, compact);
      const forced = (activeSet && activeSet.has(n.id)) || (hl && hl.has(n.id)) || n.id === state.focus ||
        (state.selected && n.id === state.selected.id);
      const show = forced || n.group || (display.labels > 0 && r * t.k >= threshold);
      if (!show) continue;
      const px = 12 / t.k;
      ctx.font = `${Math.max(9, 12) / t.k}px Inter, -apple-system, Segoe UI, sans-serif`;
      if (!forced && (activeSet || (hl && hl.size))) continue; // hover / search: only the isolated set is labelled
      const alpha = forced ? 1 : Math.min(1, Math.max(0.35, (r * t.k - threshold + 4) / 8));
      const text = truncate(n.label || n.id, forced ? 60 : 32) + (n.members ? ` (${n.members})` : "");
      const w = ctx.measureText(text).width;
      ctx.fillStyle = theme.canvas; ctx.globalAlpha = alpha * 0.75;
      ctx.fillRect(n.x + r + px * 0.5 - 2 / t.k, n.y - 8 / t.k, w + 4 / t.k, 16 / t.k);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = theme.ink;
      ctx.fillText(text, n.x + r + px * 0.5, n.y);
    }
    // member counts inside larger groups
    ctx.textAlign = "center";
    for (const n of state.nodes) {
      if (!n.group || n.x === undefined) continue;
      const r = nodeRadius(n, display, compact);
      if (r * t.k < 11) continue;
      ctx.globalAlpha = alphaFor(n.id);
      ctx.font = `bold ${Math.min(r, 10) / Math.sqrt(t.k)}px Inter, sans-serif`;
      ctx.fillStyle = theme.ink; ctx.fillText(String(n.members), n.x, n.y + 0.5 / t.k);
    }
    ctx.restore();
  }

  // ---------------------------------------------------------------- interaction
  const zoomB = d3zoom().scaleExtent([0.08, 12])
    .filter((event) => {
      if (event.type === "mousedown" || event.type === "touchstart") {
        const [px, py] = pointer(event, canvas);
        if (hit(px, py)) return false; // let drag take the node
      }
      return (!event.ctrlKey || event.type === "wheel") && !event.button;
    })
    .on("zoom", (event) => { if (event.sourceEvent) state.autoFit = false; state.transform = event.transform; draw(); });
  const dragB = d3drag().container(canvas)
    .subject((event) => hit(event.x, event.y))
    .on("start", (event) => { event.subject.fx = event.subject.x; event.subject.fy = event.subject.y; if (!reducedMotion()) state.sim?.alphaTarget(0.3).restart(); })
    .on("drag", (event) => { const [gx, gy] = toGraph(event.x, event.y); event.subject.fx = gx; event.subject.fy = gy; if (reducedMotion()) { state.sim?.tick(2); draw(); } })
    .on("end", (event) => { event.subject.fx = null; event.subject.fy = null; state.sim?.alphaTarget(0); draw(); });
  const sel = select(canvas);
  sel.call(dragB).call(zoomB).on("dblclick.zoom", null);

  let downAt = null;
  canvas.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
  canvas.addEventListener("click", (e) => {
    if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4) return; // it was a pan
    const [px, py] = pointer(e, canvas);
    const n = hit(px, py);
    state.selected = n || null;
    onSelect?.(state.selected); draw();
  });
  canvas.addEventListener("dblclick", (e) => {
    const [px, py] = pointer(e, canvas);
    const n = hit(px, py);
    if (!n) { fit(); return; }
    if (n.group) onExpand?.(n); else onFocus?.(n);
  });
  canvas.addEventListener("mousemove", (e) => {
    const [px, py] = pointer(e, canvas);
    const n = hit(px, py);
    if (n !== state.hovered) { state.hovered = n; canvas.style.cursor = n ? "pointer" : "grab"; onHover?.(n); draw(); }
  });
  canvas.addEventListener("mouseleave", () => { if (state.hovered) { state.hovered = null; onHover?.(null); draw(); } });
  canvas.style.cursor = "grab";

  // ---------------------------------------------------------------- view
  function fit(padding = compact ? 24 : 48) {
    const pts = state.nodes.filter((n) => n.x !== undefined);
    if (!pts.length || !state.width) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of pts) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
    const w = Math.max(40, x1 - x0), h = Math.max(40, y1 - y0);
    const k = Math.min(6, Math.max(0.08, Math.min((state.width - padding * 2) / w, (state.height - padding * 2) / h)));
    const t = zoomIdentity.translate(state.width / 2 - k * (x0 + x1) / 2, state.height / 2 - k * (y0 + y1) / 2).scale(k);
    sel.call(zoomB.transform, t);
  }
  const resetView = () => { fit(); };
  function selectNode(id) {
    state.selected = id ? state.byId.get(id) || null : null;
    onSelect?.(state.selected); draw();
  }
  function highlight(query) {
    const q = (query || "").trim().toLowerCase();
    state.highlighted = q ? new Set(state.nodes.filter((n) => (n.label || "").toLowerCase().includes(q) || n.id.toLowerCase().includes(q)).map((n) => n.id)) : null;
    draw();
    return state.highlighted ? state.highlighted.size : null;
  }

  // ---------------------------------------------------------------- accessible node list
  function renderList() {
    ol.textContent = "";
    const ordered = [...state.nodes].sort((a, b) => (b.id === state.focus) - (a.id === state.focus) || (b.degree || 0) - (a.degree || 0));
    ordered.slice(0, A11Y_CAP).forEach((n, i) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button"; b.className = "gc-a11y-node"; b.tabIndex = i === 0 ? 0 : -1; b.dataset.id = n.id;
      b.textContent = `${n.layer} ${n.group ? "group" : n.kind}: ${n.label}${n.members ? ` (${n.members} members)` : ""}` +
        `${n.gap ? " — gap" : ""}${n.id === state.focus ? " — focus" : ""}, ${n.degree || 0} connections`;
      b.addEventListener("click", () => { selectNode(n.id); });
      b.addEventListener("focus", () => { state.hovered = n; draw(); });
      b.addEventListener("blur", () => { if (state.hovered === n) { state.hovered = null; draw(); } });
      li.append(b); ol.append(li);
    });
    if (ordered.length > A11Y_CAP) {
      const li = document.createElement("li"); li.className = "mono";
      li.textContent = `${ordered.length - A11Y_CAP} more nodes not listed; narrow the view with the filters.`;
      ol.append(li);
    }
  }
  ol.addEventListener("keydown", (e) => {
    const buttons = [...ol.querySelectorAll("button")];
    const i = buttons.indexOf(document.activeElement);
    if (i < 0) return;
    const move = (j) => { buttons.forEach((b) => (b.tabIndex = -1)); buttons[j].tabIndex = 0; buttons[j].focus(); e.preventDefault(); };
    if (e.key === "ArrowDown" || e.key === "ArrowRight") move(Math.min(buttons.length - 1, i + 1));
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") move(Math.max(0, i - 1));
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(buttons.length - 1);
    else if (e.key.toLowerCase() === "f") { const n = state.byId.get(buttons[i].dataset.id); if (n) { e.preventDefault(); if (n.group) onExpand?.(n); else onFocus?.(n); } }
    else if (e.key === "Escape") { selectNode(null); }
  });

  function destroy() {
    state.destroyed = true;
    state.sim?.stop(); ro?.disconnect(); themeObserver.disconnect();
    if (state.frame) cancelAnimationFrame(state.frame);
    sel.on(".zoom", null).on(".drag", null);
    canvas.remove(); list.remove();
    root.classList.remove("gc-root", "gc-compact");
  }

  resize();
  return { setData, setOptions, select: selectNode, highlight, fit, resetView, destroy, get nodes() { return state.nodes; }, get state() { return state; } };
}

export default createGraphCanvas;
