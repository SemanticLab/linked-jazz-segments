#!/usr/bin/env python
"""Step 1: build gemini-embedding-2 embedding docs for every topic segment.

  uv run pipeline/build_docs.py --run v2 --builds clustering

Reads the Linked Jazz mashup database (../ch-jazz-mashup/linked_jazz.sqlite:
segments, blocks, segment_tags, v_segment_people, documents) and writes

  data/docs.jsonl.gz                          one line per doc: metadata + inner text
  data/runs/{run}/batch-input/{build}-NNN.jsonl   Batch API request lines per build
  data/stats.json

Two builds from ONE inner doc (gemini-embedding-2 has no task_type parameter —
the task is an in-prompt wrapper, see README):
  retrieval:   title: {interviewee — segment title} | text: {inner}   (asymmetric,
               for user search queries embedded as "task: search result | query: …")
  clustering:  task: clustering | query: {inner minus the Topic line}  (symmetric,
               for segment↔segment similarity / "more like this")
The clustering build drops the `Topic: {category}` head line: the ablation
(analysis/ablation_topic_line.py) showed that verbatim label makes 65% of a
segment's nearest neighbours share its segmenter category (32% without it,
chance 10%) — HDBSCAN would rediscover the 16 categories instead of stories.
The retrieval build keeps it (queries carry no such line, so it cannot bias).

Inner doc = labeled head (interviewee + interview year, chapter > title, period
discussed, topic category, summary) + the transcript blocks of the segment with
speaker labels resolved to names. Collection / institution is NOT in the text
(it rides as metadata) — the archives-interface v1 lesson: a verbatim source
label in every doc gives same-source pairs a cosine bonus and walls the
collections off from each other.

Docs over MAX_PART_CHARS are split into parts "{segment_id}.p{n}" (a handful).
Batch request key = "r|{docid}" / "c|{docid}".
"""
import argparse, gzip, json, re, sqlite3
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent.parent / "ch-jazz-mashup" / "linked_jazz.sqlite"
DATA = HERE.parent / "data"
ap = argparse.ArgumentParser()
ap.add_argument("--run", default="v1", help="run name: data/runs/<run>/batch-input")
ap.add_argument("--builds", default="retrieval,clustering")
ARGS = ap.parse_args()
BUILDS = [b for b in ARGS.builds.split(",") if b in ("retrieval", "clustering")]   # "none" = docs only
BATCH_IN = DATA / "runs" / ARGS.run / "batch-input"
OUTPUT_DIM = 1536
MAX_PART_CHARS = 24_000          # ~6k tokens, under the 8192-token input limit
SHARD_LINES = 20_000
SKIP_BLOCK_TYPES = {"boilerplate", "header", "removed_duplicate"}
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


def speaker_map(meta_json, participants):
    """speaker label -> display name for one document."""
    m = json.loads(meta_json or "{}")
    out = {}
    rm = m.get("relationships_meta") or {}
    if rm.get("interviewee_label") and rm.get("interviewee"):
        out[rm["interviewee_label"]] = rm["interviewee"]
    for name, labels in participants:
        for lab in json.loads(labels or "[]"):
            out.setdefault(lab, name)
    # two-speaker interview: the label that is not the interviewee is the interviewer
    spk = [x for x in (m.get("speakers") or []) if x]
    if len(spk) == 2 and sum(x in out for x in spk) == 1:
        other = next(x for x in spk if x not in out)
        out[other] = m.get("interviewer") or "INTERVIEWER"
    return out


def split_parts(lines, limit=MAX_PART_CHARS):
    parts, cur, n = [], [], 0
    for ln in lines:
        while len(ln) > limit:                     # pathological single block
            cut = ln.rfind(". ", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            if cur:
                parts.append(cur); cur, n = [], 0
            parts.append([ln[:cut].strip()])
            ln = ln[cut:].strip()
        if ln and n + len(ln) > limit and cur:
            parts.append(cur); cur, n = [], 0
        if ln:
            cur.append(ln); n += len(ln)
    if cur:
        parts.append(cur)
    return parts or [[]]


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    docs_meta = {r["doc_id"]: r for r in con.execute(
        "SELECT doc_id, collection, title, interviewee, year, transcript_url, meta_json FROM documents")}
    parts_by_doc = defaultdict(list)
    for r in con.execute("SELECT doc_id, name, labels FROM doc_participants"):
        parts_by_doc[r["doc_id"]].append((r["name"], r["labels"]))
    spk = {d: speaker_map(docs_meta[d]["meta_json"], parts_by_doc[d]) for d in docs_meta}

    tags = defaultdict(lambda: {"place": [], "venue": [], "band": []})
    for r in con.execute("SELECT segment_id, kind, name_canonical, focus FROM segment_tags "
                         "ORDER BY segment_id, focus DESC, tag_id"):
        lst = tags[r["segment_id"]][r["kind"]]
        if r["name_canonical"] not in lst:
            lst.append(r["name_canonical"])
    people = defaultdict(list)
    for r in con.execute("SELECT segment_id, person, qid, mentions FROM v_segment_people "
                         "ORDER BY segment_id, mentions DESC"):
        people[r["segment_id"]].append((r["person"], r["qid"]))

    DATA.mkdir(exist_ok=True); BATCH_IN.mkdir(parents=True, exist_ok=True)
    for f in BATCH_IN.glob("*.jsonl"):
        f.unlink()
    stats = Counter(); chars = 0; n_parts = 0
    shard, in_shard, out = -1, SHARD_LINES, {}

    def roll():
        nonlocal shard, in_shard, out
        for f in out.values():
            f.close()
        shard += 1; in_shard = 0
        out = {b: open(BATCH_IN / f"{b}-{shard:03d}.jsonl", "w") for b in BUILDS}

    def req(key, text):
        return json.dumps({"key": key, "request": {"output_dimensionality": OUTPUT_DIM,
                           "content": {"parts": [{"text": text}]}}}, ensure_ascii=False) + "\n"

    segs = con.execute("SELECT * FROM segments ORDER BY segment_id").fetchall()
    with gzip.open(DATA / "docs.jsonl.gz", "wt") as df:
        for s in segs:
            d = docs_meta[s["doc_id"]]
            sid = s["segment_id"]
            smap = spk[s["doc_id"]]
            lines = []
            for b in con.execute("SELECT type, speaker, text FROM blocks WHERE block_id BETWEEN ? AND ? "
                                 "ORDER BY block_id", (s["start_block_id"], s["end_block_id"])):
                if b["type"] in SKIP_BLOCK_TYPES or not (b["text"] or "").strip():
                    continue
                txt = re.sub(r"\s+", " ", b["text"]).strip()
                who = (b["speaker"] or "").strip()
                if b["type"] == "dialogue" and who:
                    txt = f"{smap.get(who, who)}: {txt}"
                lines.append(txt)
            if not lines:
                stats["skipped_empty"] += 1
                continue
            head = []
            who = d["interviewee"] or "unknown interviewee"
            head.append(f"Interview with {who}" + (f" ({d['year']})" if d["year"] else ""))
            sect = " > ".join(x for x in (s["chapter"], s["title"]) if x)
            if sect:
                head.append(f"Section: {sect}")
            if s["period"]:
                head.append(f"Period discussed: {s['period']}")
            if s["category"]:
                head.append(f"Topic: {CATEGORY_LABEL.get(s['category'], s['category'])}")
            if s["summary"]:
                head.append(f"Summary: {s['summary']}")
            title = f"{who} — {s['title'] or 'untitled segment'}"
            t = tags[sid]
            for pi, part in enumerate(split_parts(lines)):
                body = "\n".join(head) + "\n\n## Transcript" + (f" (part {pi+1})" if pi else "") \
                       + "\n" + "\n".join(part)
                docid = str(sid) + (f".p{pi}" if pi else "")
                rec = {
                    "docid": docid, "segment_id": sid, "part": pi, "doc_id": s["doc_id"],
                    "collection": d["collection"], "interviewee": d["interviewee"],
                    "year": d["year"], "ord": s["ord"], "chapter": s["chapter"],
                    "title": title, "segment_title": s["title"], "category": s["category"],
                    "summary": s["summary"], "period": s["period"],
                    "year_from": s["year_from"], "year_to": s["year_to"],
                    "words": s["words"], "start_page": s["start_page"], "start_block": s["start_block"],
                    "url": f"{d['transcript_url']}#b{s['start_page']}-{s['start_block']}",
                    "places": t["place"], "venues": t["venue"], "bands": t["band"],
                    "people": [p for p, _ in people[sid]],
                    "qids": [q for _, q in people[sid] if q],
                    "text": body,
                }
                df.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if in_shard >= SHARD_LINES:
                    roll()
                if "retrieval" in out:
                    out["retrieval"].write(req(f"r|{docid}", f"title: {title} | text: {body}"))
                if "clustering" in out:
                    inner = re.sub(r"^Topic: .*\n", "", body, flags=re.M)
                    out["clustering"].write(req(f"c|{docid}", f"task: clustering | query: {inner}"))
                in_shard += 1
                stats["docs"] += 1; chars += len(body)
                if pi:
                    n_parts += 1
            stats["segments"] += 1
    for f in out.values():
        f.close()
    est = chars / 4
    summary = {**stats, "extra_parts": n_parts, "total_chars": chars,
               "est_tokens_per_build": round(est),
               "est_cost_usd_both_builds_batch": round(2 * est / 1e6 * 0.10, 2),
               "output_dimensionality": OUTPUT_DIM, "batch_shards": shard + 1,
               "run": ARGS.run, "builds": BUILDS}
    json.dump(summary, open(DATA / "stats.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
