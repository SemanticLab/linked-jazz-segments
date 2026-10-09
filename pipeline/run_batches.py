#!/usr/bin/env python
"""Step 2: adaptive Gemini Batch API orchestrator for gemini-embedding-2.

Adapted from archives-interface/frontier/pipeline/run_batches.py. Tier quotas
reject large jobs (a few M enqueued tokens pass, ~10M fail), so the request
lines in data/batch-input/*.jsonl (sorted file order, concatenated) are
submitted as ~CHUNK-line jobs with up to MAXCONC in flight; each job's output
is downloaded as it succeeds, quota rejections wait (halving the chunk once if
persistent), terminal failures are resubmitted. Keys are self-describing
("r|docid" / "c|docid"), so merge_vectors.py joins by key, not order.

  uv run pipeline/run_batches.py <run>      # data/runs/<run>/batch-input from build_docs.py

State: data/runs/<run>/batch-state.json; outputs: data/runs/<run>/batch-output/job-*.jsonl.
Resumable — re-run any time. API key: $GOOGLE_AI.
"""
import json, os, sys, time, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
from google import genai
from google.genai import types

DATA = Path(__file__).resolve().parent.parent / "data" / "runs" / sys.argv[1]
BATCH_IN, OUT = DATA / "batch-input", DATA / "batch-output"
STATE_PATH = DATA / "batch-state.json"
MODEL, CHUNK, MAXCONC = "gemini-embedding-2", 8000, 3
TERMINAL = {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
FAILED = TERMINAL - {"JOB_STATE_SUCCEEDED"}

client = genai.Client(api_key=os.environ["GOOGLE_AI"])
OUT.mkdir(exist_ok=True)

lines = []
for f in sorted(BATCH_IN.glob("*.jsonl")):
    lines += open(f).readlines()
print(f"{len(lines)} total request lines", flush=True)

state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"chunk": CHUNK, "jobs": []}


def save():
    STATE_PATH.write_text(json.dumps(state, indent=2))


def next_lo():
    return max((j["hi"] for j in state["jobs"]), default=0)


def submit(lo, hi):
    tmp = DATA / "batch-tmp.jsonl"
    tmp.write_text("".join(lines[lo:hi]))
    up = client.files.upload(file=str(tmp), config=types.UploadFileConfig(mime_type="jsonl"))
    job = client.batches.create_embeddings(
        model=MODEL, src=types.EmbeddingsBatchJobSource(file_name=up.name))
    return job.name


save()
quota_waits = 0
while True:
    active = 0
    for j in state["jobs"]:
        if j["downloaded"] or j["state"] in FAILED:
            continue
        b = client.batches.get(name=j["name"])
        j["state"] = b.state.name
        if b.state.name == "JOB_STATE_SUCCEEDED":
            dest = getattr(b.dest, "file_name", None) if b.dest else None
            if dest:
                raw = client.files.download(file=dest)
                p = OUT / f"job-{j['lo']:07d}-{j['hi']:07d}.jsonl"
                p.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
                n = sum(1 for _ in open(p))
                print(f"downloaded {p.name}: {n} results (expected {j['hi']-j['lo']})", flush=True)
            else:
                print(f"WARNING {j['name']} succeeded but no dest file", flush=True)
            j["downloaded"] = True
        elif b.state.name in TERMINAL:
            print(f"JOB {j['name']} [{j['lo']}:{j['hi']}] {b.state.name} — resubmitting range", flush=True)
            j["downloaded"] = True
            state["jobs"].append({"name": None, "lo": j["lo"], "hi": j["hi"],
                                  "state": "RESUBMIT", "downloaded": False})
        else:
            active += 1
    save()
    pending = [j for j in state["jobs"] if j["state"] == "RESUBMIT" and not j["name"]]
    while active < MAXCONC:
        if pending:
            j = pending.pop(0); lo, hi = j["lo"], j["hi"]
        elif next_lo() < len(lines):
            lo = next_lo(); hi = min(lo + state["chunk"], len(lines)); j = None
        else:
            break
        try:
            name = submit(lo, hi)
            if j is None:
                state["jobs"].append({"name": name, "lo": lo, "hi": hi,
                                      "state": "JOB_STATE_PENDING", "downloaded": False})
            else:
                j["name"], j["state"] = name, "JOB_STATE_PENDING"
            active += 1; quota_waits = 0
            print(f"submitted [{lo}:{hi}] -> {name}  ({len(lines)-next_lo()} lines beyond)", flush=True)
            save()
        except Exception as e:
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                quota_waits += 1
                if quota_waits >= 5 and state["chunk"] > 2000:
                    state["chunk"] //= 2; quota_waits = 0
                    print(f"quota pressure: chunk -> {state['chunk']}", flush=True)
                else:
                    print(f"quota full (active={active}), waiting 90s", flush=True)
                break
            raise
    done = sum(j["hi"] - j["lo"] for j in state["jobs"]
               if j["downloaded"] and j["state"] == "JOB_STATE_SUCCEEDED")
    if done >= len(lines) or (active == 0 and next_lo() >= len(lines) and not pending
                              and not [j for j in state["jobs"] if not j["downloaded"]]):
        print("ALL DONE", flush=True); break
    time.sleep(90)
save()
print(f"finished: {sum(1 for j in state['jobs'] if j['downloaded'])} jobs, outputs in {OUT}", flush=True)
