"""The channel's lineage: which logical cells were duplicated, merged, split
or run into a new cell from which, as the overview's lines read it.

``lineage(channel)`` answers ``GET /api/channels/{channel}/lineage`` from
the ``cell_moves`` table, the layout and the ``removed_cells`` record; it
never walks version chains or file rows. ``announce_lineage`` is the one
emitter of the ``lineage_changed`` channel event: every operation that
writes a move, removes a cell or restores one calls it after its commit so
the Removed shelf, the journal and the lines refetch.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable

import cell_moves
import removed_cells
from time_wire import iso

_logger = logging.getLogger(__name__)


def announce_lineage(channel: str, cell_ids: Iterable[object]) -> None:
    """Broadcast ``lineage_changed {cell_ids}`` on the channel's SSE.

    Best effort, after the operation committed: a lost event only delays a
    refetch. ``cell_ids`` are logical (canvas) cell ids; duplicates and
    empty ids are dropped, order is kept. No ids, no event.
    """
    ids = list(dict.fromkeys(str(cid) for cid in cell_ids if cid))
    if not ids:
        return
    try:
        from collimate._host.channel_events import LineageChanged
        from events import broadcast
        broadcast(channel, LineageChanged(ids).to_dict())
    except Exception:
        _logger.debug("lineage_changed broadcast failed for %s", ids, exc_info=True)


_USER = {"kind": "user", "runner_id": None, "runner_name": None}


def user_actor() -> dict:
    """The ``by`` / ``made_by`` record of the person at the keyboard."""
    return dict(_USER)


def runners_of(conn, run_ids: Iterable[str]) -> dict[str, dict]:
    """``{run_id: {"runner_id", "runner_name"}}`` from the runs' own columns
    (``runner_snapshot.name`` is the name the run started under)."""
    ids = sorted({str(rid) for rid in run_ids if rid})
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 450):
        chunk = ids[start:start + 450]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT id, runner_id, runner_snapshot FROM runs WHERE id IN ({marks})", chunk,
        ):
            out[row[0]] = {"runner_id": row[1], "runner_name": snapshot_name(row[2])}
    return out


def runner_actor(runner: dict | None) -> dict:
    """The ``by`` record of a runner (``runners_of`` value)."""
    runner = runner or {}
    return {"kind": "runner", "runner_id": runner.get("runner_id"),
            "runner_name": runner.get("runner_name")}


def snapshot_name(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return None
    name = snapshot.get("name") if isinstance(snapshot, dict) else None
    return name if isinstance(name, str) else None


def live_cells(ch) -> dict[str, dict]:
    """``{cell_id: {"name", "stack_id", "created_at"}}`` for every cell in the
    channel's layout (main and minimized stacks), copied under ``state_lock``."""
    from cell_placement import iter_stacks
    from state import state_lock
    with state_lock:
        return {str(cell.id): {"name": cell.name, "stack_id": str(stack.id),
                               "created_at": cell.created_at}
                for stack in iter_stacks(ch) for cell in stack.cells}


class CellLabels:
    """Names and open/removed status of logical cells, from the layout, the
    ``removed_cells`` record and, for a cell in neither, its own version row."""

    def __init__(self, conn, live: dict[str, dict]):
        self._conn = conn
        self.live = live
        self.removed = {record.cell_id: record for record in removed_cells.list_removed(conn)}
        self._rows: dict[str, str | None] = {}

    def label(self, cell_id: str) -> dict:
        live = self.live.get(cell_id)
        if live is not None:
            return {"cell_id": cell_id, "name": live["name"], "status": "open", "stack_id": live["stack_id"]}
        record = self.removed.get(cell_id)
        name = record.name if record is not None else self._rows.get(cell_id)
        return {"cell_id": cell_id, "name": name or "Cell", "status": "removed"}

    def labels(self, cell_ids: Iterable[str]) -> list[dict]:
        ids = list(dict.fromkeys(cell_ids))
        unknown = [cid for cid in ids
                   if cid not in self.live and cid not in self.removed and cid not in self._rows]
        for start in range(0, len(unknown), 450):
            chunk = unknown[start:start + 450]
            marks = ",".join("?" * len(chunk))
            found = dict(self._conn.execute(
                f"SELECT id, version_name FROM cells WHERE id IN ({marks})", chunk).fetchall())
            for cid in chunk:
                self._rows[cid] = found.get(cid)
        return [self.label(cid) for cid in ids]


def group_moves(moves: Iterable[cell_moves.CellMove]) -> list[list[cell_moves.CellMove]]:
    """One group per operation as the overview draws it: a merge's inputs
    into one result, a split's outputs from one source, and each duplicate
    or run-in-new-cell pair on its own (one Duplicate stack writes a pair per
    copied cell under a single op id). Oldest first; rows inside a group in
    ``ord`` order."""
    groups: dict[tuple, list[cell_moves.CellMove]] = {}
    for move in sorted(moves, key=lambda m: m.id):
        if move.op == "merge":
            key: tuple = ("merge", move.op_id, move.to_cell)
        elif move.op == "split":
            key = ("split", move.op_id, move.from_cell)
        else:
            key = (move.op, move.id)
        groups.setdefault(key, []).append(move)
    for rows in groups.values():
        rows.sort(key=lambda m: (m.ord, m.id))
    return sorted(groups.values(), key=lambda rows: min(m.id for m in rows))


def group_ends(rows: list[cell_moves.CellMove]) -> tuple[list[dict], list[dict]]:
    """The group's ``from`` and ``to`` ends, ``[{cell_id, version_id}]``,
    without repeats."""
    def ends(pairs):
        return [{"cell_id": cid, "version_id": vid} for cid, vid in dict.fromkeys(pairs)]
    return (ends((m.from_cell, m.from_version) for m in rows),
            ends((m.to_cell, m.to_version) for m in rows))


def related_groups(conn, cell_ids: Iterable[str]) -> list[list[cell_moves.CellMove]]:
    """Every operation group that touches one of ``cell_ids``, complete (a
    merge's other inputs, a split's other outputs), through the two indexes."""
    first = cell_moves.moves_for(conn, cell_ids)
    if not first:
        return []
    wanted = {key for key in (_group_key(m) for m in first)}
    others = {m.from_cell for m in first} | {m.to_cell for m in first}
    rows = {m.id: m for m in first}
    for move in cell_moves.moves_for(conn, others):
        if _group_key(move) in wanted:
            rows.setdefault(move.id, move)
    return group_moves(rows.values())


def _group_key(move: cell_moves.CellMove) -> tuple:
    if move.op == "merge":
        return ("merge", move.op_id, move.to_cell)
    if move.op == "split":
        return ("split", move.op_id, move.from_cell)
    return (move.op, move.id)


def lineage(channel: str) -> dict:
    """``{cells, moves}`` for the overview's lines and Only connected.

    ``cells`` holds every cell in the layout (``status`` 'open' with its
    ``stack_id``), every removed record, and any other cell a move names
    ('removed'). ``moves`` is one entry per operation (``group_moves``):
    ``{id, op, at, op_id, from, to}`` plus ``runner`` for a run in a new cell.
    Reads the ``cell_moves`` table, the layout and the removed record only.
    """
    from channel_db import open_channel_db
    from state import require_channel, state_lock

    with state_lock:
        ch = require_channel(channel)
    live = live_cells(ch)
    conn = open_channel_db(channel)
    try:
        labels = CellLabels(conn, live)
        groups = group_moves(cell_moves.moves_in_channel(conn))
        runners = runners_of(conn, (rows[0].op_id for rows in groups if rows[0].op == "run_cell"))
        moves = []
        for rows in groups:
            sources, results = group_ends(rows)
            move = {"id": rows[0].id, "op": rows[0].op, "at": iso(rows[0].created_at),
                    "op_id": rows[0].op_id, "from": sources, "to": results}
            if rows[0].op == "run_cell":
                runner = runners.get(rows[0].op_id) or {}
                move["runner"] = {"runner_id": runner.get("runner_id"),
                                  "runner_name": runner.get("runner_name")}
            moves.append(move)
        named = list(live) + list(labels.removed)
        named += [end["cell_id"] for move in moves for end in (*move["from"], *move["to"])]
        return {"cells": labels.labels(named), "moves": moves}
    finally:
        conn.close()
