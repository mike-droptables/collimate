# SPDX-License-Identifier: MIT
"""Collimate asset trees: the parent-folders-are-cells layout and its reader.

Submodules: :mod:`.layout` (scan a tree's cells), :mod:`.asset_cells`
(read them as :class:`CellSpec`), :mod:`.libraries` (``.library`` packages
and the ``shared:`` names that use them), :mod:`.suggested` (the suggested
channel setups in ``suggested/``) and :mod:`.models`. The app's ``collimate``
wheel ships the first-party trees as package data and reads them where they
are installed.
"""
from .asset_cells import read_asset_cells, read_cell
from .layout import (
    BUILTIN_DIR,
    CELL_MANIFEST,
    CellLayout,
    read_cell_manifest,
    scan_cells,
)
from .libraries import LIBRARY_ID, bundled_libraries, library_id, read_library, shared_names
from .models import DEFAULT_CELL_ORDER, CellSpec, FileSpec
from .suggested import SUGGESTED_DIR, CellRef, Suggestion, read_suggested_cell, read_suggestions, read_tutorial_cell

__all__ = [
    "BUILTIN_DIR",
    "CELL_MANIFEST",
    "DEFAULT_CELL_ORDER",
    "LIBRARY_ID",
    "CellLayout",
    "CellRef",
    "CellSpec",
    "FileSpec",
    "SUGGESTED_DIR",
    "Suggestion",
    "bundled_libraries",
    "library_id",
    "read_asset_cells",
    "read_cell",
    "read_cell_manifest",
    "read_library",
    "read_suggested_cell",
    "read_suggestions",
    "read_tutorial_cell",
    "scan_cells",
    "shared_names",
]
