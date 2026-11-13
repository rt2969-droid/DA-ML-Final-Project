"""SQLite storage for the NYC directory, Yelp ID mappings, aliases, and the 24h Yelp cache.

Lifetimes (see docs/SPEC.md):
- directory, mappings, aliases, yelp_sample: permanent (NYC data, Yelp IDs, our own metadata).
- yelp_cache: Yelp content, valid for at most 24 hours after ``fetched_at``.

Expiry is checked here, in plain Python, never through Streamlit's cache. Every cache
read filters out expired rows, so stale Yelp data is never returned even if a purge
has not run yet. The app must work with an empty cache.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

CACHE_TTL = timedelta(hours=24)

SCHEMA = """
CREATE TABLE IF NOT EXISTS directory (
    nyc_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT,
    zip         TEXT,
    lat         REAL,
    lon         REAL,
    cuisine     TEXT,
    source      TEXT NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mappings (
    nyc_id     TEXT PRIMARY KEY REFERENCES directory(nyc_id),
    yelp_id    TEXT NOT NULL UNIQUE,
    matched_at TEXT NOT NULL,
    method     TEXT NOT NULL,
    source     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS aliases (
    alias      TEXT NOT NULL,
    nyc_id     TEXT NOT NULL REFERENCES directory(nyc_id),
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (alias, nyc_id)
);

-- Fixed sample of Yelp business IDs that refresh.py re-fetches (IDs only; permanent).
CREATE TABLE IF NOT EXISTS yelp_sample (
    yelp_id     TEXT PRIMARY KEY,
    search_area TEXT,
    added_at    TEXT NOT NULL
);

-- Yelp content: expires CACHE_TTL after fetched_at.
CREATE TABLE IF NOT EXISTS yelp_cache (
    yelp_id      TEXT PRIMARY KEY,
    name         TEXT,
    rating       REAL,
    review_count INTEGER,
    price        TEXT,
    categories   TEXT,
    lat          REAL,
    lon          REAL,
    address      TEXT,
    zip          TEXT,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
"""

# Columns added after the first release; connect() adds them to older databases.
_CACHE_MIGRATIONS = {"address": "TEXT", "zip": "TEXT"}

PERMANENT_TABLES = ("directory", "mappings", "aliases", "yelp_sample")

# Column names in the NYC DOHMH export -> our directory columns.
NYC_COLUMN_MAP = {
    "camis": "nyc_id",
    "dba": "name",
    "zipcode": "zip",
    "latitude": "lat",
    "longitude": "lon",
    "cuisine_description": "cuisine",
}

PRICE_LEVELS = {"$": 1, "$$": 2, "$$$": 3, "$$$$": 4}


# ---------------------------------------------------------------- time helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Timestamps must be timezone-aware (UTC).")
    return moment.astimezone(timezone.utc).isoformat()


def from_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"Stored timestamp has no timezone: {text!r}")
    return moment


def is_expired(fetched_at: str, now: datetime | None = None) -> bool:
    """True when a cached Yelp record is 24 hours old or older (or unreadable)."""
    now = now or utc_now()
    try:
        return now - from_iso(fetched_at) >= CACHE_TTL
    except (TypeError, ValueError):
        return True


def price_level(price: str | None) -> int | None:
    """'$$' -> 2. Unknown or malformed price stays None (unknown is not cheap)."""
    return PRICE_LEVELS.get(price) if isinstance(price, str) else None


# ---------------------------------------------------------------- connection

def connect(path: str | Path = "data/restaurants.sqlite") -> sqlite3.Connection:
    """Open (and create if needed) the database. Use ':memory:' for tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(yelp_cache)")}
    for column, kind in _CACHE_MIGRATIONS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE yelp_cache ADD COLUMN {column} {kind}")
    conn.commit()
    return conn


# ---------------------------------------------------------------- loaders

def _read(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=list(params))


def load_directory(conn: sqlite3.Connection) -> pd.DataFrame:
    return _read(conn, "SELECT * FROM directory ORDER BY nyc_id")


def load_mappings(conn: sqlite3.Connection) -> pd.DataFrame:
    return _read(conn, "SELECT * FROM mappings ORDER BY nyc_id")


def load_aliases(conn: sqlite3.Connection) -> pd.DataFrame:
    return _read(conn, "SELECT * FROM aliases ORDER BY alias, nyc_id")


def load_sample(conn: sqlite3.Connection) -> pd.DataFrame:
    return _read(conn, "SELECT * FROM yelp_sample ORDER BY yelp_id")


# ---------------------------------------------------------------- permanent writes

def add_mapping(
    conn: sqlite3.Connection,
    nyc_id: str,
    yelp_id: str,
    method: str,
    source: str,
    matched_at: datetime | None = None,
) -> None:
    """Map one NYC record to one Yelp ID.

    Re-mapping the same nyc_id updates it. A yelp_id already mapped to a DIFFERENT
    restaurant raises sqlite3.IntegrityError (never silently moves the mapping).
    """
    with conn:
        conn.execute(
            "INSERT INTO mappings (nyc_id, yelp_id, matched_at, method, source)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(nyc_id) DO UPDATE SET yelp_id = excluded.yelp_id,"
            " matched_at = excluded.matched_at, method = excluded.method, source = excluded.source",
            (nyc_id, yelp_id, to_iso(matched_at or utc_now()), method, source),
        )


def add_alias(
    conn: sqlite3.Connection,
    alias: str,
    nyc_id: str,
    source: str,
    created_at: datetime | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, nyc_id, source, created_at)"
            " VALUES (?, ?, ?, ?)",
            (alias, nyc_id, source, to_iso(created_at or utc_now())),
        )


def add_to_sample(
    conn: sqlite3.Connection,
    yelp_ids: Iterable[str],
    search_area: str | None = None,
    added_at: datetime | None = None,
) -> None:
    stamp = to_iso(added_at or utc_now())
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO yelp_sample (yelp_id, search_area, added_at) VALUES (?, ?, ?)",
            [(yid, search_area, stamp) for yid in yelp_ids],
        )


# ---------------------------------------------------------------- yelp cache

CACHE_FIELDS = ("name", "rating", "review_count", "price", "categories", "lat", "lon",
                "address", "zip")


def upsert_cache(
    conn: sqlite3.Connection,
    records: Iterable[Mapping],
    source: str,
    fetched_at: datetime | None = None,
) -> int:
    """Store Yelp records. Each needs 'yelp_id'; fetched_at defaults to now (UTC).

    A record may carry its own 'fetched_at' (datetime); otherwise the call's value is used.
    """
    default_stamp = fetched_at or utc_now()
    rows = []
    for rec in records:
        if not rec.get("yelp_id"):
            raise ValueError("Every cached record needs a yelp_id.")
        stamp = rec.get("fetched_at") or default_stamp
        rows.append(
            (rec["yelp_id"], *(rec.get(f) for f in CACHE_FIELDS), source, to_iso(stamp))
        )
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO yelp_cache"
            f" (yelp_id, {', '.join(CACHE_FIELDS)}, source, fetched_at)"
            f" VALUES ({', '.join('?' * (len(CACHE_FIELDS) + 3))})",
            rows,
        )
    return len(rows)


def get_fresh_cache(
    conn: sqlite3.Connection,
    yelp_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Cached Yelp rows younger than 24h. Expired rows are never returned."""
    now = now or utc_now()
    if yelp_ids is None:
        df = _read(conn, "SELECT * FROM yelp_cache")
    else:
        ids = list(dict.fromkeys(yelp_ids))
        if not ids:
            return _read(conn, "SELECT * FROM yelp_cache WHERE 0")
        marks = ",".join("?" * len(ids))
        df = _read(conn, f"SELECT * FROM yelp_cache WHERE yelp_id IN ({marks})", ids)
    fresh = ~df["fetched_at"].map(lambda s: is_expired(s, now)).astype(bool)
    df = df[fresh].reset_index(drop=True)
    df["price_level"] = df["price"].map(price_level).astype("Int64")
    return df


def cache_is_stale(
    conn: sqlite3.Connection,
    yelp_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> bool:
    """True when any requested ID (default: the whole sample) lacks a fresh cache row."""
    if yelp_ids is None:
        yelp_ids = load_sample(conn)["yelp_id"].tolist()
    wanted = set(yelp_ids)
    if not wanted:
        return False
    fresh = set(get_fresh_cache(conn, wanted, now)["yelp_id"])
    return bool(wanted - fresh)


def purge_expired(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete expired Yelp rows. Returns how many were removed."""
    now = now or utc_now()
    rows = conn.execute("SELECT yelp_id, fetched_at FROM yelp_cache").fetchall()
    expired = [(r["yelp_id"],) for r in rows if is_expired(r["fetched_at"], now)]
    with conn:
        conn.executemany("DELETE FROM yelp_cache WHERE yelp_id = ?", expired)
    return len(expired)


def clear_cache(conn: sqlite3.Connection) -> None:
    """Drop all Yelp content (the cache is always rebuildable)."""
    with conn:
        conn.execute("DELETE FROM yelp_cache")


# ---------------------------------------------------------------- CSV import / export

def import_directory_csv(
    conn: sqlite3.Connection,
    path: str | Path,
    source: str,
    imported_at: datetime | None = None,
) -> int:
    """Load NYC restaurant records. Accepts our column names or the NYC DOHMH names.

    Duplicate nyc_ids keep the first row (inspection data repeats each restaurant).
    Returns the number of restaurants stored.
    """
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.rename(columns=NYC_COLUMN_MAP)
    missing = {"nyc_id", "name"} - set(df.columns)
    if missing:
        raise ValueError(f"Directory CSV is missing columns: {sorted(missing)}")
    for col in ("address", "zip", "lat", "lon", "cuisine"):
        if col not in df.columns:
            df[col] = None
    df["nyc_id"] = df["nyc_id"].str.strip()
    df = df[df["nyc_id"].notna() & df["nyc_id"].ne("") & df["name"].notna()]
    df = df.drop_duplicates(subset="nyc_id", keep="first")
    for col in ("lat", "lon"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
        # NYC data uses 0 for "no coordinates".
        df.loc[df[col].eq(0), col] = None
    stamp = to_iso(imported_at or utc_now())
    rows = [
        (r.nyc_id, r.name.strip(), _clean(r.address), _clean(r.zip),
         _num(r.lat), _num(r.lon), _clean(r.cuisine), source, stamp)
        for r in df.itertuples(index=False)
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO directory"
            " (nyc_id, name, address, zip, lat, lon, cuisine, source, imported_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def import_table_csv(conn: sqlite3.Connection, table: str, path: str | Path) -> int:
    """Re-import a CSV produced by export_table_csv (permanent tables only)."""
    _check_permanent(table)
    df = pd.read_csv(path, dtype=str).where(lambda d: d.notna(), None)
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    extra = set(df.columns) - set(cols)
    if extra:
        raise ValueError(f"Unexpected columns for {table}: {sorted(extra)}")
    marks = ",".join("?" * len(df.columns))
    with conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(df.columns)}) VALUES ({marks})",
            df.itertuples(index=False, name=None),
        )
    return len(df)


def export_table_csv(conn: sqlite3.Connection, table: str, path: str | Path) -> int:
    """Write a permanent table to CSV. The Yelp cache is never exported."""
    _check_permanent(table)
    df = _read(conn, f"SELECT * FROM {table}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)


def _check_permanent(table: str) -> None:
    if table not in PERMANENT_TABLES:
        raise ValueError(
            f"{table!r} cannot be imported/exported as CSV; allowed: {PERMANENT_TABLES}. "
            "Yelp content must not leave the 24-hour cache."
        )


def _clean(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _num(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


# ---------------------------------------------------------------- candidate pool

POOL_COLUMNS = ["restaurant_id", "name", "address", "zip", "yelp_id", "categories", "lat", "lon",
                "rating", "review_count", "price_level", "data_fetched_at", "name_source"]

YELP_ONLY_PREFIX = "yelp:"


def build_pool(conn: sqlite3.Connection, use_yelp: bool, now: datetime | None = None) -> pd.DataFrame:
    """Restaurants in the recommender's input format.

    use_yelp=True (amendment 2): every FRESH Yelp record, matched to NYC or not.
      - Matched: restaurant_id = NYC id, NYC name/address (the permanent record).
      - Unmatched: restaurant_id = "yelp:<id>", Yelp name/address (24h, like other Yelp fields).
      Categories, coordinates, rating, reviews and price come from Yelp (NYC coordinates
      fill gaps). Expired records are never included.
    use_yelp=False: every NYC record; NYC cuisine as categories, Yelp fields missing.
    """
    directory = load_directory(conn)
    if not use_yelp:
        from restaurant_alts.name_matching import collapse_duplicates
        mapped = set(load_mappings(conn)["nyc_id"])
        pool = collapse_duplicates(directory, mapped).rename(columns={"nyc_id": "restaurant_id", "cuisine": "categories"})
        for col in ("yelp_id", "rating", "review_count", "price_level", "data_fetched_at"):
            pool[col] = None
        pool["name_source"] = "nyc"
        return pool[POOL_COLUMNS]

    fresh = get_fresh_cache(conn, now=now).rename(columns={"fetched_at": "data_fetched_at"})
    nyc = directory.rename(columns={c: f"nyc_{c}" for c in directory.columns if c != "nyc_id"})
    pool = (fresh.merge(load_mappings(conn)[["nyc_id", "yelp_id"]], on="yelp_id", how="left")
                 .merge(nyc, on="nyc_id", how="left"))
    matched = pool["nyc_id"].notna() & pool["nyc_name"].notna()
    pool["restaurant_id"] = pool["nyc_id"].where(matched, YELP_ONLY_PREFIX + pool["yelp_id"])
    for col in ("name", "address", "zip"):
        pool[col] = pool[f"nyc_{col}"].where(matched, pool[col])
    for col in ("lat", "lon"):
        pool[col] = pool[col].astype(float).fillna(pool[f"nyc_{col}"].astype(float))
    pool["name_source"] = matched.map({True: "nyc", False: "yelp"})
    return pool[POOL_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------- seed files (deployment)

def _meta(conn: sqlite3.Connection, key: str, value: str | None = None) -> str | None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    if value is not None:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        return value
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def seed_signature(folder: str | Path = "seed") -> str | None:
    """Hash of the seed CSVs, or None when there is no seed."""
    import hashlib
    folder = Path(folder)
    if not (folder / "directory.csv").exists():
        return None
    digest = hashlib.sha256()
    for table in PERMANENT_TABLES:
        path = folder / f"{table}.csv"
        if path.exists():
            digest.update(table.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def mark_seed_synced(conn: sqlite3.Connection, folder: str | Path = "seed") -> None:
    """Record that this database already matches the seed (used after export-seed)."""
    sig = seed_signature(folder)
    if sig:
        _meta(conn, "seed_signature", sig)


def lock_seed(conn: sqlite3.Connection) -> None:
    """Never replace this database from seed/ (e.g. the synthetic demo database)."""
    _meta(conn, "seed_lock", "1")


def import_seed(conn: sqlite3.Connection, folder: str | Path = "seed") -> dict[str, int]:
    """Make the permanent tables match <folder>/*.csv whenever the seed files change.

    Streamlit Cloud has no reliable disk and keeps old files across redeploys, so the app
    calls this on start. When the seed differs from what this database was loaded from,
    the permanent tables are replaced and the Yelp cache is cleared (it may hold
    restaurants from an old sample; it is rebuilt on the next click). Seed files hold NYC
    data and Yelp business IDs only, never Yelp content.
    """
    sig = seed_signature(folder)
    if sig is None or _meta(conn, "seed_lock") or _meta(conn, "seed_signature") == sig:
        return {}
    folder = Path(folder)
    with conn:
        for table in ("aliases", "mappings", "yelp_sample", "directory"):  # FK-safe order
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM yelp_cache")
    loaded = {}
    for table in PERMANENT_TABLES:  # directory first (foreign keys)
        path = folder / f"{table}.csv"
        if path.exists():
            loaded[table] = import_table_csv(conn, table, path)
    _meta(conn, "seed_signature", sig)
    return loaded
