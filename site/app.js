/* Linked Jazz segment search — fully static, client-side.
   The page mirrors the search API's query string, so every search is a shareable link:
     ?q=…                 the query, plus any facet (&collection=… &band=… &year_from=… …)
     ?similar=<segment>   "more like this" from the clustering index (&same_doc=1 keeps the same interview)
   The API (a Lambda over S3 Vectors + gemini) returns everything a card needs; the passage in
   full comes from the transcript reader's data/seg/<id>.json on demand.
   data/facets.json  archives, topics, interviewees, story themes and communities (loaded at start)
   data/vocab.json   place / venue / band / people names for the pickers (loaded on first use)   */
"use strict";

const API = "https://vbl5pa2nmnl4ucnizt5l2oqsxi0octzh.lambda-url.us-east-1.on.aws/";
const READER = "https://semlab.io/linked-jazz-transcripts/";
const NETWORK = "https://thisismattmiller.github.io/linked-jazz-2026-network/";
const RELEVANT = 0.65;                       // cosine below this: nothing in the corpus really matches
const LIST = ["place", "venue", "band", "person", "qid"];
const SCALAR = ["collection", "category", "interviewee", "doc_id", "theme", "community",
                "year_from", "year_to", "interview_year_from", "interview_year_to"];
const KIND_LABEL = { place: "Place", venue: "Venue", band: "Band", person: "Person", qid: "Person",
                     category: "Topic", interviewee: "Interviewee", doc_id: "Interview", theme: "Theme",
                     community: "Story", year_from: "Discussed from", year_to: "Discussed to",
                     interview_year_from: "Recorded from", interview_year_to: "Recorded to" };
const EXAMPLES = ["brass bands at funerals", "working the Streckfus riverboats", "playing in an Army band during the war",
                  "learning to read music as a child", "how Charlie Parker changed the way people played",
                  "a bandleader who never paid the men", "battles of the bands at the Savoy Ballroom",
                  "touring the South under Jim Crow", "the first time hearing Louis Armstrong"];
const ICON_OUT = `<svg viewBox="0 0 20 20" fill="none" aria-hidden="true" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M8 4H4.75A.75.75 0 0 0 4 4.75v10.5c0 .414.336.75.75.75h10.5a.75.75 0 0 0 .75-.75V12M11.5 4H16v4.5M16 4l-7 7"/></svg>`;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");
const ordinal = (n) => n + (["th", "st", "nd", "rd"][(n % 100 > 10 && n % 100 < 14) ? 0 : Math.min(n % 10, 4) % 4] || "th");
const COLL = { tulane: "Tulane", hamilton: "Hamilton", si: "Smithsonian", rutgers: "Rutgers" };

// ---------------------------------------------------------------- theme (as the reader)
(function theme() {
  const btn = $("theme"), root = document.documentElement;
  const isDark = () => root.getAttribute("data-theme") === "dark" ||
    (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const paint = () => { const d = isDark(); btn.textContent = d ? "Light theme" : "Dark theme"; btn.setAttribute("aria-pressed", String(d)); };
  btn.addEventListener("click", () => {
    const next = isDark() ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("lj-theme", next); } catch (e) { /* private mode */ }
    paint();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", paint);
  paint();
})();

// ---------------------------------------------------------------- state <-> url
const state = { q: "", similar: null, same_doc: false, k: 15, rerank: true, f: {} };
function emptyFacets() {
  const f = {};
  SCALAR.forEach((k) => { f[k] = ""; });
  LIST.forEach((k) => { f[k] = []; });
  return f;
}
state.f = emptyFacets();
const names = {};                            // qid -> display name, from the pickers and the passages
let facets = null, vocab = null, vocabPromise = null;
let lastSearchUrl = null;                    // where "Back to the search" goes from "more like this"

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.q = (p.get("q") || "").trim();
  state.similar = /^\d+$/.test(p.get("similar") || "") ? p.get("similar") : null;
  state.same_doc = p.get("same_doc") === "1";
  state.k = [15, 30, 50].includes(+p.get("k")) ? +p.get("k") : 15;
  state.rerank = p.get("rerank") !== "0";
  state.f = emptyFacets();
  SCALAR.forEach((k) => { state.f[k] = (p.get(k) || "").trim(); });
  LIST.forEach((k) => { state.f[k] = p.getAll(k).map((v) => v.trim()).filter(Boolean); });
}
function params() {
  const p = new URLSearchParams();
  if (state.similar) { p.set("similar", state.similar); if (state.same_doc) p.set("same_doc", "1"); }
  else if (state.q) { p.set("q", state.q); if (!state.rerank) p.set("rerank", "0"); }
  if (state.k !== 15) p.set("k", String(state.k));
  SCALAR.forEach((k) => { if (state.f[k]) p.set(k, state.f[k]); });
  LIST.forEach((k) => state.f[k].forEach((v) => p.append(k, v)));
  return p;
}
function writeUrl(push) {
  const qs = params().toString();
  const url = qs ? `?${qs}` : location.pathname;
  if (url === location.search || (qs === "" && location.search === "")) return;
  history[push ? "pushState" : "replaceState"](null, "", url);
}
function activeCount() {
  return SCALAR.filter((k) => state.f[k]).length + LIST.reduce((n, k) => n + state.f[k].length, 0);
}

// ---------------------------------------------------------------- facets: collections, topics, stories
const catLabel = (id) => facets?.categories.find((c) => c.id === id)?.label || id;
const themeOf = (id) => facets?.themes.find((t) => String(t.id) === String(id));
const communityOf = (id) => facets?.communities.find((c) => String(c.id) === String(id));

async function loadFacets() {
  try { facets = await (await fetch("data/facets.json")).json(); }
  catch (e) { facets = { collections: [], categories: [], interviewees: [], themes: [], communities: [], counts: {} }; }
  if (facets.counts?.segments) $("n-segments").textContent = fmt(facets.counts.segments);
  if (facets.counts?.documents) $("n-docs").textContent = fmt(facets.counts.documents);

  const row = $("collections"), anchor = $("morefilters");
  facets.collections.forEach((c) => {
    const b = document.createElement("button");
    b.className = "lj-rel"; b.type = "button"; b.dataset.c = c.id; b.setAttribute("aria-pressed", "false");
    b.title = c.name;
    b.innerHTML = `${esc(c.label)} <span>${fmt(c.segments)}</span>`;
    row.insertBefore(b, anchor);
  });
  const cat = $("f-category");
  facets.categories.forEach((c) => cat.add(new Option(`${c.label} (${fmt(c.segments)})`, c.id)));
  const th = $("f-theme");
  const story = facets.themes.filter((t) => t.kind !== "meta"), meta = facets.themes.filter((t) => t.kind === "meta");
  const grp = (label, items, sel) => {
    const g = document.createElement("optgroup"); g.label = label;
    items.sort((a, b) => a.name.localeCompare(b.name)).forEach((t) => g.appendChild(new Option(`${t.name} (${fmt(t.size)})`, t.id)));
    sel.appendChild(g);
  };
  grp("Stories", story, th);
  if (meta.length) grp("Interview mechanics", meta, th);
  fillCommunities();
}
function fillCommunities() {
  const sel = $("f-community");
  while (sel.options.length > 1) sel.remove(1);
  const inTheme = (c) => !state.f.theme || String(c.theme) === String(state.f.theme);
  const story = facets.communities.filter((c) => c.kind !== "meta" && inTheme(c));
  const meta = facets.communities.filter((c) => c.kind === "meta" && inTheme(c));
  const grp = (label, items) => {
    if (!items.length) return;
    const g = document.createElement("optgroup"); g.label = label;
    items.sort((a, b) => a.name.localeCompare(b.name)).forEach((c) => g.appendChild(new Option(`${c.name} (${fmt(c.size)})`, c.id)));
    sel.appendChild(g);
  };
  grp(state.f.theme ? "Communities in this theme" : "Stories", story);
  grp("Interview mechanics", meta);
  sel.value = state.f.community;
  if (sel.value !== state.f.community) sel.value = "";
}

// the panel's controls show the state; the pickers only add to it
function syncPanel() {
  $("q").value = state.q;
  $("f-category").value = state.f.category;
  ["year_from", "year_to", "interview_year_from", "interview_year_to"].forEach((k) => { $(`f-${k}`).value = state.f[k]; });
  $("f-theme").value = state.f.theme;
  if (facets) fillCommunities();
  $("f-k").value = String(state.k);
  document.querySelector(`input[name="rerank"][value="${state.rerank ? 1 : 0}"]`).checked = true;
  document.querySelectorAll("#collections button[data-c]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.c === state.f.collection)));
  const panelFacet = ["category", "theme", "community", "interviewee", "doc_id", "year_from", "year_to", "interview_year_from", "interview_year_to"]
    .some((k) => state.f[k]) || LIST.some((k) => state.f[k].length) || !state.rerank || state.k !== 15;
  if (panelFacet && $("panel").hidden) togglePanel(true);
  renderChips();
}
function togglePanel(open) {
  const panel = $("panel"), btn = $("morefilters");
  const show = open ?? panel.hidden;
  panel.hidden = !show;
  btn.setAttribute("aria-expanded", String(show));
  btn.textContent = show ? "Fewer filters" : "More filters";
}

// the active filters as removable chips (collection has its own row)
function renderChips() {
  const box = $("active"), chips = [];
  const chip = (kind, value, label) => chips.push(
    `<button class="lj-rel chip" type="button" aria-pressed="true" data-kind="${kind}" data-value="${esc(value)}"
       aria-label="Remove filter ${esc(KIND_LABEL[kind])}: ${esc(label)}">${esc(KIND_LABEL[kind])}<span>${esc(label)}</span><span class="x" aria-hidden="true">×</span></button>`);
  SCALAR.filter((k) => k !== "collection" && state.f[k]).forEach((k) => {
    const v = state.f[k];
    const label = k === "category" ? catLabel(v) : k === "theme" ? (themeOf(v)?.name || `theme ${v}`)
                : k === "community" ? (communityOf(v)?.name || `community ${v}`) : v;
    chip(k, v, label);
  });
  LIST.forEach((k) => state.f[k].forEach((v) => chip(k, v, k === "qid" ? (names[v] || v) : v)));
  box.hidden = !chips.length;
  box.innerHTML = chips.length ? `<span class="label">Narrowed to</span>${chips.join("")}` : "";
  const note = $("story-note"), c = state.f.community ? communityOf(state.f.community) : null;
  note.hidden = !c?.narrative;
  note.textContent = c?.narrative || "";
  if (state.f.qid.some((q) => !names[q])) loadVocab().then(renderChips);   // names for qids that came from a link
}
$("active").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-kind]");
  if (!b) return;
  const { kind, value } = b.dataset;
  if (LIST.includes(kind)) state.f[kind] = state.f[kind].filter((v) => v !== value);
  else state.f[kind] = "";
  if (kind === "theme") state.f.community = "";
  syncPanel();
  run(true);
});

// ---------------------------------------------------------------- pickers (type-ahead on the vocabularies)
function loadVocab() {
  if (!vocabPromise) {
    vocabPromise = fetch("data/vocab.json").then((r) => r.json()).then((v) => {
      vocab = v;
      v.people.forEach((p) => { if (!names[p.q]) names[p.q] = p.n; });
      return v;
    }).catch(() => { vocabPromise = null; throw new Error("vocab"); });
  }
  return vocabPromise;
}
async function sourceFor(kind) {
  if (kind === "interviewee") return facets?.interviewees || [];
  const v = vocab || await loadVocab();
  if (kind === "people") return v.people.concat(v.unresolved);
  return v[kind] || [];
}
function match(items, needle, kind) {
  const n = needle.toLowerCase();
  const tier = (it) => {
    const s = it.n.toLowerCase();
    if (s.startsWith(n)) return 0;
    if (s.includes(" " + n) || s.includes("“" + n) || s.includes('"' + n)) return 1;
    if (s.includes(n)) return 2;
    if (kind === "people" && it.a?.some((a) => a.toLowerCase().includes(n))) return 3;
    return 9;
  };
  const out = [];
  for (const it of items) {
    const t = tier(it);
    if (t < 9) out.push([t, it]);
  }
  out.sort((a, b) => a[0] - b[0] || (b[1].s || b[1].d || 0) - (a[1].s || a[1].d || 0));
  return out.slice(0, 7).map((x) => x[1]);
}
function metaFor(it, kind, needle) {
  if (kind === "interviewee") {
    const colls = Object.keys(it.c).map((c) => COLL[c] || c).join(", ");
    const y = it.y ? (it.y[0] === it.y[1] ? `, ${it.y[0]}` : `, ${it.y[0]}–${it.y[1]}`) : "";
    return `${colls} · ${it.d} ${it.d === 1 ? "interview" : "interviews"}${y}`;
  }
  const parts = [];
  if (kind === "people") {
    const n = needle.toLowerCase(), alias = it.a?.find((a) => a.toLowerCase().includes(n) && !it.n.toLowerCase().includes(n));
    if (alias) parts.push(`as “${alias}”`);
    if (it.y) parts.push(it.y);
    else if (!it.q) parts.push("not in Wikidata");
  } else if (it.q) parts.push(it.q);
  parts.push(`${fmt(it.s)} ${it.s === 1 ? "passage" : "passages"}`);
  return parts.join(" · ");
}
function setupPicker(wrap) {
  const kind = wrap.dataset.kind, input = wrap.querySelector("input");
  let items = [], sel = -1, menu = null, seq = 0;
  const close = () => { if (menu) menu.remove(); menu = null; items = []; sel = -1; input.removeAttribute("aria-expanded"); };
  const paint = () => {
    menu?.querySelectorAll("li[role=option]").forEach((li, i) => li.setAttribute("aria-selected", String(i === sel)));
  };
  const show = (rows, note) => {
    close();
    menu = document.createElement("ul");
    menu.className = "lj-search-menu"; menu.setAttribute("role", "listbox");
    if (note) menu.innerHTML = `<li class="note">${esc(note)}</li>`;
    else menu.innerHTML = rows.map((it) => `<li role="option" aria-selected="false"><span>${esc(it.n)}</span><span>${esc(metaFor(it, kind, input.value.trim()))}</span></li>`).join("");
    menu.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus in the field
    menu.addEventListener("click", (e) => { const li = e.target.closest("li[role=option]"); if (li) pick(items[[...menu.querySelectorAll("li[role=option]")].indexOf(li)]); });
    wrap.appendChild(menu);
    input.setAttribute("aria-expanded", "true");
    items = rows; sel = rows.length ? 0 : -1; paint();
  };
  const pick = (it) => {
    if (!it) return;
    if (kind === "interviewee") state.f.interviewee = it.n;
    else if (kind === "people") {
      if (it.q) { names[it.q] = it.n; if (!state.f.qid.includes(it.q)) state.f.qid.push(it.q); }
      else if (!state.f.person.includes(it.n)) state.f.person.push(it.n);
    } else {
      const k = { places: "place", venues: "venue", bands: "band" }[kind];
      if (!state.f[k].includes(it.n)) state.f[k].push(it.n);
    }
    input.value = ""; close();
    syncPanel();
    run(true);
  };
  input.addEventListener("input", async () => {
    const needle = input.value.trim(), my = ++seq;
    if (needle.length < 2) { close(); return; }
    let src;
    try {
      if (kind !== "interviewee" && !vocab) show([], "Loading the names…");
      src = await sourceFor(kind);
    } catch (e) { if (my === seq) show([], "The names could not be loaded. Reload the page to try again."); return; }
    if (my !== seq) return;
    const rows = match(src, needle, kind);
    if (!rows.length) show([], kind === "interviewee" ? "No interviewee by that name." : "No name matches among those in three or more passages.");
    else show(rows);
  });
  input.addEventListener("keydown", (e) => {
    if (!menu || !items.length) { if (e.key === "Escape") close(); return; }
    if (e.key === "ArrowDown") { sel = (sel + 1) % items.length; paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = (sel - 1 + items.length) % items.length; paint(); e.preventDefault(); }
    else if (e.key === "Enter") { pick(items[sel]); e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}
document.querySelectorAll(".picker").forEach(setupPicker);

// ---------------------------------------------------------------- the controls
$("sform").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (q.length === 1) { setStatus("A search needs at least two characters.", true); return; }
  state.q = q;
  state.similar = null;
  run(true);
});
$("collections").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-c]");
  if (!b) return;
  state.f.collection = b.dataset.c;
  syncPanel();
  run(true);
});
$("morefilters").addEventListener("click", () => togglePanel());
$("f-category").addEventListener("change", (e) => { state.f.category = e.target.value; syncPanel(); run(true); });
$("f-theme").addEventListener("change", (e) => { state.f.theme = e.target.value; state.f.community = ""; syncPanel(); run(true); });
$("f-community").addEventListener("change", (e) => {
  state.f.community = e.target.value;
  const c = communityOf(e.target.value);
  if (c && !state.f.theme) state.f.theme = "";               // a community implies its theme; the API takes both
  syncPanel(); run(true);
});
["year_from", "year_to", "interview_year_from", "interview_year_to"].forEach((k) => {
  $(`f-${k}`).addEventListener("change", (e) => {
    const v = e.target.value.trim();
    state.f[k] = /^\d{4}$/.test(v) ? v : "";
    e.target.value = state.f[k];
    syncPanel(); run(true);
  });
});
document.querySelectorAll('input[name="rerank"]').forEach((r) => r.addEventListener("change", (e) => { state.rerank = e.target.value === "1"; run(true); }));
$("f-k").addEventListener("change", (e) => { state.k = +e.target.value; run(true); });
$("clear").addEventListener("click", () => { state.f = emptyFacets(); state.rerank = true; state.k = 15; syncPanel(); run(true); });
window.addEventListener("popstate", () => { readUrl(); syncPanel(); run(false); });

// ---------------------------------------------------------------- search
let inflight = 0;
function setStatus(text, error) {
  const s = $("status");
  s.textContent = text;
  s.classList.toggle("error", !!error);
}
function apiUrl() {
  const p = params();
  p.delete("k"); p.set("k", String(state.k));
  return `${API}?${p.toString()}`;
}
async function run(push) {
  writeUrl(push);
  const my = ++inflight;
  $("more").hidden = true;
  if (!state.q && !state.similar) {
    $("similar-head").hidden = true;
    $("count").textContent = "";
    setStatus("");
    renderEmpty();
    document.title = "Search the oral histories — Linked Jazz";
    return;
  }
  document.title = `${state.similar ? "Passages like one" : state.q} — Linked Jazz`;
  $("count").textContent = "";
  if (state.similar) renderSimilarHead();
  else { $("similar-head").hidden = true; lastSearchUrl = location.search; }
  setStatus(state.similar ? "Finding the nearest passages…"
          : state.rerank ? "Searching… the model rereads the closest matches, which takes a few seconds." : "Searching…");
  $("results").setAttribute("aria-busy", "true");
  const t0 = performance.now();
  let r, body;
  try {
    r = await fetch(apiUrl());
    body = await r.json();
  } catch (e) {
    if (my !== inflight) return;
    $("results").removeAttribute("aria-busy");
    setStatus("The search service could not be reached. Check the connection and try again.", true);
    return;
  }
  if (my !== inflight) return;
  $("results").removeAttribute("aria-busy");
  const secs = ((performance.now() - t0) / 1000).toFixed(1);
  if (r.status === 429) { setStatus(body.error === "daily capacity reached"
      ? "The search service has reached its daily limit. Try again tomorrow."
      : "More than ten searches in a minute from this connection. Wait a moment, then search again.", true); return; }
  if (r.status === 404) { setStatus("There is no passage with that id to compare with.", true); return; }
  if (!r.ok) { setStatus(body.error ? `The search failed: ${body.error}.` : `The search failed (${r.status}). Try again in a minute.`, true); return; }
  renderResults(body, secs);
}

// ---------------------------------------------------------------- rendering
function renderEmpty() {
  $("results").innerHTML = `<div class="empty">
    <h2>Try a question the musicians answered</h2>
    <ul class="examples">${EXAMPLES.map((q) => `<li><button class="lj-btn lj-btn-sm" type="button" data-q="${esc(q)}">${esc(q)}</button></li>`).join("")}</ul>
    <p>The search matches meaning rather than words, so “a bandleader who never paid the men” finds passages about
      wages, tips and leaders who skipped town, whether or not they use those words. Passages are the topic segments
      of each transcript, so a result is one stretch of conversation on one subject, with a title and a summary.</p>
    <p>Each result opens in place to read in full, links to its place in the transcript, and can start a new search
      for passages like it.</p>
    <ul class="facts">
      <li><b>${fmt(facets?.counts?.segments || 61357)}</b> passages</li>
      <li><b>${fmt(facets?.counts?.documents || 1314)}</b> interviews</li>
      <li><b>${facets?.collections.length || 4}</b> archives</li>
      <li><b>${fmt(facets?.communities.filter((c) => c.kind !== "meta").length || 94)}</b> story communities</li>
    </ul></div>`;
}
$("results").addEventListener("click", (e) => {
  const ex = e.target.closest("button[data-q]");
  if (ex) { state.q = ex.dataset.q; state.similar = null; $("q").value = state.q; run(true); return; }
  const f = e.target.closest("button[data-filter]");
  if (f) { addFilter(f.dataset.filter, f.dataset.value, f.dataset.label); return; }
  const read = e.target.closest("button[data-read]");
  if (read) { toggleRead(read.closest(".hit"), read); return; }
  const sim = e.target.closest("button[data-similar]");
  if (sim) { state.similar = sim.dataset.similar; state.same_doc = false; run(true); window.scrollTo({ top: 0 }); return; }
  const nav = e.target.closest("button[data-goto]");
  if (nav) { showPassage(nav.closest(".hit"), nav.dataset.goto); }
});
function addFilter(kind, value, label) {
  if (LIST.includes(kind)) { if (state.f[kind].includes(value)) return; state.f[kind].push(value); if (kind === "qid" && label) names[value] = label; }
  else { if (state.f[kind] === value) return; state.f[kind] = value; }
  syncPanel();
  run(true);
  window.scrollTo({ top: 0 });
}

const segTitle = (h) => {
  const t = h.title || "";
  const pre = `${h.interviewee} — `;
  return t.startsWith(pre) ? t.slice(pre.length) : t;
};
const readerUrl = (h) => `${READER}?t=${encodeURIComponent(h.doc_id)}#s${h.segment_id}`;

function nameGroup(label, kind, values, extra) {
  if (!values?.length) return "";
  return `<span class="group"><span class="label">${label}</span>${values.map((v, i) =>
    `${i ? ", " : ""}<button type="button" data-filter="${kind}" data-value="${esc(extra?.[i]?.value ?? v)}" data-label="${esc(v)}">${esc(v)}</button>`).join("")}</span>`;
}

function card(h, i, mode) {
  const coll = COLL[h.collection] || h.collection;
  const when = h.year ? `, ${h.year}` : "";
  const community = h.community != null ? communityOf(h.community) : null;
  const meta = [];
  if (h.category) meta.push(`<li>Topic <button type="button" data-filter="category" data-value="${esc(h.category)}">${esc(catLabel(h.category))}</button></li>`);
  if (h.period) meta.push(`<li>About <b>${esc(h.period)}</b></li>`);
  else if (h.year_from) meta.push(`<li>About <b>${h.year_from}${h.year_to && h.year_to !== h.year_from ? `–${h.year_to}` : ""}</b></li>`);
  if (h.chapter) meta.push(`<li>Chapter <b>${esc(h.chapter)}</b></li>`);
  if (community && community.kind !== "meta") meta.push(`<li>Story <button type="button" data-filter="community" data-value="${h.community}">${esc(community.name)}</button></li>`);
  let score = "";
  if (mode === "source") score = "";
  else if (mode === "similar") score = `${h.score.toFixed(2)} similarity to the passage above`;
  else {
    score = `${h.score.toFixed(2)} similarity`;
    if (mode === "reranked" && h.vrank !== h.rank) score += h.vrank > h.rank
      ? ` · the model moved it up from ${ordinal(h.vrank + 1)}` : ` · the model moved it down from ${ordinal(h.vrank + 1)}`;
    else if (mode === "reranked") score += " · kept here by the model";
  }
  return `<li class="hit" id="hit-${h.segment_id}" data-id="${h.segment_id}">
    <div class="hit-head">
      <p class="who"><button type="button" data-filter="interviewee" data-value="${esc(h.interviewee)}"><b>${esc(h.interviewee)}</b></button> · ${esc(coll)}${when}</p>
      ${i != null ? `<span class="rank">${i + 1}</span>` : ""}
    </div>
    <h2><button type="button" data-read aria-expanded="false">${esc(segTitle(h))}</button></h2>
    ${h.summary ? `<p class="sum">${esc(h.summary)}</p>` : ""}
    ${h.snippet ? `<p class="snip">${esc(h.snippet)}</p>` : ""}
    ${meta.length ? `<ul class="meta">${meta.join("")}</ul>` : ""}
    ${(h.places?.length || h.venues?.length || h.bands?.length || h.people?.length) ? `<p class="names">
      ${nameGroup("Places", "place", h.places)}${nameGroup("Venues", "venue", h.venues)}${nameGroup("Bands", "band", h.bands)}${nameGroup("People", "person", h.people)}</p>` : ""}
    <div class="hit-foot">
      ${score ? `<span class="score">${score}</span>` : `<span class="score"></span>`}
      <button class="lj-btn lj-btn-sm" type="button" data-read aria-pressed="false">Read the passage</button>
      ${mode === "source" ? "" : `<button class="lj-btn lj-btn-sm" type="button" data-similar="${h.segment_id}">More like this</button>`}
      <a class="lj-btn lj-btn-sm" href="${esc(readerUrl(h))}" target="_blank" rel="noopener">Open in the transcript${ICON_OUT}</a>
    </div>
    <div class="passage" hidden></div>
  </li>`;
}

function renderResults(body, secs) {
  const hits = body.results || [];
  const similar = state.similar != null;
  const mode = similar ? "similar" : body.reranked ? "reranked" : "vector";
  $("count").textContent = hits.length ? `${fmt(hits.length)} ${hits.length === 1 ? "passage" : "passages"}` : "";
  const parts = [];
  if (!hits.length) parts.push(similar ? "No other passage is close enough with these filters." : "No passage matches these filters. Remove one and search again.");
  else {
    parts.push(`${fmt(hits.length)} ${hits.length === 1 ? "passage" : "passages"}`);
    if (!similar && body.candidates < state.k) parts.push(`only ${fmt(body.candidates)} match these filters`);
    if (mode === "reranked") parts.push("reread and reordered by the model");
    else if (!similar && state.rerank) parts.push("in similarity order; the model did not answer this time");
    else parts.push("in similarity order");
    parts.push(`${secs} s`);
    if (!similar && hits[0].score < RELEVANT) parts.push("nothing in the interviews matches this closely, so these are the nearest passages");
  }
  setStatus(parts.join(" · "));
  $("results").innerHTML = hits.length ? `<ol class="hits">${hits.map((h, i) => card(h, i, mode)).join("")}</ol>` : "";
  const more = $("more");
  if (hits.length >= state.k && state.k < 50) {
    const next = state.k === 15 ? 30 : 50;
    more.innerHTML = `<button class="lj-btn" type="button" id="morebtn">Show ${next} passages</button>`;
    more.hidden = false;
    $("morebtn").addEventListener("click", () => { state.k = next; $("f-k").value = String(next); run(true); });
  } else more.hidden = true;
}

// ---------------------------------------------------------------- "more like this"
async function renderSimilarHead() {
  const head = $("similar-head");
  head.hidden = false;
  head.innerHTML = `<span class="label">Passages like</span><span class="what">passage ${esc(state.similar)}…</span>`;
  let s;
  try { s = await loadSeg(state.similar); } catch (e) { s = null; }
  if (String(state.similar) !== String(s?.id)) return;
  const h = hitFromSeg(s);
  head.innerHTML = `<span class="label">Passages like</span>
    <span class="what"><b>${esc(s.title)}</b> · ${esc(s.doc.interviewee)}, ${esc(COLL[s.doc.collection] || s.doc.collection)}${s.doc.year ? `, ${s.doc.year}` : ""}</span>
    <button class="lj-rel" type="button" id="samedoc" aria-pressed="${state.same_doc}">Include the same interview</button>
    ${lastSearchUrl != null ? `<a class="back" href="${esc(lastSearchUrl || "./")}" id="back">Back to the search</a>` : `<a class="back" href="./">New search</a>`}
    <ol class="hits" style="flex-basis:100%">${card(h, null, "source")}</ol>`;
  $("samedoc").addEventListener("click", () => { state.same_doc = !state.same_doc; run(true); });
  $("back")?.addEventListener("click", (e) => { e.preventDefault(); history.pushState(null, "", lastSearchUrl || location.pathname); readUrl(); syncPanel(); run(false); });
  head.addEventListener("click", (e) => {
    const read = e.target.closest("button[data-read]"); if (read) { toggleRead(read.closest(".hit"), read); return; }
    const f = e.target.closest("button[data-filter]"); if (f) { addFilter(f.dataset.filter, f.dataset.value, f.dataset.label); return; }
    const nav = e.target.closest("button[data-goto]"); if (nav) showPassage(nav.closest(".hit"), nav.dataset.goto);
  });
}
function hitFromSeg(s) {
  return { segment_id: s.id, doc_id: s.doc.id, collection: s.doc.collection, interviewee: s.doc.interviewee, year: s.doc.year,
    category: s.category, title: s.title, summary: s.summary, chapter: s.chapter, period: s.period, year_from: s.year_from, year_to: s.year_to,
    community: s.story?.community, theme: s.story?.theme, score: 1, vrank: 0, rank: 0,
    places: s.tags?.places?.map((t) => t.name), venues: s.tags?.venues?.map((t) => t.name), bands: s.tags?.bands?.map((t) => t.name),
    people: s.people?.map((p) => p.name) };
}

// ---------------------------------------------------------------- the passage in full (reader data)
const segCache = new Map();
function loadSeg(id) {
  if (!segCache.has(id)) {
    segCache.set(id, fetch(`${READER}data/seg/${id}.json`).then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .catch((e) => { segCache.delete(id); throw e; }));
  }
  return segCache.get(id);
}
function toggleRead(hit, btn) {
  const box = hit.querySelector(".passage");
  const open = box.hidden;
  hit.querySelectorAll("button[data-read]").forEach((b) => { if (b.hasAttribute("aria-pressed")) b.setAttribute("aria-pressed", String(open)); else b.setAttribute("aria-expanded", String(open)); });
  const snip = hit.querySelector(".snip");
  if (snip) snip.hidden = open;                 // the passage in full replaces its excerpt
  const label = hit.querySelector(".hit-foot button[data-read]");
  if (!open) { box.hidden = true; label.textContent = "Read the passage"; return; }
  label.textContent = "Close the passage";
  box.hidden = false;
  showPassage(hit, hit.dataset.id);
}
async function showPassage(hit, id) {
  const box = hit.querySelector(".passage");
  box.hidden = false;
  box.innerHTML = `<p class="caption">Loading the passage…</p>`;
  let s;
  try { s = await loadSeg(id); }
  catch (e) { box.innerHTML = `<p class="status error">The passage could not be loaded from the transcript site. <a href="${esc(READER)}?t=${esc(hit.dataset.doc || "")}" target="_blank" rel="noopener">Open the transcript</a> instead.</p>`; return; }
  if (box.hidden) return;
  const own = String(s.id) === String(hit.dataset.id);
  const turns = (s.blocks || []).map((b) => `<div class="turn"><div class="spk">${esc(b.s)}</div><p class="tx">${esc(b.x)}</p></div>`).join("");
  const people = (s.people || []).filter((p) => p.name);
  const peopleLine = people.length ? `<p class="names"><span class="label">People in this passage</span>${people.map((p, i) =>
    `${i ? ", " : ""}<button type="button" data-filter="${p.qid ? "qid" : "person"}" data-value="${esc(p.qid || p.name)}" data-label="${esc(p.name)}">${esc(p.name)}</button>`).join("")}</p>` : "";
  const src = [s.doc.inst, s.doc.interviewer ? `interviewed by ${s.doc.interviewer}` : "", s.doc.date || s.doc.year || ""].filter(Boolean).join(", ");
  box.innerHTML = `
    <p class="where">${own ? "" : `<span>Now reading <b>${esc(s.title)}</b></span>`}
      <span>Passage <b>${s.ord + 1}</b> of ${fmt(s.n_segments)} in this interview</span>
      ${s.words ? `<span><b>${fmt(s.words)}</b> words</span>` : ""}${s.period ? `<span>about <b>${esc(s.period)}</b></span>` : ""}</p>
    ${turns || `<p class="caption">This segment has no transcript text.</p>`}
    <div class="nav">
      ${s.prev != null ? `<button type="button" data-goto="${s.prev}">← Previous passage</button>` : "<span></span>"}
      ${s.next != null ? `<button type="button" class="next" data-goto="${s.next}">Next passage →</button>` : ""}
    </div>
    ${peopleLine}
    <p class="source">${esc(src)} · <a href="${esc(s.url)}" target="_blank" rel="noopener">Open in the transcript</a>
      ${s.doc.source_url ? ` · <a href="${esc(s.doc.source_url)}" target="_blank" rel="noopener">The original at the archive</a>` : ""}</p>`;
  if (!own) box.scrollIntoView({ block: "start", behavior: "smooth" });
}

// ---------------------------------------------------------------- start
(async function start() {
  readUrl();
  await loadFacets();
  syncPanel();
  run(false);
})();
