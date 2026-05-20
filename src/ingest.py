"""Download source CSV from sutochno.ru and load into SQLite."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SOURCE_URL = "https://static.sutochno.ru/doc/files/xml/yrl_searchapp_hotels.csv"
DATA_DIR = ROOT / "data"
META_DIR = ROOT / "output" / "meta"
SOURCE_CSV = DATA_DIR / "source.csv"
DB_PATH = DATA_DIR / "feeds.db"

PRICE_RE = re.compile(r"(\d+)")


def download(url: str = SOURCE_URL, dest: Path = SOURCE_CSV) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return dest


def parse_price(raw: str) -> int | None:
    if not raw:
        return None
    m = PRICE_RE.search(raw)
    return int(m.group(1)) if m else None


def parse_float(raw: str) -> float | None:
    try:
        return float(raw) if raw not in ("", None) else None
    except ValueError:
        return None


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS properties (
            property_id  TEXT PRIMARY KEY,
            name         TEXT,
            final_url    TEXT,
            image_url    TEXT,
            destination  TEXT,
            price        INTEGER,
            star_rating  INTEGER,
            score        REAL,
            max_score    REAL,
            facilities   TEXT,
            raw_price    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_destination ON properties(destination);
        CREATE INDEX IF NOT EXISTS idx_score       ON properties(score);
    """)
    return conn


def load_csv_into_db(csv_path: Path = SOURCE_CSV, db_path: Path = DB_PATH) -> int:
    conn = init_db(db_path)
    conn.execute("DELETE FROM properties")

    rows = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch: list[tuple] = []
        for row in reader:
            batch.append((
                row["Property ID"],
                row["Property name"],
                row["Final URL"],
                row["Image URL"],
                row["Destination name"],
                parse_price(row.get("Price", "")),
                int(row["Star rating"]) if row.get("Star rating", "").isdigit() else 0,
                parse_float(row.get("Score", "")),
                parse_float(row.get("Max score", "")),
                row.get("Facilities", ""),
                row.get("Price", ""),
            ))
            if len(batch) >= 1000:
                conn.executemany(
                    "INSERT OR REPLACE INTO properties VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                rows += len(batch)
                batch.clear()
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO properties VALUES (?,?,?,?,?,?,?,?,?,?,?)", batch
            )
            rows += len(batch)

    conn.commit()
    conn.close()
    return rows


def export_meta(db_path: Path = DB_PATH) -> None:
    """Export filter-helper JSON files: city list, amenity list, star list — with counts.
    Consumed by the UI to populate dropdowns from real data."""
    META_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    cities = [
        {"name": name, "count": n}
        for name, n in conn.execute(
            "SELECT destination, COUNT(*) FROM properties "
            "WHERE destination IS NOT NULL AND destination != '' "
            "GROUP BY destination ORDER BY COUNT(*) DESC"
        ).fetchall()
    ]
    (META_DIR / "cities.json").write_text(
        json.dumps(cities, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fac_counter: Counter[str] = Counter()
    for (raw,) in conn.execute(
        "SELECT facilities FROM properties WHERE facilities IS NOT NULL AND facilities != ''"
    ):
        for piece in raw.split(";"):
            piece = piece.strip()
            if piece:
                fac_counter[piece] += 1
    amenities = [{"name": n, "count": c} for n, c in fac_counter.most_common()]
    (META_DIR / "amenities.json").write_text(
        json.dumps(amenities, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    stars = [
        {"stars": s, "count": n}
        for s, n in conn.execute(
            "SELECT star_rating, COUNT(*) FROM properties "
            "GROUP BY star_rating ORDER BY star_rating"
        ).fetchall()
    ]
    (META_DIR / "stars.json").write_text(
        json.dumps(stars, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    conn.close()
    print(f"  meta: {len(cities)} cities, {len(amenities)} amenities, {len(stars)} star groups")


def run() -> None:
    print(f"Downloading {SOURCE_URL}...")
    path = download()
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"  saved {size_mb:.1f} MB → {path}")
    print("Loading into SQLite...")
    n = load_csv_into_db()
    print(f"  inserted {n} rows → {DB_PATH}")
    print("Exporting meta JSON...")
    export_meta()


if __name__ == "__main__":
    run()
