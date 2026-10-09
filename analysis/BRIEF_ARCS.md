# Writing the narrative arc of a story community

You have one JSON pack: a *story community* — segments of jazz oral-history
interviews (Tulane's Hogan Jazz Archive, Hamilton's Fillius Jazz Archive,
Rutgers' Institute of Jazz Studies, the Smithsonian) that different musicians
told about the same thing, grouped by embedding similarity and already named.
The pack gives the community's name, theme, a framing paragraph and threads,
and then its core segments in chronological order of the period they discuss
(undated ones last): for each, `segment_id`, teller, archive, year interviewed,
title, period, summary, focus places/venues/bands and the people named.

Your job is to turn that pile of separately told stories into ONE larger
narrative arc — the chapter a historian would write from these witnesses:
how the thing began, how it changed over time and place, how it ended or
where it stands, told through the musicians' own experiences.

## How to work
1. Read every segment line. Note the time span, the places, the recurring
   figures, and where the tellers agree or contradict each other.
2. Decide the chapters: 3–7 phases, usually by period (and place when the
   story moved — New Orleans → Chicago → New York), occasionally by sub-story
   when time is not the axis. Each chapter must rest on segments in the pack.
3. You may read the full transcript of up to 12 segments to get a detail or a
   usable quotation right:
   `sqlite3 /Users/m/git/ch-jazz-mashup/linked_jazz.sqlite "SELECT speaker, text FROM blocks WHERE segment_id=<id> ORDER BY block_id"`
   Quote verbatim only what you read there (≤ 25 words per quote, teller named).
   Do not read or write anything else.
4. Write for a general reader with no jazz background, in plain, concrete
   prose. Every claim comes from the segments; never add outside knowledge of
   jazz history, never invent dates, places or people.

## Return (structured output only)
- `community_id`
- `title`: the arc's title, 4–10 words, a chapter heading, not a label
- `overview`: 1 paragraph (5–8 sentences): the whole arc — what, when, where,
  who tells it, how it changes, what it meant to them
- `chapters`: 3–7, in order, each:
  - `title` (3–8 words), `period` (as written, e.g. "1910s–1920s"),
    `year_from`, `year_to` (integers or null), `places` (0–4 names)
  - `narrative`: 1–2 paragraphs (6–12 sentences) told through named tellers'
    experiences, with at most 2 short verbatim quotes you actually read
  - `segment_ids`: 4–15 segment ids this chapter rests on, most telling first
  - `voices`: 3–6 teller names most present in this chapter
- `tensions`: 2–5 one-sentence contradictions or disagreements between
  tellers, each naming who says what
- `reading_list`: 6–10 `{segment_id, why}` — the passages to read in full,
  across the whole arc, `why` ≤ 20 words
- `coverage_note`: one sentence on what in the community the arc leaves out
  (segments that did not fit a chapter), or "none"
