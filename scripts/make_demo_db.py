"""Build a SYNTHETIC demo database so the app can be tried without NYC files or a Yelp key.

Every restaurant, address and rating here is made up. The "Yelp" rows are fake and
marked source="synthetic_demo"; like real cache rows they expire 24 hours after creation
(rerun this script to renew them).

    python scripts/make_demo_db.py
    DATABASE_PATH=data/demo.sqlite streamlit run app.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from restaurant_alts import data_store as ds  # noqa: E402

CUISINES = {
    "Italian": ["Italian, Pizza", "Italian, Wine Bars", "Italian", "Italian, Pasta Shops"],
    "Thai": ["Thai", "Thai, Noodles", "Thai, Asian Fusion"],
    "Mexican": ["Mexican", "Mexican, Tacos", "Mexican, Cocktail Bars"],
    "Japanese": ["Japanese, Sushi Bars", "Ramen, Japanese", "Japanese, Izakaya"],
    "American": ["New American", "American (Traditional), Burgers", "New American, Wine Bars"],
}
WORDS = ["Copper", "Juniper", "Lantern", "Maple", "Harbor", "Saffron", "Willow", "Ember",
         "Cobalt", "Fig", "Sparrow", "Clover", "Basil", "Quarry", "Atlas", "Marigold"]
KINDS = ["Kitchen", "House", "Table", "Room", "Corner", "Social", "Counter", "Garden"]


def main(path: str = "data/demo.sqlite", n: int = 60, seed: int = 7) -> None:
    rng = random.Random(seed)
    Path(path).unlink(missing_ok=True)
    conn = ds.connect(path)
    ds.lock_seed(conn)  # keep the demo data; never replace it from seed/
    now = ds.utc_now()
    rows, cache = [], []
    names = set()
    for i in range(n):
        family = rng.choice(list(CUISINES))
        while (name := f"{rng.choice(WORDS)} {rng.choice(KINDS)}") in names:
            pass
        names.add(name)
        nyc_id = f"demo-{i:03d}"
        lat = 40.705 + rng.random() * 0.09
        lon = -74.015 + rng.random() * 0.05
        rows.append((nyc_id, name, f"{rng.randint(1, 400)} Example St", f"100{rng.randint(1, 40):02d}",
                     lat, lon, family))
        cache.append({"yelp_id": f"fake-yelp-{i:03d}", "name": name,
                      "rating": rng.choice([3.5, 4.0, 4.0, 4.5, 4.5, 5.0]),
                      "review_count": rng.choice([15, 40, 90, 150, 300, 800, 2000]),
                      "price": rng.choice(["$", "$$", "$$", "$$$", None]),
                      "categories": rng.choice(CUISINES[family]), "lat": lat, "lon": lon})
    # Two branches with the same name, to try branch selection.
    rows.append(("demo-900", "Copper Kitchen", "12 Sample Ave", "10011", 40.738, -73.999, "Italian"))
    rows.append(("demo-901", "Copper Kitchen", "88 Sample Ave", "10024", 40.785, -73.976, "Italian"))

    csv = Path(path).with_suffix(".directory.csv")
    csv.write_text("nyc_id,name,address,zip,lat,lon,cuisine\n" + "\n".join(
        ",".join(str(v) for v in r) for r in rows) + "\n")
    ds.import_directory_csv(conn, csv, source="synthetic_demo")
    csv.unlink()
    for i, c in enumerate(cache):
        ds.add_mapping(conn, f"demo-{i:03d}", c["yelp_id"], method="synthetic", source="synthetic_demo")
    ds.add_to_sample(conn, [c["yelp_id"] for c in cache], search_area="Demo")
    ds.upsert_cache(conn, cache, source="synthetic_demo", fetched_at=now)
    print(f"Wrote {path}: {len(rows)} synthetic restaurants, {len(cache)} fake Yelp rows "
          "(expire in 24h).")


if __name__ == "__main__":
    main(*sys.argv[1:2])
