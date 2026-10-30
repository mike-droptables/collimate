"""The removed-cell record: cells off the canvas whose history is kept.

One ``removed_cells`` row per logical cell taken off the canvas by Remove
(``reason`` 'removed') or consumed by Merge or Split ('merged', 'split').
The row holds what the Removed shelf lists (name, icon, the stack it left
and how many cells that stack had, the cell's own created/updated times,
when it left) and what restore rebuilds the layout cell from:
``metadata_json`` is the cell's layout dump without files (its tags included,
after any remove zone's action, edited on the shelf by ``set_tags``) and ``display_json`` is its app display
descriptor. Every removal replaces the row and restamps ``removed_at``, so it
is the cell's last-removed time (the shelf's "Last removed" sort). File bytes
and encoding live solely in the typed version chain. Timestamps are stored and returned as UTC ISO with offset.

Writers leave the commit to the caller so the row lands in the removal's
transaction.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from time_wire import iso
from cell_display import take_display, validate_display, write_display

if TYPE_CHECKING:
    from models import Cell

REASONS = frozenset({"removed", "merged", "split"})

_COLUMNS = ("cell_id, name, icon, stack_name, stack_cell_count, cell_created_at, "
            "cell_updated_at, removed_at, pinned, metadata_json, reason, display_json")

_CAPTURE_LIVE_DISPLAY = object()


@dataclass(frozen=True)
class RemovedCell:
    cell_id: str
    name: str
    icon: str | None
    stack_name: str | None
    stack_cell_count: int
    created_at: str | None  # the cell's own, UTC ISO with offset
    updated_at: str | None
    removed_at: str
    pinned: str | None
    metadata: dict[str, Any]  # layout Cell dump without files
    reason: str
    display: dict[str, Any] | None

    @property
    def tags(self) -> list[str]:
        """The tags the record's cell carried when it left, as recorded."""
        return list(self.metadata.get("tags", []))


def _row(row) -> RemovedCell:
    metadata = json.loads(row[9])
    if not isinstance(metadata, dict):
        raise ValueError("Removed cell metadata must be an object")
    return RemovedCell(
        cell_id=row[0], name=row[1], icon=row[2], stack_name=row[3],
        stack_cell_count=row[4], created_at=row[5], updated_at=row[6], removed_at=row[7],
        pinned=row[8], metadata=metadata,
        reason=row[10],
        display=validate_display(json.loads(row[11])) if row[11] is not None else None,
    )


def remember(conn: sqlite3.Connection, *, cell: Cell, stack_name: str | None,
             stack_cell_count: int, removed_at: str | datetime, reason: str,
             display: Any = _CAPTURE_LIVE_DISPLAY,
             metadata_overrides: dict[str, Any] | None = None) -> None:
    """Record ``cell`` as off the canvas, replacing an earlier record of the
    same cell, ``removed_at`` included. ``metadata_overrides`` replace
    those keys of the cell's dump in ``metadata_json`` only (a remove zone's
    resulting ``tags``); the live cell is untouched. Raises ValueError for an
    unknown ``reason``; does not commit."""
    if reason not in REASONS:
        raise ValueError(f"unknown removal reason: {reason!r}")
    if display is _CAPTURE_LIVE_DISPLAY:
        display = take_display(conn, str(cell.id))
    else:
        # Compensation for a committed transition preserves its original shelf
        # descriptor and clears the live projection in this same transaction.
        display = validate_display(display) if display is not None else None
        write_display(conn, str(cell.id), None)
    metadata = cell.model_dump(mode="json", exclude={"files"})
    metadata.update(metadata_overrides or {})
    conn.execute(
        f"INSERT OR REPLACE INTO removed_cells ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(cell.id), cell.name, cell.icon, stack_name, max(int(stack_cell_count), 1),
         iso(cell.created_at), iso(cell.updated_at), iso(removed_at), cell.pinned,
         json.dumps(metadata),
         reason,
         json.dumps(display) if display is not None else None),
    )


def set_tags(conn: sqlite3.Connection, cell_id: str, tags: list[str]) -> bool:
    """Replace the record's tags (``metadata_json["tags"]``) with ``tags``,
    already normalized (``cell_tags.normalize_tags``); nothing else of the
    record changes, ``removed_at`` included. Returns whether a record
    existed; does not commit."""
    row = conn.execute("SELECT metadata_json FROM removed_cells WHERE cell_id=?", (str(cell_id),)).fetchone()
    if row is None:
        return False
    metadata = json.loads(row[0])
    if not isinstance(metadata, dict):
        raise ValueError("Removed cell metadata must be an object")
    metadata["tags"] = list(tags)
    conn.execute("UPDATE removed_cells SET metadata_json=? WHERE cell_id=?", (json.dumps(metadata), str(cell_id)))
    return True


def forget(conn: sqlite3.Connection, cell_id: str) -> bool:
    """Drop the record (the cell is back on the canvas). Returns whether one
    existed; does not commit."""
    return conn.execute("DELETE FROM removed_cells WHERE cell_id=?", (str(cell_id),)).rowcount == 1


def list_removed(conn: sqlite3.Connection) -> list[RemovedCell]:
    """Every record, most recently removed first."""
    return [_row(row) for row in conn.execute(
        f"SELECT {_COLUMNS} FROM removed_cells ORDER BY removed_at DESC, cell_id")]


def get(conn: sqlite3.Connection, cell_id: str) -> RemovedCell | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM removed_cells WHERE cell_id=?", (str(cell_id),)).fetchone()
    return _row(row) if row else None
