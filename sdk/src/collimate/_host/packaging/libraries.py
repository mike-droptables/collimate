# SPDX-License-Identifier: MIT
"""Shared libraries: ``.library`` packages and the ``shared:`` names that use them.

A library is a definition package like a generator: one folder at the top of
a cell holding its definition, ``<id>/<id>.library``, beside its code. The
folder's name is the library's id and the name its code imports by, an
underscore name (``_env_shared``)::

    _env_shared/
      _env_shared.library     # id, name, description (what agents read)
      __init__.py             # the code, imported as ``_env_shared``
      git_run.py

A generator's cell names the libraries its generators import under ``shared:``
in its ``cell.yaml``; a host resolves each name, at admission and when it
stages a run, from the libraries of the channel's Capabilities stack first,
then from the libraries the app ships (the bundled ``generators/`` tree, where
each library is a top-level underscore folder, which is no cell).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .layout import parse_cell_manifest
from .models import FileSpec

#: A library's id: its folder's name and its import name.
LIBRARY_ID = re.compile(r"^_[a-z][a-z0-9_]*$")
#: The suffix of a library's definition file.
LIBRARY_SUFFIX = ".library"


def shared_names(cell_manifest: str | bytes | None) -> list[str]:
    """The library names a cell's ``cell.yaml`` (its body; ``None`` for a cell
    without one) lists under ``shared:``, in order.

    Raises ``ValueError`` for a body that is not YAML, a ``shared`` that is
    not a list of names, or a name that is no library id."""
    if cell_manifest is None:
        return []
    try:
        names = parse_cell_manifest(cell_manifest).get("shared") or []
    except yaml.YAMLError as exc:
        raise ValueError(f"cell.yaml is not YAML: {exc}") from exc
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("cell.yaml 'shared' must be a list of library names")
    for name in names:
        if not LIBRARY_ID.match(name):
            raise ValueError(f"cell.yaml 'shared' names {name!r}, which is no library id "
                             "(an underscore name such as _my_helpers)")
    return list(dict.fromkeys(names))


def library_id(definition_path: str, body: str | bytes) -> str | None:
    """The id of the library whose definition is ``definition_path`` (cell
    relative) with ``body``, or None when it is no well-placed library: a
    mapping whose ``id`` is a library id and names the folder the definition
    sits in at the top of its cell."""
    parts = definition_path.split("/")
    if len(parts) != 2 or not parts[1].endswith(LIBRARY_SUFFIX):
        return None
    try:
        manifest = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str):
        return None
    identifier = manifest["id"]
    return identifier if LIBRARY_ID.match(identifier) and identifier == parts[0] else None


def _files_under(folder: Path, prefix: str) -> list[FileSpec]:
    from .asset_cells import _files_under as files_under
    return files_under(folder, prefix)


def _is_library(root: Path, name: str) -> bool:
    """Whether ``root/<name>/`` holds the definition of the library ``name``."""
    folder = root / name
    if not LIBRARY_ID.match(name) or not folder.is_dir():
        return False
    return any(definition.is_file()
               and library_id(f"{name}/{definition.name}", definition.read_bytes()) == name
               for definition in sorted(folder.glob("*" + LIBRARY_SUFFIX)))


def read_library(root: Path | str, name: str) -> list[FileSpec] | None:
    """The bundled library ``name`` of the tree ``root`` (a ``generators/``
    folder): every file of ``root/<name>/``, named ``<name>/…`` as a cell
    holds it, when that folder holds the library's definition; else None."""
    root = Path(root)
    return _files_under(root / name, f"{name}/") if _is_library(root, name) else None


def bundled_libraries(root: Path | str) -> list[str]:
    """The names of the libraries the tree ``root`` (a ``generators/``
    folder) holds, sorted."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if _is_library(root, child.name))


__all__ = ["LIBRARY_ID", "LIBRARY_SUFFIX", "bundled_libraries", "library_id", "read_library", "shared_names"]
