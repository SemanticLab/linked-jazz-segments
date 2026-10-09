#!/usr/bin/env python
"""Re-embed only the segments whose text changed since the last build and
update both vector matrices and both S3 Vectors indexes in place.

  uv run pipeline/build_docs.py --run <run> --builds retrieval,clustering   # new docs.jsonl.gz
  uv run pipeline/refresh_changed.py data/docs.before.jsonl.gz             # old snapshot

Requires the same docid set as before (a changed set means segment ids moved:
do the full batch rebuild instead). Uses the interactive embedContent API
(fine for a few thousand docs). Story-cluster ids in the metadata are kept.
"""
import gzip, json, os, re, sys, time
from pathlib import Path
import numpy as np
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "search"))
import load_vectors as lv  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
old = {json.loads(l)["docid"]: json.loads(l)["text"] for l in gzip.open(sys.argv[1], "rt")}
new = {}
for l in gzip.open(DATA / "docs.jsonl.gz", "rt"):
    d = json.loads(l); new[d["docid"]] = d
if set(old) != set(new):
    raise SystemExit(f"docid set changed (+{len(set(new)-set(old))} -{len(set(old)-set(new))}): full rebuild needed")
changed = [k for k in new if new[k]["text"] != old[k]]
print(f"{len(changed)} of {len(new)} docs changed")
if not changed:
    raise SystemExit

client = genai.Client(api_key=os.environ["GOOGLE_AI"])
WRAP = {"retrieval": lambda d: f"title: {d['title']} | text: {d['text']}",
        "clustering": lambda d: "task: clustering | query: " + re.sub(r"^Topic: .*\n", "", d["text"], flags=re.M)}


def embed(texts):
    for attempt in range(6):
        try:
            r = client.models.embed_content(model="gemini-embedding-2",
                                            contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
                                            config=types.EmbedContentConfig(output_dimensionality=1536))
            return np.asarray([e.values for e in r.embeddings], dtype=np.float32)
        except Exception as e:
            print(f"  retry {attempt}: {str(e)[:120]}"); time.sleep(5 * (attempt + 1))
    raise SystemExit("embedding failed")


for build in ("retrieval", "clustering"):
    keys = json.loads((DATA / "vectors" / f"{build}-keys.json").read_text())
    row = {k: i for i, k in enumerate(keys)}
    X = np.fromfile(DATA / "vectors" / f"{build}.f32", dtype=np.float32).reshape(-1, 1536)
    vecs = []
    for i in range(0, len(changed), 20):
        chunk = changed[i:i + 20]
        M = embed([WRAP[build](new[k]) for k in chunk])
        M /= np.linalg.norm(M, axis=1, keepdims=True)
        for k, v in zip(chunk, M):
            X[row[k]] = v
            vecs.append({"key": k, "data": {"float32": v.tolist()}, "metadata": lv.metadata(new[k], build)})
        print(f"  {build}: {i + len(chunk)}/{len(changed)}", flush=True)
    X.tofile(DATA / "vectors" / f"{build}.f32")
    for i in range(0, len(vecs), 500):
        lv.client.put_vectors(vectorBucketName=lv.BUCKET, indexName=lv.INDEX[build], vectors=vecs[i:i + 500])
    print(f"{build}: {len(vecs)} vectors re-embedded and re-put into {lv.INDEX[build]}")
