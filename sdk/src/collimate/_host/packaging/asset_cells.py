# SPDX-License-Identifier: MIT
"""Read an asset tree's cells as :class:`~collimate._host.packaging.models.CellSpec`.

An asset tree (the workspace's ``content/generators/`` or
``content/renderers/``, or the app's ``builtins/``) is laid out as
parent-folders-are-cells (see :mod:`collimate._host.packaging.layout`). Hosts
reset shipped cells from what :func:`read_asset_cells` returns; a new
channel's cells come from a suggestion (:mod:`collimate._host.packaging.suggested`),
which reads each cell folder with :func:`read_cell`.

Inside a cell, each content folder is placed under its basename and the
loose files, ``cell.yaml`` among them, at the cell root. The libraries a
``cell.yaml`` names under ``shared:`` are not read into the cell: they are
definitions of their own (:mod:`collimate._host.packaging.libraries`), which
a host resolves by name from the channel's Capabilities, then from the
libraries it ships.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from .layout import CELL_MANIFEST, CellLayout, cell_layout, scan_cells
from .models import CellSpec, FileSpec

# Deterministic UUID namespace. Combined with the cell's kind and display
# name, this gives each cell a stable id across reads, so two cells of one
# tree must not share a display name; read_asset_cells refuses the tree when
# they do.
_CELL_ID_NAMESPACE = uuid.UUID("0bda06ef-1a24-4c63-a6a4-3c6b86e85418")

_LANG_BY_EXT = {
    "py": "python", "js": "javascript", "ts": "typescript", "tsx": "typescript",
    "json": "json", "yaml": "yaml", "yml": "yaml",
    "md": "markdown", "css": "css", "html": "html",
}

# Top-level cell children that are never cell content.
_SKIPPED_CELL_DIRS = ("__pycache__", "_runs")


def _check_kind(kind: str) -> None:
    if kind not in ("generator", "renderer"):
        raise ValueError(f"unknown asset kind: {kind!r}")


def _cell_id(kind: str, name: str) -> str:
    """The deterministic id of a cell of ``kind`` with display ``name``."""
    return str(uuid.uuid5(_CELL_ID_NAMESPACE, f"{kind}-cell-{name}"))


def _lang_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _LANG_BY_EXT.get(ext, "text")


def _file_spec_for(path: Path, name: str) -> FileSpec:
    """One source file as a FileSpec. UTF-8 files carry text ``content``;
    anything else carries raw ``data`` plus its sha256 ``blob_hash`` (the
    content-addressed id a host's CAS keys blobs by). Nothing is dropped."""
    raw = path.read_bytes()
    try:
        return FileSpec(name=name, language=_lang_for(name), content=raw.decode("utf-8"))
    except UnicodeDecodeError:
        return FileSpec(name=name, language=_lang_for(name), content="",
                        blob_hash=hashlib.sha256(raw).hexdigest(), data=raw)


def _files_under(abs_dir: Path, prefix: str = "") -> list[FileSpec]:
    """Every file under ``abs_dir``, named ``<prefix><rel>``. ``__pycache__``
    dirs and ``.pyc`` files are skipped, but ``__init__.py`` is kept: it is
    the real source of a library's package."""
    out: list[FileSpec] = []
    for path in sorted(abs_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(abs_dir)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        out.append(_file_spec_for(path, prefix + rel.as_posix()))
    return out


def _cell_spec(cell: CellLayout, kind: str) -> CellSpec:
    """One tree cell: its subfolders, each under its basename, and its loose
    files (``cell.yaml`` among them) at the cell root."""
    files: list[FileSpec] = []

    for child in sorted(cell.path.iterdir()):
        if child.is_dir():
            if child.name in _SKIPPED_CELL_DIRS:
                continue
            files.extend(_files_under(child, f"{child.name}/"))
        elif child.is_file():
            files.append(_file_spec_for(child, child.name))

    return CellSpec(
        id=_cell_id(kind, cell.name), name=cell.name, files=files,
        folder=cell.folder, icon=cell.icon, order=cell.order, extra=cell.extra,
    )


def _has_content(cell: CellSpec) -> bool:
    """True when the cell holds a file beside its ``cell.yaml``: a folder
    holding only its manifest is no cell."""
    return any(f.name != CELL_MANIFEST for f in cell.files)


def read_cell(cell_dir: Path | str, kind: str, name: str | None = None) -> CellSpec:
    """One cell folder, read exactly as :func:`read_asset_cells` reads it
    inside its tree: ``cell.yaml`` metadata and the folder's content.
    ``name``, when given, replaces the display name (and so keys the id).

    Raises ``FileNotFoundError`` when ``cell_dir`` is not a directory and
    ``ValueError`` for an unknown kind."""
    _check_kind(kind)
    cell_dir = Path(cell_dir)
    if not cell_dir.is_dir():
        raise FileNotFoundError(f"cell folder not found: {cell_dir}")
    cell = cell_layout(cell_dir)
    if name is not None:
        cell.name = name
    return _cell_spec(cell, kind)


def read_asset_cells(asset_root: Path | str, kind: str) -> list[CellSpec]:
    """The cells of an asset tree, in display order, with their files.

    ``kind`` is ``"generator"`` or ``"renderer"``; it seeds the deterministic
    cell ids (``uuid5("<kind>-cell-<name>")``). Cells with no files beside
    their ``cell.yaml`` are left out.

    Raises ``FileNotFoundError`` when ``asset_root`` is missing, and
    ``ValueError`` for an unknown kind, a tree that yields no cells, or two
    cells that share a display name (their ids would collide).
    """
    _check_kind(kind)
    asset_root = Path(asset_root)
    cells = [_cell_spec(cell, kind) for cell in scan_cells(asset_root)]
    cells = [c for c in cells if _has_content(c)]
    if not cells:
        raise ValueError(
            f"asset tree {asset_root} produced zero cells; is it structured "
            f"as <cell>/<folder>/...?"
        )
    folder_by_id: dict[str, str] = {}
    for c in cells:
        other = folder_by_id.setdefault(c.id, c.folder)
        if other != c.folder:
            raise ValueError(
                f"cell folders {other!r} and {c.folder!r} under {asset_root} "
                f"share the display name {c.name!r}, so they would get the "
                "same cell id; give one a distinct 'name' in its cell.yaml"
            )
    return cells


__all__ = ["read_asset_cells", "read_cell"]
