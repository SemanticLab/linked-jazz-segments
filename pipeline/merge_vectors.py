#!/usr/bin/env python
"""Step 3: merge one run's batch outputs into per-build vector matrices.

  uv run pipeline/merge_vectors.py <run>

Reads data/runs/<run>/batch-output/*.jsonl (lines: {key, response.embedding.values}),
joins against data/docs.jsonl.gz by key and writes, for each build the run
contains (r = retrieval, c = clustering):

  data/vectors/{retrieval,clustering}.f32        N x 1536 float32, row i = keys[i]
  data/vectors/{retrieval,clustering}-keys.json  docids in row order
  data/vectors/stats.json                        coverage, missing keys, errors

Duplicate keys (resubmitted ranges) keep the first occurrence. Missing keys
are embedded interactively by `embed_missing.py` (writes data/batch-output/
missing-*.jsonl in the same line format, so re-running this script picks
them up).
"""
import gzip, json, sys
from pathlib import Path
import numpy as np

DATA = Path(__file__).resolve().parent.parent / "data"
RUN = DATA / "runs" / sys.argv[1]
OUT = DATA / "vectors"
OUT.mkdir(exist_ok=True)
DIMS = 1536

docids = [json.loads(l)["docid"] for l in gzip.open(DATA / "docs.jsonl.gz", "rt")]
print(f"{len(docids)} docs expected per build")

vecs = {"r": {}, "c": {}}
errors = []
for f in sorted((RUN / "batch-output").glob("*.jsonl")):
    for ln in open(f):
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            errors.append((f.name, "unparseable line")); continue
        key = d.get("key")
        vals = (d.get("response") or {}).get("embedding", {}).get("values")
        if not key or not vals:
            errors.append((f.name, f"{key}: {json.dumps(d)[:160]}")); continue
        build, docid = key.split("|", 1)
        if docid not in vecs[build]:
            v = np.asarray(vals, dtype=np.float32)
            if v.shape != (DIMS,):
                errors.append((f.name, f"{key}: dims {v.shape}")); continue
            vecs[build][docid] = v

stats = {"run": sys.argv[1], "docs": len(docids), "error_lines": len(errors), "errors_sample": errors[:20]}
for build, name in (("r", "retrieval"), ("c", "clustering")):
    got = vecs[build]
    if not got:
        print(f"{name}: not in this run, left untouched"); continue
    covered = [d for d in docids if d in got]
    missing = [d for d in docids if d not in got]
    mat = np.stack([got[d] for d in covered]) if covered else np.zeros((0, DIMS), np.float32)
    norms = np.linalg.norm(mat, axis=1) if len(mat) else np.array([1.0])
    mat.tofile(OUT / f"{name}.f32")
    (OUT / f"{name}-keys.json").write_text(json.dumps(covered))
    stats[name] = {"vectors": len(covered), "missing": len(missing),
                   "norm_min": float(norms.min()), "norm_max": float(norms.max())}
    (OUT / f"{name}-missing.json").write_text(json.dumps(missing))
    print(f"{name}: {len(covered)} vectors, {len(missing)} missing, "
          f"norms {norms.min():.4f}-{norms.max():.4f}")
json.dump(stats, open(OUT / f"stats-{sys.argv[1]}.json", "w"), indent=2)
print(f"{len(errors)} error lines")
