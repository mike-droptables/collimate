"""A cell's journal: what happened to it, newest first, as the overview's
journal pane reads it (``GET /api/channels/{channel}/cells/{cell_id}/journal``
and, for every cell at once, ``GET /api/channels/{channel}/journal``).

Events and their sources:

* ``make`` — a cell came to be: its layout or removed-record ``created_at``,
  else its root version's time. A cell another cell was duplicated, merged,
  split or run into (a ``cell_moves`` result) has no ``make``: the move is
  its making.
* ``edit`` — a later version on the cell's chain that someone wrote. The
  author is decided by ``source_type``, never by ``run_id`` (the run-start
  draft flush files the user's edit under the generator run's id). Hidden:
  versions that touch only ``_runs/``, the host's run-folder sweep, the
  checkpoints that Remove, Merge and Split write, and the version rows a
  Fork, Merge or Split writes (their move events tell those).
* ``run`` — a generator run on the cell (``runs.kind='generator'``), at its
  end (its start while it runs). Its own versions are folded into it:
  ``files_written`` are the paths they changed, ``version_id`` the newest.
* ``run_cell`` — a run in a new cell: the run, with the source cell first
  and the new cell second in ``cells``.
* ``cell_renamed`` — a cell's metadata name transition, with literal
  old/new names and a captured actor, committed with the layout in app.db.
* ``duplicate``, ``merge``, ``split`` — one per operation group
  (``cell_lineage.group_moves``); ``cells`` lists the sources then the
  results.

Every time is UTC ISO 8601 with an offset (``time_wire.iso``). Pages are
cut by ``before`` (exclusive) and ``limit``; a page never splits a second,
so passing the last event's ``t`` as the next ``before`` loses nothing.
File lists are capped at ``FILES_CAP`` paths; ``files_total`` counts them all.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException

import cell_lineage
from run_folders import is_run_path
from time_wire import iso

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
FILES_CAP = 100

_CHUNK = 450
_BATCH = 400
# Checkpoints written by an operation that has its own event (or none).
_OPERATION_RUN_PROMPTS = frozenset({"remove cell"})
_OPERATION_RUN_GENERATORS = frozenset({"merge", "split"})
# Version rows whose story is a move event.
_MOVE_SOURCE_TYPES = frozenset({"branch", "merge", "split", "genesis"})
# Runs' end, else start, in SQLite's text form (ended_at is wire ISO, UTC).
_RUN_TIME = "COALESCE(replace(substr(ended_at, 1, 19), 'T', ' '), created_at)"
_RUN_COLUMNS = ("id, status, created_at, ended_at, note, outcome, runner_id, "
                "runner_snapshot, target_cell_id")


@dataclass
class _Event:
    t: str
    kind: str
    key: str
    cells: list[str]
    by: dict | None = None
    version_id: str | None = None
    files: list[str] = field(default_factory=list)
    files_total: int = 0
    run: dict | None = None  # the runs row, for run and run_cell
    root_id: str | None = None  # make: the root version, when it has one
    old_name: str | None = None
    new_name: str | None = None


def journal(channel: str, cell_id: str | None, *, before: str | None, limit: int) -> list[dict]:
    """The journal page of ``cell_id`` (every cell when None)."""
    from channel_db import open_channel_db
    from state import require_channel, state_lock

    try:
        before_iso = iso(before) if before else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"before must be an ISO 8601 time with an offset: {exc}") from exc
    with state_lock:
        ch = require_channel(channel)
    live = cell_lineage.live_cells(ch)
    conn = open_channel_db(channel)
    try:
        labels = cell_lineage.CellLabels(conn, live)
        if cell_id is not None:
            candidates = _cell_candidates(conn, cell_id, live, labels)
        else:
            candidates = _channel_candidates(conn, before_iso, limit, live, labels)
        candidates += _rename_candidates(str(ch.id), cell_id, before_iso, limit)
        page = _cut(candidates, before_iso, limit)
        return _complete(conn, ch, page, labels)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def _rename_candidates(channel_id: str, cell_id: str | None,
                       before_iso: str | None, limit: int) -> list[_Event]:
    import json
    from database import get_global_db_connection
    conn = get_global_db_connection()
    try:
        where = "channel_id=?"
        params: list = [channel_id]
        if cell_id is not None:
            where += " AND cell_id=?"
            params.append(cell_id)
        if before_iso is not None:
            where += " AND created_at<?"
            params.append(before_iso)
        boundary = conn.execute(
            f"SELECT created_at FROM cell_rename_events WHERE {where} "
            "ORDER BY created_at DESC LIMIT 1 OFFSET ?", (*params, limit - 1)).fetchone()
        if boundary is not None:
            where += " AND created_at>=?"
            params.append(boundary[0])
        return [_Event(t=row["created_at"], kind="cell_renamed",
                       key=f"rename:{row['event_id']}", cells=[row["cell_id"]],
                       by=json.loads(row["actor_json"]), old_name=row["old_name"],
                       new_name=row["new_name"])
                for row in conn.execute(f"SELECT * FROM cell_rename_events WHERE {where}", params)]
    finally:
        conn.close()


def _cell_candidates(conn, cell_id: str, live: dict, labels) -> list[_Event]:
    from cell_files import walk_versions

    chain = walk_versions(conn, cell_id)
    rows = _version_rows(conn, chain)
    groups = cell_lineage.related_groups(conn, [cell_id])
    made_by_move = any(m.to_cell == cell_id and _created(m) for rows_ in groups for m in rows_)
    run_rows = {row["id"]: dict(row) for row in conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE kind='generator' AND target_cell_id=?", (cell_id,))}
    run_cell_groups = {rows_[0].op_id: rows_ for rows_ in groups if rows_[0].op == "run_cell"}
    run_rows.update(_runs(conn, set(run_cell_groups) - set(run_rows)))

    events: list[_Event] = []
    own = rows.get(cell_id)
    if not made_by_move and (own is not None or cell_id in live or cell_id in labels.removed):
        root_id = (own["root_id"] or cell_id) if own is not None else None
        events.append(_make(conn, cell_id, root_id, live, labels))
    edits = [row for vid, row in rows.items()
             if row["parent_version_id"] is not None
             and (row["source_type"] != "generator" or row["run_id"] not in run_rows)]
    events += _edits(conn, edits, lambda _row: cell_id)
    events += _run_events(run_rows.values(), run_cell_groups)
    events += _move_events(groups, run_rows)
    return events


def _created(move) -> bool:
    """Whether ``move`` made its ``to_cell``: the result starts a new chain
    (its first version is the cell's root, or it has none yet). A Split that
    brings a merge input back writes a child version on that cell's own
    chain, and the cell's make stays its own."""
    return move.to_version is None or move.to_version == move.to_cell


def _channel_candidates(conn, before_iso: str | None, limit: int, live: dict, labels) -> list[_Event]:
    import cell_moves

    groups = cell_lineage.group_moves(cell_moves.moves_in_channel(conn))
    targets = {m.to_cell for rows in groups for m in rows if _created(m)}
    events: list[_Event] = []

    # Makes: every cell in the layout or the removed record, and any other
    # chain root (a cell neither on the canvas nor recorded).
    known = set(live) | set(labels.removed)
    roots = {row[0]: row[1] for row in conn.execute(
        "SELECT v.version_id, c.created_at FROM cell_file_versions v "
        "JOIN cells c ON c.id = v.version_id WHERE v.parent_version_id IS NULL")}
    for cid in known | set(roots):
        if cid not in targets:
            events.append(_make(conn, cid, cid if cid in roots else None, live, labels, roots.get(cid)))

    # Edits: non-root versions someone wrote, newest first in batches until
    # the page (and its last second) is covered.
    before_sql = _sql_time(before_iso)
    found: list[_Event] = []
    offset = 0
    while True:
        batch = [dict(row) for row in conn.execute(
            "SELECT c.id, c.run_id, c.source_type, c.generator_name, c.created_at, "
            "v.parent_version_id, v.root_id FROM cells c "
            "JOIN cell_file_versions v ON v.version_id = c.id "
            "WHERE v.parent_version_id IS NOT NULL AND c.source_type IS NOT 'generator' "
            "AND (? IS NULL OR c.created_at < ?) "
            "ORDER BY c.created_at DESC, c.rowid DESC LIMIT ? OFFSET ?",
            (before_sql, before_sql, _BATCH, offset))]
        if not batch:
            break
        offset += len(batch)
        found += _edits(conn, batch, lambda row: row["root_id"])
        if len(batch) < _BATCH:
            break
        if len(found) >= limit:
            found.sort(key=lambda e: e.t, reverse=True)
            if (iso(batch[-1]["created_at"]) or "") < found[limit - 1].t:
                break
    events += found

    # Runs: the newest ``limit`` (and the rest of the last one's second).
    params: list = [before_sql, before_sql]
    where = f"kind='generator' AND (? IS NULL OR {_RUN_TIME} < ?)"
    boundary = conn.execute(
        f"SELECT {_RUN_TIME} FROM runs WHERE {where} ORDER BY {_RUN_TIME} DESC LIMIT 1 OFFSET ?",
        (*params, limit - 1)).fetchone()
    if boundary is not None:
        where += f" AND {_RUN_TIME} >= ?"
        params.append(boundary[0])
    run_rows = {row["id"]: dict(row) for row in conn.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE {where}", params)}
    run_cell_groups = {rows[0].op_id: rows for rows in groups if rows[0].op == "run_cell"}
    events += _run_events(run_rows.values(), run_cell_groups)
    known_runs = set(run_rows) | set(_runs(conn, set(run_cell_groups) - set(run_rows)))
    events += _move_events(groups, {rid: {} for rid in known_runs})
    return events


def _make(conn, cell_id: str, root_id: str | None, live: dict, labels,
          root_created: str | None = None) -> _Event:
    record = labels.removed.get(cell_id)
    if cell_id in live:
        t = iso(live[cell_id]["created_at"])
    elif record is not None and record.created_at:
        t = record.created_at
    else:
        if root_created is None and root_id is not None:
            row = conn.execute("SELECT created_at FROM cells WHERE id=?", (root_id,)).fetchone()
            root_created = row[0] if row else None
        t = iso(root_created) if root_created else ""
    return _Event(t=t or "", kind="make", key=f"make:{cell_id}", cells=[cell_id], root_id=root_id)


def _edits(conn, rows: list[dict], logical) -> list[_Event]:
    """Edit events of non-root version ``rows``, hidden ones dropped."""
    runs = _system_runs(conn, {row["run_id"] for row in rows if row["run_id"]})
    kept = [row for row in rows if _edit_visible(row, runs.get(row["run_id"]))]
    paths = _delta_paths(conn, [row["id"] for row in kept])
    runners = cell_lineage.runners_of(conn, {row["run_id"] for row in kept
                                             if row["source_type"] == "generator"})
    events = []
    for row in kept:
        changed = paths.get(row["id"], [])
        if changed and all(is_run_path(p) for p in changed):
            continue
        changed = sorted(p for p in changed if not is_run_path(p))
        by = (cell_lineage.runner_actor(runners.get(row["run_id"]))
              if row["source_type"] == "generator" else cell_lineage.user_actor())
        events.append(_Event(t=iso(row["created_at"]) or "", kind="edit", key=f"edit:{row['id']}",
                             cells=[logical(row)], by=by, version_id=row["id"],
                             files=changed[:FILES_CAP], files_total=len(changed)))
    return events


def _edit_visible(row: dict, run: dict | None) -> bool:
    if row["source_type"] in _MOVE_SOURCE_TYPES:
        return False
    if row["source_type"] == "manual_edit" and row["generator_name"] == "host":
        return False
    if run is not None and run["kind"] != "generator" and (
            run["prompt"] in _OPERATION_RUN_PROMPTS or run["generator_name"] in _OPERATION_RUN_GENERATORS):
        return False
    return True


def _run_events(runs, run_cell_groups: dict) -> list[_Event]:
    events = []
    for run in runs:
        group = run_cell_groups.get(run["id"])
        cells = [run["target_cell_id"]] if run["target_cell_id"] else []
        if group is not None:
            cells = list(dict.fromkeys([*(m.from_cell for m in group), *(m.to_cell for m in group), *cells]))
        events.append(_Event(t=iso(run["ended_at"] or run["created_at"]) or "",
                             kind="run_cell" if group is not None else "run",
                             key=f"run:{run['id']}", cells=cells, run=run))
    return events


def _move_events(groups, runs: dict) -> list[_Event]:
    events = []
    for rows in groups:
        op = rows[0].op
        if op == "run_cell" and rows[0].op_id in runs:
            continue  # told by its run's event
        sources, results = cell_lineage.group_ends(rows)
        versions = {end["version_id"] for end in results}
        events.append(_Event(
            t=rows[0].created_at, kind=op, key=f"move:{rows[0].id}",
            cells=list(dict.fromkeys(end["cell_id"] for end in (*sources, *results))),
            by=cell_lineage.user_actor(),
            version_id=next(iter(versions)) if len(versions) == 1 else None))
    return events


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def _cut(events: list[_Event], before_iso: str | None, limit: int) -> list[_Event]:
    kept = [e for e in events if e.t and (before_iso is None or e.t < before_iso)]
    kept.sort(key=lambda e: (e.t, e.key), reverse=True)
    if len(kept) <= limit:
        return kept
    last = kept[limit - 1].t
    end = limit
    while end < len(kept) and kept[end].t == last:
        end += 1
    return kept[:end]


def _complete(conn, ch, page: list[_Event], labels) -> list[dict]:
    runs = [e.run for e in page if e.run is not None]
    written = _files_written(conn, [run["id"] for run in runs])
    runners = cell_lineage.runners_of(conn, [run["id"] for run in runs])
    _complete_makes(conn, ch, [e for e in page if e.kind == "make"])
    names = {label["cell_id"]: label for label in labels.labels(cid for e in page for cid in e.cells)}
    out = []
    for e in page:
        run = None
        if e.run is not None:
            paths, version_id = written.get(e.run["id"], ([], None))
            e.by = cell_lineage.runner_actor(runners.get(e.run["id"]))
            e.files, e.files_total, e.version_id = paths[:FILES_CAP], len(paths), version_id
            run = {"run_id": e.run["id"], "runner_id": e.by["runner_id"],
                   "runner_name": e.by["runner_name"], "status": e.run["status"],
                   "outcome": e.run["outcome"], "note": e.run["note"],
                   "started_at": iso(e.run["created_at"]), "ended_at": iso(e.run["ended_at"]),
                   "files_written": e.files}
        out.append({"t": e.t, "kind": e.kind, "by": e.by or cell_lineage.user_actor(),
                    "cells": [_cell_ref(names[cid]) for cid in e.cells],
                    "run": run, "files": e.files, "files_total": e.files_total,
                    "version_id": e.version_id})
        if e.kind == "cell_renamed":
            out[-1].update(old_name=e.old_name, new_name=e.new_name)
    return out


def _cell_ref(label: dict) -> dict:
    return {"cell_id": label["cell_id"], "name": label["name"], "status": label["status"]}


def _complete_makes(conn, ch, makes: list[_Event]) -> None:
    """Who made each cell (its root version's ``source_type``: a generator
    root is its runner's, anything else the user's) and its first files."""
    from state import state_lock
    from cell_placement import iter_stacks

    roots = [e.root_id for e in makes if e.root_id]
    meta = {}
    for start in range(0, len(roots), _CHUNK):
        chunk = roots[start:start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        meta.update({row[0]: row for row in conn.execute(
            f"SELECT c.id, c.source_type, c.run_id, v.file_count FROM cells c "
            f"LEFT JOIN cell_file_versions v ON v.version_id = c.id WHERE c.id IN ({marks})", chunk)})
    runners = cell_lineage.runners_of(conn, {row[2] for row in meta.values() if row[1] == "generator"})
    unversioned = {e.cells[0] for e in makes if e.root_id not in meta}
    layout_files: dict[str, list[str]] = {}
    if unversioned:
        with state_lock:
            for stack in iter_stacks(ch):
                for cell in stack.cells:
                    if str(cell.id) in unversioned:
                        layout_files[str(cell.id)] = [f.name for f in cell.files]
    for e in makes:
        row = meta.get(e.root_id)
        if row is None:
            files = sorted(p for p in layout_files.get(e.cells[0], []) if not is_run_path(p))
            e.files, e.files_total = files[:FILES_CAP], len(files)
            e.by = cell_lineage.user_actor()
            continue
        e.version_id = row[0]
        e.by = (cell_lineage.runner_actor(runners.get(row[2])) if row[1] == "generator"
                else cell_lineage.user_actor())
        e.files = [r[0] for r in conn.execute(
            "SELECT filepath FROM cell_files WHERE cell_id=? AND blob_hash IS NOT NULL "
            "AND filepath NOT LIKE '\\_runs/%' ESCAPE '\\' ORDER BY filepath LIMIT ?",
            (row[0], FILES_CAP))]
        e.files_total = max(int(row[3] or 0), len(e.files))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _chunks(ids):
    ids = list(ids)
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        yield chunk, ",".join("?" * len(chunk))


def _version_rows(conn, version_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk, marks in _chunks(version_ids):
        for row in conn.execute(
            f"SELECT c.id, c.run_id, c.source_type, c.generator_name, c.created_at, "
            f"v.parent_version_id, v.root_id FROM cells c "
            f"LEFT JOIN cell_file_versions v ON v.version_id = c.id WHERE c.id IN ({marks})", chunk,
        ):
            out[row["id"]] = dict(row)
    return {vid: out[vid] for vid in version_ids if vid in out}


def _system_runs(conn, run_ids) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk, marks in _chunks(sorted(run_ids)):
        for row in conn.execute(
            f"SELECT id, kind, prompt, generator_name FROM runs WHERE id IN ({marks})", chunk,
        ):
            out[row["id"]] = dict(row)
    return out


def _runs(conn, run_ids) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk, marks in _chunks(sorted(run_ids)):
        for row in conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs WHERE id IN ({marks})", chunk):
            out[row["id"]] = dict(row)
    return out


def _delta_paths(conn, version_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for chunk, marks in _chunks(version_ids):
        for row in conn.execute(
            f"SELECT cell_id, filepath FROM cell_files WHERE cell_id IN ({marks})", chunk,
        ):
            out.setdefault(row[0], []).append(row[1])
    return out


def _files_written(conn, run_ids: list[str]) -> dict[str, tuple[list[str], str | None]]:
    """``{run_id: (paths, version_id)}``: the paths the run's own generator
    versions changed (outside ``_runs/``) and the newest such version."""
    paths: dict[str, dict[str, None]] = {}
    newest: dict[str, str] = {}
    for chunk, marks in _chunks(run_ids):
        for row in conn.execute(
            f"SELECT c.run_id, c.id, cf.filepath FROM cells c "
            f"JOIN cell_file_versions v ON v.version_id = c.id AND v.parent_version_id IS NOT NULL "
            f"JOIN cell_files cf ON cf.cell_id = c.id "
            f"WHERE c.run_id IN ({marks}) AND c.source_type = 'generator' ORDER BY c.rowid", chunk,
        ):
            if is_run_path(row[2]):
                continue
            paths.setdefault(row[0], {})[row[2]] = None
            newest[row[0]] = row[1]
    return {rid: (sorted(found), newest.get(rid)) for rid, found in paths.items()}


def _sql_time(wire: str | None) -> str | None:
    """A wire time (UTC, ``+00:00``) in SQLite's ``CURRENT_TIMESTAMP`` form."""
    return wire[:19].replace("T", " ") if wire else None
