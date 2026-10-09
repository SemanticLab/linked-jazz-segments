#!/usr/bin/env python
"""Join the naming results onto the packs -> data/clusters/story_clusters.json
(what ch-jazz-mashup/export_sqlite.py loads into story_clusters).

  uv run analysis/assemble_clusters.py data/clusters/names-*.json

Each names file is the `named` list of a community-naming workflow run
({level, id, name, narrative, threads, kind, coherence}); later files override
earlier ones for the same (level, id). Packs without a name are still emitted
(name = null) so the hierarchy stays complete.
"""
import json, sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "clusters"
names = {}
for f in sys.argv[1:]:
    for r in json.load(open(f)):
        names[(r["level"], int(r["id"]))] = r
PARENT = {"community": ("theme", "theme"), "fine": ("community", "community")}
rows = []
for p in sorted(OUT.glob("packs/*.json")):
    pk = json.load(open(p))
    key = (pk["level"], pk["id"])
    nm = names.get(key, {})
    rec = {"level": pk["level"], "id": pk["id"], "name": nm.get("name"), "narrative": nm.get("narrative"),
           "threads": nm.get("threads") or [], "kind": nm.get("kind"), "coherence": nm.get("coherence"),
           "parent_level": None, "parent_id": None,
           "core_size": pk["core_size"], "total_size": pk["total_size"], "n_interviews": pk["n_interviews"],
           "period": pk["period_10_90"], "collections": pk["collections"], "categories": pk["categories"],
           "facets": {k: v[:6] for k, v in pk["facets"].items()}}
    if pk["level"] in PARENT:
        rec["parent_level"], field = PARENT[pk["level"]]
        rec["parent_id"] = pk.get(field)
    rows.append(rec)
rows.sort(key=lambda r: (["theme", "community", "fine"].index(r["level"]), r["id"]))
json.dump(rows, open(OUT / "story_clusters.json", "w"), indent=1, ensure_ascii=False)
by = Counter((r["level"], r["name"] is not None) for r in rows)
kinds = Counter((r["level"], r["kind"]) for r in rows if r["kind"])
print({f"{l} {'named' if n else 'unnamed'}": c for (l, n), c in sorted(by.items())})
print({f"{l} {k}": c for (l, k), c in sorted(kinds.items())})
