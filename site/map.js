/* Linked Jazz story map.

   data/map.json       the communities (HDBSCAN story clusters) with a 2-D position and radius, and
                       inside each one an icon per person who told part of it; the people list
   data/map/c<id>.json every person's passages in that community, fetched when the pointer enters it
   data/portraits.json + assets/portraits.jpg   one 64px cell per interviewee with a picture

   One canvas, d3-zoom for pan and zoom, a quadtree for hit-testing. Everything is drawn in world
   units (the layout's pixels at zoom 1) except the labels, which stay at screen size and are
   placed greedily so they never overlap. */

const READER = "https://semlab.io/linked-jazz-transcripts/";
const COLL = { tulane: "Tulane", hamilton: "Hamilton", si: "Smithsonian", rutgers: "Rutgers" };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");
const ICON_OUT = `<svg viewBox="0 0 20 20" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 4H4.75A.75.75 0 0 0 4 4.75v10.5c0 .414.336.75.75.75h10.5a.75.75 0 0 0-.75-.75V12M11.5 4H16v4.5M16 4l-7 7"/></svg>`;

// ---------------------------------------------------------------- theme (as the search page)
(function theme() {
  const btn = $("theme"), root = document.documentElement;
  const isDark = () => root.getAttribute("data-theme") === "dark" ||
    (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const paint = () => { const d = isDark(); btn.textContent = d ? "Light theme" : "Dark theme"; btn.setAttribute("aria-pressed", String(d)); };
  btn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("lj-theme", next); } catch (e) { /* private mode */ }
    paint(); readColors(); draw();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { paint(); readColors(); draw(); });
  paint();
})();

// ---------------------------------------------------------------- data
let M = null, portraits = null, atlas = null;
let comms = [], people = [], icons = [], tree = null, world = null;
const snippets = new Map();                                   // cid -> Promise<{people: {pi: [...]}}>
const hover = { icon: null, comm: null };
const sel = { icon: null, comm: null, person: null };         // what the panel shows / what is highlighted

function initials(name) {
  const words = name.replace(/"[^"]*"|\([^)]*\)/g, " ").replace(/[.,]/g, " ").split(/\s+/).filter(Boolean)
    .filter((w) => !/^(jr|sr|ii|iii|iv|dr|mr|mrs|ms)$/i.test(w));
  if (!words.length) return name.slice(0, 2).toUpperCase();
  return ((words[0][0] || "") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase();
}

async function load() {
  const [m, p] = await Promise.all([
    fetch("data/map.json").then((r) => r.json()),
    fetch("data/portraits.json").then((r) => (r.ok ? r.json() : { cell: 64, cols: 1, qids: {} })).catch(() => ({ cell: 64, cols: 1, qids: {} })),
  ]);
  M = m; portraits = p;
  const img = new Image();
  img.onload = () => { atlas = img; draw(); };
  img.src = "assets/portraits.jpg";

  people = M.people.map((x, i) => ({ i, name: x.n, colls: x.c, qid: x.q, comms: x.i, ini: initials(x.n),
    img: x.q != null && portraits.qids[x.q] != null ? portraits.qids[x.q] : null, icons: [] }));
  comms = M.communities.map((c) => {
    const cm = { ...c, icons: [], hull: null, labelCache: new Map() };
    cm.icons = c.people.map((a) => {
      const ic = { p: people[a[0]], x: c.x + a[1], y: c.y + a[2], r: a[3], n: a[4], seg: a[5], c: cm };
      ic.p.icons.push(ic);
      return ic;
    });
    delete cm.people;
    // the outline: the smoothed convex hull of the icons' padded discs
    const pts = [];
    cm.icons.forEach((ic) => { for (let a = 0; a < 12; a++) pts.push([ic.x + (ic.r + 14) * Math.cos(a * Math.PI / 6), ic.y + (ic.r + 14) * Math.sin(a * Math.PI / 6)]); });
    const hull = d3.polygonHull(pts);
    const path = new Path2D();
    if (hull) d3.line().curve(d3.curveBasisClosed).context(path)(hull);
    else path.arc(c.x, c.y, c.r, 0, Math.PI * 2);
    cm.hull = path;
    cm.hullPts = hull;
    cm.top = hull ? Math.min(...hull.map((q) => q[1])) : c.y - c.r;
    return cm;
  });
  comms.sort((a, b) => b.n_people - a.n_people);
  icons = comms.flatMap((c) => c.icons);
  tree = d3.quadtree().x((d) => d.x).y((d) => d.y).addAll(icons);
  world = {
    x0: Math.min(...comms.map((c) => c.x - c.r)), x1: Math.max(...comms.map((c) => c.x + c.r)),
    y0: Math.min(...comms.map((c) => c.y - c.r)) - 40, y1: Math.max(...comms.map((c) => c.y + c.r)),
  };
  $("n-comm").textContent = fmt(M.counts.communities);
  $("status").textContent = "";
  setupZoom();
  fit(false);
  readRoute();
  draw();
}

const commById = (id) => comms.find((c) => c.id === Number(id));
function getSnippets(cid) {
  if (!snippets.has(cid)) {
    snippets.set(cid, fetch(`data/map/c${cid}.json`).then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .catch((e) => { snippets.delete(cid); throw e; }));
  }
  return snippets.get(cid);
}

// ---------------------------------------------------------------- canvas, zoom, colours
const stage = $("stage"), canvas = $("map"), ctx = canvas.getContext("2d");
let W = 0, H = 0, dpr = 1, t = d3.zoomIdentity, zoom = null, raf = 0;
const col = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  ["surface", "surface-raised", "line", "line-strong", "ink", "ink-muted", "blue", "blue-deep", "blue-soft"].forEach((k) => { col[k] = cs.getPropertyValue("--" + k).trim(); });
  col.dark = matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.getAttribute("data-theme") !== "light" || document.documentElement.getAttribute("data-theme") === "dark";
}
readColors();

let userMoved = false;   // once the reader pans or zooms, a resize keeps their view instead of refitting
function resize() {
  const w = Math.max(1, stage.clientWidth), h = Math.max(1, stage.clientHeight), d = Math.min(window.devicePixelRatio || 1, 2);
  if (w === W && h === H && d === dpr) return;
  W = w; H = h; dpr = d;
  canvas.width = W * dpr; canvas.height = H * dpr;
  if (world && zoom && !userMoved) fit(false); else draw();
}

function setupZoom() {
  zoom = d3.zoom().scaleExtent([0.05, 6]).on("zoom", (ev) => { t = ev.transform; if (ev.sourceEvent) userMoved = true; draw(); })
    .on("start", () => canvas.classList.add("grabbing")).on("end", () => canvas.classList.remove("grabbing"));
  d3.select(canvas).call(zoom).on("dblclick.zoom", null);
  $("zin").addEventListener("click", () => d3.select(canvas).transition().duration(250).call(zoom.scaleBy, 1.6));
  $("zout").addEventListener("click", () => d3.select(canvas).transition().duration(250).call(zoom.scaleBy, 1 / 1.6));
  $("zfit").addEventListener("click", () => fit(true));
}
function fitTo(x0, y0, x1, y1, animate, kmax = 6, pad = 40) {
  const k = Math.min(kmax, Math.min((W - 2 * pad) / (x1 - x0 || 1), (H - 2 * pad) / (y1 - y0 || 1)));
  const panelW = $("panel").hidden || W < 720 ? 0 : 420;
  const tr = d3.zoomIdentity.translate((W - panelW) / 2 - k * (x0 + x1) / 2, H / 2 - k * (y0 + y1) / 2).scale(k);
  const s = d3.select(canvas);
  if (animate) s.transition().duration(600).call(zoom.transform, tr); else s.call(zoom.transform, tr);
  userMoved = true;
}
const fit = (animate) => { fitTo(world.x0, world.y0, world.x1, world.y1, animate, 6, 64); userMoved = false; };
function fitComm(c, animate) { fitTo(c.x - c.r, c.top - 30, c.x + c.r, c.y + c.r, animate, 2.5, 48); }
function fitPerson(p, animate) {
  if (!p.icons.length) return;
  if (p.icons.length === 1) { const ic = p.icons[0]; fitTo(ic.x - 140, ic.y - 140, ic.x + 140, ic.y + 140, animate, 2.5); return; }
  const xs = p.icons.map((i) => i.x), ys = p.icons.map((i) => i.y);
  fitTo(Math.min(...xs) - 120, Math.min(...ys) - 120, Math.max(...xs) + 120, Math.max(...ys) + 120, animate, 2.5);
}

// ---------------------------------------------------------------- drawing
function draw() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); }
let labelRects = [];                                            // screen rects of the labels drawn in the last frame

function wrap(c, maxW) {
  const key = Math.round(maxW / 10);
  if (c.labelCache.has(key)) return c.labelCache.get(key);
  const words = c.name.split(" "), lines = []; let line = "";
  words.forEach((w) => {
    const test = line ? line + " " + w : w;
    if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = w; } else line = test;
  });
  if (line) lines.push(line);
  if (lines.length > 3) { lines.length = 3; lines[2] = lines[2].replace(/\s*\S*$/, "") + "…"; }
  const out = { lines, w: Math.max(...lines.map((l) => ctx.measureText(l).width)) };
  c.labelCache.set(key, out);
  return out;
}

function render() {
  if (!M) return;
  const k = t.k;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = col["surface-raised"]; ctx.fillRect(0, 0, W, H);
  ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * t.x, dpr * t.y);
  const vx0 = -t.x / k, vy0 = -t.y / k, vx1 = (W - t.x) / k, vy1 = (H - t.y) / k;
  const visible = comms.filter((c) => c.x + c.r > vx0 && c.x - c.r < vx1 && c.y + c.r > vy0 && c.y - c.r < vy1);
  const focusComm = hover.comm || sel.comm || (hover.icon && hover.icon.c) || (sel.icon && sel.icon.c);
  const hlPerson = hover.icon ? hover.icon.p : sel.person;

  // the outlines
  ctx.lineWidth = 1 / k;
  visible.forEach((c) => {
    const on = c === focusComm;
    ctx.fillStyle = on ? col["blue-soft"] : col.surface;
    ctx.strokeStyle = on ? col.blue : col.line;
    ctx.lineWidth = (on ? 1.5 : 1) / k;
    ctx.fill(c.hull); ctx.stroke(c.hull);
  });

  // the people
  const rk = (r) => r * k;
  const cell = portraits.cell, cols = portraits.cols;
  visible.forEach((c) => {
    c.icons.forEach((ic) => {
      const sr = rk(ic.r);
      if (sr < 1.2) return;
      const isHl = hlPerson && ic.p === hlPerson, isHov = ic === hover.icon || ic === sel.icon;
      if (sr < 6 && !isHl && !isHov) {                        // far out: a plain dot, no ring
        ctx.beginPath(); ctx.arc(ic.x, ic.y, ic.r, 0, Math.PI * 2);
        ctx.fillStyle = col.dark ? "#5a564f" : "#c6c1b7"; ctx.fill();
        return;
      }
      const hasImg = atlas && ic.p.img != null && sr >= 4;
      if (hasImg) {
        ctx.save(); ctx.beginPath(); ctx.arc(ic.x, ic.y, ic.r, 0, Math.PI * 2); ctx.clip();
        ctx.drawImage(atlas, (ic.p.img % cols) * cell, Math.floor(ic.p.img / cols) * cell, cell, cell, ic.x - ic.r, ic.y - ic.r, ic.r * 2, ic.r * 2);
        ctx.restore();
      } else {
        ctx.beginPath(); ctx.arc(ic.x, ic.y, ic.r, 0, Math.PI * 2);
        ctx.fillStyle = col.dark ? "#3a3833" : "#e9e6df"; ctx.fill();
        if (sr >= 7) {
          ctx.fillStyle = col.ink; ctx.font = `600 ${Math.max(6, ic.r * 0.9)}px "Libre Franklin", sans-serif`;
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText(ic.p.ini, ic.x, ic.y + ic.r * 0.04);
        }
      }
      ctx.beginPath(); ctx.arc(ic.x, ic.y, ic.r, 0, Math.PI * 2);
      ctx.lineWidth = (isHov ? 3 : isHl ? 2.5 : 1) / k;
      ctx.strokeStyle = isHov || isHl ? col.blue : col.line; ctx.stroke();
      if (isHov) { ctx.beginPath(); ctx.arc(ic.x, ic.y, ic.r + 2.5 / k, 0, Math.PI * 2); ctx.lineWidth = 1 / k; ctx.strokeStyle = col["blue-deep"]; ctx.stroke(); }
    });
  });

  // the labels, in screen space, biggest community first; the focused one always wins
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = `600 13px "Libre Franklin", sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
  labelRects = [];
  const order = focusComm ? [focusComm, ...visible.filter((c) => c !== focusComm)] : visible;
  order.forEach((c) => {
    const maxW = Math.max(150, Math.min(320, 2.4 * c.r * k));
    const { lines, w } = wrap(c, maxW);
    const cx = c.x * k + t.x, bottom = c.top * k + t.y - 6, top = bottom - lines.length * 17;
    const rect = { x0: cx - w / 2 - 6, y0: top - 4, x1: cx + w / 2 + 6, y1: bottom + 4, c };
    if (rect.x1 < 0 || rect.x0 > W || rect.y1 < 0 || rect.y0 > H) return;
    if (c !== focusComm && labelRects.some((r) => !(rect.x1 < r.x0 || rect.x0 > r.x1 || rect.y1 < r.y0 || rect.y0 > r.y1))) return;
    labelRects.push(rect);
    ctx.lineWidth = 4; ctx.strokeStyle = col["surface-raised"]; ctx.lineJoin = "round";
    ctx.fillStyle = c === focusComm ? col["blue-deep"] : col.ink;
    lines.forEach((l, i) => { const y = top + (i + 1) * 17 - 4; ctx.strokeText(l, cx, y); ctx.fillText(l, cx, y); });
  });
}

// ---------------------------------------------------------------- hit testing, hover, tooltip
const tip = $("tip");
function hit(px, py) {
  const wx = (px - t.x) / t.k, wy = (py - t.y) / t.k;
  const reach = Math.max(6 / t.k, 2);
  const ic = tree.find(wx, wy, 24 + reach);
  if (ic && Math.hypot(ic.x - wx, ic.y - wy) <= ic.r + reach) return { icon: ic, comm: ic.c };
  const lab = labelRects.find((r) => px >= r.x0 && px <= r.x1 && py >= r.y0 && py <= r.y1);
  if (lab) return { icon: null, comm: lab.c, label: true };
  const c = comms.find((c) => Math.hypot(c.x - wx, c.y - wy) <= c.r && (!c.hullPts || d3.polygonContains(c.hullPts, [wx, wy])));
  return { icon: null, comm: c || null };
}

function avatar(p, cls = "lj-avatar") {
  if (p.img != null) {
    const cell = portraits.cell, cols = portraits.cols, x = (p.img % cols) * cell, y = Math.floor(p.img / cols) * cell;
    return `<span class="${cls}" style="background:url(assets/portraits.jpg) -${x}px -${y}px / ${cols * cell}px auto"></span>`;
  }
  return `<span class="${cls}">${esc(p.ini)}</span>`;
}
const archives = (p) => p.colls.map((c) => COLL[c] || c).join(", ");

let tipFor = null;
function showTip(h, px, py) {
  if (!h.icon && !h.comm) { tip.hidden = true; tipFor = null; return; }
  const key = h.icon ? h.icon : h.comm;
  if (key !== tipFor) {
    tipFor = key;
    if (h.icon) {
      const ic = h.icon, p = ic.p;
      tip.innerHTML = `<div class="who">${avatar(p)}<div><b>${esc(p.name)}</b>${esc(archives(p))} · ${ic.n} passage${ic.n === 1 ? "" : "s"} here</div></div>
        <p class="title" id="tip-title">…</p><p class="quote" id="tip-quote" hidden></p><p class="hint">Click for everything they say in this story</p>`;
      getSnippets(ic.c.id).then((d) => {
        if (tipFor !== ic) return;
        const s = (d.people[String(p.i)] || [])[0];
        if (!s) return;
        $("tip-title").textContent = s.t + (s.p ? ` · ${s.p}` : "");
        const q = $("tip-quote"); q.textContent = s.q; q.hidden = !s.q;
        placeTip(px, py);
      }).catch(() => { if (tipFor === ic) $("tip-title").textContent = "The passages could not be loaded."; });
    } else {
      const c = h.comm;
      tip.innerHTML = `<p class="comm">${esc(c.name)}</p><p class="theme">${c.kind === "mixed" ? "A loose grouping · " : ""}${esc(c.parent_name || "")}</p>
        <p>${fmt(c.n_people)} people · ${fmt(c.n_segments)} passages${c.period ? ` · mostly ${c.period[0]}–${c.period[1]}` : ""}</p>
        <p class="hint">${h.label ? "Click for the story and who tells it" : "Scroll to zoom in, click the title for the story"}</p>`;
    }
  }
  tip.hidden = false;
  placeTip(px, py);
}
function placeTip(px, py) {
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = px + 18, y = py + 18;
  if (x + w > W - 8) x = Math.max(8, px - w - 18);
  if (y + h > H - 8) y = Math.max(8, py - h - 18);
  tip.style.left = x + "px"; tip.style.top = y + "px";
}

let lastMove = null;
canvas.addEventListener("pointermove", (ev) => {
  if (!M || ev.pointerType === "touch") return;
  const r = canvas.getBoundingClientRect(), px = ev.clientX - r.left, py = ev.clientY - r.top;
  lastMove = [px, py];
  const h = hit(px, py);
  const changed = h.icon !== hover.icon || h.comm !== hover.comm;
  hover.icon = h.icon; hover.comm = h.icon ? null : h.comm;
  canvas.classList.toggle("hand", !!(h.icon || h.label));
  if (h.comm) getSnippets(h.comm.id).catch(() => {});
  showTip(h, px, py);
  if (changed) draw();
});
canvas.addEventListener("pointerleave", () => { hover.icon = null; hover.comm = null; tip.hidden = true; tipFor = null; draw(); });
let downAt = null;
canvas.addEventListener("pointerdown", (ev) => { downAt = [ev.clientX, ev.clientY]; });
canvas.addEventListener("pointerup", (ev) => {
  if (!M || !downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  const r = canvas.getBoundingClientRect(), h = hit(ev.clientX - r.left, ev.clientY - r.top);
  if (h.icon) openPerson(h.icon);
  else if (h.comm && h.label) openComm(h.comm);
  else if (h.comm && ev.pointerType === "touch") { hover.comm = h.comm; showTip(h, ev.clientX - r.left, ev.clientY - r.top); draw(); }
  else if (!h.comm && ev.pointerType === "touch") { tip.hidden = true; hover.comm = null; draw(); }
});

// ---------------------------------------------------------------- the panel
const panel = $("panel"), body = $("panel-body");
$("close").addEventListener("click", closePanel);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !panel.hidden) closePanel(); });
function closePanel() {
  panel.hidden = true; sel.icon = null; sel.comm = null; sel.person = null;
  setRoute({}); draw();
}
function setRoute(q) {
  const p = new URLSearchParams();
  if (q.c != null) p.set("c", q.c);
  if (q.p) p.set("p", q.p);
  const s = p.toString();
  history.replaceState(null, "", location.pathname + (s ? "?" + s : ""));
}
function readRoute() {
  const p = new URLSearchParams(location.search);
  const c = p.get("c") != null ? commById(p.get("c")) : null, name = p.get("p");
  if (name) {
    const person = people.find((x) => x.name === name);
    if (person) {
      const ic = c ? person.icons.find((i) => i.c === c) : person.icons[0];
      if (ic) { openPerson(ic, true); return; }
    }
  }
  if (c) openComm(c, true);
}
const searchUrl = (c, who) => `./?q=${encodeURIComponent(c.name)}&community=${c.id}${who ? `&interviewee=${encodeURIComponent(who)}` : ""}`;

function openPerson(ic, initial) {
  const p = ic.p, c = ic.c;
  sel.icon = ic; sel.comm = null; sel.person = p;
  tip.hidden = true;
  const others = p.icons.filter((i) => i !== ic);
  body.innerHTML = `<p class="label">In the story</p>
    <p class="meta"><button type="button" data-comm="${c.id}"><b>${esc(c.name)}</b></button></p>
    <div class="person-head">${avatar(p)}<h2>${esc(p.name)}</h2></div>
    <p class="meta">${esc(archives(p))} · <b>${ic.n}</b> passage${ic.n === 1 ? "" : "s"} in this story${others.length ? ` · also in <b>${others.length}</b> other${others.length === 1 ? "" : "s"}` : ""}</p>
    <div class="actions"><a class="lj-btn lj-btn-sm" href="${searchUrl(c, p.name)}">Search their passages ${ICON_OUT}</a></div>
    <h3>What they say here</h3><ol class="passages" id="passages"><li class="caption">Loading…</li></ol>
    ${others.length ? `<h3>Also tells</h3><ul class="others">${others.sort((a, b) => b.n - a.n).map((o) => `<li><button type="button" data-icon="${o.c.id}">${esc(o.c.name)}</button><span>${o.n}</span></li>`).join("")}</ul>` : ""}`;
  panel.hidden = false; panel.scrollTop = 0;
  fitPerson({ icons: [ic] }, !initial);
  setRoute({ c: c.id, p: p.name });
  draw();
  getSnippets(c.id).then((d) => {
    if (sel.icon !== ic) return;
    const list = d.people[String(p.i)] || [];
    $("passages").innerHTML = list.map((s) => `<li class="passage">
      <h4>${esc(s.t)}</h4>
      <p class="when">${[s.p ? `about ${s.p}` : "", s.y ? `interviewed ${s.y}` : "", s.ch].filter(Boolean).map(esc).join(" · ")}</p>
      ${s.s ? `<p class="sum">${esc(s.s)}</p>` : ""}
      ${s.q ? `<p class="quote">${esc(s.q)}</p>` : ""}
      <p class="links"><a href="${esc(s.u)}" target="_blank" rel="noopener">Read the passage${ICON_OUT}</a></p>
    </li>`).join("") || `<li class="caption">Nothing could be loaded.</li>`;
  }).catch(() => { $("passages").innerHTML = `<li class="caption">The passages could not be loaded.</li>`; });
}

function openComm(c, initial) {
  sel.comm = c; sel.icon = null; sel.person = null;
  tip.hidden = true;
  const fac = (label, items) => items && items.length ? `<div><span class="label">${label}</span> ${items.map(([n, k]) => `${esc(n)} <span>(${k})</span>`).join(", ")}</div>` : "";
  const colls = Object.entries(c.collections || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${COLL[k] || k} ${v}`).join(" · ");
  body.innerHTML = `<p class="label">${c.kind === "mixed" ? "Loose grouping" : "Story"}${c.parent_name ? ` · part of ${esc(c.parent_name)}` : ""}</p>
    <h2>${esc(c.name)}</h2>
    <p class="meta"><b>${fmt(c.n_people)}</b> people · <b>${fmt(c.n_segments)}</b> passages${c.period ? ` · mostly about <b>${c.period[0]}–${c.period[1]}</b>` : ""}<br>${esc(colls)}</p>
    <p class="narrative">${esc(c.narrative)}</p>
    ${c.threads && c.threads.length ? `<ul class="threads">${c.threads.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <div class="facets">${fac("Places", c.facets.places)}${fac("Venues", c.facets.venues)}${fac("Bands", c.facets.bands)}${fac("People named", c.facets.people)}</div>
    <div class="actions"><a class="lj-btn lj-btn-sm" href="${searchUrl(c)}">Search this story ${ICON_OUT}</a></div>
    <h3>Who tells it</h3>
    <ul class="folks">${c.icons.map((ic) => `<li><button type="button" data-person="${ic.p.i}">${esc(ic.p.name)}</button><span>${ic.n}</span></li>`).join("")}</ul>`;
  panel.hidden = false; panel.scrollTop = 0;
  fitComm(c, !initial);
  setRoute({ c: c.id });
  draw();
}
body.addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-comm], button[data-icon], button[data-person]");
  if (!b) return;
  if (b.dataset.comm) openComm(commById(b.dataset.comm));
  else if (b.dataset.icon) { const ic = sel.person.icons.find((i) => i.c.id === Number(b.dataset.icon)); if (ic) openPerson(ic); }
  else if (b.dataset.person) { const ic = sel.comm.icons.find((i) => i.p.i === Number(b.dataset.person)); if (ic) openPerson(ic); }
});

// ---------------------------------------------------------------- find a person or a story
(function find() {
  const input = $("q"), box = input.parentElement;
  let menu = null, rows = [], cursor = -1;
  const close = () => { if (menu) menu.remove(); menu = null; rows = []; cursor = -1; input.removeAttribute("aria-expanded"); };
  const show = () => {
    close();
    const q = input.value.trim().toLowerCase();
    if (!M || q.length < 2) return;
    const ppl = people.filter((p) => p.name.toLowerCase().includes(q)).slice(0, 8).map((p) => ({ kind: "person", p }));
    const cms = comms.filter((c) => c.name.toLowerCase().includes(q) || (c.threads || []).some((t) => t.toLowerCase().includes(q))).slice(0, 8).map((c) => ({ kind: "story", c }));
    rows = [...cms, ...ppl];
    menu = document.createElement("ul"); menu.className = "lj-search-menu"; menu.setAttribute("role", "listbox");
    if (!rows.length) menu.innerHTML = `<li class="note">Nobody and no story by that name.</li>`;
    rows.forEach((r, i) => {
      const li = document.createElement("li"); li.setAttribute("role", "option"); li.dataset.i = i;
      li.innerHTML = r.kind === "person" ? `<span>${esc(r.p.name)}</span><span>${r.p.icons.length} ${r.p.icons.length === 1 ? "story" : "stories"}</span>`
        : `<span>${esc(r.c.name)}</span><span class="kind">story</span>`;
      li.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(r); });
      menu.appendChild(li);
    });
    box.appendChild(menu); input.setAttribute("aria-expanded", "true");
  };
  const pick = (r) => {
    input.value = ""; close();
    if (r.kind === "person") { sel.person = r.p; sel.icon = null; sel.comm = null; panel.hidden = true; fitPerson(r.p, true); if (r.p.icons.length === 1) openPerson(r.p.icons[0]); else { setRoute({ p: r.p.name }); draw(); } }
    else openComm(r.c);
  };
  input.addEventListener("input", show);
  input.addEventListener("focus", show);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (ev) => {
    if (!menu || !rows.length) { if (ev.key === "Escape") close(); return; }
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault(); cursor = (cursor + (ev.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
      [...menu.children].forEach((li, i) => li.setAttribute("aria-selected", String(i === cursor)));
    } else if (ev.key === "Enter") { ev.preventDefault(); pick(rows[cursor >= 0 ? cursor : 0]); }
    else if (ev.key === "Escape") close();
  });
  $("find").addEventListener("submit", (ev) => ev.preventDefault());
})();

new ResizeObserver(resize).observe(stage);
resize();
load().catch((e) => { $("status").textContent = "The map could not be loaded. " + (e && e.message ? e.message : ""); console.error(e); });
