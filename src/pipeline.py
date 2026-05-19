"""End-to-end orchestrator: ingest source → render images → build per-segment feeds."""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from . import build_feed, ingest

ROOT = Path(__file__).resolve().parent.parent
SEGMENTS_YAML = ROOT / "config" / "segments.yaml"


def run(skip_ingest: bool = False, only_segment: str | None = None) -> None:
    if not skip_ingest:
        ingest.run()
    else:
        print("Skipping ingest (--skip-ingest)")

    cfg = yaml.safe_load(SEGMENTS_YAML.read_text(encoding="utf-8"))
    base_image_url = cfg["base_image_url"]

    for segment in cfg["segments"]:
        if only_segment and segment["id"] != only_segment:
            continue
        build_feed.build(segment, base_image_url=base_image_url)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Don't re-download source CSV; reuse existing DB")
    ap.add_argument("--segment", help="Run only this segment id")
    args = ap.parse_args()
    run(skip_ingest=args.skip_ingest, only_segment=args.segment)


if __name__ == "__main__":
    main()
