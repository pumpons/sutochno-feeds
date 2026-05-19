"""Download source CSV from sutochno.ru and load into SQLite."""
from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

import requests

SOURCE_URL = "https://static.sutochno.ru/doc/files/xml/yrl_searchapp_hotels.csv"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
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


def run() -> None:
    print(f"Downloading {SOURCE_URL}...")
    path = download()
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"  saved {size_mb:.1f} MB → {path}")
    print("Loading into SQLite...")
    n = load_csv_into_db()
    print(f"  inserted {n} rows → {DB_PATH}")


if __name__ == "__main__":
    run()
