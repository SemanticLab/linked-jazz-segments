# Data model — segment search

What a search interface over the segment embeddings has to work with: the
unit being searched, the facets attached to it, what the vector store holds,
what the API takes and returns, and where to get facet values for pickers.
(The story arcs are out of scope here; they only appear as two optional
filter ids.)

```
Interview (documents) 1 ──< Segment (segments) 1 ──< Block (blocks)      transcript text
                                   │
                                   ├──< SegmentTag (segment_tags)         place / venue / band facets
                                   ├──< SegmentPerson (v_segment_people)  people named, with QIDs
                                   └──  VectorRecord × 2                  retrieval-v1, clustering-v2
```

Everything the UI shows comes from one of three places: the **vector record**
(returned by the API with every hit — enough to render a result without a
second request), the **mashup database** `linked_jazz.sqlite` (facet
vocabularies, counts, full transcript text), and the **transcript reader**
(deep links).

## 1. The unit: a segment

A segment is a topic-contiguous run of transcript blocks inside one interview,
cut by the segmentation pass (one Sonnet agent per interview) with a title,
summary, category and the period it talks about. 61,357 segments over 1,314
interviews (avg 47 per interview, 316 words, range 0–8,327). All of them are
embedded (61,313 with text; 7 long ones as two parts that the API collapses).

| field | type | source | notes |
|---|---|---|---|
| `segment_id` | int | `segments` | stable within one DB build; the vector key is `str(segment_id)` (`"123.p1"` for an extra part) |
| `doc_id` | string | `documents` | the interview |
| `ord` | int | | 0-based position within the interview |
| `chapter` | string | | the broader section the segmenter put it in ("Early life and training") |
| `title` | string | | segment title ("Why bass: a drummer father, family of musicians, and football") |
| `category` | enum (16) | | see §4 |
| `summary` | string (~150 chars) | | one-sentence summary |
| `period` | string | | years discussed, as the agent wrote them ("c. 1949-50", "1920s-30s"); 80% of segments have one |
| `year_from`, `year_to` | int | | parsed from `period`; 1700–2079; NULL when no period |
| `words`, `num_blocks` | int | | |
| `start_page/start_block … end_page/end_block` | int | | block range; `blocks.segment_id` is the reverse link |
| `url` | string | derived | `documents.transcript_url + "#b{start_page}-{start_block}"` — opens the reader at the segment |

Interview-level fields the UI will show on a hit: `collection` (tulane 600
docs / hamilton 506 / rutgers 114 / si 94), `interviewee` (1,070 distinct
display names), `interviewer`, `year` of the interview (1948–2026; **NULL for
all 114 Rutgers and 76 of 94 SI interviews** — the source metadata has no date),
`title` of the interview, `source_url` (the archive's own page).

## 2. The facets attached to a segment

### Places, venues, bands (`segment_tags`)
Tagged by an agent that read the segment. One row per mention of a thing in a
segment, with:

| field | values |
|---|---|
| `kind` | `place` (87,757 tags, 6,133 distinct names) · `venue` (36,812 / 10,322) · `band` (46,212 / 8,346) |
| `name` | spelling normalised within the transcript |
| `name_canonical` | corpus-wide spelling after agent-judged variant groups ("Apollo Theatre" → "Apollo Theater", four spellings of Jimmie Lunceford Orchestra → one). **Facet on this.** |
| `qualifier` | place: its type (city / neighborhood / street / state / region / country / other) · venue: the city it is in · band: its leader |
| `parent` | place only: the city a street or neighborhood is in |
| `focus` | 1 = the segment is substantially about it (37% of tags), 0 = passing mention |

Long tail: 3,874 canonical names occur in ≥ 5 segments; the rest are
one-offs. `v_segment_facets` gives segments / focus segments / interviews per
canonical name for pickers and counts.

### People (`v_segment_people`)
The audited person mentions inside the segment's blocks: 54,372 segments name
at least one person; 135k per-document person entries resolving to 9,580
distinct Wikidata QIDs. Names are the per-interview canonical form (the same
person can be "Louis Armstrong" in one interview and "Satchmo" in another), so
**filter cross-interview by `qid`** and use the name for display. People
without a QID (local figures) are only reachable by name.

### Story-cluster ids (optional)
`theme` (0–53), `community` (0–117), `story_core` (0/1) — the embedding
communities. For search they are just two more filterable ints; names live in
`story_clusters`.

## 3. The vector records (S3 Vectors, bucket `linked-jazz-segments`, us-east-1)

Two indexes, both 1,536-dim float32 cosine, keyed by the docid, ENHANCED mode
(filters are evaluated *before* the similarity search, so a narrow filter still
returns a full top-K).

| index | vector of | query with | used for |
|---|---|---|---|
| `retrieval-v1` | `title: {interviewee — title} \| text: {inner doc}` | `task: search result \| query: {q}` | **search** |
| `clustering-v2` | `task: clustering \| query: {inner doc minus Topic line}` | another segment's stored vector | **"more like this"** |

The inner doc = labelled head (interviewee + year, chapter > title, period,
topic, summary) + the full transcript of the segment with speaker names. The
collection name is deliberately not in the text.

Metadata on every `retrieval-v1` vector (this is what the API returns):

| key | type | filterable | from |
|---|---|---|---|
| `doc_id` | string | ✔ | documents |
| `collection` | string | ✔ | tulane / hamilton / rutgers / si |
| `interviewee` | string | ✔ | display name |
| `year` | int | ✔ | interview year (absent when unknown) |
| `category` | string | ✔ | the 16 categories |
| `year_from`, `year_to` | int | ✔ | period discussed (absent when none) |
| `places[]`, `venues[]`, `bands[]` | list of string | ✔ | `name_canonical`, focus tags first, max 15 each |
| `people[]` | list of string | ✔ | names, most-mentioned first, max 15 |
| `qids[]` | list of string | ✔ | Wikidata ids of those people |
| `theme`, `community`, `story_core` | int | ✔ | story clusters |
| `title` | string | ✖ | "{interviewee} — {segment title}" |
| `summary`, `chapter`, `period` | string | ✖ | |
| `url` | string | ✖ | reader deep link |
| `text` | string ≤ 16k chars | ✖ | the inner doc (head + transcript) — the reranker reads it; the API returns a 400-char `snippet` from it, not the whole thing |

Limits that shaped this: filterable metadata ≤ 2 KB per vector (lists are
capped at 15 entries and trimmed — 620 segments lost a tail entry; the longest
record is 1.2 KB), total metadata ≤ 40 KB, ≤ 10 non-filterable keys. List
filters match ANY element (`{"bands": {"$eq": "Count Basie Orchestra"}}` hits
every segment tagged with that band).

`clustering-v2` carries the same filterable keys and only `title`, `summary`,
`url`.

## 4. Vocabularies

**Categories** (segments): band_or_employer 10,090 · person_portrait 9,775 ·
music_technique_style 8,012 · reflections 4,444 · venue_city_scene 3,965 ·
family_childhood 3,702 · recording_session 3,434 · business_money 3,085 ·
education_training 3,028 · interview_logistics 2,883 · touring_travel 2,419 ·
race_society 1,770 · later_career 1,652 · health_personal 1,551 · other 997 ·
military_service 550. (`meta.segment_categories` in the DB; human labels in
`pipeline/build_docs.py: CATEGORY_LABEL`.)

**Facet pickers**: `v_segment_facets` (kind, name, segments, focus_segments,
interviews, spellings); `segment_tags_fts` for type-ahead on names;
`documents` for interviewees and collections; `persons`/`wd_people` for people
with QIDs (and `wd_people.image` thumbnails where Wikidata has one).

## 5. The search API

`GET https://vbl5pa2nmnl4ucnizt5l2oqsxi0octzh.lambda-url.us-east-1.on.aws/?…`
(public Function URL, CORS `*`, JSON).

### Request

| param | type | semantics |
|---|---|---|
| `q` | string 2–300 | the query; embedded as `task: search result \| query: {q}` |
| `k` | int 1–50 (15) | results returned; `2k` candidates (min 20, max 100) go to the reranker |
| `rerank` | `0` to skip | default: gemini-3.8-flash reorders the candidates (~2 s) |
| `collection`, `category`, `interviewee`, `doc_id` | string | exact match |
| `place`, `venue`, `band`, `person`, `qid` | string, **repeatable** | list facets; repeated values are OR-ed (`$in`), different facets AND-ed |
| `year_from`, `year_to` | int | period discussed **overlaps** [year_from, year_to] (segments with no period are excluded once either is set) |
| `interview_year_from`, `interview_year_to` | int | date of the interview (excludes undated Rutgers/SI) |
| `theme`, `community` | int | story-cluster ids |
| `similar` | segment_id | "more like this" mode: ignores `q`, no rerank, excludes the same interview unless `same_doc=1`; facets still apply |

All facets together become one S3 Vectors pre-filter, e.g.
`?q=…&collection=hamilton&band=Count Basie Orchestra&year_from=1935&year_to=1950` →
`{"$and":[{"collection":{"$eq":"hamilton"}},{"bands":{"$eq":"Count Basie Orchestra"}},{"year_to":{"$gte":1935}},{"year_from":{"$lte":1950}}]}`.
The filter actually applied is echoed back as `filter`.

### Response

```json
{
  "query": "brass bands playing at funerals",
  "filter": null,                 // or the S3 Vectors filter object
  "reranked": true,               // false = vector order (rerank skipped or failed)
  "candidates": 20,               // how many the vector search returned
  "results": [
    {
      "id": "13899", "segment_id": 13899, "rank": 0, "vrank": 4, "score": 0.759,
      "doc_id": "alma9945593577206326", "collection": "tulane",
      "interviewee": "John Casimir", "year": 1959, "category": "music_technique_style",
      "title": "John Casimir — Funeral procedure and music",
      "summary": "Step by step what the band plays going to the undertaker, the church and the cemetery, …",
      "chapter": "Brass band customs", "period": "",
      "people": ["Joe Watkins"], "qids": ["Q41798551"],
      "theme": 21, "community": 46, "story_core": 1,
      "snippet": "Richard B. 'Dick' Allen: Now, say on an average funeral, how would you … ",
      "url": "https://semlab.io/linked-jazz-transcripts/?t=alma9945593577206326#b1-92"
    }
  ]
}
```

- Keys are omitted when empty: this hit has no `places`/`venues`/`bands` tags,
  no `year_from`/`year_to` (the segmenter wrote no period), so the UI must treat
  every facet key as optional.
- `score` = cosine similarity of the segment to the query (good hits 0.70–0.82;
  an off-topic query tops out ≈ 0.57, so ≈ 0.65 is a usable "nothing relevant"
  line). `vrank` = position in the vector search, `rank` = final position after
  the rerank; showing both lets the UI explain "moved up by the reranker".
- `similar` mode returns `{"similar_to": 20000, "filter": …, "results": [...]}`
  with the same hit shape (no `snippet`, `chapter`, `period`; `score` is
  segment↔segment similarity, typically 0.85–0.92 for good neighbours).
- Errors: 400 `{"error": …}` for bad input, 429 with `Retry-After` on the rate
  limit, 503 `{"error": "search backend: …"}` if the vector store is unreachable.
  Empty `results` (and `candidates: 0`) when a filter matches nothing.

### Behaviour the UI should plan around
- Latency: reranked query 2.5–4.5 s, `rerank=0` ≈ 1 s, cache hit 40 ms
  (`x-cache: hit|miss`; reranked responses are cached 24 h, keyed on query +
  filter + k). A UI that debounces type-ahead should use `rerank=0` for
  previews and rerank only the committed query.
- Rate limit 10 requests / minute / IP and 2,000 / day globally (env-tunable);
  reserved concurrency 5.
- The hit carries everything needed to render a card and link into the reader;
  the full segment text comes from the reader site's `data/seg/<id>.json` (§5b).
- Parts: a long segment's extra part is collapsed into the first; `id` is the
  key that was hit, `segment_id` the integer.

## 5b. Full segment text: the segment JSON on the reader site

`https://semlab.io/linked-jazz-transcripts/data/seg/<segment_id>.json`
— one static file per segment (built by `build_transcripts.py` from the same
DB; same origin as the reader, so a search UI hosted alongside it needs no
CORS). It carries the full speaker-attributed text plus everything above:

```
{ id, ord, n_segments, prev, next,
  doc: { id, title, collection, inst, interviewee, interviewer, date, year, source_url, url },
  chapter, title, category, summary, period, year_from, year_to, words,
  start: [page, block], end: [page, block], url,        // reader deep link #s<id>
  tags: { places: [{name, type, in?, focus}], venues: [{name, city, focus}], bands: [{name, leader, focus}] },
  people: [{name, qid, n}],
  story: { theme, theme_name, community, community_name, core } | null,
  blocks: [{p, b, s, x}] }
```

So the flow is: API hit (card: title, summary, snippet, facets, score) →
on expand, fetch `seg/<id>.json` (full text, prev/next for "keep reading") →
"open in reader" = `url` (`?t=<doc>#s<id>`, highlights the whole segment).
The reader's transcript file `t_<fnv>.json` also lists the interview's
segments (ids, titles, block ranges), for a table of contents.

## 6. Where to get what

| need | source |
|---|---|
| facet values + counts for pickers | `v_segment_facets`, `documents` (collection, interviewee, year), `meta.segment_categories` |
| type-ahead on place/venue/band names | `segment_tags_fts` |
| people with portraits | `persons` ↔ `wd_people` (QID, label, description, image) |
| full segment text | reader site `data/seg/<segment_id>.json` (§5b); or `blocks` where `segment_id = ?` in the DB |
| neighbouring segments of the same interview | `segments` where `doc_id = ?` order by `ord` |
| who is in a segment | `v_segment_people` |
| a segment's story community (name, narrative) | `segment_story_clusters` → `story_clusters` |
| rebuild the vectors after a data change | `pipeline/build_docs.py` → `run_batches.py` → `merge_vectors.py` → `search/load_vectors.py` (README) |
