# SPDX-License-Identifier: MIT
"""Collimate asset trees: the parent-folders-are-cells layout and its reader.

Submodules: :mod:`.layout` (scan a tree's cells), :mod:`.asset_cells`
(read them as :class:`CellSpec`), :mod:`.suggested` (the suggested channel
setups in ``suggested/``) and :mod:`.models`. The app's ``collimate``
wheel ships the first-party trees as package data and reads them where they
are installed.
"""
from .asset_cells import read_asset_cells, read_cell, shared_libraries
from .layout import (
    BUILTIN_DIR,
    CELL_MANIFEST,
    CellLayout,
    read_cell_manifest,
    scan_cells,
    shared_dir,
)
from .models import DEFAULT_CELL_ORDER, CellSpec, FileSpec
from .suggested import SUGGESTED_DIR, CellRef, Suggestion, read_suggested_cell, read_suggestions, read_tutorial_cell

__all__ = [
    "BUILTIN_DIR",
    "CELL_MANIFEST",
    "DEFAULT_CELL_ORDER",
    "CellLayout",
    "CellRef",
    "CellSpec",
    "FileSpec",
    "SUGGESTED_DIR",
    "Suggestion",
    "read_asset_cells",
    "read_cell",
    "read_cell_manifest",
    "read_suggested_cell",
    "read_suggestions",
    "read_tutorial_cell",
    "scan_cells",
    "shared_dir",
    "shared_libraries",
]
