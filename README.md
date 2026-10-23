# linked-jazz-segments — semantic search over the topic segments

Embeds every topic segment of the Linked Jazz oral-history corpus
(`../ch-jazz-mashup/linked_jazz.sqlite`: 61,357 segments of 1,311 interviews,
Tulane / Hamilton / Rutgers / Smithsonian) with **gemini-embedding-2**, stores the
vectors with their facets in an **S3 Vectors** bucket, and serves a search Lambda
that embeds the query, pre-filters on facets, and reranks with **gemini-3.8-flash**.

Sibling of `~/git/archives-interface/frontier` (finding-aid search) — same
model, same two-build design, same batch orchestrator, same Lambda shape.

## Embedding design

gemini-embedding-2 has no `task_type` parameter: the task is an instruction
written into the text. We build ONE inner document per segment and wrap it two
ways ([Gemini docs: "Task types with Embeddings 2"](https://ai.google.dev/gemini-api/docs/embeddings)):

| Build | Wrapper on the document | Query side | Used for |
|---|---|---|---|
| **retrieval** (asymmetric) | `title: {interviewee — segment title} \| text: {inner}` | `task: search result \| query: {q}` | user search |
| **clustering** (symmetric) | `task: clustering \| query: {inner minus the Topic line}` | the stored vector of another segment | "more like this", clustering, segment↔segment similarity |

1536 dimensions (MRL truncation; gemini-embedding-2 auto-normalizes non-3072
dims, and the Lambda re-normalizes the query anyway), cosine distance.

### The inner document

```
Interview with Bob Cranshaw (1995)
Section: Early life and training > Why bass: a drummer father, family of musicians, and football
Period discussed: c. 1949-50
Topic: family and childhood
Summary: Cranshaw wanted to be a percussionist but his father was a drummer …

## Transcript
MW: Yeah. You are an outstanding bass player. … how did you get started?
Bob Cranshaw: I'm really a percussionist. I started out wanting to be …
```

- Head = interviewee + interview year, chapter > segment title, period discussed,
  category (human label), the segmenter's summary. Then the segment's blocks in
  order (dialogue / prose / notes / stage directions; headers, boilerplate and
  removed duplicates dropped), speaker labels resolved to names where the audit
  knows them (interviewee label, co-subjects, the other label of a two-speaker
  interview → interviewer).
- **Not in the text:** the collection / institution. The archives-interface v1
  lesson: a verbatim source label in every doc gives same-source pairs a ~0.05
  cosine bonus and walls collections off from each other. Collection, facets and
  identifiers ride as vector metadata only.
- **The clustering build drops the `Topic:` line.** Ablation
  (`analysis/ablation_topic_line.py`, 2,000 segments re-embedded live): with the
  line, 65% of a segment's 10 nearest neighbours share its segmenter category;
  without it 32% (chance 10%) — the same-vs-cross-category cosine gap halves
  (0.033 → 0.015). HDBSCAN on the with-line vectors would largely rediscover the
  16 categories. The rest of the head is innocent (no-Topic ≈ transcript-only),
  and the retrieval build keeps the line (queries carry none, so it cannot bias).
  Runs are versioned under `data/runs/<run>/`: v1 = both builds with the line
  (retrieval-v1 index, `data/vectors/clustering-v1-topic.f32` kept for reference),
  v2 = clustering without it (`clustering-v2` index).
- **Full-corpus neighbour check (v2 vectors, 5,000 query segments vs all 61k):**
  Topic bias gone (10-NN same category 66% → 37%), but 72% of a segment's
  nearest neighbours are other segments of the *same interview* (same speaker
  names on every line, same head, same era) and 93% the same collection. The
  sample ablation hid this (few segments per interview were sampled).
  **Per-interview mean-centering** of the vectors (subtract each interview's
  mean, re-normalize — no re-embedding) fixes it: same-interview 72% → 11%,
  same-collection 93% → 60%, and topical coherence *rises* (same category
  37% → 53%). That centered representation is the input for HDBSCAN; the raw
  vectors stay in `clustering-v2` for "more like this" (which excludes the same
  interview by filter anyway). Per-collection centering alone does nothing.
- Segments longer than 24,000 chars (7 of them) are split into parts
  `{segment_id}.p{n}` (the 8,192-token input limit); the Lambda collapses parts
  back to one segment.
- 61,320 docs (44 segments have no text blocks), 126 M chars ≈ 31.5 M tokens per
  build; both builds ≈ **$6.31** at the batch rate.

## Metadata stored with each vector (the facets)

Filterable (≤ 2 KB per vector; lists ordered focus-first / most-mentioned-first,
capped at 15 and trimmed to fit — 620 docs lost a tail entry):

| key | from |
|---|---|
| `doc_id`, `collection`, `interviewee`, `year` | documents |
| `category`, `year_from`, `year_to` | segments (period discussed, parsed) |
| `places[]`, `venues[]`, `bands[]` | segment_tags.name_canonical (the Sonnet tag pass + Opus variant merge) |
| `people[]`, `qids[]` | v_segment_people (audited person mentions in the segment's blocks) |

Non-filterable (retrieval index): `title`, `summary`, `chapter`, `period`, `url`
(transcript-reader deep link), `text` (the inner doc, ≤ 16,000 chars — what the
reranker reads). The clustering index carries only `title`, `summary`, `url`.

S3 Vectors `$eq` / `$in` on a list-valued key match ANY element, so
`{"bands": {"$eq": "Count Basie Orchestra"}}` finds every segment tagged with
that band. The indexes are `ENHANCED` mode: filters are evaluated BEFORE the
similarity search, so narrow filters still return full top-K.

## Layout

```
pipeline/build_docs.py      sqlite -> data/docs.jsonl.gz + data/batch-input/{retrieval,clustering}-NNN.jsonl + stats
pipeline/run_batches.py     Gemini Batch API orchestrator (resumable; data/batch-state.json, data/batch-output/)
pipeline/merge_vectors.py   batch output -> data/vectors/{build}.f32 + {build}-keys.json (+ missing lists)
pipeline/embed_missing.py   interactive top-up for anything the batch missed
analysis/ablation_topic_line.py  the Topic-line ablation (live API, 2,000 segments)
analysis/cluster_hdbscan.py      centering + PCA + UMAP + HDBSCAN (py3.12 inline-deps script)
analysis/assign_communities.py   peripheral assignment, hierarchy, naming packs
analysis/assemble_clusters.py    names + packs -> story_clusters.json
analysis/build_arc_packs.py      per-community chronological packs for the arc writers
analysis/assemble_arcs.py        validate citations -> story_arcs.json
pipeline/build_site_data.py sqlite -> site/data/{facets,vocab}.json (the search site's pickers)
site/                       the search site (static; GitHub Pages)
search/load_vectors.py      creates bucket linked-jazz-segments + indexes retrieval-v1 / clustering-v2, loads with metadata (resumable)
search/lambda_function.py   the search Lambda (stdlib + boto3)
search/query.py             run the handler locally or against the deployed URL
search/deploy.sh            zip + create/update the Lambda + Function URL
```

Run order (all with `uv run`, Python 3.14; `$GOOGLE_AI` and AWS creds from the shell):

```
uv run pipeline/build_docs.py --run v3 --builds retrieval,clustering   # or one build
uv run pipeline/run_batches.py v3       # background; ~40 min for 122,640 requests at 3 jobs in flight
uv run pipeline/merge_vectors.py v3     # then embed_missing.py v3 + merge again if anything is missing
uv run search/load_vectors.py           # both indexes; or `retrieval` / `clustering` (bump INDEX names for a new version)
search/deploy.sh
uv run search/query.py "Storyville piano players" band="Count Basie Orchestra"
```

## Story communities (HDBSCAN)

`analysis/cluster_hdbscan.py` (uv-managed Python 3.12 for umap/numba): per-interview
centering → PCA(100) → UMAP(10d, cosine, cached) → HDBSCAN (min_samples 10, eom)
at min_cluster_size 200 / 50 / 15 → `data/clusters/centered/`. Noise is 36–47%
(short, diverse segments); lowering `min_samples` or `leaf` selection does not
change that, and HDBSCAN's soft membership gives noise points ~0, so
`analysis/assign_communities.py` adds a flagged *peripheral* assignment instead
(majority of the 15 nearest core neighbours in UMAP space; vote share median
1.0; such points sit 5× farther out than core points and agree with the target
cluster's modal category 47% vs 58% for core, chance 10%). It also builds the
hierarchy (theme ⊃ community ⊃ fine, by majority vote of core members) and a
naming pack per cluster (stats, facets, period, 20 most central + 20 spread
samples).

Naming: one Sonnet agent per pack (`analysis/BRIEF_NAMING.md`, workflow script
`community-naming-*.js`; 386 agents, ~15M tokens, ~6 min) → `name`, `narrative`,
`threads`, `kind` (story / meta / mixed), `coherence` 1–5.
`analysis/assemble_clusters.py` joins names and packs → `data/clusters/story_clusters.json`,
which `ch-jazz-mashup/export_sqlite.py` loads into `story_clusters` /
`segment_story_clusters` (keyed by doc_id + ord, so segment renumbering is safe).
Result (2026-10-08): 54 themes, 118 communities, 214 fine clusters; 355 story,
24 meta (interview openings, closing thanks, tape changes), 7 mixed; all mixed
across the four archives, no interview-dominated cluster. A fourth level, `story`
(mcs 10, min_samples 3, 490 clusters, 32 of them dominated by one interview), was
added for the story map — see that section. Vector metadata carries
`theme`, `community`, `story_core`, so the Lambda can filter on them.

## Narrative arcs

`analysis/build_arc_packs.py` writes one pack per non-meta community: its name,
narrative and threads plus ALL core segments in the order of the years discussed
(teller, archive, interview year, title, period, summary, focus tags, people;
capped at 600 by round-robin across decades — only 2 communities exceed it).
`analysis/BRIEF_ARCS.md` + workflow `story-arcs-*.js`: one Opus agent per
community reads the pack, may read up to 12 full transcripts through sqlite,
and writes the arc — title, overview, 3–7 chapters (period, places, narrative
through named tellers with ≤ 2 short verbatim quotes, the segment ids it rests
on, voices), tensions, reading list, coverage note. 109 agents, 11.6M tokens,
11 min. `analysis/assemble_arcs.py` validates every citation against community
membership (1 of 9,473 dropped) → `data/clusters/story_arcs.json` → export
tables `story_arcs`, `story_arc_chapters`, `story_arc_evidence`. 97% of the
998 quotations ≥ 5 words are verbatim in the cited interviews.

## Search API

```
GET {function-url}/?q=brass+bands+at+funerals
```

| param | meaning |
|---|---|
| `q` | query text, 2–300 chars |
| `k` | results (default 15, max 50); 2k candidates (max 100) go to the reranker |
| `rerank=0` | skip the LLM rerank |
| `collection` `category` `interviewee` `doc_id` | exact-match facets |
| `place` `venue` `band` `person` `qid` | list facets, repeatable (`&band=A&band=B` = any of) |
| `year_from` `year_to` | period discussed overlaps the range |
| `interview_year_from` `interview_year_to` | date of the interview |
| `theme` `community` | story-cluster ids (`story_clusters` in the mashup DB) |
| `similar=<segment_id>` | "more like this" from the clustering index (same facets apply; `same_doc=1` to allow hits from the same interview) |

Response: `{query, filter, reranked, candidates, results: [{id, segment_id, doc_id,
collection, interviewee, year, category, title, summary, chapter, period, url,
year_from, year_to, places, venues, bands, people, qids, snippet, score, vrank, rank}]}`
— `score` is the cosine similarity, `vrank` the vector-search rank, `rank` the final
(reranked) position. Rerank failures fall back to vector order with
`reranked: false`.

Pipeline inside the Lambda: `task: search result | query: {q}` →
gemini-embedding-2 (1536d, normalized) → `QueryVectors` on `retrieval-v1`
(topK 2k, metadata filter from the facets, parts collapsed) → gemini-3.8-flash
reads title + summary + 700 chars of transcript per candidate and returns a JSON
ranking (`responseSchema`, temperature 0, `thinkingLevel` = env `RERANK_THINKING`,
default `low`: ~2 s per rerank; the model's default thinking level took 12–25 s
for a near-identical ordering) → top k.

QA (2026-10-08, 20 topical queries): every query's top-5 was on topic; the
reranker keeps on average 6.4 of the vector top-10 and changes the top-1 in 13/20,
consistently in the direction the prompt asks for (first-hand accounts over
mentions: "army bands" → musicians who played in Army bands rather than draft
avoidance; "riverboats" → Pops Foster's own Streckfus seasons). Cosine scores of
good hits sit at 0.70–0.82; an off-corpus query ("quantum chromodynamics") tops
out at 0.57, so ~0.65 is a usable "nothing relevant" threshold for a UI.

Protection is the archives-search scheme and optional (env `TABLE`): per-IP
`RATE_PER_MIN`, global `DAY_CAP`, 24 h cache of reranked responses in DynamoDB,
keys prefixed `jz#`; DynamoDB failures fail open. Reserved concurrency 5.

## The search site (`site/`)

A static page over the API, deployed to GitHub Pages by
`.github/workflows/deploy.yml` on every push to `main` (one-time setup: repo
Settings → Pages → Source = "GitHub Actions"); the sibling transcript reader
uses the same layout. No build step for the page itself:

| file | what |
|---|---|
| `site/index.html`, `site/app.js`, `site/site.css` | the page: query, archive chips, a filter panel (topic, years discussed, interview year, interviewee, person, place, venue, band, story theme / community, order, k), result cards, the passage in full, "more like this" |
| `site/tokens.css`, `site/lj.css`, `site/assets/linked-jazz-mark.png` | the Linked Jazz design system (tokens, the component stylesheet verbatim, the mark) |
| `site/data/facets.json` | loaded at start: archives, the 16 topics, all 1,070 interviewees, the 54 themes and 118 story communities (names, sizes, periods, narratives) — 233 KB |
| `site/data/vocab.json` | loaded the first time a picker is used: 2,018 places, 2,203 venues, 2,214 bands (≥ 3 segments, with qualifier and counts), 4,830 Wikidata-resolved people (label, dates, description, aliases) and 2,503 unresolved names (≥ 5 segments) — 1 MB |

`uv run pipeline/build_site_data.py` rebuilds the two data files from the
mashup DB (under a second); rerun it after any corpus or cluster change.

The page mirrors the API's query string, so every search is a link
(`?q=riverboats&band=Count+Basie+Orchestra&year_from=1930`,
`?similar=10920`). A card shows what the vector record carries (title, summary,
a 400-char excerpt, topic, period, chapter, story community, the places /
venues / bands / people it names — each a click to narrow the search), the
cosine score, and where the reranker moved it. "Read the passage" fetches the
reader site's `data/seg/<id>.json` and shows the full speaker-attributed text
with previous / next passage, the people in it (filter by QID), and the links
to the transcript and to the archive. "More like this" switches to the
clustering index (`similar=`), with a toggle to include the same interview.
Rate-limit (429), backend (503) and "nothing matches this closely" (top score
below 0.65) states have their own messages. Light and dark themes, down to
360px wide.

## The story map (`site/map.html`)

A second page of the site: the stories laid out in a plane, each one an
outline holding an icon per person who tells part of it (their portrait where
Linked Jazz or Wikidata has one, otherwise initials; the icon grows with their
number of passages). Hovering an icon shows the person, their archive and the first
substantial thing they say in their most central passage there; clicking opens
every passage they have in that community with reader links, and lists the other
communities they appear in. Clicking a community's title opens its narrative,
threads, facets and the list of who tells it. A find box jumps to a person or a
story; `?c=<community>&p=<name>` are shareable links.

| file | what |
|---|---|
| `analysis/build_map.py` | the layout (py3.12 uv script, ~30 s) at the `story` level by default (`--level community` for the 118): per-cluster centroids of the per-interview-centered clustering vectors → metric MDS → discs pushed apart; inside each community, PCA(2) of each person's mean UMAP-10 position among that community's people, collision-resolved. Writes `site/data/map.json` (0.8 MB: positions, names, narratives, people) and `site/data/map/c<id>.json` (18 MB in all, one per community, fetched on hover: every person's passages there with title, summary, period, quote, reader link). The quote is the teller's first substantial statement, never the interviewer's question: speaker labels vary by archive (`MR. HERMAN`, `Brown`, `Q`/`A`, `INTERVIEWER`, bare or surname-first names), so the teller's label is matched on any name token, else `A`, else the non-interviewer label with the most words |
| `analysis/fetch_portraits.py` | the portrait atlas: `wd_people.semlab_thumb_url` (Linked Jazz's own square thumbnails) first, else the Wikidata P18 image via Commons `Special:FilePath?width=128`; originals cached in `data/portraits/`, atlas `site/assets/portraits.jpg` (64 px cells, 500 people) + `site/data/portraits.json` (qid → cell) |
| `site/map.html`, `site/map.js`, `site/map.css` | the page: one canvas, d3-zoom (d3 7.9 from cdnjs), a quadtree for hit-testing, labels at screen size placed greedily so they never overlap |

The map's unit is the **story level**: a fourth HDBSCAN level added 2026-10-17 because
118 communities felt too few for 1,300 interviews — `cluster_hdbscan.py --levels 10
--min-samples 3` on the cached UMAP (490 clusters, 47.5% noise; a sweep showed noise
stays at 44–48% whatever the settings, `leaf` selection pushes it to 60%+), attached
to a community by `assign_communities.py` (`story` key in `segment_communities.jsonl`,
packs `story-*.json`), named by the same Sonnet brief (`names-story.json`: 277 story,
183 mixed, 30 meta; 490 agents, 19M tokens, 5 min) and assembled into
`story_clusters.json` as level `story` with `parent_level = community`. The map draws
the 460 non-meta stories (18,894 icons) and labels the mixed ones "loose grouping".
Only CORE members place a person in a story. Rerun `build_map.py` after any cluster
change; `fetch_portraits.py` only when the people table changes (it skips what is cached).

## Deploy

`search/deploy.sh` creates function `linked-jazz-segments-search` (python3.13,
60 s, 512 MB) reusing archives-search's execution role, sets the env
(`GOOGLE_AI`, `VBUCKET`, `RINDEX`, `CINDEX`, `TABLE`, …), reserves concurrency 5
and opens a public Function URL. Later runs push new code (`deploy.sh env` also
rewrites the environment).

The role needs read access to the new bucket. The `commandline` user cannot edit
IAM, so — as for `archives-frontier-vectors` — it is a **bucket policy** the
owner attaches once:

```
aws s3vectors put-vector-bucket-policy --region us-east-1 --vector-bucket-name linked-jazz-segments --policy '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow",
    "Principal": {"AWS": "<the Lambda execution role ARN>"},
    "Action": ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"],
    "Resource": "arn:aws:s3vectors:us-east-1:<account>:bucket/linked-jazz-segments/index/*"}]}'
```

(`aws lambda get-function-configuration --function-name archives-search --query Role`
prints the role ARN; the archives bucket's policy is the template:
`aws s3vectors get-vector-bucket-policy --vector-bucket-name archives-frontier-vectors`.)
