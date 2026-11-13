# Manhattan Restaurant Alternatives

Enter a Manhattan restaurant you like, confirm the right match, set filters, and click
**Find alternatives**. The app suggests comparable restaurants and explains each one from
the data. It is plain Python and Streamlit: no LLM, LangChain or OpenAI calls at runtime.

> ⚠️ **Yelp terms are UNVERIFIED.** We have not confirmed that Yelp's terms allow our
> analysis, deployment, or display of Yelp data for this project. **Owner and date for
> resolving this: TBD by the team.** Whether to replace Yelp with another licensed source
> for ratings and review counts is also **open**.
> The Yelp free trial is time-limited (**30 days**) and capped at about **5,000 calls**.

---

## Quick start (synthetic demo, no keys needed)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/make_demo_db.py
DATABASE_PATH=data/demo.sqlite streamlit run app.py
```

The demo database contains made-up restaurants and fake "Yelp" rows that expire 24 hours
after you create them (rerun the script to renew them). Try a typo like `copper kitchn`,
or `Copper Kitchen` to see branch selection.

## Setup with real data

1. **Install** (as above) and create your local config:
   ```bash
   cp .env.example .env
   ```
   Put your Yelp key in `.env` (`YELP_API_KEY=...`). Leave it empty to run local-only.
2. **Load the NYC directory** (the permanent record of names, addresses and cuisines):
   ```bash
   python scripts/manage.py import-directory path/to/nyc_restaurants_ml.csv
   ```
   The importer accepts the NYC DOHMH column names (`CAMIS`, `DBA`, `ADDRESS`, `ZIPCODE`,
   `Latitude`, `Longitude`, `CUISINE_DESCRIPTION`) or our own (`nyc_id`, `name`, ...).
   Repeated inspection rows are collapsed; `0` coordinates are treated as missing.
3. **Build the fixed Yelp sample once** (about 15–30 calls). This picks up to 50 restaurants
   in each of 10 Manhattan areas (500 total), stores their Yelp IDs permanently, caches their Yelp
   data for 24 hours, and maps them to NYC records by name + address (one-to-one only):
   ```bash
   python -m restaurant_alts.refresh build-sample
   ```
   It refuses to run if a sample already exists, so the sample never changes silently;
   pass `--replace` to deliberately delete the old sample (and its cache) and build a new one.
4. **Run the app:**
   ```bash
   streamlit run app.py
   ```

## How refresh works

Yelp content may be kept for **at most 24 hours**, so nothing runs on a schedule.

- The app contacts Yelp **only when someone clicks _Find alternatives_**. Typing, choosing
  a match, changing filters and Streamlit reruns never call Yelp.
- On a click, if the sample's Yelp data is missing or 24 hours old, the app reruns the
  sample's searches (about 15–30 calls) and keeps only restaurants already in the sample.
  Any that don't come back, or are now closed, are labelled **not refreshed** and left out;
  they are never replaced.
- Clicks within 24 hours of the last refresh make **no** Yelp calls.
- If the chosen restaurant is outside the sample, that click makes one Business Match call
  (accepted only when Yelp returns exactly one business with the same ZIP) and one details
  call. Otherwise it's described with NYC data and marked **not enriched**.
- Expired rows are deleted on every click, and are never shown even before deletion.
- Every Yelp call is counted per session and per day; the app stops at
  `YELP_DAILY_CALL_LIMIT` (default 300/day).

Manual admin commands (also never scheduled):

```bash
python -m restaurant_alts.refresh refresh   # refresh the fixed sample now
python -m restaurant_alts.refresh purge     # delete expired Yelp rows
```

### When Yelp is unavailable

Missing key, expired key (401/403), rate limit (429), the daily limit, or a network error
all switch the app to **local-only mode**: results are ranked by cuisine similarity from NYC
data, Yelp fields show "unavailable", and the rating, review and price filters are
disabled with a visible explanation. Yelp data fetched in the last 24 hours is still used
without a key; older data never is.

## Methodology

**Name matching** (`restaurant_alts/name_matching.py`, local only): normalize (case,
accents, apostrophes, `&` → "and", punctuation), then exact match, then aliases, then
RapidFuzz suggestions (top 5, score ≥ 70). Fuzzy scores never pick a restaurant; duplicate
names and multiple branches always require the user to choose by address.

**Similarity** (`restaurant_alts/features.py`): TF-IDF on cuisine categories only (generic
tags like "Restaurants" removed). The vectorizer is fit on the candidate pool and the
reference is transformed with the same vectorizer, so unseen categories are ignored.
Cosine similarity (0–1) is the only gate: candidates need ≥ 0.30.

| Similarity | Label |
|---|---|
| 0.80+ | very similar |
| 0.50–0.79 | similar |
| 0.30–0.49 | loosely similar |

*Score reflects category overlap only, not menu, atmosphere, or quality.*

**Candidates:** with Yelp data, every sampled restaurant with fresh Yelp data, whether or
not it matches an NYC record (unmatched ones show Yelp's name and address, which expire
with the other Yelp fields). The reference itself, or a Yelp listing of it (same Yelp ID,
or same name in the same ZIP), is never returned. Without Yelp data, NYC records only.

**Retrieval** (`restaurant_alts/recommender.py`):
- Stage 1 (local): exclude the reference, apply required-cuisine and max-distance
  filters, keep similarity ≥ 0.30, take the top 30.
- Stage 2: apply min rating (default 4.0), min reviews (default 20), max reviews
  `= min(absolute max, int(reference reviews × ratio))` (default ratio 50%), and price cap.
  Limits are inclusive. Return the top 10.

**Ranking** (strict priority, best first):
1. cuisine band (`floor(similarity × 10)`, higher first)
2. distance band: ≤1 km, ≤3 km, ≤5 km, >5 km, unknown last
3. price-level difference from the reference, unknown last
4. rating (higher first, missing last)
5. exact distance (missing last)
6. name A–Z

If the reference has no location or no price, that tier is skipped for everyone and the
app says so. Each result lists the tiers used and skipped, with reasons.

**Missing data is never favourable:** unknown price fails a price cap; unknown distance
fails a distance filter; unknown rating or review count fails the minimums. Filters are
never relaxed silently: conflicts (e.g. a max-review limit below the minimum) and disabled
filters are explained, and empty results name the filters to relax.

**Explanations** are templates, e.g. *"Shares the Italian and Pizza categories (cuisine
similarity 0.72); 1.2 km away; has 15% as many reviews as your reference; same price
level."* Distance and review clauses appear only when known; a missing price reads
"price information is unavailable".

## Data

SQLite for development (`DATABASE_PATH`, default `data/restaurants.sqlite`, gitignored).

| Table | Contents | Lifetime |
|---|---|---|
| `directory` | NYC id, name, address, zip, lat/lon, cuisine, source, imported_at | Permanent |
| `mappings` | NYC id ↔ Yelp ID, matched_at, method, source | Permanent |
| `aliases` | alias → NYC id, source | Permanent |
| `yelp_sample` | fixed Yelp business IDs, search area | Permanent (IDs only) |
| `yelp_calls` | Yelp calls per UTC day | Permanent (counts only) |
| `yelp_cache` | Yelp name, address, ZIP, rating, reviews, price, categories, coordinates, fetched_at | **24 hours** |

CSV import/export works for the permanent tables only; the Yelp cache can never be
exported. `python scripts/manage.py export-seed` writes `seed/*.csv`, and the app loads
them automatically into an empty database (for deployment).

## Testing

```bash
pytest
```

All tests use synthetic data and mocked Yelp responses. Never put real Yelp data in tests,
fixtures or Git.

Planned manual evaluation: 20–30 real reference restaurants, compared with a baseline of
"same cuisine, sorted by rating" under the same filters.

## Deployment (Streamlit Community Cloud)

- Deploy from `main`. `main` redeploys automatically, so merge only through reviewed PRs.
- Add `YELP_API_KEY` (and optionally `YELP_DAILY_CALL_LIMIT`) under the app's
  **Settings → Secrets**; see `.streamlit/secrets.toml.example`.
- Streamlit Cloud has no reliable disk: the database starts empty after each restart, is
  loaded from the committed `seed/` files (12,532 NYC restaurants, 500 sampled Yelp IDs,
  312 mappings), and the Yelp cache is rebuilt on the next click. When `seed/` changes in a
  push, the app replaces its tables from the new seed and clears the Yelp cache on start.
- Choose **Python 3.12** under Advanced settings when creating the app (the pinned packages
  have no ready-made builds for newer Python versions). It can't be changed afterwards
  without deleting and recreating the app.
- To change the sample: `python -m restaurant_alts.refresh build-sample --replace`, then
  `python scripts/manage.py export-seed`, and commit `seed/` via a PR.
- If a key is ever committed, **rotate it immediately** in the Yelp developer portal.

Never commit: `.env`, `.streamlit/secrets.toml`, `.venv/`, `__pycache__/`,
`.pytest_cache/`, `data/` (including `*.sqlite`), Yelp exports, or notebook outputs.

## Limitations

- Similarity reflects category overlap only — not menu, atmosphere, quality, or how easy it
  is to get a table. Fewer reviews is a rough visibility signal, nothing more.
- Alternatives come from the fixed Yelp sample (500 restaurants across 10 areas) when Yelp
  is available; you can search for any of the 12,532 NYC restaurants as the reference. Search-area names are not verified neighbourhood boundaries.
- 310 of the 500 sampled Yelp restaurants match an NYC record. The other 190 are still
  candidates while their Yelp data is fresh (shown with Yelp's name and address), but drop
  out in local-only mode.
- The NYC file has no coordinates, so distance is only known from Yelp data; in
  local-only mode the distance filter and tier can't be used.
- In local-only mode, NYC cuisine is usually a single label, so similarity is mostly 1.0
  or 0.0 and the ranking relies more on distance.
- The fuzzy-match cutoff (70) and default filters are starting assumptions to be tested
  (e.g. review ratio 25/50/75%).

## Open decisions

- **Yelp terms:** whether Yelp permits our academic/app use (owner and date TBD).
- **Replacing Yelp** with another licensed source for ratings and review counts.
- **Notebooks:** whether the original data-collection and recommendation notebooks are
  committed with outputs stripped, or left out. They are not in the repo yet.

## Project layout

```
app.py                      Streamlit UI
restaurant_alts/
  data_store.py             SQLite schema, CSV import/export, 24h cache, candidate pool
  name_matching.py          normalize → exact → alias → fuzzy suggestions
  features.py               category tokens, TF-IDF similarity, distance, bands
  recommender.py            filters, two stages, tiered ranking, explanations
  yelp_client.py            all Yelp access, call counting, error types
  refresh.py                refresh on click, sample build, mapping, purge
scripts/
  manage.py                 import NYC directory, export/import seed files
  make_demo_db.py           synthetic demo database
tests/                      pytest (synthetic + mocked only)
docs/SPEC.md                spec amendments and decisions
```
