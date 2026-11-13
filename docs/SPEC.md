# Build spec: decisions and amendments

The full build spec lives with the team. This file records decisions made during the build
that change or clarify it.

## Amendment 1: refresh on click (approved 2026-11-13)

Problem: similarity (Yelp categories) and distance (Yelp coordinates) need Yelp content,
which may be kept at most 24 hours, so Stage 1 cannot rely on a permanent local copy.

Decision:
- **Section 3:** when a user clicks **Find alternatives** and the Yelp cache is missing or
  older than 24 hours, that click refreshes the fixed restaurant sample before ranking.
  Clicks within 24 hours use the cache and make no Yelp calls. Still no scheduled calls
  and no calls on keystrokes or reruns.
- **Section 7 (`refresh.py`):** refresh by rerunning the same Yelp Business Search queries
  used to build the sample (about 30 calls for ~200 restaurants) and keep only business
  IDs already in the sample. A sampled restaurant missing from the new results is labelled
  "not refreshed"; it is never replaced, and its stale data is never used.
- Fallback is unchanged: if Yelp is unavailable, rank from local NYC data only and label
  Yelp fields "unavailable".

## Amendment 2: candidates come from Yelp, search stays on NYC (approved 2026-11-13)

Problem: only 125 of the 200 sampled Yelp restaurants match an NYC record one-to-one, so
requiring a match dropped over a third of the candidates.

Decision:
- **Reference search** stays local on the NYC directory (works without Yelp, any
  Manhattan restaurant). The chosen restaurant is matched to Yelp on the click.
- **Candidates** (Yelp mode) are every fresh Yelp record in the cache, matched or not.
  Matched ones keep the NYC name/address; unmatched ones use the Yelp name/address, which
  are Yelp content and expire after 24 hours with the other Yelp fields. Their id is
  `yelp:<business id>`; the UI labels them "name and address from Yelp".
- Section 3's "NYC record is the permanent record" now applies to matched restaurants only.
- The reference is excluded by id, by Yelp ID, and by same normalized name + ZIP, so a
  Yelp-only listing of the same restaurant is never returned.
- Local-only fallback is unchanged: NYC records only, so unmatched Yelp listings drop out.

## Clarification: ranking rule

The tiered ranking (cuisine band, distance band, price gap, rating, distance, name)
replaces the earlier "score rounded to 2 decimals" rule. Similarity is rounded to
2 decimals before computing the cuisine band, so 0.80 is never placed in the 0.7 band
by floating-point error.

## Still open

- Whether Yelp permits our academic/app use (owner and date TBD).
- Whether to replace Yelp with another licensed source.
- Whether notebooks are committed (outputs stripped) or left out.

## Resolved

- Sample enlarged to 50 per area (500 restaurants; 14 calls to build, about 15–30 per
  refresh) so strict filters still leave candidates (2026-11-13).
- Duplicate NYC records of one restaurant (same normalized name, address and ZIP; 358 in the
  NYC file) are shown once; the record matched to Yelp, else one with a cuisine, is kept.
- The deployed app reloads its permanent tables from `seed/` whenever the seed changes.

- Seed files are committed (2026-11-13): NYC directory, 200 Yelp sample IDs from a new
  `build-sample` run, 125 name+address mappings. IDs only, no Yelp content.
- First commit (project skeleton) went directly to `main`; every later phase is a PR.
- Without Yelp data, the price cap control is disabled in the UI with an explanation
  (same as rating/review filters).
- Fresh Yelp data (< 24h) is used even when no key is configured; stale data never is.
