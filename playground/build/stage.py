"""Stage: copy the main app's api/ and frontend/ into playground/.stage/.

The staged copies are what the rest of the build edits and builds, so
APP/api and APP/frontend on disk are never touched. The copy is exact
except for the excluded dev-only trees below; every difference the demo
needs is applied afterwards by patches.py and vendor.py.
"""
from __future__ import annotations

import shutil
from pathlib import Path

API_EXCLUDES = {
    ".venv", ".collimate", "tests", "tests_live", "static",
    "__pycache__", "uv.lock", "dist", "build", ".pytest_cache", ".ruff_cache",
}
FRONTEND_EXCLUDES = {"node_modules", "dist", "coverage", "playwright-report", "test-results"}


def _ignore(excludes: set[str]):
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in excludes or n == "__pycache__" or n.endswith(".pyc")}
    return ignore


def copy_tree(src: Path, dst: Path, excludes: set[str]) -> None:
    if not src.is_dir():
        raise SystemExit(f"stage: source tree missing: {src}")
    shutil.copytree(src, dst, ignore=_ignore(excludes), symlinks=False)


def run(app_dir: Path, stage_dir: Path) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True)
    copy_tree(app_dir / "api", stage_dir / "api", API_EXCLUDES)
    copy_tree(app_dir / "frontend", stage_dir / "frontend", FRONTEND_EXCLUDES)
