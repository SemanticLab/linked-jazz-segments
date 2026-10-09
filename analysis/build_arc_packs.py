#!/usr/bin/env python
"""Arc packs: everything an agent needs to write the narrative arc of one story
community — the cluster's name/narrative/threads and ALL its core segments in
chronological order of the period discussed (undated last), each with teller,
archive, interview year, title, period, summary, focus tags and top people.

  uv run analysis/build_arc_packs.py            # data/clusters/arc_packs/community-<id>.json

Communities with more than CAP core segments are thinned to CAP, keeping the
highest-probability members evenly across decades. Meta clusters are skipped.
"""
import json, sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path.home() / "git" / "ch-jazz-mashup" / "linked_jazz.sqlite"
OUT = Path(__file__).resolve().parent.parent / "data" / "clusters" / "arc_packs"
OUT.mkdir(exist_ok=True)
CAP = 600

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
clusters = {r["cluster_id"]: dict(r) for r in con.execute(
    "SELECT * FROM story_clusters WHERE level='community' AND kind != 'meta'")}
themes = {r["cluster_id"]: r["name"] for r in con.execute("SELECT cluster_id, name FROM story_clusters WHERE level='theme'")}
tags = defaultdict(lambda: defaultdict(list))
for r in con.execute("SELECT segment_id, kind, name_canonical FROM segment_tags WHERE focus=1 ORDER BY tag_id"):
    if r["name_canonical"] not in tags[r["segment_id"]][r["kind"]]:
        tags[r["segment_id"]][r["kind"]].append(r["name_canonical"])
people = defaultdict(list)
for r in con.execute("SELECT segment_id, person FROM v_segment_people ORDER BY segment_id, mentions DESC"):
    if len(people[r["segment_id"]]) < 4:
        people[r["segment_id"]].append(r["person"])

n = 0
for cid, c in clusters.items():
    rows = con.execute("""
        SELECT s.segment_id, s.interviewee, s.collection, d.year AS interview_year, s.title, s.period,
               s.year_from, s.year_to, s.summary, s.words, sc.prob
        FROM segment_story_clusters sc JOIN v_segments s USING(segment_id) JOIN documents d USING(doc_id)
        WHERE sc.level='community' AND sc.cluster_id=? AND sc.member='core'
        ORDER BY s.year_from IS NULL, s.year_from, s.year_to""", (cid,)).fetchall()
    rows = [dict(r) for r in rows]
    if len(rows) > CAP:
        by_decade = defaultdict(list)
        for r in rows:
            by_decade[(r["year_from"] or 9999) // 10].append(r)
        for lst in by_decade.values():
            lst.sort(key=lambda r: -r["prob"])
        keep, i = [], 0
        while len(keep) < CAP:          # round-robin across decades, best members first
            added = False
            for dec in sorted(by_decade):
                if i < len(by_decade[dec]) and len(keep) < CAP:
                    keep.append(by_decade[dec][i]); added = True
            if not added:
                break
            i += 1
        rows = sorted(keep, key=lambda r: (r["year_from"] is None, r["year_from"] or 0, r["year_to"] or 0))
    segs = []
    for r in rows:
        t = tags[r["segment_id"]]
        segs.append({"segment_id": r["segment_id"], "teller": r["interviewee"], "archive": r["collection"],
                     "interviewed": r["interview_year"], "title": r["title"], "period": r["period"],
                     "year_from": r["year_from"], "summary": r["summary"],
                     "places": t.get("place", [])[:3], "venues": t.get("venue", [])[:3],
                     "bands": t.get("band", [])[:3], "people": people[r["segment_id"]]})
    pack = {"community_id": cid, "name": c["name"], "theme": themes.get(c["parent_id"]),
            "narrative": c["narrative"], "threads": json.loads(c["threads"] or "[]"),
            "kind": c["kind"], "coherence": c["coherence"], "core_size": c["core_size"],
            "n_interviews": c["n_interviews"], "period_10_90": [c["period_from"], c["period_to"]],
            "facets": json.loads(c["facets"] or "{}"), "segments_in_pack": len(segs), "segments": segs}
    json.dump(pack, open(OUT / f"community-{cid}.json", "w"), indent=1, ensure_ascii=False)
    n += 1
print(f"{n} arc packs in {OUT}")
