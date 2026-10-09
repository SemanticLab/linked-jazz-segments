#!/usr/bin/env python
"""Validate the story-arc workflow output -> data/clusters/story_arcs.json.

  uv run analysis/assemble_arcs.py data/clusters/arcs-*.json

Each input is the `arcs` list of a story-arcs run. Every segment_id an arc
cites must belong to its community (core or peripheral); others are dropped
and counted. Segments are also keyed by (doc_id, ord) in `segment_map` so the
mashup export can re-link after a re-segmentation.
"""
import json, sqlite3, sys
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "clusters"
DB = Path.home() / "git" / "ch-jazz-mashup" / "linked_jazz.sqlite"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
members = {}
for sid, cid in con.execute("SELECT segment_id, cluster_id FROM segment_story_clusters WHERE level='community'"):
    members[sid] = cid
key_of = {sid: [d, o] for sid, d, o in con.execute("SELECT segment_id, doc_id, ord FROM segments")}

arcs, dropped, seen = [], Counter(), set()
for f in sys.argv[1:]:
    for a in json.load(open(f)):
        cid = a["community_id"]
        if cid in seen:
            continue
        seen.add(cid)
        for ch in a["chapters"]:
            ok = [s for s in ch["segment_ids"] if members.get(s) == cid]
            dropped[cid] += len(ch["segment_ids"]) - len(ok)
            ch["segment_ids"] = ok
        a["reading_list"] = [r for r in a["reading_list"] if members.get(r["segment_id"]) == cid]
        arcs.append({"level": "community", "cluster_id": cid, **{k: v for k, v in a.items() if k != "community_id"}})
cited = {s for a in arcs for ch in a["chapters"] for s in ch["segment_ids"]} | \
        {r["segment_id"] for a in arcs for r in a["reading_list"]}
json.dump({"arcs": arcs, "segment_map": {s: key_of[s] for s in sorted(cited)}},
          open(OUT / "story_arcs.json", "w"), indent=1, ensure_ascii=False)
print(f"{len(arcs)} arcs, {sum(len(a['chapters']) for a in arcs)} chapters, {len(cited)} cited segments, "
      f"{sum(dropped.values())} citations dropped (not in community): {dict(dropped.most_common(5))}")
