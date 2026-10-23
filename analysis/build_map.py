#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy", "scikit-learn"]
# ///
"""The community map: a 2-D layout of the story communities and the people who tell them.

  uv run analysis/build_map.py

One disc per story (the "story" HDBSCAN level: min cluster size 10, min_samples 3, ~490
clusters; `--level community` for the 118 mcs50 communities; meta "interview mechanics"
clusters left out), placed by metric MDS on the cosine distances between community centroids (per-
interview centered clustering vectors, as in cluster_hdbscan.py), then pushed apart until
no two overlap. Inside a community, one icon per interviewee with at least one CORE segment
in it, placed by PCA(2) of their mean 10-d UMAP position among that community's people and
collision-resolved; icon radius grows with the number of passages.

Writes
  site/data/map.json          communities (position, radius, name, narrative, threads,
                              period, facets) with their people icons; the people list
                              (name, archives, QID, which communities they appear in)
  site/data/map/c<id>.json    per community: every person's passages there (title, summary,
                              period, a quote in the teller's own words, reader link), most
                              central first — fetched by the page on hover
"""
import gzip, json, re, sqlite3, sys, time
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
from sklearn.decomposition import PCA
from sklearn.manifold import MDS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = ROOT.parent / "ch-jazz-mashup" / "linked_jazz.sqlite"
SITE = ROOT / "site" / "data"
(SITE / "map").mkdir(parents=True, exist_ok=True)
for old in (SITE / "map").glob("c*.json"):          # a different level has different ids
    old.unlink()
DIM, UD = 1536, 10
QUOTE_CHARS = 300
LEVEL = sys.argv[sys.argv.index("--level") + 1] if "--level" in sys.argv else "story"
PARENT_LEVEL = {"story": "community", "community": "theme", "fine": "community"}[LEVEL]
t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.0f}s] {m}", file=sys.stderr, flush=True)

# ---------------------------------------------------------------- inputs
keys = json.load(open(DATA / "vectors" / "clustering-keys.json"))
kpos = {k: i for i, k in enumerate(keys)}
meta = {}
with gzip.open(DATA / "docs.jsonl.gz", "rt") as f:
    for line in f:
        d = json.loads(line); meta[d["docid"]] = d
clusters = {(c["level"], c["id"]): c for c in json.load(open(DATA / "clusters" / "story_clusters.json"))}
comms = {cid: c for (lv, cid), c in clusters.items() if lv == LEVEL and c["kind"] != "meta"}
log(f"{len(comms)} {LEVEL} clusters (meta left out), {len(keys)} vectors")

# core members per community, one entry per segment (parts collapsed to part 0)
members = defaultdict(list)                      # cid -> [(docid, prob)]
for line in open(DATA / "clusters" / "segment_communities.jsonl"):
    r = json.loads(line)
    c = r[LEVEL]
    if c["member"] != "core" or c["id"] not in comms or ".p" in r["docid"]:
        continue
    members[c["id"]].append((r["docid"], c["prob"]))

umap = np.fromfile(DATA / "clusters" / "centered" / "umap10.f32", dtype=np.float32).reshape(-1, UD)

con = sqlite3.connect(DB)
qid_of = {}                                      # interviewee display name -> QID (principal)
for name, qid in con.execute("""SELECT d.interviewee, di.qid FROM documents d
        JOIN doc_interviewees di ON di.doc_id = d.doc_id AND di.role = 'principal'
        WHERE di.qid IS NOT NULL AND d.interviewee IS NOT NULL"""):
    qid_of.setdefault(name, qid)

# ---------------------------------------------------------------- community centroids -> 2-D
X = np.fromfile(DATA / "vectors" / "clustering.f32", dtype=np.float32).reshape(-1, DIM)
who = np.array([meta[k]["doc_id"] for k in keys])
for d in np.unique(who):
    idx = np.flatnonzero(who == d)
    if len(idx) >= 3:
        X[idx] -= X[idx].mean(0)
X /= np.linalg.norm(X, axis=1, keepdims=True)
cids = sorted(comms)
C = np.stack([X[[kpos[k] for k, _ in members[c]]].mean(0) for c in cids])
C /= np.linalg.norm(C, axis=1, keepdims=True)
del X
D = 1 - C @ C.T
np.fill_diagonal(D, 0); D = np.clip(D, 0, None)
pos = MDS(n_components=2, dissimilarity="precomputed", random_state=0, n_init=8, max_iter=600,
          normalized_stress="auto").fit_transform(D)
log("community centroids placed (MDS)")


# ---------------------------------------------------------------- collision solver
def relax(P, r, iters=300, pull=0.004, pad=0.0):
    """Push overlapping discs apart; a weak pull to the origin keeps the cloud compact."""
    P = P.astype(np.float64).copy()
    n = len(P)
    R = (r[:, None] + r[None, :]) + pad
    np.fill_diagonal(R, 0)
    for it in range(iters):
        d = P[:, None, :] - P[None, :, :]
        dist = np.sqrt((d ** 2).sum(-1)) + 1e-9
        over = np.clip(R - dist, 0, None)
        np.fill_diagonal(over, 0)
        if it > 40 and over.max() < 0.05:
            break
        push = (d / dist[..., None]) * (over * 0.5)[..., None]
        # ties: jitter identical positions
        same = (dist < 1e-6) & (R > 0)
        if same.any():
            P += np.random.default_rng(it).normal(0, 0.1, P.shape)
        P += push.sum(1)
        P -= P * pull
    return P


# ---------------------------------------------------------------- people inside each community
people_ix, people = {}, []                       # name -> index, [{n, c:set, q}]
def person(name, coll):
    if name not in people_ix:
        people_ix[name] = len(people)
        people.append({"n": name, "c": Counter(), "q": qid_of.get(name), "i": []})
    p = people[people_ix[name]]
    p["c"][coll] += 1
    return people_ix[name]


HONORIFIC = re.compile(r"^(mr|mrs|ms|miss|dr|prof|rev)\.?\s+", re.I)
INTERVIEWER = re.compile(r"^(interviewer|q|question|host|moderator|speaker[_ ]?\d+|unknown|unidentified)\b", re.I)


def norm_label(lbl):
    return re.sub(r"[^a-z ]", "", HONORIFIC.sub("", lbl.strip()).lower()).strip()


def teller_label(d, turns):
    """Which speaker label is the interviewee. Transcripts label speakers every which way
    ("MR. HERMAN", "Brown", "Q"/"A", "INTERVIEWER", a bare surname, names surname-first), so:
    a label sharing a name token with the interviewee wins; else "A"; else, among labels that
    are not interviewer-like, whoever says the most words (the interviewee talks most)."""
    words = {}
    for lbl, txt in turns:
        if lbl:
            words[lbl] = words.get(lbl, 0) + len(txt.split())
    if not words:
        return None
    toks = {t for t in norm_label(d.get("interviewee") or "").split() if len(t) > 2}
    squashed = {t.replace(" ", "") for t in toks}
    def matches(lbl):
        n = norm_label(lbl)
        return bool(toks & set(n.split())) or any(t in n.replace(" ", "") for t in squashed if len(t) > 3)
    cands = [l for l in words if matches(l) and not INTERVIEWER.match(l)]
    if cands:
        return max(cands, key=words.get)
    if any(norm_label(l) == "a" for l in words):
        return next(l for l in words if norm_label(l) == "a")
    rest = [l for l in words if not INTERVIEWER.match(l) and norm_label(l) != "a"]
    if len(words) >= 2 and rest:
        return max(rest, key=words.get)
    if len(words) == 1 and rest:
        return rest[0]
    return None


def quote(d):
    """The first substantial thing the teller says in the segment (never the interviewer's
    question), cut at a word boundary."""
    text = d["text"]
    i = text.find("\n## Transcript\n")
    lines = [l for l in text[i + 15:].split("\n") if l.strip()] if i >= 0 else []
    turns = []
    for l in lines:
        m = re.match(r"([^:]{1,40}):\s*(.*)", l)
        turns.append((m.group(1).strip(), m.group(2)) if m else ("", l))
    teller = teller_label(d, turns)
    own = [t for lbl, t in turns if (lbl == teller if teller else not lbl) and t.strip()]
    if not own:
        return ""
    statements = [t for t in own if not t.rstrip().endswith("?")] or own
    line = next((t for t in statements if len(t) >= 120), max(statements, key=len))
    line = " ".join(line.split())
    if len(line) > QUOTE_CHARS:
        line = line[:QUOTE_CHARS].rsplit(" ", 1)[0] + "…"
    return line


out_comms, radii = [], []
for c in cids:
    info = comms[c]
    by_person = defaultdict(list)
    for k, prob in members[c]:
        d = meta[k]
        by_person[d["interviewee"] or d["doc_id"]].append((k, prob))
    names = sorted(by_person, key=lambda n: -len(by_person[n]))
    n_seg = np.array([len(by_person[n]) for n in names], dtype=float)
    r = np.clip(5 + 2.4 * np.sqrt(n_seg), 6, 18)
    U = np.stack([umap[[kpos[k] for k, _ in by_person[n]]].mean(0) for n in names])
    P2 = PCA(n_components=2, random_state=0).fit_transform(U) if len(names) > 2 else np.zeros((len(names), 2))
    # scale so the discs fill about 55% of a circle around the centre, then settle collisions
    area = (np.pi * r ** 2).sum()
    R0 = np.sqrt(area / (np.pi * 0.55))
    spread = np.sqrt((P2 ** 2).sum(1)).mean() or 1.0
    P2 = P2 * (R0 * 0.75 / spread)
    P2 = relax(P2, r, iters=250, pull=0.01, pad=1.5)
    P2 = relax(P2, r, iters=120, pull=0.0, pad=1.5)      # clear the residual overlaps
    P2 -= P2.mean(0)
    rad = float(max((np.sqrt((P2 ** 2).sum(1)) + r).max() + 10, 28))
    radii.append(rad)
    icons = []
    seg_lists = {}
    for j, n in enumerate(names):
        segs = sorted(by_person[n], key=lambda kp: -kp[1])
        d0 = meta[segs[0][0]]
        pi = person(n, d0["collection"])
        people[pi]["i"].append(c)
        icons.append([pi, round(float(P2[j, 0]), 1), round(float(P2[j, 1]), 1), round(float(r[j]), 1),
                      int(n_seg[j]), int(d0["segment_id"])])
        seg_lists[str(pi)] = [{
            "id": meta[k]["segment_id"], "t": meta[k]["segment_title"], "s": meta[k]["summary"],
            "p": meta[k]["period"], "y": meta[k]["year"], "ch": meta[k]["chapter"], "q": quote(meta[k]),
            "u": meta[k]["url"], "pr": round(prob, 2), "a": meta[k]["collection"],
        } for k, prob in segs]
    json.dump({"id": c, "people": seg_lists}, open(SITE / "map" / f"c{c}.json", "w"), ensure_ascii=False,
              separators=(",", ":"))
    parent = clusters.get((PARENT_LEVEL, info["parent_id"]))
    out_comms.append({
        "id": c, "name": info["name"] or f"Story {c}", "kind": info["kind"], "coherence": info["coherence"],
        "parent": info["parent_id"], "parent_name": parent["name"] if parent else None,
        "narrative": info["narrative"], "threads": info["threads"], "period": info["period"],
        "n_people": len(names), "n_segments": int(n_seg.sum()), "n_interviews": info["n_interviews"],
        "facets": {k: v[:5] for k, v in info["facets"].items()},
        "collections": info["collections"], "r": round(rad, 1), "people": icons,
    })
log(f"{len(people)} people placed in {len(out_comms)} communities")

# ---------------------------------------------------------------- place the communities
R = np.array(radii)
pos = pos - pos.mean(0)
# start compact: the MDS cloud scaled so its extent is about the sum of a row of discs
ext = np.abs(pos).max() or 1.0
pos = pos * (R.sum() * 0.22 / ext)
pos = relax(pos, R, iters=600, pull=0.003, pad=34)   # pad leaves room for the label above
pos = relax(pos, R, iters=200, pull=0.0, pad=34)
pos -= pos.mean(0)
for cm, (x, y) in zip(out_comms, pos):
    cm["x"], cm["y"] = round(float(x), 1), round(float(y), 1)
# closest pairs (for a sanity check) and the theme adjacency
near = []
for i in range(len(cids)):
    d = np.sqrt(((pos - pos[i]) ** 2).sum(1)) - R - R[i]; d[i] = np.inf
    near.append(float(d.min()))
log(f"community discs: radius {R.min():.0f}–{R.max():.0f}, nearest gap median {np.median(near):.0f}px, "
    f"extent {np.ptp(pos[:,0]) + 2*R.max():.0f} x {np.ptp(pos[:,1]) + 2*R.max():.0f}")

for p in people:
    p["c"] = sorted(p["c"], key=lambda k: -p["c"][k])
out = {
    "built": time.strftime("%Y-%m-%d"), "level": LEVEL,
    "counts": {"communities": len(out_comms), "people": len(people),
               "segments": int(sum(c["n_segments"] for c in out_comms)), "icons": int(sum(len(c["people"]) for c in out_comms))},
    "parents": {str(cid): cl["name"] for (lv, cid), cl in clusters.items() if lv == PARENT_LEVEL},
    "communities": out_comms, "people": people,
}
json.dump(out, open(SITE / "map.json", "w"), ensure_ascii=False, separators=(",", ":"))
log(f"wrote site/data/map.json ({(SITE / 'map.json').stat().st_size/1e6:.1f} MB) and "
    f"{len(out_comms)} snippet files ({sum(p.stat().st_size for p in (SITE/'map').glob('c*.json'))/1e6:.1f} MB)")
