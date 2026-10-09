#!/usr/bin/env python
"""Step 4: create the S3 Vectors bucket + indexes and load both builds with facets.

  uv run search/load_vectors.py [retrieval|clustering] [--recreate]

Bucket  linked-jazz-segments (us-east-1)
Index   retrieval-v1   — the search index: query vectors are "task: search result | query: …"
Index   clustering-v2  — segment↔segment similarity ("more like this"); v2 = clustering
        build embedded WITHOUT the Topic head line (ablation, see README)
Both: 1536 dims, cosine, float32. Keys are docids ("{segment_id}" or "{segment_id}.p{n}").

Metadata (retrieval index)
  filterable (≤ 2 KB/vector): doc_id, collection, interviewee, year, category,
      year_from, year_to, places[], venues[], bands[], people[], qids[],
      theme, community (story-cluster ids from data/clusters/segment_communities.jsonl,
      incl. peripheral assignments; story_core = 1 when HDBSCAN core at community level)
      (lists ordered focus-first / most-mentioned-first and trimmed to fit)
  non-filterable: title, summary, chapter, period, url, text (≤ TEXT_CAP chars)
Clustering index carries the same filterable keys and only title/summary/url.

The Lambda's read access to the bucket is a bucket policy the owner attaches
by hand (see README "Deploy"); this script only creates and fills.
Resumable: progress checkpointed per batch in data/vectors/s3v-{index}.ckpt.json.
"""
import gzip, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import boto3
import numpy as np

DATA = Path(__file__).resolve().parent.parent / "data"
BUCKET, REGION, DIMS = "linked-jazz-segments", "us-east-1", 1536
INDEX = {"retrieval": "retrieval-v1", "clustering": "clustering-v2"}
NONFILTER = {"retrieval": ["title", "summary", "chapter", "period", "url", "text"],
             "clustering": ["title", "summary", "url"]}
BATCH, FILTER_CAP, TEXT_CAP = 500, 1900, 16_000
LIST_KEYS = ("places", "venues", "bands", "people", "qids")

builds = [a for a in sys.argv[1:] if a in INDEX] or list(INDEX)
STORY = {}
sc = DATA / "clusters" / "segment_communities.jsonl"
if sc.exists():
    for line in open(sc):
        r = json.loads(line)
        STORY[r["docid"]] = (r["theme"]["id"], r["community"]["id"], r["community"]["member"] == "core")
    print(f"story clusters for {len(STORY)} docs")
client = boto3.client("s3vectors", region_name=REGION)


def ensure_bucket():
    try:
        client.get_vector_bucket(vectorBucketName=BUCKET)
        print(f"bucket {BUCKET} exists")
    except client.exceptions.NotFoundException:
        client.create_vector_bucket(vectorBucketName=BUCKET)
        print(f"created bucket {BUCKET}")


def ensure_index(build):
    name = INDEX[build]
    if "--recreate" in sys.argv:
        try:
            client.delete_index(vectorBucketName=BUCKET, indexName=name); print(f"deleted {name}")
            time.sleep(5)
        except client.exceptions.NotFoundException:
            pass
    try:
        client.get_index(vectorBucketName=BUCKET, indexName=name)
        print(f"index {name} exists")
    except client.exceptions.NotFoundException:
        client.create_index(vectorBucketName=BUCKET, indexName=name, dataType="float32",
                            dimension=DIMS, distanceMetric="cosine",
                            metadataConfiguration={"nonFilterableMetadataKeys": NONFILTER[build]})
        print(f"created index {name}")


def filterable(d):
    m = {"doc_id": d["doc_id"], "collection": d["collection"],
         "interviewee": d["interviewee"] or "", "category": d["category"] or ""}
    for k in ("year", "year_from", "year_to"):
        if d.get(k) is not None:
            m[k] = int(d[k])
    st = STORY.get(d["docid"].split(".")[0])
    if st:
        m["theme"], m["community"], m["story_core"] = st[0], st[1], int(st[2])
    for k in LIST_KEYS:
        if d.get(k):
            m[k] = [str(x)[:80] for x in d[k][:15]]
    # trim the longest list until the serialized size fits the 2 KB budget
    while len(json.dumps(m, ensure_ascii=False).encode()) > FILTER_CAP:
        k = max((k for k in LIST_KEYS if m.get(k)), key=lambda k: len(json.dumps(m[k])), default=None)
        if k is None:
            break
        m[k] = m[k][:-1]
        if not m[k]:
            del m[k]
    return m


def metadata(d, build):
    m = filterable(d)
    m["title"] = d["title"][:300]
    m["summary"] = d["summary"] or ""
    m["url"] = d["url"]
    if build == "retrieval":
        m["chapter"] = d["chapter"] or ""
        m["period"] = d["period"] or ""
        m["text"] = d["text"][:TEXT_CAP]
    return m


def load(build):
    keys = json.loads((DATA / "vectors" / f"{build}-keys.json").read_text())
    X = np.fromfile(DATA / "vectors" / f"{build}.f32", dtype=np.float32).reshape(-1, DIMS)
    assert len(keys) == len(X), (len(keys), len(X))
    docs = {}
    with gzip.open(DATA / "docs.jsonl.gz", "rt") as f:
        for line in f:
            d = json.loads(line)
            docs[d["docid"]] = metadata(d, build)
    ckpt = DATA / "vectors" / f"s3v-{INDEX[build]}.ckpt.json"
    done = set(json.load(open(ckpt))) if ckpt.exists() else set()
    batches = [(b, range(b * BATCH, min((b + 1) * BATCH, len(keys))))
               for b in range((len(keys) + BATCH - 1) // BATCH) if b not in done]
    print(f"{build}: {len(keys)} vectors, {len(done)} batches done, {len(batches)} to go", flush=True)
    t0, done = time.time(), list(done)

    def put(job):
        b, idxs = job
        vecs = [{"key": keys[i], "data": {"float32": X[i].tolist()}, "metadata": docs[keys[i]]}
                for i in idxs]
        for attempt in range(5):
            try:
                client.put_vectors(vectorBucketName=BUCKET, indexName=INDEX[build], vectors=vecs)
                return b
            except Exception as e:
                if attempt == 4:
                    raise
                print(f"batch {b} retry {attempt}: {str(e)[:200]}", flush=True)
                time.sleep(3 * (attempt + 1))

    with ThreadPoolExecutor(max_workers=6) as ex:
        for n, b in enumerate(ex.map(put, batches)):
            done.append(b)
            if (n + 1) % 10 == 0:
                json.dump(done, open(ckpt, "w"))
                rate = (n + 1) / (time.time() - t0)
                print(f"{n+1}/{len(batches)} batches, eta {int((len(batches)-n-1)/rate)}s", flush=True)
    json.dump(done, open(ckpt, "w"))
    print(f"{build}: load complete", flush=True)


if __name__ == "__main__":
    ensure_bucket()
    for build in builds:
        ensure_index(build)
        load(build)
