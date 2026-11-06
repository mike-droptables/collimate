#!/usr/bin/env python3
"""Render every app icon from the one mark.

The mark is the top chrome's glyph (frontend/src/components/DesktopTitleBar.tsx
MarkGlyph); ``frontend/public/favicon.svg`` is its icon form, brass on the
canvas colour inside a rounded square. Every other icon derives from that
file here:

    landing/favicon.svg                                   copy
    frontend/public/favicon.ico                           16, 32, 48
    desktop/icons/app-icon.svg                            copy
    desktop/icons/app-icon.png                            512
    desktop/icons/app-icon.ico                            16 … 256
    desktop/icons/app-icon.icns                           16 … 1024
    desktop/icons/hicolor/<size>/apps/collimate.png       16 … 512
    desktop/icons/hicolor/scalable/apps/collimate.svg     copy

Chromium rasterises the SVG once at 1024 px (ImageMagick's own SVG renderer
mangles the stroked arms); Pillow downscales with Lanczos. Run from the repo
root with the workspace environment, which has Playwright, plus Pillow:

    uv run --with pillow python build/render_icons.py
"""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "frontend" / "public" / "favicon.svg"
RENDER_PX = 1024
HICOLOR_SIZES = (16, 24, 32, 48, 64, 96, 128, 192, 256, 512)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
FAVICON_ICO_SIZES = (16, 32, 48)

COPIES = (
    ROOT / "landing" / "favicon.svg",
    ROOT / "desktop" / "icons" / "app-icon.svg",
    ROOT / "desktop" / "icons" / "hicolor" / "scalable" / "apps" / "collimate.svg",
)


def render_master() -> bytes:
    """The SVG as a 1024 px PNG with a transparent page, from Chromium."""
    from playwright.sync_api import sync_playwright

    html = (
        "<!doctype html><html><head><style>html,body{margin:0;background:transparent}"
        f"svg{{display:block;width:{RENDER_PX}px;height:{RENDER_PX}px}}</style></head>"
        f"<body>{MASTER.read_text(encoding='utf-8')}</body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": RENDER_PX, "height": RENDER_PX}, device_scale_factor=1)
        page.set_content(html)
        png = page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": RENDER_PX, "height": RENDER_PX})
        browser.close()
    return png


def main() -> int:
    from PIL import Image

    master = Image.open(io.BytesIO(render_master())).convert("RGBA")
    if master.size != (RENDER_PX, RENDER_PX):
        print(f"error: rendered {master.size}, expected {RENDER_PX} px square", file=sys.stderr)
        return 1

    def scaled(px: int) -> Image.Image:
        return master.resize((px, px), Image.Resampling.LANCZOS)

    for path in COPIES:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MASTER, path)
    icons = ROOT / "desktop" / "icons"
    for px in HICOLOR_SIZES:
        target = icons / "hicolor" / f"{px}x{px}" / "apps" / "collimate.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        scaled(px).save(target, optimize=True)
    scaled(512).save(icons / "app-icon.png", optimize=True)
    scaled(256).save(icons / "app-icon.ico", sizes=[(px, px) for px in ICO_SIZES])
    master.save(icons / "app-icon.icns")
    scaled(48).save(ROOT / "frontend" / "public" / "favicon.ico", sizes=[(px, px) for px in FAVICON_ICO_SIZES])
    print(f"rendered the mark from {MASTER.relative_to(ROOT)} into {len(COPIES)} SVG copies, "
          f"{len(HICOLOR_SIZES)} hicolor PNGs, app-icon.png/.ico/.icns and favicon.ico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
