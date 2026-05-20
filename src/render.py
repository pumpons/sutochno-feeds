"""Render HTML templates to PNG images via Playwright. Cached by content hash."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from io import BytesIO

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "config" / "templates"
ASSETS_DIR = TEMPLATES_DIR / "assets"
IMAGES_DIR = ROOT / "output" / "images"

VIEWPORT = {"width": 1200, "height": 1200}
SUPERSAMPLE = 2  # render at 2× then downscale for crisper text

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
        return IMAGES_DIR / f"{self.cache_key()}.png"


def render_jobs(jobs: list[RenderJob], concurrency: int = 4) -> dict[str, Path]:
    """Render all jobs, return mapping property_id → image path."""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    todo = [j for j in jobs if not j.output_path().exists()]
    skipped = len(jobs) - len(todo)
    print(f"  render: {len(todo)} new, {skipped} cached")

    result: dict[str, Path] = {j.property_id: j.output_path() for j in jobs}

    if not todo:
        return result

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for i, job in enumerate(todo, 1):
                template = _env.get_template(f"{job.template}.html")
                html = template.render(**job.context, **_ASSETS)

                page = browser.new_page(viewport=VIEWPORT, device_scale_factor=SUPERSAMPLE)
                page.set_content(html, wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                hires_bytes = page.screenshot(type="png")
                page.close()

                img = Image.open(BytesIO(hires_bytes))
                img = img.resize((VIEWPORT["width"], VIEWPORT["height"]), Image.LANCZOS)
                img.save(job.output_path(), "PNG", optimize=True)

                if i % 50 == 0 or i == len(todo):
                    print(f"    {i}/{len(todo)}")
        finally:
            browser.close()

    return result
