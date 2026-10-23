#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy", "scikit-learn"]
# ///
"""Turn the HDBSCAN levels into a three-level story hierarchy + naming packs.

Levels (data/clusters/centered/hdbscan-mcs{N}.jsonl):
  theme      mcs200   (~54)    broad narrative themes
  community  mcs50    (~118)   the story communities — the main unit
  fine       mcs15    (~214)   sub-threads
  story      mcs10, min_samples 3 (~490)   the story map's level, attached to a community
Each community is attached to the theme most of its core members fall in;
each fine cluster to its community likewise.

Noise points (44% at the community level) get a *peripheral* assignment: the
majority label among their 15 nearest HDBSCAN-core neighbours in UMAP space
(vote share and distance kept so consumers can threshold). Core points keep
HDBSCAN's label and membership probability.

Writes data/clusters/
  segment_communities.jsonl   docid, segment_id, doc_id, ord, per level: id, membership
                              (core|peripheral), prob (HDBSCAN prob or vote share), dist
  packs/{level}-{id}.json     what a naming agent needs: stats, facets, period,
                              interviews, 40 sample titles+summaries (top-prob + spread)
  hierarchy.json              theme -> communities -> fine, with sizes
"""
import gzip, json, random
from collections import Counter, defaultdict
from pathlib import Path
import numpy as np
from sklearn.neighbors import NearestNeighbors

ROOT = Path(__file__).resolve().parent.parent / "data"
CL = ROOT / "clusters" / "centered"
OUT = ROOT / "clusters"
PACKS = OUT / "packs"; PACKS.mkdir(exist_ok=True)
LEVELS = {"theme": 200, "community": 50, "fine": 15, "story": 10}   # story: mcs 10, min_samples 3 (the map's level)
K = 15

meta = {}
with gzip.open(ROOT / "docs.jsonl.gz", "rt") as f:
    for line in f:
        d = json.loads(line); meta[d["docid"]] = d
emb = np.fromfile(CL / "umap10.f32", dtype=np.float32).reshape(-1, 10)
labels, probs, keys = {}, {}, None
for lv, mcs in LEVELS.items():
    rows = [json.loads(l) for l in open(CL / f"hdbscan-mcs{mcs}.jsonl")]
    if keys is None:
        keys = [r["docid"] for r in rows]
    labels[lv] = np.array([r["cluster"] for r in rows]); probs[lv] = np.array([r["prob"] for r in rows])

# peripheral assignment per level
assign = {}
for lv in LEVELS:
    lab = labels[lv]
    core = np.flatnonzero(lab >= 0); noise = np.flatnonzero(lab < 0)
    nn = NearestNeighbors(n_neighbors=K).fit(emb[core])
    dist, idx = nn.kneighbors(emb[noise])
    votes = lab[core][idx]
    maj = np.array([np.bincount(v).argmax() for v in votes])
    share = np.array([(v == m).mean() for v, m in zip(votes, maj)])
    a = {"id": lab.copy(), "member": np.where(lab >= 0, "core", "peripheral"),
         "prob": probs[lv].copy(), "dist": np.zeros(len(lab), np.float32)}
    a["id"][noise] = maj; a["prob"][noise] = share; a["dist"][noise] = dist.mean(1)
    assign[lv] = a
    print(f"{lv}: {lab.max()+1} clusters, {len(core)} core, {len(noise)} peripheral "
          f"(vote share median {np.median(share):.2f})")

# hierarchy: each child cluster's CORE members vote with their parent-level
# assignment (core or peripheral), so every child gets a parent
def parent_map(child, parent):
    m = defaultdict(Counter)
    for c, p in zip(labels[child], assign[parent]["id"]):
        if c >= 0:
            m[c][int(p)] += 1
    return {int(c): int(v.most_common(1)[0][0]) for c, v in m.items()}
comm_theme = parent_map("community", "theme")
fine_comm = parent_map("fine", "community")
story_comm = parent_map("story", "community")

with open(OUT / "segment_communities.jsonl", "w") as f:
    for i, k in enumerate(keys):
        rec = {"docid": k, "segment_id": meta[k]["segment_id"], "doc_id": meta[k]["doc_id"], "ord": meta[k]["ord"]}
        for lv in LEVELS:
            a = assign[lv]
            rec[lv] = {"id": int(a["id"][i]), "member": str(a["member"][i]),
                       "prob": round(float(a["prob"][i]), 3), "dist": round(float(a["dist"][i]), 3)}
        f.write(json.dumps(rec) + "\n")

random.seed(3)
hier = {"themes": {}}
for lv in LEVELS:
    a = assign[lv]; lab = labels[lv]
    for c in range(lab.max() + 1):
        core = np.flatnonzero(lab == c); allm = np.flatnonzero(a["id"] == c)
        docs = [meta[keys[i]] for i in allm]
        cdocs = [meta[keys[i]] for i in core]
        itv = Counter(d["interviewee"] or d["doc_id"] for d in docs)
        years = sorted(d["year_from"] for d in cdocs if d["year_from"])
        top = core[np.argsort(-probs[lv][core])[:20]]
        rest = [i for i in core if i not in set(top)]
        spread = random.sample(rest, min(20, len(rest)))
        samples = [{"segment_id": meta[keys[i]]["segment_id"], "interviewee": meta[keys[i]]["interviewee"],
                    "title": meta[keys[i]]["segment_title"], "period": meta[keys[i]]["period"],
                    "summary": meta[keys[i]]["summary"]} for i in list(top) + spread]
        pack = {"level": lv, "id": int(c), "core_size": int(len(core)), "total_size": int(len(allm)),
                "n_interviews": len(itv),
                "collections": dict(Counter(d["collection"] for d in docs)),
                "categories": dict(Counter(d["category"] for d in cdocs).most_common()),
                "period_10_90": [years[len(years)//10], years[-max(1, len(years)//10)]] if len(years) >= 5 else None,
                "top_interviewees": itv.most_common(8),
                "facets": {k: Counter(x for d in cdocs for x in d[k]).most_common(8)
                           for k in ("places", "venues", "bands", "people")},
                "samples": samples}
        if lv == "community":
            pack["theme"] = comm_theme.get(c, -1)
        if lv in ("fine", "story"):
            pack["community"] = (fine_comm if lv == "fine" else story_comm).get(c, -1)
        json.dump(pack, open(PACKS / f"{lv}-{c}.json", "w"), indent=1, ensure_ascii=False)
for t in range(labels["theme"].max() + 1):
    hier["themes"][t] = {"core_size": int((labels["theme"] == t).sum()),
                         "communities": {c: {"core_size": int((labels["community"] == c).sum()),
                                             "fine": [f for f, cc in fine_comm.items() if cc == c]}
                                         for c, tt in comm_theme.items() if tt == t}}
hier["orphan_communities"] = [c for c, t in comm_theme.items() if t == -1]
json.dump(hier, open(OUT / "hierarchy.json", "w"), indent=1)
print(f"packs: {len(list(PACKS.glob('*.json')))}; communities without theme: {hier['orphan_communities']}")
