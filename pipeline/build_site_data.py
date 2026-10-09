#!/usr/bin/env python3
"""Build the static facet vocabularies the search site needs, from the mashup DB.

Writes into site/data/:

  facets.json   the small file the page loads at start: collections, the 16
                categories, every interviewee, and the story themes and
                communities (names, sizes, periods, a narrative per community)
  vocab.json    the large file loaded the first time a picker is used:
                place / venue / band names (>= MIN_SEGMENTS segments) with their
                qualifier and counts, and people — every Wikidata-resolved person
                in >= MIN_SEGMENTS segments (label, dates, description) plus the
                unresolved names in >= MIN_UNRESOLVED segments

The names are the ones the search API filters on (segment_tags.name_canonical,
documents.interviewee, persons.qid / persons.canonical, story cluster ids).

Usage:
  uv run pipeline/build_site_data.py [--db ~/git/ch-jazz-mashup/linked_jazz.sqlite]
"""
import argparse, json, os, time
from collections import Counter, defaultdict
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent
# the human labels of the 16 categories (same as pipeline/build_docs.py: CATEGORY_LABEL,
# which parses argv on import and so is not imported here)
CATEGORY_LABEL = {
    "band_or_employer": "bands and employers", "person_portrait": "portrait of a person",
    "music_technique_style": "musical technique and style", "reflections": "reflections",
    "venue_city_scene": "venues, cities and scenes", "family_childhood": "family and childhood",
    "recording_session": "recording sessions", "business_money": "business and money",
    "education_training": "education and training", "interview_logistics": "interview logistics",
    "touring_travel": "touring and travel", "race_society": "race and society",
    "later_career": "later career", "health_personal": "health and personal life",
    "military_service": "military service", "other": "other",
}

OUT = HERE.parent / "site" / "data"
MIN_SEGMENTS = 3          # tags and QID people
MIN_UNRESOLVED = 5        # people with no QID (reachable by name only)

COLLECTIONS = {
    "tulane": ("Tulane", "Hogan Jazz Archive, Tulane University"),
    "hamilton": ("Hamilton", "Fillius Jazz Archive, Hamilton College"),
    "si": ("Smithsonian", "Smithsonian Jazz Oral History Program"),
    "rutgers": ("Rutgers", "Institute of Jazz Studies, Rutgers University"),
}


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/git/ch-jazz-mashup/linked_jazz.sqlite"))
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    # ---- facets.json ---------------------------------------------------------
    colls = []
    for r in con.execute("SELECT collection, COUNT(*) docs, SUM(num_segments) segs, MIN(year) y0, MAX(year) y1 "
                         "FROM documents GROUP BY collection"):
        short, long_ = COLLECTIONS.get(r["collection"], (r["collection"], r["collection"]))
        colls.append({"id": r["collection"], "label": short, "name": long_, "docs": r["docs"],
                      "segments": r["segs"], "years": [r["y0"], r["y1"]]})
    colls.sort(key=lambda c: -c["docs"])

    cats_n = json.loads(con.execute("SELECT value FROM meta WHERE key='segment_categories'").fetchone()[0])
    cats = [{"id": k, "label": CATEGORY_LABEL.get(k, k), "segments": n}
            for k, n in sorted(cats_n.items(), key=lambda kv: -kv[1])]

    ivs = {}
    for r in con.execute("SELECT interviewee, collection, COUNT(*) docs, SUM(num_segments) segs, "
                         "MIN(year) y0, MAX(year) y1 FROM documents WHERE interviewee IS NOT NULL "
                         "GROUP BY interviewee, collection"):
        e = ivs.setdefault(r["interviewee"], {"n": r["interviewee"], "c": {}, "d": 0, "s": 0, "y": [None, None]})
        e["c"][r["collection"]] = r["docs"]
        e["d"] += r["docs"]
        e["s"] += r["segs"] or 0
        for i, y in enumerate((r["y0"], r["y1"])):
            if y is not None and (e["y"][i] is None or (y < e["y"][i] if i == 0 else y > e["y"][i])):
                e["y"][i] = y
    interviewees = sorted(ivs.values(), key=lambda e: e["n"].lower())
    for e in interviewees:
        if e["y"] == [None, None]:
            del e["y"]

    themes, communities = [], []
    for r in con.execute("SELECT level, cluster_id, name, kind, coherence, parent_id, core_size, total_size, "
                         "n_interviews, period_from, period_to, narrative FROM story_clusters "
                         "WHERE level IN ('theme','community') ORDER BY level, cluster_id"):
        e = {"id": r["cluster_id"], "name": r["name"], "kind": r["kind"], "size": r["total_size"],
             "core": r["core_size"], "interviews": r["n_interviews"]}
        if r["period_from"]:
            e["period"] = [r["period_from"], r["period_to"]]
        if r["level"] == "theme":
            themes.append(e)
        else:
            e["theme"] = r["parent_id"]
            e["narrative"] = r["narrative"]
            communities.append(e)

    counts = json.loads(con.execute("SELECT value FROM meta WHERE key='counts'").fetchone()[0])
    facets = {"built": time.strftime("%Y-%m-%d"),
              "counts": {"documents": counts["documents"], "segments": counts["segments"]},
              "collections": colls, "categories": cats, "interviewees": interviewees,
              "themes": themes, "communities": communities}
    (OUT / "facets.json").write_text(dumps(facets))
    print(f"facets.json: {len(colls)} collections, {len(cats)} categories, {len(interviewees)} interviewees, "
          f"{len(themes)} themes, {len(communities)} communities ({(OUT / 'facets.json').stat().st_size // 1024} KB)")

    # ---- vocab.json ----------------------------------------------------------
    qual = defaultdict(Counter)
    for r in con.execute("SELECT kind, name_canonical, qualifier, COUNT(*) c FROM segment_tags "
                         "WHERE qualifier IS NOT NULL AND qualifier != '' GROUP BY 1, 2, 3"):
        qual[(r["kind"], r["name_canonical"])][r["qualifier"]] += r["c"]
    tags = {"places": [], "venues": [], "bands": []}
    KIND = {"place": "places", "venue": "venues", "band": "bands"}
    for r in con.execute("SELECT kind, name_canonical name, COUNT(*) s, SUM(focus) f, COUNT(DISTINCT doc_id) d "
                         f"FROM segment_tags GROUP BY kind, name_canonical HAVING s >= {MIN_SEGMENTS} "
                         "ORDER BY s DESC, name"):
        e = {"n": r["name"], "s": r["s"], "f": r["f"], "d": r["d"]}
        q = qual.get((r["kind"], r["name"]))
        if q:
            e["q"] = q.most_common(1)[0][0]
        tags[KIND[r["kind"]]].append(e)

    wd = {r["qid"]: r for r in con.execute("SELECT qid, label, description, birth, death FROM wd_people")}
    people = []
    names_for = defaultdict(Counter)
    for r in con.execute("SELECT qid, person, COUNT(DISTINCT segment_id) s FROM v_segment_people "
                         "WHERE qid IS NOT NULL GROUP BY qid, person"):
        names_for[r["qid"]][r["person"]] += r["s"]
    for r in con.execute("SELECT qid, COUNT(DISTINCT segment_id) s, COUNT(DISTINCT person_id) d FROM v_segment_people "
                         f"WHERE qid IS NOT NULL GROUP BY qid HAVING s >= {MIN_SEGMENTS} ORDER BY s DESC"):
        w = wd.get(r["qid"])
        label = (w["label"] if w and w["label"] else names_for[r["qid"]].most_common(1)[0][0])
        e = {"q": r["qid"], "n": label, "s": r["s"], "d": r["d"]}
        if w:
            if w["birth"] or w["death"]:
                e["y"] = f"{(w['birth'] or '')[:4]}–{(w['death'] or '')[:4]}"
            if w["description"]:
                e["t"] = w["description"][:80]
        alias = [n for n, _ in names_for[r["qid"]].most_common(3) if n.lower() != label.lower()]
        if alias:
            e["a"] = alias
        people.append(e)
    unresolved = []
    for r in con.execute("SELECT person, COUNT(DISTINCT segment_id) s, COUNT(DISTINCT person_id) d FROM v_segment_people "
                         f"WHERE qid IS NULL GROUP BY person HAVING s >= {MIN_UNRESOLVED} ORDER BY s DESC"):
        unresolved.append({"n": r["person"], "s": r["s"], "d": r["d"]})

    vocab = {"built": facets["built"], "min_segments": MIN_SEGMENTS, **tags,
             "people": people, "unresolved": unresolved}
    (OUT / "vocab.json").write_text(dumps(vocab))
    print(f"vocab.json: {len(tags['places'])} places, {len(tags['venues'])} venues, {len(tags['bands'])} bands, "
          f"{len(people)} people with QIDs, {len(unresolved)} unresolved names "
          f"({(OUT / 'vocab.json').stat().st_size // 1024} KB) in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
