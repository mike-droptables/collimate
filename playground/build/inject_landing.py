"""Copy the landing page into dist/ and add the demo button to its hero.

The main landing has no demo button (the playground is the only place the
demo exists), so the build injects web/landing-hero.html — the button plus
its CSS — right after the hero-actions opening tag. The anchor must occur
exactly once; a landing redesign that moves it fails the build here.
"""
from __future__ import annotations

import shutil
from pathlib import Path

HERO_ANCHOR = '<div class="hero-actions">\n'


def run(app_dir: Path, dist_dir: Path, web_dir: Path) -> None:
    landing = app_dir / "landing"
    if not (landing / "index.html").is_file():
        raise SystemExit(f"inject_landing: {landing / 'index.html'} missing")
    for item in landing.iterdir():
        if item.name.startswith("_") or item.name.startswith("."):
            continue  # design scratch (_archive) never ships
        target = dist_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)

    snippet_path = web_dir / "landing-hero.html"
    if not snippet_path.is_file():
        raise SystemExit(f"inject_landing: {snippet_path} missing")
    snippet = snippet_path.read_text(encoding="utf-8").rstrip("\n") + "\n"
    index = dist_dir / "index.html"
    html = index.read_text(encoding="utf-8")
    if html.count(HERO_ANCHOR) != 1:
        raise SystemExit(f"inject_landing: hero anchor {HERO_ANCHOR!r} not found exactly once in {index}")
    html = html.replace(HERO_ANCHOR, HERO_ANCHOR + snippet, 1)
    index.write_text(html, encoding="utf-8")
