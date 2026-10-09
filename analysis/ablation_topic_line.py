#!/usr/bin/env python
"""Ablation: does the verbatim `Topic: {category}` head line bias the clustering
vectors toward the 16 segmenter categories?

Takes a random sample of segments, re-embeds their clustering-build text through
the live embedContent API in two variants — without the Topic line, and
transcript-only (no head at all) — and compares, against the stored vectors
(full head), the same-category vs cross-category cosine gap and the share of
each segment's 10 nearest neighbours (within the sample) that share its
category / interviewee / collection.

  uv run analysis/ablation_topic_line.py [N=2000]
Writes data/ablation/{variant}.f32 (resumable per variant) and prints the table.
"""
import gzip, json, os, random, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from google import genai
from google.genai import types

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "ablation"; OUT.mkdir(exist_ok=True)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
DIMS, K = 1536, 10
client = genai.Client(api_key=os.environ["GOOGLE_AI"])

docs = {}
for l in gzip.open(DATA / "docs.jsonl.gz", "rt"):
    d = json.loads(l)
    if d["part"] == 0 and d["category"]:
        docs[d["docid"]] = d
keys = json.loads((DATA / "vectors" / "clustering-keys.json").read_text())
row = {k: i for i, k in enumerate(keys)}
X = np.fromfile(DATA / "vectors" / "clustering.f32", dtype=np.float32).reshape(-1, DIMS)

random.seed(7)
sample = sorted(random.sample(sorted(docs), N))
(OUT / "sample.json").write_text(json.dumps(sample))
print(f"{len(sample)} segments sampled")


def variant_text(d, variant):
    t = d["text"]
    if variant == "no_topic":
        t = re.sub(r"^Topic: .*\n", "", t, flags=re.M)
    elif variant == "transcript_only":
        t = t.split("## Transcript", 1)[1].split("\n", 1)[1]
    return f"task: clustering | query: {t}"


def embed(texts):
    for attempt in range(6):
        try:
            r = client.models.embed_content(model="gemini-embedding-2", contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
                                            config=types.EmbedContentConfig(output_dimensionality=1536))
            return [e.values for e in r.embeddings]
        except Exception as e:
            time.sleep(5 * (attempt + 1))
            print(f"  retry {attempt}: {str(e)[:100]}", flush=True)
    raise SystemExit("embedding failed")


def vectors(variant):
    p = OUT / f"{variant}.f32"
    if p.exists():
        return np.fromfile(p, dtype=np.float32).reshape(-1, DIMS)
    chunks = [[variant_text(docs[k], variant) for k in sample[i:i + 20]] for i in range(0, len(sample), 20)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(embed, chunks))
    M = np.asarray([v for r in res for v in r], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    M.tofile(p)
    print(f"{variant}: embedded {len(M)} in {time.time()-t0:.0f}s", flush=True)
    return M


def analyse(M, label):
    S = M @ M.T
    np.fill_diagonal(S, -1)
    cat = np.array([docs[k]["category"] for k in sample])
    who = np.array([docs[k]["doc_id"] for k in sample])
    col = np.array([docs[k]["collection"] for k in sample])
    same_cat = cat[:, None] == cat[None, :]
    mask = ~np.eye(len(M), dtype=bool)
    same_mean = S[same_cat & mask].mean(); diff_mean = S[~same_cat & mask].mean()
    nn = np.argsort(-S, axis=1)[:, :K]
    r = {"label": label, "same_cat_mean": same_mean, "cross_cat_mean": diff_mean,
         "gap": same_mean - diff_mean,
         "nn_same_category": (cat[nn] == cat[:, None]).mean(),
         "nn_same_interview": (who[nn] == who[:, None]).mean(),
         "nn_same_collection": (col[nn] == col[:, None]).mean()}
    return r


full = X[[row[k] for k in sample]]
rows = [analyse(full, "full head (as embedded)"),
        analyse(vectors("no_topic"), "head without Topic line"),
        analyse(vectors("transcript_only"), "transcript only (no head)")]
# chance levels
cat = np.array([docs[k]["category"] for k in sample]); col = np.array([docs[k]["collection"] for k in sample])
pc = sum((cat == c).mean() ** 2 for c in set(cat)); pcol = sum((col == c).mean() ** 2 for c in set(col))
print(f"\nchance: same category {pc:.3f}, same collection {pcol:.3f}, same interview ~{1/len(set(docs[k]['doc_id'] for k in sample)):.4f}")
print(f"{'variant':28s} {'same-cat':>9s} {'cross-cat':>9s} {'gap':>7s} | {'nn same cat':>11s} {'nn same intvw':>13s} {'nn same coll':>12s}")
for r in rows:
    print(f"{r['label']:28s} {r['same_cat_mean']:9.4f} {r['cross_cat_mean']:9.4f} {r['gap']:7.4f} | "
          f"{r['nn_same_category']:11.3f} {r['nn_same_interview']:13.3f} {r['nn_same_collection']:12.3f}")
base = rows[0]["gap"]
for r in rows[1:]:
    print(f"  {r['label']}: category gap {100*(r['gap']-base)/base:+.0f}% vs full head")
json.dump(rows, open(OUT / "results.json", "w"), indent=2, default=float)
