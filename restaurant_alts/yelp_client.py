"""All Yelp access lives here, so it can be swapped or disabled without touching other modules.

- Calls happen only when the app asks (on the "Find alternatives" click); nothing here
  schedules or pre-fetches.
- Every HTTP attempt is counted per session and per UTC day; a local daily cap stops calls
  before Yelp's trial quota (about 5,000 total) runs out.
- Missing key, auth errors (401/403), rate limits (429), the local cap, and network or
  server errors all raise a ``YelpUnavailable`` subclass, so callers can fall back to
  local-only results.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests

API_ROOT = "https://api.yelp.com/v3"
TIMEOUT_SECONDS = 15
DEFAULT_DAILY_LIMIT = 300


# ---------------------------------------------------------------- errors

class YelpUnavailable(Exception):
    """Base class: Yelp can't be used right now; fall back to local data."""
    reason = "Yelp data is unavailable."


class MissingKey(YelpUnavailable):
    reason = "No Yelp API key is configured."


class AuthError(YelpUnavailable):
    reason = "Yelp rejected the API key (expired or invalid)."


class RateLimited(YelpUnavailable):
    reason = "Yelp's rate limit or quota was reached (HTTP 429)."


class QuotaExceeded(YelpUnavailable):
    reason = "This app's daily Yelp call limit was reached."


class RequestFailed(YelpUnavailable):
    reason = "The Yelp request failed (network or server error)."


# Yelp error codes that mean the key is the problem (sometimes sent with HTTP 400).
AUTH_CODES = {"TOKEN_INVALID", "TOKEN_MISSING", "UNAUTHORIZED_ACCESS_TOKEN", "INVALID_AUTHORIZATION_METHOD"}


def _error_code(response) -> str | None:
    """Yelp's short error code (e.g. 'TOKEN_INVALID'), never the request or key."""
    if response.ok:
        return None
    try:
        code = (response.json().get("error") or {}).get("code")
    except Exception:
        return None
    return str(code)[:40] if code else None


# ---------------------------------------------------------------- call counting

class CallCounter:
    """Counts calls this session, and per UTC day (in SQLite when a connection is given)."""

    def __init__(self, conn: sqlite3.Connection | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.session_calls = 0
        self._memory_days: dict[str, int] = {}
        if conn is not None:
            conn.execute("CREATE TABLE IF NOT EXISTS yelp_calls (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
            conn.commit()

    def _day(self) -> str:
        return self.clock().astimezone(timezone.utc).date().isoformat()

    def today(self) -> int:
        day = self._day()
        if self.conn is None:
            return self._memory_days.get(day, 0)
        row = self.conn.execute("SELECT calls FROM yelp_calls WHERE day = ?", (day,)).fetchone()
        return row[0] if row else 0

    def record(self) -> None:
        self.session_calls += 1
        day = self._day()
        if self.conn is None:
            self._memory_days[day] = self._memory_days.get(day, 0) + 1
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO yelp_calls (day, calls) VALUES (?, 1)"
                " ON CONFLICT(day) DO UPDATE SET calls = calls + 1", (day,))


# ---------------------------------------------------------------- parsing

def parse_business(business: dict) -> dict:
    """Yelp business JSON -> our cache record (plus address fields used for matching)."""
    location = business.get("location") or {}
    coords = business.get("coordinates") or {}
    return {
        "yelp_id": business.get("id"),
        "name": business.get("name"),
        "rating": business.get("rating"),
        "review_count": business.get("review_count"),
        "price": business.get("price"),
        "categories": ", ".join(c.get("title", "") for c in business.get("categories") or []),
        "lat": coords.get("latitude"),
        "lon": coords.get("longitude"),
        "zip": str(location.get("zip_code") or "").strip() or None,
        "address": ", ".join(location.get("display_address") or []) or None,
        "is_closed": business.get("is_closed"),
    }


# ---------------------------------------------------------------- client

def load_api_key() -> str | None:
    """YELP_API_KEY from the environment, else Streamlit secrets. None when absent."""
    key = os.environ.get("YELP_API_KEY", "").strip()
    if key:
        return key
    from pathlib import Path
    secret_files = (Path(".streamlit/secrets.toml"), Path.home() / ".streamlit/secrets.toml")
    if not any(p.exists() for p in secret_files):
        return None  # touching st.secrets without a file prints an error in the app
    try:
        import streamlit as st
        return str(st.secrets.get("YELP_API_KEY", "")).strip() or None
    except Exception:  # streamlit missing, or unreadable secrets
        return None


@dataclass
class YelpClient:
    api_key: str | None
    daily_limit: int = DEFAULT_DAILY_LIMIT
    counter: CallCounter = field(default_factory=CallCounter)
    session: requests.Session = field(default_factory=requests.Session)
    disabled: bool = False

    @property
    def available(self) -> bool:
        return bool(self.api_key) and not self.disabled

    def _get(self, path: str, params: dict | None = None) -> dict:
        if self.disabled:
            raise YelpUnavailable("Yelp access is disabled.")
        if not self.api_key:
            raise MissingKey(MissingKey.reason)
        if self.counter.today() >= self.daily_limit:
            raise QuotaExceeded(QuotaExceeded.reason)
        self.counter.record()  # count every attempt, successful or not
        try:
            response = self.session.get(
                f"{API_ROOT}{path}", params=params or {}, timeout=TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except requests.RequestException as error:
            raise RequestFailed(f"{RequestFailed.reason} ({type(error).__name__})") from error
        code = _error_code(response)
        if response.status_code in (401, 403) or code in AUTH_CODES:
            raise AuthError(f"{AuthError.reason} (HTTP {response.status_code}{', ' + code if code else ''})")
        if response.status_code == 429:
            raise RateLimited(RateLimited.reason)
        if not response.ok:
            detail = f"HTTP {response.status_code}" + (f", {code}" if code else "")
            raise RequestFailed(f"{RequestFailed.reason} ({detail})")
        try:
            return response.json()
        except ValueError as error:
            raise RequestFailed(f"{RequestFailed.reason} (bad JSON)") from error

    # -- endpoints

    def search(self, location: str, offset: int = 0, limit: int = 50) -> list[dict]:
        """Business Search for restaurants in an area. One call."""
        data = self._get("/businesses/search", {
            "term": "restaurants", "categories": "restaurants", "location": location,
            "limit": limit, "offset": offset, "sort_by": "best_match",
        })
        return [parse_business(b) for b in data.get("businesses") or [] if b.get("id")]

    def details(self, yelp_id: str) -> dict:
        """Business Details for one ID. One call."""
        return parse_business(self._get(f"/businesses/{yelp_id}"))

    def business_match(self, name: str, address: str, zip_code: str | None = None,
                       city: str = "New York", state: str = "NY") -> str | None:
        """Yelp ID for a NYC record, or None when the match is uncertain. One call.

        Only a single result whose ZIP agrees (when we know ours) counts as a match.
        """
        params = {"name": name[:64], "address1": (address or "")[:64], "city": city,
                  "state": state, "country": "US", "match_threshold": "default"}
        if zip_code:
            params["postal_code"] = zip_code
        found = self._get("/businesses/matches", params).get("businesses") or []
        if len(found) != 1:
            return None
        hit = found[0]
        hit_zip = str((hit.get("location") or {}).get("zip_code") or "").strip()
        if zip_code and hit_zip and hit_zip != str(zip_code).strip():
            return None
        return hit.get("id")


def client_from_environment(conn: sqlite3.Connection | None = None) -> YelpClient:
    """Build the app's client from env/secrets. Works (as unavailable) with no key."""
    limit = int(os.environ.get("YELP_DAILY_CALL_LIMIT", DEFAULT_DAILY_LIMIT))
    disabled = os.environ.get("YELP_DISABLED", "").lower() in {"1", "true", "yes"}
    return YelpClient(api_key=load_api_key(), daily_limit=limit,
                      counter=CallCounter(conn), disabled=disabled)
