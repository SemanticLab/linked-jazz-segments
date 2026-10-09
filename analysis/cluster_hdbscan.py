#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy", "scikit-learn", "umap-learn", "hdbscan"]
# ///
"""Story communities: HDBSCAN over the (per-interview centered) clustering vectors.

  uv run analysis/cluster_hdbscan.py            # all levels
  uv run analysis/cluster_hdbscan.py --no-center  # ablation: raw vectors

Per-interview mean-centering first (subtract each interview's mean vector,
renormalize): raw vectors have 72% same-interview nearest neighbours, centered
11% (README). Then PCA(100) -> UMAP(10d, cosine, cached data/clusters/umap10.f32)
-> HDBSCAN (min_samples 10, eom) at min_cluster_size 15 / 50 / 200.

Outputs in data/clusters/:
  hdbscan-mcs{N}.jsonl          docid, cluster, membership probability
  hdbscan-mcs{N}-summary.json   per cluster: size, persistence, category /
                                collection / interview mix, top interviews,
                                period range, top facets, sample titles+summaries
Purity flags in the summary: `one_interview` (one interview >= 50% of the
cluster), `one_collection` (>= 98%).
"""
import gzip, json, sys, time
from collections import Counter
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent / "data"
DIM, UMAP_DIMS, LEVELS = 1536, 10, [15, 50, 200]
CENTER = "--no-center" not in sys.argv
out_dir = ROOT / "clusters" / ("centered" if CENTER else "raw")
out_dir.mkdir(parents=True, exist_ok=True)

keys = json.load(open(ROOT / "vectors" / "clustering-keys.json"))
meta = {}
with gzip.open(ROOT / "docs.jsonl.gz", "rt") as f:
    for line in f:
        d = json.loads(line)
        meta[d["docid"]] = d
t0 = time.time()


def log(msg):
    print(f"[{time.time()-t0:6.0f}s] {msg}", file=sys.stderr, flush=True)


umap_path = out_dir / "umap10.f32"
if umap_path.exists():
    emb = np.fromfile(umap_path, dtype=np.float32).reshape(-1, UMAP_DIMS)
    log(f"loaded cached UMAP embedding {emb.shape}")
else:
    X = np.fromfile(ROOT / "vectors" / "clustering.f32", dtype=np.float32).reshape(-1, DIM)
    log(f"loaded vectors {X.shape}")
    if CENTER:
        who = np.array([meta[k]["doc_id"] for k in keys])
        for d in np.unique(who):
            idx = np.flatnonzero(who == d)
            if len(idx) >= 3:
                X[idx] -= X[idx].mean(0)
        X /= np.linalg.norm(X, axis=1, keepdims=True)
        log("per-interview centered")
    from sklearn.decomposition import PCA
    Xp = PCA(n_components=100, svd_solver="randomized", random_state=0).fit_transform(X).astype(np.float32)
    del X
    log("PCA -> 100 dims")
    import umap
    emb = umap.UMAP(n_components=UMAP_DIMS, n_neighbors=30, min_dist=0.0, metric="cosine",
                    random_state=0, verbose=False).fit_transform(Xp).astype(np.float32)
    emb.tofile(umap_path)
    log(f"UMAP -> {UMAP_DIMS} dims, cached")

import hdbscan
for mcs in LEVELS:
    cl = hdbscan.HDBSCAN(min_cluster_size=mcs, min_samples=10, core_dist_n_jobs=-1,
                         cluster_selection_method="eom")
    labels = cl.fit_predict(emb.astype(np.float64))
    probs, pers = cl.probabilities_, cl.cluster_persistence_
    n = int(labels.max()) + 1
    noise = float((labels == -1).mean())
    log(f"mcs={mcs}: {n} clusters, {noise:.1%} noise")
    with open(out_dir / f"hdbscan-mcs{mcs}.jsonl", "w") as f:
        for k, l, p in zip(keys, labels, probs):
            f.write(json.dumps({"docid": k, "cluster": int(l), "prob": round(float(p), 3)}) + "\n")
    summary = {"min_cluster_size": mcs, "min_samples": 10, "centered": CENTER, "n_docs": len(keys),
               "n_clusters": n, "noise_fraction": round(noise, 4), "clusters": []}
    order = np.argsort([-(labels == c).sum() for c in range(n)])
    for c in order:
        idx = np.flatnonzero(labels == c)
        docs = [meta[keys[i]] for i in idx]
        cats = Counter(d["category"] for d in docs)
        cols = Counter(d["collection"] for d in docs)
        itv = Counter(d["interviewee"] or d["doc_id"] for d in docs)
        years = [d["year_from"] for d in docs if d["year_from"]]
        facets = {k: Counter(x for d in docs for x in d[k]).most_common(6) for k in ("places", "venues", "bands", "people")}
        best = idx[np.argsort(-probs[idx])[:10]]
        summary["clusters"].append({
            "cluster": int(c), "size": int(len(idx)), "persistence": round(float(pers[c]), 4),
            "n_interviews": len(itv),
            "one_interview": itv.most_common(1)[0][1] >= 0.5 * len(idx),
            "one_collection": cols.most_common(1)[0][1] >= 0.98 * len(idx),
            "categories": dict(cats.most_common()), "collections": dict(cols),
            "top_interviews": itv.most_common(5),
            "period": [int(np.percentile(years, 10)), int(np.percentile(years, 90))] if len(years) >= 5 else None,
            "facets": facets,
            "samples": [{"docid": keys[i], "title": meta[keys[i]]["title"][:120],
                         "summary": (meta[keys[i]]["summary"] or "")[:200]} for i in best],
        })
    json.dump(summary, open(out_dir / f"hdbscan-mcs{mcs}-summary.json", "w"), indent=1, ensure_ascii=False)
    flags = sum(c["one_interview"] for c in summary["clusters"])
    log(f"  one-interview clusters: {flags}; sizes of top 5: {[c['size'] for c in summary['clusters'][:5]]}")
log("done")
