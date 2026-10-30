"""The lineage record: which logical cells a Duplicate, Merge, Split or Run
in new cell made from which.

One ``cell_moves`` row per (source, result) pair. ``op_id`` is the run (a
system run for duplicate, merge and split, the generator run for run_cell)
and groups the pairs of one operation: a merge writes one row per input
with ``ord`` its position, a split one row per output. Cell ids are the
canvas (chain root) ids; ``from_version`` pins the source version the
operation read, ``to_version`` the result's first version when it has one.

Reads go through the ``from_cell`` / ``to_cell`` indexes or the table
itself; nothing here walks version chains or file rows. The writer leaves
the commit to the caller so the row lands in the operation's transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from time_wire import iso

OPS = frozenset({"duplicate", "merge", "split", "run_cell"})

# SQLite's default bound-parameter ceiling is 999 on older builds.
_CHUNK = 450

_COLUMNS = "id, op, op_id, from_cell, from_version, to_cell, to_version, ord, created_at"


@dataclass(frozen=True)
class CellMove:
    id: int
    op: str
    op_id: str
    from_cell: str
    from_version: str | None
    to_cell: str
    to_version: str | None
    ord: int
    created_at: str  # UTC ISO 8601 with offset


def _row(row) -> CellMove:
    return CellMove(
        id=row[0], op=row[1], op_id=row[2], from_cell=row[3], from_version=row[4],
        to_cell=row[5], to_version=row[6], ord=row[7], created_at=iso(row[8]) or "",
    )


def record_move(conn: sqlite3.Connection, *, op: str, op_id: str, from_cell: str,
                from_version: str | None, to_cell: str, to_version: str | None = None,
                ord: int = 0) -> int:
    """Append one lineage row and return its id. Raises ValueError for an
    unknown ``op``; does not commit."""
    if op not in OPS:
        raise ValueError(f"unknown cell move op: {op!r}")
    cursor = conn.execute(
        "INSERT INTO cell_moves (op, op_id, from_cell, from_version, to_cell, to_version, ord) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (op, str(op_id), str(from_cell), from_version, str(to_cell), to_version, int(ord)),
    )
    move_id = cursor.lastrowid
    assert move_id is not None
    return move_id


def moves_for(conn: sqlite3.Connection, cell_ids: Iterable[str]) -> list[CellMove]:
    """Every move with one of ``cell_ids`` on either end, oldest first."""
    ids = sorted({str(cid) for cid in cell_ids})
    found: dict[int, CellMove] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for column in ("from_cell", "to_cell"):
            for row in conn.execute(
                f"SELECT {_COLUMNS} FROM cell_moves WHERE {column} IN ({marks})", chunk,
            ):
                found.setdefault(row[0], _row(row))
    return [found[key] for key in sorted(found)]


def moves_in_channel(conn: sqlite3.Connection) -> list[CellMove]:
    """The channel's whole lineage record, oldest first."""
    return [_row(row) for row in conn.execute(f"SELECT {_COLUMNS} FROM cell_moves ORDER BY id")]
