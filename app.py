"""Streamlit UI: find comparable Manhattan restaurants.

Yelp is called only inside the "Find alternatives" button handler (via refresh.ensure_fresh
and refresh.ensure_records). Searching, picking a match and changing filters are local.
Streamlit caching is used for the database connection only, never for Yelp data.
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from restaurant_alts import data_store as ds
from restaurant_alts import features as ft
from restaurant_alts import refresh as rf
from restaurant_alts.name_matching import match_restaurant
from restaurant_alts.recommender import SCORE_NOTE, Filters, recommend
from restaurant_alts.yelp_client import client_from_environment

load_dotenv()
st.set_page_config(page_title="Restaurant Alternatives", page_icon="🍽️", layout="centered")

PRICE_LABELS = {1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}
UNAVAILABLE = "unavailable"


@st.cache_resource
def get_conn():
    conn = ds.connect(os.environ.get("DATABASE_PATH", "data/restaurants.sqlite"))
    ds.import_seed(conn, os.environ.get("SEED_DIR", "seed"))  # no-op unless the DB is empty
    return conn


@st.cache_resource
def get_client():
    return client_from_environment(get_conn())


def fmt_time(iso: str | None) -> str:
    if not iso:
        return UNAVAILABLE
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %H:%M")
    except ValueError:
        return UNAVAILABLE


def show(value, fmt="{}") -> str:
    return UNAVAILABLE if ft.is_missing(value) else fmt.format(value)


# ---------------------------------------------------------------- data status

conn, client = get_conn(), get_client()
directory = ds.load_directory(conn)
aliases = ds.load_aliases(conn)

st.title("Find a comparable restaurant")
st.caption("Pick a Manhattan restaurant you like and we'll suggest similar places.")

with st.sidebar:
    st.subheader("Data status")
    st.write(f"NYC restaurants loaded: **{len(directory):,}**")
    st.write(f"Fixed Yelp sample: **{len(ds.load_sample(conn)):,}** restaurants")
    fresh_now = ds.get_fresh_cache(conn)
    if not fresh_now.empty:
        st.write(f"Yelp data fetched: **{fmt_time(fresh_now['fetched_at'].min())}** "
                 "(kept for 24 hours)")
    if client.available:
        st.write(f"Yelp calls today: **{client.counter.today()} / {client.daily_limit}**")
    elif not fresh_now.empty:
        st.info("No Yelp key is set. Yelp data fetched in the last 24 hours is still used; "
                "after it expires, results use local NYC data only.")
    else:
        st.warning("Yelp is not configured. Results will use local NYC data only.")
    st.caption("Yelp is contacted only when you click **Find alternatives**.")

if directory.empty:
    st.error("No restaurant data is loaded yet. See the README for setup steps.")
    st.stop()

# ---------------------------------------------------------------- 1. find the reference

query = st.text_input("Restaurant name", placeholder="e.g. Joe's Pizza")
reference_id = None

if query.strip():
    match = match_restaurant(query, directory, aliases,
                             preferred_ids=set(ds.load_mappings(conn)["nyc_id"]))
    if not match.candidates:
        st.info(match.message)
    else:
        st.write(match.message)
        options = {c.nyc_id: c for c in match.candidates}

        def label(nyc_id: str) -> str:
            c = options[nyc_id]
            where = ", ".join(x for x in (c.address, c.zip) if x) or "address unknown"
            return f"{c.name} — {where}"

        ids = list(options)
        reference_id = st.radio(
            "Which restaurant do you mean?", ids, format_func=label,
            index=ids.index(match.selected_id) if match.selected_id else None,
            key=f"pick-{query}",
        )

if not reference_id:
    st.stop()

ref_nyc = directory.set_index("nyc_id").loc[reference_id]
ref_fresh = ds.build_pool(conn, use_yelp=True)
ref_fresh = ref_fresh[ref_fresh["restaurant_id"].eq(reference_id)]
ref_categories = (ref_fresh["categories"].iloc[0] if not ref_fresh.empty else ref_nyc["cuisine"])

with st.container(border=True):
    st.markdown(f"**{ref_nyc['name']}**")
    st.write(", ".join(str(x) for x in (ref_nyc["address"], ref_nyc["zip"]) if x) or "Address unknown")
    st.write(f"Cuisine: {ref_categories or UNAVAILABLE}")

# ---------------------------------------------------------------- 2. filters

yelp_possible = client.available or not fresh_now.empty
st.subheader("Filters")
if not yelp_possible:
    st.info("Rating, review and price filters are disabled because Yelp data is unavailable. "
            "Results will be ranked by cuisine similarity from local data.")

cuisine_options = ["Any"] + [c for c in (ref_categories or "").split(",") if c.strip()]
cuisine = st.selectbox("Required cuisine", [c.strip() for c in cuisine_options])

col1, col2 = st.columns(2)
with col1:
    min_rating = st.slider("Minimum rating", 0.0, 5.0, 4.0, 0.5, disabled=not yelp_possible)
    min_reviews = st.number_input("Minimum reviews", 0, 100_000, 20, 5, disabled=not yelp_possible)
    price_choice = st.selectbox("Price cap", ["Any", "$", "$$", "$$$", "$$$$"],
                                disabled=not yelp_possible)
with col2:
    ratio_choice = st.select_slider(
        "Max reviews vs. your restaurant", ["25%", "50%", "75%", "100%", "No limit"], "50%",
        disabled=not yelp_possible,
        help="Fewer reviews is a rough sign of a less-known place. It says nothing about "
             "how easy it is to get a table.")
    abs_max = st.number_input("Absolute max reviews (0 = none)", 0, 1_000_000, 0, 50,
                              disabled=not yelp_possible)
    distance_choice = st.selectbox("Maximum distance", ["Any", "1 km", "3 km", "5 km", "10 km"])

filters = Filters(
    required_cuisine=None if cuisine == "Any" else cuisine,
    min_rating=float(min_rating),
    min_reviews=int(min_reviews),
    review_ratio=None if ratio_choice == "No limit" else int(ratio_choice[:-1]) / 100,
    absolute_max_reviews=int(abs_max) or None,
    max_price_level=None if price_choice == "Any" else len(price_choice),
    max_distance_km=None if distance_choice == "Any" else float(distance_choice.split()[0]),
)
params = (reference_id, repr(filters))

# ---------------------------------------------------------------- 3. run (the only Yelp calls)

if st.button("Find alternatives", type="primary"):
    with st.spinner("Checking data and ranking restaurants…"):
        report = rf.ensure_fresh(conn, client)
        notes = []
        pool = ds.build_pool(conn, use_yelp=True)
        use_yelp = not pool.empty  # Yelp data under 24 hours old is valid even if a refresh failed
        if not report.yelp_available and use_yelp:
            notes.append(f"Yelp stopped during the refresh ({report.reason}). Using Yelp data "
                         f"from the last 24 hours; {len(report.not_refreshed)} restaurants "
                         "without fresh data are left out.")
        elif not report.yelp_available:
            notes.append(f"Yelp is unavailable: {report.reason}")
        elif report.attempted and report.not_refreshed:
            notes.append(f"{len(report.not_refreshed)} sampled restaurants didn't come back from "
                         "Yelp this time and are left out.")
        if not use_yelp:
            pool = ds.build_pool(conn, use_yelp=False)

        reference = pool[pool["restaurant_id"].eq(reference_id)]
        if reference.empty and use_yelp:
            # Reference is outside the sample: map and enrich just this one (explicit click).
            mapped, _, reason = rf.resolve_mappings(conn, client, directory[directory["nyc_id"].eq(reference_id)])
            if reference_id in mapped:
                rf.ensure_records(conn, client, [mapped[reference_id]])
                pool = ds.build_pool(conn, use_yelp=True)
                reference = pool[pool["restaurant_id"].eq(reference_id)]
        if reference.empty:
            notes.append("Your restaurant has no fresh Yelp data, so it is described with "
                         "NYC data only (not enriched).")
            reference = ds.build_pool(conn, use_yelp=False)
            reference = reference[reference["restaurant_id"].eq(reference_id)]

        result = recommend(reference.iloc[0].to_dict(), pool, filters, yelp_available=use_yelp)
        st.session_state["run"] = {"params": params, "result": result, "notes": notes,
                                   "pool": pool, "use_yelp": use_yelp,
                                   "ref": reference.iloc[0].to_dict()}

run = st.session_state.get("run")
if not run or run["ref"]["restaurant_id"] != reference_id:
    st.stop()
if run["params"] != params:
    st.info("Your selections changed. Click **Find alternatives** to update the results.")

# ---------------------------------------------------------------- 4. results

result = run["result"]
st.subheader("Alternatives")
st.caption(SCORE_NOTE)
for note in run["notes"] + result.notes:
    st.info(note)

if not result.recommendations:
    st.warning(result.empty_reason or "No results.")
    for tip in result.relax_suggestions:
        st.write(f"- {tip}")
else:
    pool = run["pool"].set_index("restaurant_id")
    for i, rec in enumerate(result.recommendations, start=1):
        with st.container(border=True):
            st.markdown(f"**{i}. {rec['name']}** · {rec['label']} "
                        f"({rec['similarity_score']:.2f})")
            if rec["restaurant_id"] in pool.index:
                addr = pool.loc[rec["restaurant_id"], "address"]
                if pool.loc[rec["restaurant_id"], "name_source"] == "yelp":
                    st.caption(f"{addr or 'Address unavailable'} · name and address from Yelp "
                               "(not matched to an NYC record)")
                elif addr:
                    st.caption(addr)
            st.write(rec["explanation"])
            st.markdown(
                f"**Rating** {show(rec['rating'])} · "
                f"**Reviews** {show(rec['review_count'], '{:,}')} · "
                f"**Price** {PRICE_LABELS.get(rec['price_level'], UNAVAILABLE)} · "
                f"**Distance** {show(rec['distance_km'], '{} km')}"
            )
            shared = ", ".join(rec["shared_categories"]) or "none"
            fetched = fmt_time(rec["data_fetched_at"]) if run["use_yelp"] else UNAVAILABLE
            st.caption(f"Shared categories: {shared} · Yelp data fetched: {fetched}")

    points = pd.DataFrame(
        [{"lat": pool.loc[r["restaurant_id"], "lat"], "lon": pool.loc[r["restaurant_id"], "lon"]}
         for r in result.recommendations if r["distance_km"] is not None]
    ).dropna()
    if not points.empty:
        st.map(points, latitude="lat", longitude="lon", size=40)

with st.expander("Filters actually applied"):
    if result.applied_filters:
        st.json(result.applied_filters)
    else:
        st.write("No filters were applied.")
    st.write("Ranking order: cuisine similarity band → distance band → price difference → "
             "rating → exact distance → name.")

if run["use_yelp"]:
    st.caption("Ratings, review counts and prices from Yelp, cached for at most 24 hours.")
