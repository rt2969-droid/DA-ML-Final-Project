"""Yelp client and refresh tests. Every response is mocked; all businesses are made up."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from restaurant_alts import data_store as ds
from restaurant_alts import refresh as rf
from restaurant_alts import yelp_client as yc
from restaurant_alts.recommender import Filters, recommend

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fakes

def biz(yid, name=None, zip_code="10001", closed=False, price="$$"):
    return {"id": yid, "name": name or f"Fake {yid}", "rating": 4.5, "review_count": 120,
            "price": price, "is_closed": closed,
            "categories": [{"alias": "italian", "title": "Italian"}],
            "coordinates": {"latitude": 40.73, "longitude": -74.0},
            "location": {"zip_code": zip_code, "display_address": ["1 Fake St", "New York, NY"]}}


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status, payload or {}, bad_json

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


class FakeSession:
    """Routes by (path, area, offset). ``handler(path, params) -> FakeResponse``."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def get(self, url, params=None, timeout=None, headers=None):
        path = url.replace(yc.API_ROOT, "")
        self.calls.append((path, dict(params or {})))
        return self.handler(path, params or {})


def client(handler, key="test-key", limit=1000, conn=None):
    return yc.YelpClient(api_key=key, daily_limit=limit,
                         counter=yc.CallCounter(conn, clock=lambda: NOW),
                         session=FakeSession(handler))


def ok_search(pages):
    """pages: {(area, offset): [business ids]}"""
    def handler(path, params):
        area = params.get("location", "").split(",")[0]
        ids = pages.get((area, params.get("offset", 0)), [])
        return FakeResponse(200, {"businesses": [biz(i) for i in ids]})
    return handler


@pytest.fixture
def conn():
    c = ds.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------- client errors

def test_missing_key_raises_without_calling():
    c = client(lambda p, q: FakeResponse(), key=None)
    assert not c.available
    with pytest.raises(yc.MissingKey):
        c.search("Chelsea")
    assert c.session.calls == [] and c.counter.session_calls == 0


@pytest.mark.parametrize("status, error", [
    (401, yc.AuthError), (403, yc.AuthError), (429, yc.RateLimited), (500, yc.RequestFailed),
])
def test_http_errors_map_to_yelp_unavailable(status, error):
    c = client(lambda p, q: FakeResponse(status))
    with pytest.raises(error) as info:
        c.search("Chelsea")
    assert isinstance(info.value, yc.YelpUnavailable)
    assert c.counter.session_calls == 1  # failed attempts still count


def test_network_error_and_bad_json():
    def boom(p, q):
        raise requests.ConnectionError("down")
    with pytest.raises(yc.RequestFailed):
        client(boom).search("Chelsea")
    with pytest.raises(yc.RequestFailed):
        client(lambda p, q: FakeResponse(bad_json=True)).search("Chelsea")


def test_daily_limit_stops_calls(conn):
    c = client(lambda p, q: FakeResponse(200, {"businesses": []}), limit=2, conn=conn)
    c.search("A")
    c.search("B")
    with pytest.raises(yc.QuotaExceeded):
        c.search("C")
    assert len(c.session.calls) == 2
    # The daily count is stored, so a new client (new session) is also blocked today.
    c2 = client(lambda p, q: FakeResponse(), limit=2, conn=conn)
    assert c2.counter.today() == 2 and c2.counter.session_calls == 0
    with pytest.raises(yc.QuotaExceeded):
        c2.details("x")


def test_daily_count_resets_next_day(conn):
    counter = yc.CallCounter(conn, clock=lambda: NOW)
    counter.record()
    tomorrow = yc.CallCounter(conn, clock=lambda: NOW + timedelta(days=1))
    assert counter.today() == 1 and tomorrow.today() == 0


def test_parse_business():
    rec = yc.parse_business(biz("a", price=None))
    assert rec["yelp_id"] == "a" and rec["categories"] == "Italian"
    assert rec["price"] is None and rec["lat"] == 40.73 and rec["zip"] == "10001"


# ---------------------------------------------------------------- business match

def test_business_match_requires_single_result_with_matching_zip():
    one = client(lambda p, q: FakeResponse(200, {"businesses": [biz("m1", zip_code="10001")]}))
    assert one.business_match("Fake", "1 Fake St", "10001") == "m1"
    wrong_zip = client(lambda p, q: FakeResponse(200, {"businesses": [biz("m1", zip_code="10002")]}))
    assert wrong_zip.business_match("Fake", "1 Fake St", "10001") is None
    two = client(lambda p, q: FakeResponse(200, {"businesses": [biz("a"), biz("b")]}))
    assert two.business_match("Fake", "1 Fake St") is None
    none = client(lambda p, q: FakeResponse(200, {"businesses": []}))
    assert none.business_match("Fake", "1 Fake St") is None


def test_resolve_mappings_skips_uncertain_and_duplicate(conn, tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("nyc_id,name,address,zip\n1,A,1 St,10001\n2,B,2 St,10001\n3,C,3 St,10001\n")
    ds.import_directory_csv(conn, csv, source="synthetic")
    answers = {"A": [biz("y1")], "B": [], "C": [biz("y1")]}  # C collides with A's Yelp ID
    c = client(lambda p, q: FakeResponse(200, {"businesses": answers[q["name"]]}))
    mapped, skipped, reason = rf.resolve_mappings(conn, c, ds.load_directory(conn))
    assert mapped == {"1": "y1"} and skipped == ["2", "3"] and reason is None
    assert ds.load_mappings(conn).iloc[0].method == "yelp_business_match"


def test_resolve_mappings_stops_on_error(conn, tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("nyc_id,name,address,zip\n1,A,1 St,10001\n2,B,2 St,10001\n")
    ds.import_directory_csv(conn, csv, source="synthetic")
    mapped, skipped, reason = rf.resolve_mappings(
        conn, client(lambda p, q: FakeResponse(429)), ds.load_directory(conn))
    assert mapped == {} and skipped == ["1", "2"] and "429" in reason


# ---------------------------------------------------------------- refresh

def seed_sample(conn):
    ds.add_to_sample(conn, ["a1", "a2"], search_area="Chelsea", added_at=NOW)
    ds.add_to_sample(conn, ["b1"], search_area="Harlem", added_at=NOW)


def test_refresh_keeps_only_sampled_ids_and_never_resamples(conn):
    seed_sample(conn)
    pages = {("Chelsea", 0): ["a1", "new-x"], ("Chelsea", 50): [], ("Harlem", 0): ["b1", "new-y"]}
    c = client(ok_search(pages))
    report = rf.refresh_sample(conn, c, NOW)
    assert report.refreshed == ["a1", "b1"]
    assert report.not_refreshed == ["a2"]  # labelled, not replaced
    assert set(ds.load_sample(conn)["yelp_id"]) == {"a1", "a2", "b1"}
    assert set(ds.get_fresh_cache(conn, now=NOW)["yelp_id"]) == {"a1", "b1"}
    assert report.calls == len(c.session.calls)


def test_refresh_stops_paging_when_area_complete(conn):
    seed_sample(conn)
    pages = {("Chelsea", 0): ["a1", "a2"] + [f"f{i}" for i in range(48)], ("Harlem", 0): ["b1"]}
    c = client(ok_search(pages))
    rf.refresh_sample(conn, c, NOW)
    assert len(c.session.calls) == 2  # one page per area


def test_closed_business_is_not_refreshed(conn):
    ds.add_to_sample(conn, ["c1"], search_area="Chelsea")
    c = client(lambda p, q: FakeResponse(200, {"businesses": [biz("c1", closed=True)]}))
    assert rf.refresh_sample(conn, c, NOW).not_refreshed == ["c1"]


def test_failed_refresh_keeps_partial_results_and_reports(conn):
    seed_sample(conn)

    def handler(path, params):
        if params["location"].startswith("Harlem"):
            return FakeResponse(429)
        return FakeResponse(200, {"businesses": [biz("a1"), biz("a2")]})
    report = rf.refresh_sample(conn, client(handler), NOW)
    assert not report.yelp_available and "429" in report.reason
    assert report.refreshed == ["a1", "a2"] and report.not_refreshed == ["b1"]


def test_expired_data_is_purged_and_not_reused_after_failed_refresh(conn):
    seed_sample(conn)
    old = NOW - timedelta(hours=30)
    ds.upsert_cache(conn, [{"yelp_id": "a1", "name": "Old"}], source="mock", fetched_at=old)
    report = rf.ensure_fresh(conn, client(lambda p, q: FakeResponse(401)), NOW)
    assert not report.yelp_available and report.purged == 1
    assert ds.get_fresh_cache(conn, now=NOW).empty
    assert conn.execute("SELECT COUNT(*) FROM yelp_cache").fetchone()[0] == 0


def test_ensure_fresh_skips_calls_when_cache_fresh(conn):
    seed_sample(conn)
    ds.upsert_cache(conn, [{"yelp_id": i} for i in ("a1", "a2", "b1")], source="mock",
                    fetched_at=NOW - timedelta(hours=1))
    c = client(lambda p, q: pytest.fail("Yelp must not be called"))
    report = rf.ensure_fresh(conn, c, NOW)
    assert not report.attempted and report.yelp_available and c.session.calls == []


def test_ensure_fresh_refreshes_when_stale(conn):
    seed_sample(conn)
    pages = {("Chelsea", 0): ["a1", "a2"], ("Harlem", 0): ["b1"]}
    report = rf.ensure_fresh(conn, client(ok_search(pages)), NOW)
    assert report.attempted and report.refreshed == ["a1", "a2", "b1"]


def test_ensure_fresh_without_key_is_fallback_not_crash(conn):
    seed_sample(conn)
    report = rf.ensure_fresh(conn, client(lambda p, q: pytest.fail("no call"), key=None), NOW)
    assert not report.yelp_available and "key" in report.reason


def test_ensure_records_uses_cache_then_details(conn):
    ds.upsert_cache(conn, [{"yelp_id": "cached"}], source="mock", fetched_at=NOW)

    def handler(path, params):
        return FakeResponse(200, biz(path.rsplit("/", 1)[-1]))
    c = client(handler)
    fresh, not_enriched, reason = rf.ensure_records(conn, c, ["cached", "new"], NOW)
    assert set(fresh["yelp_id"]) == {"cached", "new"} and not_enriched == [] and reason is None
    assert [p for p, _ in c.session.calls] == ["/businesses/new"]


def test_ensure_records_labels_not_enriched_on_error(conn):
    fresh, not_enriched, reason = rf.ensure_records(
        conn, client(lambda p, q: FakeResponse(429)), ["x", "y"], NOW)
    assert fresh.empty and not_enriched == ["x", "y"] and "429" in reason


def test_build_sample_respects_zip_and_per_area(conn):
    pages = {("Chelsea", 0): ["a", "b", "c"]}

    def handler(path, params):
        area = params["location"].split(",")[0]
        ids = pages.get((area, params["offset"]), [])
        return FakeResponse(200, {"businesses": [biz(i, zip_code="99999" if i == "b" else "10001")
                                                 for i in ids]})
    report = rf.build_sample(conn, client(handler), per_area=1, allowed_zips={"10001"},
                             areas=["Chelsea"], now=NOW)
    assert report.refreshed == ["a"]
    assert list(ds.load_sample(conn)["yelp_id"]) == ["a"]


# ---------------------------------------------------------------- fallback end to end

def test_fallback_still_returns_local_results():
    pool = pd.DataFrame([
        ("ref", "Ref", "Italian", 40.73, -74.0, None, None, None, None),
        ("a", "A", "Italian", 40.731, -74.0, None, None, None, None),
    ], columns=["restaurant_id", "name", "categories", "lat", "lon", "rating", "review_count",
                "price_level", "data_fetched_at"])
    ref = pool.iloc[0].to_dict()
    result = recommend(ref, pool, Filters(), yelp_available=False)
    assert [r["restaurant_id"] for r in result.recommendations] == ["a"]
    rec = result.recommendations[0]
    assert rec["rating"] is None and rec["review_count"] is None
    assert "price information is unavailable" in rec["explanation"]


# ---------------------------------------------------------------- pool and local mapping

def _directory(conn, tmp_path):
    csv = tmp_path / "d.csv"
    csv.write_text("nyc_id,name,address,zip,lat,lon,cuisine\n"
                   "1,Fake a1,1 Fake Street,10001,40.7,-74.0,Italian\n"
                   "2,Twin,5 Fake St,10001,,,Thai\n"
                   "3,Twin,5 Fake St,10001,,,Thai\n")
    ds.import_directory_csv(conn, csv, source="synthetic")


def test_map_locally_one_to_one_only(conn, tmp_path):
    _directory(conn, tmp_path)
    recs = [yc.parse_business(biz("a1")),                       # "Fake a1", "1 Fake St"
            {**yc.parse_business(biz("t")), "name": "Twin", "address": "5 Fake St, New York"}]
    assert rf.map_locally(conn, recs, NOW) == ["a1"]           # duplicates are not auto-mapped
    assert ds.load_mappings(conn).iloc[0].method == "name_address"


def test_build_pool_yelp_and_local(conn, tmp_path):
    _directory(conn, tmp_path)
    ds.add_mapping(conn, "1", "y1", method="m", source="synthetic")
    ds.add_mapping(conn, "2", "y2", method="m", source="synthetic")
    ds.upsert_cache(conn, [{"yelp_id": "y1", "name": "Yelp Name", "categories": "Italian, Pizza",
                            "rating": 4.5, "review_count": 10, "price": "$$", "lat": 40.71, "lon": -74.01},
                           {"yelp_id": "y2", "categories": "Thai"}],
                    source="mock", fetched_at=NOW)
    ds.upsert_cache(conn, [{"yelp_id": "y2"}], source="mock", fetched_at=NOW - timedelta(days=2))
    pool = ds.build_pool(conn, use_yelp=True, now=NOW)
    assert list(pool["restaurant_id"]) == ["1"]                # y2 expired -> left out
    row = pool.iloc[0]
    assert row["name"] == "Fake a1"                            # NYC name kept
    assert (row["categories"], row["price_level"], row["lat"]) == ("Italian, Pizza", 2, 40.71)
    local = ds.build_pool(conn, use_yelp=False, now=NOW)
    assert len(local) == 2 and local["rating"].isna().all()  # the two "Twin" records collapse
    assert local.set_index("restaurant_id").loc["2", "categories"] == "Thai"


def test_fresh_cache_usable_without_key(conn):
    seed_sample(conn)
    ds.upsert_cache(conn, [{"yelp_id": i} for i in ("a1", "a2", "b1")], source="mock", fetched_at=NOW)
    report = rf.ensure_fresh(conn, client(lambda p, q: pytest.fail("no call"), key=None), NOW)
    assert report.yelp_available and not report.attempted


# ---------------------------------------------------------------- amendment 2: Yelp-only candidates

def test_pool_includes_unmatched_yelp_listings(conn, tmp_path):
    _directory(conn, tmp_path)
    ds.add_mapping(conn, "1", "y1", method="m", source="synthetic")
    ds.upsert_cache(conn, [
        {"yelp_id": "y1", "name": "Yelp Spelling", "categories": "Italian", "address": "1 Fake St",
         "zip": "10001", "lat": 40.71, "lon": -74.0},
        {"yelp_id": "y9", "name": "Only On Yelp", "categories": "Thai", "address": "9 Fake Ave",
         "zip": "10009", "lat": 40.72, "lon": -73.99},
    ], source="mock", fetched_at=NOW)
    ds.upsert_cache(conn, [{"yelp_id": "old", "name": "Expired", "categories": "Thai"}],
                    source="mock", fetched_at=NOW - timedelta(days=2))
    pool = ds.build_pool(conn, use_yelp=True, now=NOW).set_index("restaurant_id")
    assert set(pool.index) == {"1", "yelp:y9"}                      # expired never included
    assert pool.loc["1", ["name", "name_source"]].tolist() == ["Fake a1", "nyc"]
    assert pool.loc["yelp:y9", ["name", "address", "zip", "name_source"]].tolist() == [
        "Only On Yelp", "9 Fake Ave", "10009", "yelp"]
    local = ds.build_pool(conn, use_yelp=False, now=NOW)
    assert not local["restaurant_id"].str.startswith("yelp:").any()  # fallback: NYC only


def test_old_database_gets_new_cache_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE yelp_cache (yelp_id TEXT PRIMARY KEY, name TEXT, rating REAL,"
                " review_count INTEGER, price TEXT, categories TEXT, lat REAL, lon REAL,"
                " source TEXT NOT NULL, fetched_at TEXT NOT NULL)")
    old.commit(); old.close()
    conn = ds.connect(path)
    ds.upsert_cache(conn, [{"yelp_id": "a", "address": "1 St", "zip": "10001"}], source="mock")
    assert ds.get_fresh_cache(conn).iloc[0]["zip"] == "10001"


def test_reference_yelp_twin_is_never_returned():
    pool = pd.DataFrame([
        ("123", "Same Place", "10001", "y1", "Italian"),
        ("yelp:y1", "Same Place", "10001", "y1", "Italian"),        # same Yelp ID
        ("yelp:y2", "Same  Place!", "10001", "y2", "Italian"),      # same name + ZIP
        ("yelp:y3", "Same Place", "10002", "y3", "Italian"),        # other branch: kept
        ("yelp:y4", "Other Place", "10001", "y4", "Italian"),
    ], columns=["restaurant_id", "name", "zip", "yelp_id", "categories"])
    ref = pool.iloc[0].to_dict()
    result = recommend(ref, pool, Filters(min_rating=None, min_reviews=None, review_ratio=None))
    assert sorted(r["restaurant_id"] for r in result.recommendations) == ["yelp:y3", "yelp:y4"]


def test_yelp_error_code_is_reported_and_bad_token_is_auth_error():
    bad = client(lambda p, q: FakeResponse(400, {"error": {"code": "TOKEN_INVALID", "description": "x"}}))
    with pytest.raises(yc.AuthError, match="TOKEN_INVALID"):
        bad.search("Chelsea")
    other = client(lambda p, q: FakeResponse(400, {"error": {"code": "VALIDATION_ERROR"}}))
    with pytest.raises(yc.RequestFailed, match="HTTP 400, VALIDATION_ERROR"):
        other.search("Chelsea")
