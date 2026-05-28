"""Render HTML templates to PNG images via Playwright. Cached by content hash."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "config" / "templates"
ASSETS_DIR = TEMPLATES_DIR / "assets"
IMAGES_DIR = ROOT / "output" / "images"

VIEWPORT = {"width": 1200, "height": 1200}
SUPERSAMPLE = 2  # render at 2× then downscale for crisper text
DEFAULT_CONCURRENCY = int(os.environ.get("RENDER_CONCURRENCY", "4"))

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


@dataclass(frozen=True)
class RenderJob:
    property_id: str
    template: str
    context: dict  # values for jinja vars + identifies overlay state

    def cache_key(self) -> str:
        payload = json.dumps(
            {"template": self.template, "context": self.context, "assets": _ASSETS_FP},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:16]

    def output_path(self) -> Path:
        return IMAGES_DIR / f"{self.cache_key()}.jpg"


def _render_one(browser, job: RenderJob) -> None:
    template = _env.get_template(f"{job.template}.html")
    html = template.render(**job.context, **_ASSETS)
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=SUPERSAMPLE)
    try:
        page = context.new_page()
        page.set_content(html, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        hires_bytes = page.screenshot(type="png")
    finally:
        context.close()
    img = Image.open(BytesIO(hires_bytes)).convert("RGB")
    img = img.resize((VIEWPORT["width"], VIEWPORT["height"]), Image.LANCZOS)
    img.save(job.output_path(), "JPEG", quality=85, optimize=True, progressive=True)


def _worker(queue: Queue, errors: list, done: list[int], lock: threading.Lock, total: int) -> None:
    """One thread owns one playwright + browser for its lifetime.
    Playwright's sync API ties greenlets to threads, so each worker must
    create and tear down its own playwright instance — they cannot be shared."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            while True:
                try:
                    job = queue.get_nowait()
                except Empty:
                    return
                try:
                    _render_one(browser, job)
                except Exception as e:
                    errors.append((job.property_id, str(e)))
                with lock:
                    done[0] += 1
                    if done[0] % 50 == 0 or done[0] == total:
                        print(f"    {done[0]}/{total}")
        finally:
            browser.close()


def render_jobs(jobs: list[RenderJob], concurrency: int | None = None) -> dict[str, Path]:
    """Render all jobs in parallel, return mapping property_id → image path."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    concurrency = concurrency or DEFAULT_CONCURRENCY

    todo = [j for j in jobs if not j.output_path().exists()]
    skipped = len(jobs) - len(todo)
    print(f"  render: {len(todo)} new, {skipped} cached, workers={concurrency}")

    result: dict[str, Path] = {j.property_id: j.output_path() for j in jobs}

    if not todo:
        return result

    queue: Queue = Queue()
    for job in todo:
        queue.put(job)

    errors: list[tuple[str, str]] = []
    done = [0]
    lock = threading.Lock()
    threads = [
        threading.Thread(target=_worker, args=(queue, errors, done, lock, len(todo)))
        for _ in range(min(concurrency, len(todo)))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for pid, err in errors:
        print(f"    ! failed {pid}: {err}")

    return result
