"""Admin commands for the local database. None of these call Yelp.

    python scripts/manage.py import-directory path/to/nyc_restaurants_ml.csv
    python scripts/manage.py export-seed      # writes seed/*.csv (NYC data + Yelp IDs only)
    python scripts/manage.py import-seed      # loads seed/*.csv into an empty database
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from restaurant_alts import data_store as ds  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the restaurant database.")
    parser.add_argument("action", choices=["import-directory", "export-seed", "import-seed"])
    parser.add_argument("path", nargs="?", help="CSV to import (import-directory)")
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "data/restaurants.sqlite"))
    parser.add_argument("--seed", default="seed")
    parser.add_argument("--source", default="nyc_dohmh_open_data")
    args = parser.parse_args(argv)
    conn = ds.connect(args.db)

    if args.action == "import-directory":
        if not args.path:
            parser.error("import-directory needs the CSV path")
        print(f"Imported {ds.import_directory_csv(conn, args.path, source=args.source)} restaurants.")
    elif args.action == "export-seed":
        for table in ds.PERMANENT_TABLES:
            n = ds.export_table_csv(conn, table, Path(args.seed) / f"{table}.csv")
            print(f"{table}: {n} rows")
        ds.mark_seed_synced(conn, args.seed)  # this DB already matches the new seed
    else:
        print(ds.import_seed(conn, args.seed) or "Nothing imported (already up to date, locked, or no seed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
