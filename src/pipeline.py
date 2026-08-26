"""End-to-end orchestrator: ingest source → render images → build per-segment feeds."""
from __future__ import annotations

import argparse
from itertools import zip_longest
from pathlib import Path

import yaml

from . import build_feed, ingest
from .render import IMAGES_DIR, RenderJob, render_jobs

ROOT = Path(__file__).resolve().parent.parent
SEGMENTS_YAML = ROOT / "config" / "segments.yaml"


def _interleave(job_lists: list[list[RenderJob]]) -> list[RenderJob]:
    """Round-robin the per-segment job lists.
    Rows arrive sorted by score, so interleaving means an interrupted render
    leaves every feed holding its best objects, instead of filling the first
    segments completely and leaving the last ones empty."""
    out: list[RenderJob] = []
    for tier in zip_longest(*job_lists):
        out.extend(job for job in tier if job is not None)
    return out


def _cleanup_images(keep: set[str]) -> None:
    """Delete rendered images no feed references any more.
    Without this the published site grows without bound, since every price
    change leaves its old banner behind."""
    removed = freed = 0
    for path in IMAGES_DIR.glob("*.webp"):
        if path.name in keep:
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[cleanup] removed {removed} unreferenced images ({freed / 1e6:.0f} MB)")


def run(skip_ingest: bool = False, only_segment: str | None = None) -> None:
    if not skip_ingest:
        ingest.run()
    else:
        print("Skipping ingest (--skip-ingest)")

    cfg = yaml.safe_load(SEGMENTS_YAML.read_text(encoding="utf-8"))
    base_image_url = cfg["base_image_url"]

    plans = [
        build_feed.plan(segment)
        for segment in cfg["segments"]
        if not only_segment or segment["id"] == only_segment
    ]
    if not plans:
        print("No segments matched — nothing to do")
        return

    jobs = _interleave([p.jobs for p in plans])
    print(f"\n[render] {len(jobs)} images across {len(plans)} segments")
    image_paths = render_jobs(jobs)

    print()
    for p in plans:
        build_feed.write(p, image_paths, base_image_url)

    # Only prune when the whole config was built; a --segment run knows nothing
    # about the images the other segments still need.
    if not only_segment:
        _cleanup_images({path.name for path in image_paths.values()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Don't re-download source CSV; reuse existing DB")
    ap.add_argument("--segment", help="Run only this segment id")
    args = ap.parse_args()
    run(skip_ingest=args.skip_ingest, only_segment=args.segment)


if __name__ == "__main__":
    main()
