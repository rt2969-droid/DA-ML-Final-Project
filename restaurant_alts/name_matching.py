"""Local restaurant-name lookup: normalize -> exact -> alias -> fuzzy (top 5).

Rules (spec section 5):
- A fuzzy score never establishes identity: fuzzy results are suggestions only.
- Several branches or duplicate names always require the user to choose.
- Only a single exact or alias match to one restaurant is pre-selected, and the
  UI still shows it for confirmation.
- No match returns a clear empty state.

Works on plain DataFrames (from data_store.load_directory / load_aliases), so it never
touches the database or Yelp.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz, process

FUZZY_LIMIT = 5
FUZZY_CUTOFF = 70  # WRatio 0-100; below this a name is not shown as a suggestion

EXACT, ALIAS, SUGGESTIONS, NO_MATCH = "exact", "alias", "suggestions", "no_match"


# ---------------------------------------------------------------- normalization

def normalize_name(value) -> str:
    """Lowercase, strip accents, '&' -> 'and', drop apostrophes, other punctuation -> space."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text)  # Joe's -> joes
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


_ADDRESS_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "west": "w", "east": "e",
    "north": "n", "south": "s", "place": "pl", "boulevard": "blvd",
}


def normalize_address(value) -> str:
    """First comma section, normalized; '10th' -> '10', 'Street' -> 'st'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = normalize_name(str(value).split(",")[0])
    text = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1", text)
    for word, short in _ADDRESS_ABBREVIATIONS.items():
        text = re.sub(rf"\b{word}\b", short, text)
    return text


# ---------------------------------------------------------------- results

@dataclass(frozen=True)
class Candidate:
    nyc_id: str
    name: str
    address: str | None
    zip: str | None
    score: float  # 100 for exact/alias; WRatio for fuzzy
    method: str   # exact | alias | fuzzy


@dataclass
class MatchResult:
    query: str
    status: str
    candidates: list[Candidate] = field(default_factory=list)
    selected_id: str | None = None
    message: str = ""

    @property
    def requires_choice(self) -> bool:
        return self.selected_id is None and bool(self.candidates)


# ---------------------------------------------------------------- duplicates

def collapse_duplicates(directory: pd.DataFrame, preferred_ids=None) -> pd.DataFrame:
    """One row per (normalized name, normalized address, ZIP).

    The NYC file can hold several records for one restaurant (e.g. a re-permit). Keeps,
    in order: a record in ``preferred_ids`` (e.g. matched to Yelp), one with a cuisine,
    the lowest nyc_id. Different addresses (real branches) are never merged.
    """
    if directory.empty:
        return directory
    preferred = set(preferred_ids or ())
    d = directory.assign(
        _k=directory["name"].map(normalize_name) + "|" + directory["address"].map(normalize_address)
           + "|" + directory["zip"].fillna("").astype(str),
        _pref=~directory["nyc_id"].isin(preferred),
        _nocuisine=directory["cuisine"].isna() if "cuisine" in directory.columns else False,
    )
    d = d.sort_values(["_pref", "_nocuisine", "nyc_id"], kind="mergesort")
    d = d.drop_duplicates("_k", keep="first").sort_index()
    return d.drop(columns=["_k", "_pref", "_nocuisine"])


# ---------------------------------------------------------------- matching

def match_restaurant(
    query: str,
    directory: pd.DataFrame,
    aliases: pd.DataFrame | None = None,
    limit: int = FUZZY_LIMIT,
    cutoff: float = FUZZY_CUTOFF,
    preferred_ids=None,
) -> MatchResult:
    """Find the restaurant a user typed. ``directory`` needs nyc_id, name, address, zip.

    Duplicate records of the same restaurant (same name, address and ZIP) are shown once;
    ``preferred_ids`` picks which record represents them.
    """
    key = normalize_name(query)
    if not key:
        return MatchResult(query, NO_MATCH, message="Enter a restaurant name to search.")
    directory = collapse_duplicates(directory, preferred_ids)

    names = directory["name"].map(normalize_name)

    # 1. Exact match on normalized names
    exact = directory[names.eq(key)]
    if not exact.empty:
        return _decide(query, EXACT, _candidates(exact, 100.0, "exact"))

    # 2. Alias match
    if aliases is not None and not aliases.empty:
        alias_ids = aliases.loc[aliases["alias"].map(normalize_name).eq(key), "nyc_id"]
        hits = directory[directory["nyc_id"].isin(set(alias_ids))]
        if not hits.empty:
            return _decide(query, ALIAS, _candidates(hits, 100.0, "alias"))

    # 3. Fuzzy suggestions (never a selection)
    choices = dict(zip(directory["nyc_id"], names))
    found = process.extract(key, choices, scorer=fuzz.WRatio, limit=limit, score_cutoff=cutoff)
    if not found:
        return MatchResult(
            query, NO_MATCH,
            message=f"No restaurant in our Manhattan list matches “{query.strip()}”. "
                    "Check the spelling or try a shorter name.",
        )
    scores = {nyc_id: score for _, score, nyc_id in found}
    rows = directory[directory["nyc_id"].isin(scores)]
    cands = [
        Candidate(r.nyc_id, r.name, _opt(r.address), _opt(r.zip), round(scores[r.nyc_id], 1), "fuzzy")
        for r in rows.itertuples(index=False)
    ]
    return MatchResult(
        query, SUGGESTIONS, _sorted(cands),
        message="No exact match. Did you mean one of these? Please choose the right restaurant.",
    )


def _decide(query: str, status: str, cands: list[Candidate]) -> MatchResult:
    if len(cands) == 1:
        c = cands[0]
        return MatchResult(query, status, cands, selected_id=c.nyc_id,
                           message=f"Found {c.name}. Please confirm this is the right restaurant.")
    return MatchResult(
        query, status, cands,
        message=f"Found {len(cands)} restaurants with this name. Choose the right address.",
    )


def _candidates(rows: pd.DataFrame, score: float, method: str) -> list[Candidate]:
    return _sorted([
        Candidate(r.nyc_id, r.name, _opt(r.address), _opt(r.zip), score, method)
        for r in rows.itertuples(index=False)
    ])


def _sorted(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (-c.score, c.name.lower(), c.address or "", c.nyc_id))


def _opt(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return str(value)
