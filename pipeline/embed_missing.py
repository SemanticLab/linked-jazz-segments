#!/usr/bin/env python
"""Step 3b: embed the docs the batch run missed, via the interactive API.

  uv run pipeline/embed_missing.py <run>

Reads data/vectors/{retrieval,clustering}-missing.json (from merge_vectors.py),
re-reads their request lines from data/batch-input, and writes
data/batch-output/missing-{build}.jsonl in the batch output line format
({key, response: {embedding: {values}}}). Re-run merge_vectors.py afterwards.
"""
import json, os, sys, time
from pathlib import Path
from google import genai
from google.genai import types

DATA = Path(__file__).resolve().parent.parent / "data"
RUN = DATA / "runs" / sys.argv[1]
client = genai.Client(api_key=os.environ["GOOGLE_AI"])

want = set()
for name, b in (("retrieval", "r"), ("clustering", "c")):
    p = DATA / "vectors" / f"{name}-missing.json"
    if p.exists():
        want |= {f"{b}|{d}" for d in json.loads(p.read_text())}
print(f"{len(want)} missing keys")
if not want:
    raise SystemExit

reqs = {}
for f in sorted((RUN / "batch-input").glob("*.jsonl")):
    for ln in open(f):
        d = json.loads(ln)
        if d["key"] in want:
            reqs[d["key"]] = d["request"]["content"]["parts"][0]["text"]
keys = sorted(reqs)
out = open(RUN / "batch-output" / "missing-interactive.jsonl", "a")
for i in range(0, len(keys), 20):
    chunk = keys[i:i + 20]
    for attempt in range(5):
        try:
            r = client.models.embed_content(
                model="gemini-embedding-2", contents=[types.Content(parts=[types.Part(text=reqs[k])]) for k in chunk],
                config=types.EmbedContentConfig(output_dimensionality=1536))
            break
        except Exception as e:
            print(f"retry {attempt}: {e}"); time.sleep(10 * (attempt + 1))
    else:
        raise SystemExit("giving up")
    for k, e in zip(chunk, r.embeddings):
        out.write(json.dumps({"key": k, "response": {"embedding": {"values": e.values}}}) + "\n")
    out.flush()
    print(f"{i + len(chunk)}/{len(keys)}", flush=True)
