# SPDX-License-Identifier: MIT
"""Read the suggested channel setups a content tree holds in ``suggested/``.

Each ``suggested/<id>.json`` is one suggestion (the file stem is its id)
holding exactly six keys::

    {
      "name": "Standard",
      "description": "…",
      "generators": [["Web", "generators/web"], …],
      "renderers":  [["Documents", "renderers/documents"], …],
      "tutorial":   [["Welcome", "suggested/tutorial/welcome"]],
      "quick_fields": {"agent-chat": [{"account": "model"}]}
    }

Each pair is ``[cell name, folder]``, the folder relative to the tree root
(the folder holding ``suggested/``, ``generators/`` and ``renderers/``), in
seeding order. A generator or renderer folder is an asset-tree cell folder
(:mod:`collimate._host.packaging.layout`), read as :func:`read_asset_cells` reads
it in its tree; the pair's name is its display name and so keys its id. A
tutorial folder's files are the cell's files verbatim, for a cell that
starts on the channel's canvas.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .asset_cells import _cell_id, _files_under, _has_content, read_cell
from .models import CellSpec

SUGGESTED_DIR = "suggested"
_KEYS = ("name", "description", "generators", "renderers", "tutorial", "quick_fields")
_SECTIONS = ("generators", "renderers", "tutorial")


@dataclass(frozen=True)
class CellRef:
    """One ``[cell name, folder]`` pair of a suggestion."""

    name: str
    folder: str


@dataclass(frozen=True)
class Suggestion:
    """One ``suggested/<id>.json``: the cells a new channel starts from."""

    id: str
    name: str
    description: str
    generators: list[CellRef]
    renderers: list[CellRef]
    tutorial: list[CellRef]
    quick_fields: dict[str, list[dict[str, str | bool]]]


def _folder(root: Path, folder: str) -> Path:
    """The directory a pair's folder names, refused unless it is a relative
    POSIX path that stays inside ``root``."""
    parts = folder.split("/")
    if (folder.startswith("/") or "\\" in folder
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError(f"folder {folder!r} must be a relative path inside the tree")
    path = root / folder
    resolved_root = root.resolve()
    if not path.resolve().is_relative_to(resolved_root):
        raise ValueError(f"folder {folder!r} leaves the tree")
    if not path.is_dir():
        raise ValueError(f"folder {folder!r} is not a directory under {root}")
    return path


def _section(root: Path, data: dict, key: str) -> list[CellRef]:
    pairs = data[key]
    if not isinstance(pairs, list):
        raise ValueError(f"{key!r} must be a list of [name, folder] pairs")
    refs: list[CellRef] = []
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(part, str) for part in pair)):
            raise ValueError(f"{key!r} holds {pair!r}, not a [name, folder] pair")
        name, folder = pair
        if not name.strip():
            raise ValueError(f"{key!r} holds a pair with an empty name")
        if any(ref.name == name for ref in refs):
            raise ValueError(f"{key!r} names {name!r} twice; the cell ids would collide")
        _folder(root, folder)
        refs.append(CellRef(name=name, folder=folder))
    return refs


def _quick_fields(value: object) -> dict[str, list[dict[str, str | bool]]]:
    """Ordered compact controls keyed by the id of a packaged runner.

    The API checks the ids and references against the actual generator
    schemas after loading the packages. Here the catalog shape is exact.
    """
    if not isinstance(value, dict):
        raise ValueError("'quick_fields' must be an object keyed by runner id")
    for runner_id, refs in value.items():
        if not isinstance(runner_id, str) or not runner_id.strip():
            raise ValueError("'quick_fields' runner ids must be nonempty strings")
        if not isinstance(refs, list) or not refs:
            raise ValueError(f"quick_fields for {runner_id!r} must be a nonempty list")
        seen: set[tuple[str, str | bool]] = set()
        for ref in refs:
            if not isinstance(ref, dict) or len(ref) != 1:
                raise ValueError(f"quick_fields for {runner_id!r} needs one-key references")
            key, target = next(iter(ref.items()))
            if key in ("account", "setting"):
                if not isinstance(target, str) or not target.strip():
                    raise ValueError(f"quick_fields for {runner_id!r}: {key} must name a nonempty string")
            elif key != "prompt" or target is not True:
                raise ValueError(f"quick_fields for {runner_id!r} must use account, setting, or prompt: true")
            identity = (key, target)
            if identity in seen:
                raise ValueError(f"quick_fields for {runner_id!r} repeats {ref!r}")
            seen.add(identity)
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f"duplicate JSON key {key!r}")
        data[key] = value
    return data


def _suggestion(root: Path, path: Path) -> Suggestion:
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    if not isinstance(data, dict):
        raise ValueError("a suggestion must be a JSON object")
    missing = [key for key in _KEYS if key not in data]
    extra = sorted(set(data) - set(_KEYS))
    if missing or extra:
        raise ValueError(f"keys must be exactly {list(_KEYS)}: missing {missing}, unknown {extra}")
    for key in ("name", "description"):
        if not isinstance(data[key], str):
            raise ValueError(f"{key!r} must be a string")
    if not data["name"].strip():
        raise ValueError("'name' must not be empty")
    generators, renderers, tutorial = (_section(root, data, key) for key in _SECTIONS)
    return Suggestion(id=path.stem, name=data["name"], description=data["description"],
                      generators=generators, renderers=renderers, tutorial=tutorial,
                      quick_fields=_quick_fields(data["quick_fields"]))


def read_suggestions(root: Path | str) -> list[Suggestion]:
    """Every suggestion under ``root/suggested``, sorted by id.

    Raises ``FileNotFoundError`` when ``root/suggested`` is missing and
    ``ValueError``, naming the file, for a suggestion that is not valid JSON,
    has other keys than the six, a section that is not a list of
    ``[name, folder]`` string pairs, an empty or repeated cell name, or a
    folder that is not a directory inside ``root``.
    """
    root = Path(root)
    directory = root / SUGGESTED_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"no {SUGGESTED_DIR}/ folder under {root}")
    suggestions: list[Suggestion] = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stem):
        try:
            suggestions.append(_suggestion(root, path))
        except (ValueError, OSError) as exc:
            raise ValueError(f"{SUGGESTED_DIR}/{path.name}: {exc}") from exc
    return suggestions


def read_suggested_cell(root: Path | str, ref: CellRef, kind: str) -> CellSpec:
    """A suggestion's generator or renderer cell (``kind`` is
    ``"generator"`` or ``"renderer"``), read like :func:`read_asset_cells`
    reads it in its tree, named and keyed by ``ref.name``.

    Raises ``ValueError`` for an unknown kind, a folder outside ``root`` or a
    folder with no files beside its ``cell.yaml``."""
    folder = _folder(Path(root), ref.folder)
    cell = read_cell(folder, kind, name=ref.name)
    if not _has_content(cell):
        raise ValueError(f"{kind} cell {ref.name!r} ({ref.folder}) holds no files")
    return cell


def read_tutorial_cell(root: Path | str, ref: CellRef) -> CellSpec:
    """A suggestion's tutorial cell: every file under its folder, named
    relative to it (``__pycache__`` and ``.pyc`` skipped).

    Raises ``ValueError`` for a folder outside ``root`` or a folder with no
    files."""
    folder = _folder(Path(root), ref.folder)
    files = _files_under(folder)
    if not files:
        raise ValueError(f"tutorial cell {ref.name!r} ({ref.folder}) holds no files")
    return CellSpec(id=_cell_id("tutorial", ref.name), name=ref.name, files=files,
                    folder=folder.name)


__all__ = ["CellRef", "SUGGESTED_DIR", "Suggestion", "read_suggested_cell",
           "read_suggestions", "read_tutorial_cell"]
