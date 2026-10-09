"""linked-jazz-segments search: GET ?q=… -> embed (gemini-embedding-2) -> S3 Vectors
(pre-filtered by facets) -> rerank (gemini-3.8-flash) -> JSON.

Query string
  q            the search text (2-300 chars)                    required unless `similar`
  similar      a segment_id: "more like this" via the clustering index (no rerank)
  k            results to return (default 15, max 50); TOPK candidates = 2k (max 100)
  rerank=0     skip the LLM rerank (vector order)
  facets — every one is optional, all are AND-ed; list facets match ANY element:
  collection   tulane | hamilton | rutgers | si
  category     one of the 16 segment categories
  interviewee  exact display name
  doc_id       one interview
  place / venue / band   canonical tag names (repeatable: &band=A&band=B = any of)
  person       person canonical name as in the mashup DB (repeatable)
  qid          Wikidata QID of a person mentioned (repeatable)
  theme / community      story-cluster ids (see story_clusters in the mashup DB)
  year_from / year_to    period discussed overlaps [year_from, year_to]
  interview_year_from / interview_year_to   date of the interview

Rerank failures degrade to vector order (reranked: false), never 500.
Protection (optional, env TABLE): per-IP rate limit, daily cap and a 24h query
cache in a DynamoDB table — same scheme as archives-search; DDB trouble fails open.
Stdlib + runtime boto3 only.
"""
import hashlib, json, math, os, re, time, urllib.request
from urllib.parse import parse_qs

import boto3

VBUCKET = os.environ.get("VBUCKET", "linked-jazz-segments")
RINDEX = os.environ.get("RINDEX", "retrieval-v1")
CINDEX = os.environ.get("CINDEX", "clustering-v2")
TABLE = os.environ.get("TABLE", "")                  # empty = no rate limit / cache
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", "10"))
DAY_CAP = int(os.environ.get("DAY_CAP", "2000"))
GOOGLE_KEY = os.environ["GOOGLE_AI"]
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-2")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "gemini-3.8-flash")
RERANK_THINKING = os.environ.get("RERANK_THINKING", "low")
GAPI = "https://generativelanguage.googleapis.com/v1beta/models/"
s3v = boto3.client("s3vectors")
ddb = boto3.client("dynamodb") if TABLE else None

CORS = {"Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"}
LIST_FACETS = {"place": "places", "venue": "venues", "band": "bands", "person": "people", "qid": "qids"}
EQ_FACETS = ("collection", "category", "interviewee", "doc_id")
INT_FACETS = ("theme", "community")
OUT_KEYS = ("doc_id", "collection", "interviewee", "year", "category", "title", "summary",
            "chapter", "period", "url", "year_from", "year_to", "theme", "community", "story_core",
            "places", "venues", "bands", "people", "qids")


def _resp(code, body, extra=None, cache=True):
    h = {**CORS, **(extra or {})}
    if not cache:
        h["Cache-Control"] = "no-store"
    return {"statusCode": code, "headers": h, "body": body}


def _post_json(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": GOOGLE_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---- DynamoDB rate limit + cache (optional) ----------------------------------
def _bump(pk, ttl_secs):
    r = ddb.update_item(TableName=TABLE, Key={"pk": {"S": pk}},
                        UpdateExpression="SET #t = if_not_exists(#t, :t) ADD n :one",
                        ExpressionAttributeNames={"#t": "ttl"},
                        ExpressionAttributeValues={":one": {"N": "1"},
                                                   ":t": {"N": str(int(time.time()) + ttl_secs)}},
                        ReturnValues="UPDATED_NEW")
    return int(r["Attributes"]["n"]["N"])


def cache_get(pk):
    item = ddb.get_item(TableName=TABLE, Key={"pk": {"S": pk}}).get("Item")
    if item and int(item.get("ttl", {}).get("N", "0")) > time.time():
        return item["body"]["S"]


def cache_put(pk, body):
    ddb.put_item(TableName=TABLE, Item={"pk": {"S": pk}, "body": {"S": body},
                                        "ttl": {"N": str(int(time.time()) + 86400)}})


# ---- search ------------------------------------------------------------------
def embed_query(q):
    r = _post_json(f"{GAPI}{EMBED_MODEL}:embedContent",
                   {"content": {"parts": [{"text": f"task: search result | query: {q}"}]},
                    "outputDimensionality": 1536}, timeout=10)
    v = r["embedding"]["values"]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def build_filter(p):
    """S3 Vectors metadata filter from the query-string facets (None = no filter)."""
    conds = []
    for k in EQ_FACETS:
        if p.get(k):
            conds.append({k: {"$eq": p[k][0]}})
    for k in INT_FACETS:
        if p.get(k) and p[k][0].lstrip("-").isdigit():
            conds.append({k: {"$eq": int(p[k][0])}})
    for qk, mk in LIST_FACETS.items():
        vals = [v for v in p.get(qk, []) if v]
        if vals:
            conds.append({mk: {"$in": vals}} if len(vals) > 1 else {mk: {"$eq": vals[0]}})
    def _int(k):
        try:
            return int(p[k][0])
        except (KeyError, ValueError, IndexError):
            return None
    yf, yt = _int("year_from"), _int("year_to")
    if yf is not None:
        conds.append({"year_to": {"$gte": yf}})
    if yt is not None:
        conds.append({"year_from": {"$lte": yt}})
    iyf, iyt = _int("interview_year_from"), _int("interview_year_to")
    if iyf is not None:
        conds.append({"year": {"$gte": iyf}})
    if iyt is not None:
        conds.append({"year": {"$lte": iyt}})
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else {"$and": conds}


def _hit(v, i):
    m = v.get("metadata", {})
    key = v["key"]
    out = {"id": key, "segment_id": int(key.split(".")[0]),
           "score": round(1.0 - v.get("distance", 0.0), 4), "vrank": i}
    for k in OUT_KEYS:
        if k in m:
            out[k] = m[k]
    if "text" in m:
        body = m["text"].split("## Transcript", 1)[-1].split("\n", 1)[-1]
        out["snippet"] = " ".join(body.split())[:400]
        out["_text"] = m["text"]
    return out


def vector_search(index, vec, topk, flt):
    kw = {"vectorBucketName": VBUCKET, "indexName": index, "queryVector": {"float32": vec},
          "topK": topk, "returnMetadata": True, "returnDistance": True}
    if flt:
        kw["filter"] = flt
    r = s3v.query_vectors(**kw)
    hits, seen = [], set()
    for v in r["vectors"]:
        sid = v["key"].split(".")[0]
        if sid in seen:                     # a long segment's extra part
            continue
        seen.add(sid)
        hits.append(_hit(v, len(hits)))
    return hits


def similar(segment_id, k, flt, same_doc):
    g = s3v.get_vectors(vectorBucketName=VBUCKET, indexName=CINDEX, keys=[str(segment_id)],
                        returnData=True, returnMetadata=True)
    if not g["vectors"]:
        return None
    src = g["vectors"][0]
    vec = src["data"]["float32"]
    if not same_doc:
        c = {"doc_id": {"$ne": src["metadata"]["doc_id"]}}
        flt = {"$and": [flt, c]} if flt else c
    hits = vector_search(CINDEX, vec, min(k + 1, 100), flt)
    return [h for h in hits if h["segment_id"] != segment_id][:k]


def rerank(q, cands, n):
    lines = []
    for i, c in enumerate(cands):
        snip = " ".join(c.get("_text", "").split("## Transcript", 1)[-1].split())[:700]
        lines.append(f"{i}. {c.get('title', '')[:120]} ({c.get('collection', '')}, "
                     f"{c.get('period') or 'period unknown'})\n   summary: {c.get('summary', '')[:300]}"
                     f"\n   transcript: {snip}")
    prompt = (
        "You are reranking passages from jazz oral-history interviews (topic segments of "
        "transcripts) for relevance to a researcher's query. Judge whether the passage really "
        "discusses what the query asks for — not just whether the words overlap. Prefer passages "
        "where the interviewee speaks about it first-hand.\n\n"
        f"Query: {q}\n\nCandidates:\n" + "\n".join(lines) +
        f"\n\nReturn the {min(n, len(cands))} most relevant candidate numbers, best first.")
    r = _post_json(f"{GAPI}{RERANK_MODEL}:generateContent",
                   {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                         # low = ~2 s per rerank; the default thinking level takes
                                         # 12-25 s for a near-identical ordering
                                         "thinkingConfig": {"thinkingLevel": RERANK_THINKING},
                                         "responseSchema": {"type": "OBJECT", "properties": {
                                             "ranking": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
                                             "required": ["ranking"]}}},
                   timeout=40)
    content = r["candidates"][0]["content"]["parts"][-1]["text"]
    order = [i for i in json.loads(content)["ranking"] if isinstance(i, int) and 0 <= i < len(cands)]
    seen = set()
    order = [i for i in order if not (i in seen or seen.add(i))]
    if not order:
        raise ValueError("empty ranking")
    return order


def handler(event, context):
    raw = event.get("rawQueryString") or ""
    p = parse_qs(raw, keep_blank_values=False)
    if not p:
        p = {k: [v] for k, v in (event.get("queryStringParameters") or {}).items()}
    q = (p.get("q", [""])[0] or "").strip()
    sim = (p.get("similar", [""])[0] or "").strip()
    try:
        k = max(1, min(int(p.get("k", ["15"])[0]), 50))
    except ValueError:
        k = 15
    flt = build_filter(p)

    if sim:
        if not sim.isdigit():
            return _resp(400, json.dumps({"error": "similar must be a segment_id"}), cache=False)
        hits = similar(int(sim), k, flt, p.get("same_doc", ["0"])[0] == "1")
        if hits is None:
            return _resp(404, json.dumps({"error": "unknown segment_id"}), cache=False)
        return _resp(200, json.dumps({"similar_to": int(sim), "filter": flt, "results": hits},
                                     ensure_ascii=False))

    if not (2 <= len(q) <= 300):
        return _resp(400, json.dumps({"error": "q must be 2-300 characters"}), cache=False)
    do_rerank = p.get("rerank", ["1"])[0] != "0"
    qkey = "jz#" + hashlib.sha256(json.dumps(
        [re.sub(r"\s+", " ", q.lower()), flt, k, do_rerank], sort_keys=True).encode()).hexdigest()[:32]
    if ddb:
        try:
            hit = cache_get(qkey)
            if hit:
                return _resp(200, hit, {"X-Cache": "hit"})
        except Exception as e:
            print(f"cache read failed (open): {e}")
        ip = (event.get("requestContext", {}).get("http", {}) or {}).get("sourceIp", "unknown")
        try:
            if _bump(f"jz#ip#{ip}#{int(time.time()) // 60}", 180) > RATE_PER_MIN:
                return _resp(429, json.dumps({"error": "rate limit: try again in a minute"}),
                             {"Retry-After": "30"}, cache=False)
            if _bump("jz#day#" + time.strftime("%Y%m%d", time.gmtime()), 172800) > DAY_CAP:
                return _resp(429, json.dumps({"error": "daily capacity reached"}),
                             {"Retry-After": "3600"}, cache=False)
        except Exception as e:
            print(f"rate limit check failed (open): {e}")

    try:
        qvec = embed_query(q)
        cands = vector_search(RINDEX, qvec, min(max(2 * k, 20), 100), flt)
    except Exception as e:
        print(f"search failed: {e}")
        return _resp(503, json.dumps({"error": f"search backend: {type(e).__name__}"}), cache=False)
    reranked = False
    if do_rerank and len(cands) > 1:
        try:
            order = rerank(q, cands, k)
            cands = [cands[i] for i in order] + [c for i, c in enumerate(cands) if i not in set(order)]
            reranked = True
        except Exception as e:
            print(f"rerank failed, vector order fallback: {e}")
    for i, c in enumerate(cands):
        c.pop("_text", None)
        c["rank"] = i
    body = json.dumps({"query": q, "filter": flt, "reranked": reranked,
                       "candidates": len(cands), "results": cands[:k]}, ensure_ascii=False)
    if ddb and reranked:
        try:
            cache_put(qkey, body)
        except Exception as e:
            print(f"cache write failed (open): {e}")
    return _resp(200, body, {"X-Cache": "miss"})
