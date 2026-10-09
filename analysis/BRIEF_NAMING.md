# Naming a story community

You are given one JSON pack describing a cluster of topic segments from jazz
oral-history interviews (Tulane's Hogan Jazz Archive, Hamilton College's Fillius
Jazz Archive, Rutgers' Institute of Jazz Studies, the Smithsonian). The segments
were grouped by embedding similarity, so the cluster is a *community of stories*
that different musicians tell about the same thing — not a category, not a
keyword. Your job: say what that thing is.

The pack has: `level` (theme = broad, community = the main unit, fine = a
sub-thread), sizes, how many interviews contribute, the collection mix, the
segmenter's category mix (a hint only — the clusters deliberately cut across
categories), the period most stories are set in (10th–90th percentile of the
years discussed), the most frequent interviewees, the top facets (places, venues,
bands, people tagged in the segments) and 40 sample segments: the 20 most central
to the cluster, then 20 spread across it, each with interviewee, title, period
and summary.

Read all 40 samples before deciding. Judge by what the stories are ABOUT, in the
musicians' terms. Then return:

- `name`: 3–8 words, specific, in plain language, as a historian would title a
  chapter ("Drafted: service bands and the war years", "Getting paid: wages, tips
  and the union scale", "Hearing Charlie Parker for the first time"). Not a
  category label ("Military service"), not a list of names.
- `narrative`: for theme/community, one paragraph (4–7 sentences) framing the
  shared story — what happens in it, when and where it is set, what the tellers
  agree on and where they differ, which kinds of musicians tell it. For a fine
  cluster, 1–2 sentences. Write from the samples; do not invent facts.
- `threads`: 3–6 short phrases naming the recurring sub-stories inside the
  cluster (theme/community only; empty list for fine).
- `kind`: `story` (a real shared subject), `meta` (interview mechanics: openings,
  closing thanks, recording logistics), or `mixed` (no single subject — say so
  honestly; name it by its loose common thread).
- `coherence`: 1–5, how much one subject the samples really share (5 = all of
  them, 3 = a clear majority, 1 = a grab bag).

Return only the structured output.
