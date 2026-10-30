# SPDX-License-Identifier: MIT
"""Tree-driven cell layout: the parent folders ARE the cells.

The grouping an asset tree ships is its directory structure — each
immediate, non-underscore subdirectory of an asset root (``generators/``,
``renderers/``, ``builtin/``) is one cell, and the folders inside
it are the cell's contents::

    generators/
      _agent_shared/          # shared library — not a cell (underscore)
      agent/                  # ← the cell "Agent"
        cell.yaml             #   name / icon / order / shared / …
        agent/                #   a generator
        agent-chat/           #   a shipped runner package; a suggestion offers it as a draft
        …

``cell.yaml`` is the optional per-cell manifest, a file of the cell that
travels with it:

    name: "Agent"             # display name (default: folder name, titled); unique within the tree — it keys the cell id
    icon: "bot"               # lucide-style icon name (frontend registry)
    order: 80                 # sort key (default 100; ties by folder name)
    shared:                   # _* helper libraries resolved into this cell at
      - _agent_shared         #   admission from the installed helper set
    extra: {…}                # arbitrary passthrough metadata

Everything that consumes a layout — the asset reader
(:mod:`asset_cells`), the renderer bundle builder, and the collimate app's
seeding and reset through that reader — reads the same scan, so the
grouping cannot drift between the repo and the app. A cell's ``shared:``
list is not read here: a host resolves it when it admits the cell, from
whatever source (:func:`~collimate._host.packaging.asset_cells.shared_libraries`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import DEFAULT_CELL_ORDER

CELL_MANIFEST = "cell.yaml"
# The tree of the app's built-in cells: the handlers and renderers the app
# cannot function without, served for every channel and never seeded.
BUILTIN_DIR = "builtin"


@dataclass
class CellLayout:
    """One cell as declared by a parent folder + its ``cell.yaml``."""

    folder: str                       # the parent folder's name (stable id-ish)
    path: Path                        # absolute path of the parent folder
    name: str                         # display name
    icon: str = ""
    order: int = DEFAULT_CELL_ORDER
    extra: dict = field(default_factory=dict)


def _titled(folder: str) -> str:
    return " ".join(w.capitalize() for w in folder.replace("-", "_").split("_") if w)


def parse_cell_manifest(text: str | bytes) -> dict:
    """A ``cell.yaml`` body parsed (``{}`` when it holds no mapping). Raises
    ``yaml.YAMLError`` for a body that is not YAML."""
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def read_cell_manifest(cell_dir: Path) -> dict:
    """The parsed ``cell.yaml`` of a cell folder (``{}`` when absent)."""
    manifest = cell_dir / CELL_MANIFEST
    if not manifest.is_file():
        return {}
    return parse_cell_manifest(manifest.read_text(encoding="utf-8"))


def scan_cells(asset_root: Path) -> list:
    """The cells of an asset tree, in display order.

    Every immediate subdirectory that doesn't start with ``_`` or ``.`` is a
    cell; underscore-prefixed directories are shared helper libraries, which
    a cell names in ``cell.yaml``'s ``shared`` list. Sorted by (``order``,
    folder name).
    """
    asset_root = Path(asset_root)
    if not asset_root.is_dir():
        raise FileNotFoundError(f"asset root not found: {asset_root}")
    cells: list[CellLayout] = []
    for child in sorted(asset_root.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if not _has_cell_content(child):
            continue  # interpreter/tooling debris (e.g. orphaned __pycache__)
        cells.append(cell_layout(child))
    cells.sort(key=lambda c: (c.order, c.folder))
    return cells


def cell_layout(cell_dir: Path) -> CellLayout:
    """One cell folder as declared by its ``cell.yaml`` (defaults when
    absent)."""
    cell_dir = Path(cell_dir)
    meta = read_cell_manifest(cell_dir)
    raw_order = meta.get("order")
    order = DEFAULT_CELL_ORDER if raw_order in (None, "") else int(raw_order)
    return CellLayout(
        folder=cell_dir.name,
        path=cell_dir,
        name=str(meta.get("name") or _titled(cell_dir.name)),
        icon=str(meta.get("icon") or ""),
        order=order,
        extra=dict(meta.get("extra") or {}),
    )


def _has_cell_content(path: Path) -> bool:
    """True when the folder holds anything beyond interpreter debris.

    A cell folder that contains only ``__pycache__`` trees / ``.pyc`` files
    (sources deleted, caches left behind) is not a cell — scanning it as one
    breaks seeding and the layout tests on otherwise-clean checkouts."""
    for p in path.rglob("*"):
        if p.is_dir():
            continue
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        return True
    return False


def shared_dir(asset_root: Path, ref: str) -> Path | None:
    """Resolve a ``shared:`` library reference against an asset root (a
    host's installed helper set). Underscore-prefixed, top-level only —
    anything else is rejected so a cell.yaml can't reach outside the tree."""
    name = ref.strip().strip("/")
    if not name.startswith("_") or "/" in name or ".." in name:
        return None
    candidate = Path(asset_root) / name
    return candidate if candidate.is_dir() else None


__all__ = ["BUILTIN_DIR", "CELL_MANIFEST", "CellLayout", "cell_layout", "parse_cell_manifest", "read_cell_manifest",
           "scan_cells", "shared_dir"]
