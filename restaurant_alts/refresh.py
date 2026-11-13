"""Refresh the 24h Yelp cache for a FIXED sample of business IDs, and delete expired rows.

Refresh-on-click (docs/SPEC.md, amendment 1): the app calls ``ensure_fresh`` only when a
user clicks "Find alternatives". If the cache for the sample is missing or older than
24 hours, the same Business Search queries that built the sample are rerun (about 30 calls
for ~200 restaurants) and only IDs already in the sample are kept. A sampled restaurant
that doesn't come back is labelled "not refreshed"; it is never replaced, and its stale
data is never used. Nothing here runs on a schedule.

Command line (explicit admin actions):
    python -m restaurant_alts.refresh purge
    python -m restaurant_alts.refresh refresh
    python -m restaurant_alts.refresh build-sample [--replace]   # creates the fixed sample
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import pandas as pd

from restaurant_alts import data_store as ds
from restaurant_alts.name_matching import normalize_address, normalize_name
from restaurant_alts.yelp_client import YelpClient, YelpUnavailable

SEARCH_AREAS = (
    "West Village", "East Village", "Lower East Side", "Chelsea", "Midtown",
    "Upper West Side", "Upper East Side", "Harlem", "Chinatown", "Financial District",
)
OFFSETS = (0, 50, 100)
PAGE_SIZE = 50
SOURCE_SEARCH = "yelp_business_search"
SOURCE_DETAILS = "yelp_business_details"


def area_location(area: str) -> str:
    return f"{area}, Manhattan, NY"


@dataclass
class RefreshReport:
    attempted: bool = False                 # did we call Yelp at all?
    yelp_available: bool = True
    reason: str | None = None               # why Yelp is unavailable, if it is
    refreshed: list[str] = field(default_factory=list)
    not_refreshed: list[str] = field(default_factory=list)
    purged: int = 0
    calls: int = 0


# ---------------------------------------------------------------- sample refresh

def refresh_sample(conn: sqlite3.Connection, client: YelpClient,
                   now: datetime | None = None) -> RefreshReport:
    """Rerun the sample's searches; cache only sampled IDs. Stops cleanly on Yelp errors."""
    now = now or ds.utc_now()
    report = RefreshReport(attempted=True, purged=ds.purge_expired(conn, now))
    sample = ds.load_sample(conn)
    sample_ids = set(sample["yelp_id"])
    if not sample_ids:
        report.attempted = False
        return report

    calls_before = client.counter.session_calls
    found: dict[str, dict] = {}
    try:
        with_area = sample[sample["search_area"].notna()]
        for area, group in with_area.groupby("search_area", sort=True):
            wanted = set(group["yelp_id"])
            for offset in OFFSETS:
                page = client.search(area_location(area), offset=offset, limit=PAGE_SIZE)
                for rec in page:
                    if rec["yelp_id"] in sample_ids and rec.get("is_closed") is not True:
                        found[rec["yelp_id"]] = rec
                if wanted <= found.keys() or len(page) < PAGE_SIZE:
                    break
        # IDs with no recorded search area: one details call each.
        for yid in sample.loc[sample["search_area"].isna(), "yelp_id"]:
            if yid not in found:
                rec = client.details(yid)
                if rec.get("yelp_id") == yid and rec.get("is_closed") is not True:
                    found[yid] = rec
    except YelpUnavailable as error:
        report.yelp_available = False
        report.reason = str(error) or error.reason

    ds.upsert_cache(conn, found.values(), source=SOURCE_SEARCH, fetched_at=now)
    map_locally(conn, found.values(), now)
    report.calls = client.counter.session_calls - calls_before
    report.refreshed = sorted(found)
    report.not_refreshed = sorted(sample_ids - found.keys())
    return report


def ensure_fresh(conn: sqlite3.Connection, client: YelpClient,
                 now: datetime | None = None) -> RefreshReport:
    """Call on the button click only. Refreshes the sample if its cache is stale."""
    now = now or ds.utc_now()
    if not ds.cache_is_stale(conn, now=now):  # fresh data needs no call (and no key)
        return RefreshReport(purged=ds.purge_expired(conn, now))
    if not client.available:
        reason = "Yelp access is disabled." if client.disabled else "No Yelp API key is configured."
        return RefreshReport(yelp_available=False, reason=reason, purged=ds.purge_expired(conn, now))
    return refresh_sample(conn, client, now)


# ---------------------------------------------------------------- per-ID enrichment

def ensure_records(conn: sqlite3.Connection, client: YelpClient, yelp_ids: Iterable[str],
                   now: datetime | None = None) -> tuple[pd.DataFrame, list[str], str | None]:
    """Fresh Yelp rows for these IDs: cache first, then one details call per missing ID.

    Returns (fresh rows, IDs left "not enriched", reason Yelp stopped or None).
    """
    now = now or ds.utc_now()
    ids = list(dict.fromkeys(i for i in yelp_ids if i))
    missing = [i for i in ids if i not in set(ds.get_fresh_cache(conn, ids, now)["yelp_id"])]
    reason = None
    if missing and client.available:
        fetched = []
        try:
            for yid in missing:
                rec = client.details(yid)
                if rec.get("yelp_id") == yid and rec.get("is_closed") is not True:
                    fetched.append(rec)
        except YelpUnavailable as error:
            reason = str(error) or error.reason
        ds.upsert_cache(conn, fetched, source=SOURCE_DETAILS, fetched_at=now)
    elif missing:
        reason = "Yelp is unavailable."
    fresh = ds.get_fresh_cache(conn, ids, now)
    not_enriched = [i for i in ids if i not in set(fresh["yelp_id"])]
    return fresh, not_enriched, reason


def resolve_mappings(conn: sqlite3.Connection, client: YelpClient,
                     nyc_rows: pd.DataFrame) -> tuple[dict[str, str], list[str], str | None]:
    """Business Match for NYC records without a mapping. Uncertain matches are skipped.

    ``nyc_rows`` needs nyc_id, name, address, zip. Returns (nyc_id -> yelp_id, skipped
    nyc_ids, reason Yelp stopped or None).
    """
    mapped, skipped, reason = {}, [], None
    known = ds.load_mappings(conn).set_index("nyc_id")["yelp_id"].to_dict()
    try:
        for r in nyc_rows.itertuples(index=False):
            if r.nyc_id in known:
                mapped[r.nyc_id] = known[r.nyc_id]
                continue
            yid = client.business_match(r.name, r.address, r.zip)
            if yid is None:
                skipped.append(r.nyc_id)
                continue
            try:
                ds.add_mapping(conn, r.nyc_id, yid, method="yelp_business_match", source="yelp")
                mapped[r.nyc_id] = yid
            except sqlite3.IntegrityError:  # Yelp ID already mapped to another NYC record
                skipped.append(r.nyc_id)
    except YelpUnavailable as error:
        reason = str(error) or error.reason
        done = set(mapped) | set(skipped)
        skipped += [i for i in nyc_rows["nyc_id"] if i not in done]
    return mapped, skipped, reason


def map_locally(conn: sqlite3.Connection, records: Iterable[dict],
                now: datetime | None = None) -> list[str]:
    """Map Yelp search results to NYC records by normalized name + address (no API calls).

    Only a one-to-one match is accepted (the notebook's rule); Yelp IDs already mapped are
    left alone. The Yelp address is used here only and never stored. Returns mapped IDs.
    """
    directory = ds.load_directory(conn)
    if directory.empty:
        return []
    keys = pd.Series(list(zip(directory["name"].map(normalize_name),
                              directory["address"].map(normalize_address))), index=directory.index)
    counts = keys.value_counts()
    already = set(ds.load_mappings(conn)["yelp_id"])
    mapped_nyc = set(ds.load_mappings(conn)["nyc_id"])
    done = []
    for rec in records:
        key = (normalize_name(rec.get("name")), normalize_address(rec.get("address")))
        if rec.get("yelp_id") in already or not all(key) or counts.get(key, 0) != 1:
            continue
        nyc_id = directory.loc[[k == key for k in keys], "nyc_id"].iloc[0]
        if nyc_id in mapped_nyc:
            continue
        try:
            ds.add_mapping(conn, nyc_id, rec["yelp_id"], method="name_address",
                           source=SOURCE_SEARCH, matched_at=now)
        except sqlite3.IntegrityError:
            continue
        mapped_nyc.add(nyc_id)
        done.append(rec["yelp_id"])
    return done


# ---------------------------------------------------------------- one-time sample build

def build_sample(conn: sqlite3.Connection, client: YelpClient, per_area: int = 20,
                 allowed_zips: set[str] | None = None, areas: Iterable[str] = SEARCH_AREAS,
                 now: datetime | None = None) -> RefreshReport:
    """Explicit admin action: pick the fixed sample (IDs stored permanently) and cache it."""
    now = now or ds.utc_now()
    report = RefreshReport(attempted=True)
    calls_before = client.counter.session_calls
    taken: dict[str, dict] = {}
    try:
        for area in areas:
            added = []
            for offset in OFFSETS:
                page = client.search(area_location(area), offset=offset, limit=PAGE_SIZE)
                for rec in page:
                    yid = rec["yelp_id"]
                    if (yid in taken or rec.get("is_closed") is True
                            or (allowed_zips is not None and rec.get("zip") not in allowed_zips)):
                        continue
                    taken[yid] = rec
                    added.append(yid)
                    if len(added) >= per_area:
                        break
                if len(added) >= per_area or len(page) < PAGE_SIZE:
                    break
            ds.add_to_sample(conn, added, search_area=area, added_at=now)
    except YelpUnavailable as error:
        report.yelp_available = False
        report.reason = str(error) or error.reason
    ds.upsert_cache(conn, taken.values(), source=SOURCE_SEARCH, fetched_at=now)
    map_locally(conn, taken.values(), now)
    report.calls = client.counter.session_calls - calls_before
    report.refreshed = sorted(taken)
    return report


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from restaurant_alts.yelp_client import client_from_environment

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=["purge", "refresh", "build-sample"])
    parser.add_argument("--db", default="data/restaurants.sqlite")
    parser.add_argument("--per-area", type=int, default=50)
    parser.add_argument("--replace", action="store_true",
                        help="build-sample only: delete the existing sample and cache first")
    args = parser.parse_args(argv)

    conn = ds.connect(args.db)
    if args.action == "purge":
        print(f"Deleted {ds.purge_expired(conn)} expired Yelp records.")
        return 0
    client = client_from_environment(conn)
    if args.action == "refresh":
        report = refresh_sample(conn, client)
    else:
        if not ds.load_sample(conn).empty:
            if not args.replace:
                print("A sample already exists; refusing to resample. "
                      "Pass --replace to delete it and build a new one.")
                return 1
            with conn:
                conn.execute("DELETE FROM yelp_sample")
            ds.clear_cache(conn)
            print("Deleted the old sample and the Yelp cache.")
        zips = set(ds.load_directory(conn)["zip"].dropna()) or None
        report = build_sample(conn, client, per_area=args.per_area, allowed_zips=zips)
    print(f"Calls: {report.calls}. Cached: {len(report.refreshed)}. "
          f"Not refreshed: {len(report.not_refreshed)}.")
    if report.reason:
        print(f"Stopped early: {report.reason}")
    return 0 if report.yelp_available else 2


if __name__ == "__main__":
    raise SystemExit(main())
