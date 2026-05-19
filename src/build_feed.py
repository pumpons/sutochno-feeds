"""Apply segment filter to DB, render images, emit output CSV in Google Hotel format."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .render import RenderJob, render_jobs

ROOT = Path(__file__).resolve().parent.parent
FEEDS_DIR = ROOT / "output" / "feeds"
DB_PATH = ROOT / "data" / "feeds.db"

OUTPUT_COLUMNS = [
    "Property ID",
    "Property name",
    "Final URL",
    "Image URL",
    "Destination name",
    "Price",
    "Star rating",
    "Score",
    "Max score",
    "Facilities",
]


def select_rows(conn: sqlite3.Connection, flt: dict, limit: int = 0) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list = []

    if dests := flt.get("destination_in"):
        placeholders = ",".join("?" for _ in dests)
        where.append(f"destination IN ({placeholders})")
        params.extend(dests)
    if (min_score := flt.get("min_score")) is not None:
        where.append("score >= ?")
        params.append(min_score)
    if (max_price := flt.get("max_price")) is not None:
        where.append("price <= ?")
        params.append(max_price)
    if (min_price := flt.get("min_price")) is not None:
        where.append("price >= ?")
        params.append(min_price)

    sql = "SELECT * FROM properties"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY score DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def build(segment: dict, base_image_url: str) -> Path:
    seg_id = segment["id"]
    template = segment["template"]
    limit = segment.get("limit", 0)

    print(f"\n[segment {seg_id}]")
    conn = sqlite3.connect(DB_PATH)
    rows = select_rows(conn, segment.get("filter", {}), limit=limit)
    conn.close()
    print(f"  matched: {len(rows)} rows")

    if not rows:
        print("  nothing to do")
        return FEEDS_DIR / f"{seg_id}.csv"

    jobs = [
        RenderJob(
            property_id=r["property_id"],
            template=template,
            context={
                "image_url":   r["image_url"],
                "name":        r["name"],
                "destination": r["destination"],
                "price":       r["price"] or "—",
                "score":       r["score"] or 0,
            },
        )
        for r in rows
    ]
    image_paths = render_jobs(jobs)

    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FEEDS_DIR / f"{seg_id}.csv"
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(OUTPUT_COLUMNS)
        for r in rows:
            img_local = image_paths[r["property_id"]]
            img_url = f"{base_image_url.rstrip('/')}/{img_local.name}"
            w.writerow([
                r["property_id"],
                r["name"],
                r["final_url"],
                img_url,
                r["destination"],
                r["raw_price"],
                r["star_rating"],
                r["score"],
                r["max_score"],
                r["facilities"],
            ])

    print(f"  wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
    return out_path
