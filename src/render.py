"""Render HTML templates to PNG images via Playwright. Cached by content hash."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from queue import Empty, Full, Queue

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "config" / "templates"
ASSETS_DIR = TEMPLATES_DIR / "assets"
IMAGES_DIR = ROOT / "output" / "images"
PHOTO_CACHE_DIR = ROOT / "data" / "photo_cache"

VIEWPORT = {"width": 1200, "height": 1200}
SUPERSAMPLE = 1.5   # render at 1.5× then downscale; 2× costs ~2× more for no visible gain
OUTPUT_SIZE = 1000  # final banner edge, px (Директ smart banners need ≥ 450)
WEBP_QUALITY = 75
WEBP_METHOD = 4     # 6 is ~2× slower for a few % smaller files
# The screenshot is an intermediate that gets re-encoded to lossy WEBP anyway,
# so PNG's lossless encode+decode is pure waste; JPEG at high quality is much
# cheaper and leaves no visible trace after the downscale.
SHOT_TYPE = "jpeg"
SHOT_QUALITY = 95
RESIZE_FILTER = Image.LANCZOS

DEFAULT_CONCURRENCY = int(os.environ.get("RENDER_CONCURRENCY", "4"))
FETCH_CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "16"))
# Photos are prefetched one chunk ahead of the renderers; this bounds how many
# decoded images sit in memory at once.
CHUNK_SIZE = int(os.environ.get("RENDER_CHUNK", "400"))
# Recycle the browser context periodically — a single page doing tens of
# thousands of set_content calls slowly leaks memory.
PAGE_RECYCLE = int(os.environ.get("PAGE_RECYCLE", "500"))
# Hard wall-clock budget for rendering. The CI job is killed outright if it
# overruns, losing the image cache and deploying nothing, so we stop rendering
# early instead and ship whatever is ready — the rest is picked up next run.
DEADLINE_MINUTES = float(os.environ.get("RENDER_DEADLINE_MINUTES", "0"))

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml", ".webp": "image/webp"}


def _load_assets() -> tuple[dict[str, str], str]:
    """Load files in config/templates/assets/ as data URIs (keyed by stem).
    Returns (assets, short_fingerprint) — fingerprint goes into the render cache
    key so changing any asset invalidates the cache for all images."""
    assets: dict[str, str] = {}
    fp = hashlib.sha1()
    if ASSETS_DIR.exists():
        for path in sorted(ASSETS_DIR.glob("*")):
            if not path.is_file():
                continue
            mime = _MIME_BY_EXT.get(path.suffix.lower())
            if mime is None:
                continue
            data = path.read_bytes()
            assets[path.stem] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            fp.update(path.name.encode("utf-8"))
            fp.update(data)
    return assets, fp.hexdigest()[:8]


_ASSETS, _ASSETS_FP = _load_assets()

# Output settings are part of the cache identity: changing size or quality must
# invalidate every previously rendered image, or the site ends up mixing formats.
_RENDER_FP = hashlib.sha1(
    json.dumps([VIEWPORT, SUPERSAMPLE, OUTPUT_SIZE, WEBP_QUALITY, WEBP_METHOD,
                SHOT_TYPE, SHOT_QUALITY, RESIZE_FILTER],
               sort_keys=True).encode("utf-8")
).hexdigest()[:6]


@dataclass(frozen=True)
class RenderJob:
    property_id: str
    template: str
    context: dict  # values for jinja vars + identifies overlay state

    def cache_key(self) -> str:
        payload = json.dumps(
            {"template": self.template, "context": self.context,
             "assets": _ASSETS_FP, "render": _RENDER_FP},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:16]

    def output_path(self) -> Path:
        return IMAGES_DIR / f"{self.cache_key()}.webp"


def _photo_data_uri(url: str) -> str | None:
    """Download a hotel photo and return it as a data: URI, or None on failure.
    Kept on disk for the run: the same hotel appears in several segments, so
    this avoids re-downloading its photo once per template."""
    if not url:
        return None
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cached = PHOTO_CACHE_DIR / key
    data: bytes | None = None
    if cached.exists():
        try:
            data = cached.read_bytes()
        except OSError:
            data = None
    if data is None:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.content
        except Exception:
            return None
        try:
            PHOTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        except OSError:
            pass  # disk cache is an optimisation, never a hard requirement
    stem = url.lower().split("?")[0]
    mime = next((m for ext, m in _MIME_BY_EXT.items() if stem.endswith(ext)), "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _render_one(page, job: RenderJob, photo: str | None) -> None:
    context = dict(job.context)
    if photo:
        context["image_url"] = photo
    template = _env.get_template(f"{job.template}.html")
    html = template.render(**context, **_ASSETS)
    # With the photo inlined there is nothing left to fetch, so "load" is enough.
    # Fall back to waiting on the network when the download failed and the
    # template still points at a remote URL.
    page.set_content(html, wait_until="load" if photo else "networkidle")
    page.evaluate("document.fonts.ready")
    shot_args = {"type": SHOT_TYPE}
    if SHOT_TYPE == "jpeg":
        shot_args["quality"] = SHOT_QUALITY
    hires_bytes = page.screenshot(**shot_args)
    img = Image.open(BytesIO(hires_bytes)).convert("RGB")
    img = img.resize((OUTPUT_SIZE, OUTPUT_SIZE), RESIZE_FILTER)
    img.save(job.output_path(), "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)


def _prefetch(todo: list[RenderJob], queue: Queue,
              stop: threading.Event, done_flag: threading.Event) -> None:
    """Feed (job, photo) pairs to the renderers, one chunk ahead of them.
    The queue is bounded, so this thread blocks once it runs far enough in
    front — that is what keeps prefetched photos from filling memory."""
    try:
        for start in range(0, len(todo), CHUNK_SIZE):
            if stop.is_set():
                return
            chunk = todo[start:start + CHUNK_SIZE]
            urls = list({j.context.get("image_url") for j in chunk if j.context.get("image_url")})
            with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
                photos = dict(zip(urls, pool.map(_photo_data_uri, urls)))
            for job in chunk:
                # Blocking put would deadlock once the renderers stop draining,
                # so retry on a timeout and re-check the stop flag each round.
                while not stop.is_set():
                    try:
                        queue.put((job, photos.get(job.context.get("image_url"))), timeout=0.5)
                        break
                    except Full:
                        continue
    finally:
        done_flag.set()


def _worker(queue: Queue, errors: list, done: list[int], lock: threading.Lock, total: int,
            deadline: float | None, stop: threading.Event, producer_done: threading.Event) -> None:
    """One thread owns one playwright + browser for its lifetime.
    Playwright's sync API ties greenlets to threads, so each worker must
    create and tear down its own playwright instance — they cannot be shared."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = None
        page = None
        since_recycle = 0
        try:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    stop.set()  # tell the prefetcher and the other renderers to wind down
                    return
                if stop.is_set():
                    return
                try:
                    item = queue.get(timeout=0.5)
                except Empty:
                    if producer_done.is_set():
                        return
                    continue
                job, photo = item
                if page is None or since_recycle >= PAGE_RECYCLE:
                    if context is not None:
                        context.close()
                    context = browser.new_context(viewport=VIEWPORT,
                                                  device_scale_factor=SUPERSAMPLE)
                    page = context.new_page()
                    since_recycle = 0
                try:
                    _render_one(page, job, photo)
                except Exception as e:
                    errors.append((job.property_id, str(e)))
                since_recycle += 1
                with lock:
                    done[0] += 1
                    if done[0] % 200 == 0 or done[0] == total:
                        print(f"    {done[0]}/{total}", flush=True)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            browser.close()


def render_jobs(jobs: list[RenderJob], concurrency: int | None = None) -> dict[tuple, Path]:
    """Render all jobs in parallel, return mapping (property_id, template) → path.
    The key carries the template because one hotel is rendered once per segment
    and those images must not be mistaken for one another.
    Only images that actually exist on disk afterwards are returned, so a
    failed render drops its row from the feed instead of pointing at a 404."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    concurrency = concurrency or DEFAULT_CONCURRENCY

    todo = [j for j in jobs if not j.output_path().exists()]
    skipped = len(jobs) - len(todo)
    print(f"  render: {len(todo)} new, {skipped} cached, workers={concurrency}")

    if todo:
        n_workers = min(concurrency, len(todo))
        queue: Queue = Queue(maxsize=CHUNK_SIZE * 2)
        errors: list[tuple[str, str]] = []
        done = [0]
        lock = threading.Lock()
        stop = threading.Event()
        producer_done = threading.Event()
        deadline = time.monotonic() + DEADLINE_MINUTES * 60 if DEADLINE_MINUTES > 0 else None
        if deadline is not None:
            print(f"  deadline: stop rendering after {DEADLINE_MINUTES:.0f} min")

        feeder = threading.Thread(target=_prefetch, args=(todo, queue, stop, producer_done),
                                  daemon=True)
        feeder.start()
        threads = [
            threading.Thread(target=_worker,
                             args=(queue, errors, done, lock, len(todo),
                                   deadline, stop, producer_done))
            for _ in range(n_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        feeder.join(timeout=30)

        for pid, err in errors[:20]:
            print(f"    ! failed {pid}: {err}")
        if len(errors) > 20:
            print(f"    ! ...and {len(errors) - 20} more failures")
        if done[0] < len(todo):
            print(f"  ! stopped early: {done[0]}/{len(todo)} rendered "
                  f"(deadline reached; the rest carries over to the next run)")

    return {(j.property_id, j.template): j.output_path()
            for j in jobs if j.output_path().exists()}
