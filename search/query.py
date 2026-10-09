#!/usr/bin/env python
"""Run the Lambda handler locally (same code path, your AWS creds + $GOOGLE_AI).

  uv run search/query.py "Storyville piano professors" [k=5] [band=Count Basie Orchestra] [rerank=0]
  uv run search/query.py similar=20000
  uv run search/query.py --url https://…lambda-url… "query" [facet=value …]   # hit the deployed one
"""
import json, os, sys, urllib.parse, urllib.request
from pathlib import Path

args = sys.argv[1:]
url = None
if args and args[0] == "--url":
    url, args = args[1], args[2:]
params = {}
for a in args:
    k, _, v = a.partition("=")
    if not v:
        k, v = "q", a
    params.setdefault(k, []).append(v)
qs = urllib.parse.urlencode(params, doseq=True)

if url:
    with urllib.request.urlopen(url.rstrip("/") + "/?" + qs, timeout=60) as r:
        body = json.load(r)
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    import lambda_function
    out = lambda_function.handler({"rawQueryString": qs, "requestContext": {}}, None)
    print("HTTP", out["statusCode"], file=sys.stderr)
    body = json.loads(out["body"])

if "error" in body:
    print(body); sys.exit(1)
print({k: v for k, v in body.items() if k != "results"})
for r in body["results"]:
    print(f"\n#{r.get('rank', r['vrank'])} (v{r['vrank']} {r['score']})  {r.get('title')}  "
          f"[{r.get('collection')} {r.get('year')}] {r.get('period') or ''}")
    print("   ", (r.get("summary") or "")[:220])
    facets = [f"{k}: {', '.join(r[k][:4])}" for k in ("places", "venues", "bands", "people") if r.get(k)]
    if facets:
        print("   ", " | ".join(facets))
    print("   ", r.get("url"))
