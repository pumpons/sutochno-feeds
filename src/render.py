"""Render HTML templates to PNG images via Playwright. Cached by content hash."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "config" / "templates"
IMAGES_DIR = ROOT / "output" / "images"

VIEWPORT = {"width": 1200, "height": 628}

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


@dataclass(frozen=True)
class RenderJob:
    property_id: str
    template: str
    context: dict  # values for jinja vars + identifies overlay state

    def cache_key(self) -> str:
        payload = json.dumps(
            {"template": self.template, "context": self.context},
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
                html = template.render(**job.context)

                page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
                page.set_content(html, wait_until="networkidle")
                page.screenshot(path=str(job.output_path()), type="png")
                page.close()

                if i % 50 == 0 or i == len(todo):
                    print(f"    {i}/{len(todo)}")
        finally:
            browser.close()

    return result
