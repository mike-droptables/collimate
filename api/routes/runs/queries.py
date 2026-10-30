"""Read-only run lookup endpoints + event backfill."""

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from database import canonical_channel_id
from channel_db import open_channel_db

from run_registry import active_managers
from time_wire import iso

router = APIRouter()


@router.get("/api/channels/{channel}/runs")
async def list_runs(channel: str, limit: int = 100):
    """List recent runs and their frozen generator and runner identities."""
    def _query():
        conn = open_channel_db(channel)
        try:
            rows = conn.execute(
                "SELECT id, prompt, status, failure_reason, created_at, "
                "generator_name, generator_cell_id, "
                "runner_id, runner_cell_id FROM runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_query)


def _run_facts(channel: str, run_ids: list[str]) -> dict[str, dict]:
    """Each run's ``{started_at, listening}``: its runs row's ``created_at``
    (SQLite UTC) as ISO 8601, and whether it has polled its annotation
    context. A channel whose database has been deleted has none."""
    import annotations
    if not run_ids:
        return {}
    try:
        conn = open_channel_db(channel)
    except HTTPException:
        return {}
    try:
        rows = conn.execute(
            f"SELECT id, created_at FROM runs WHERE id IN ({','.join('?' * len(run_ids))})",
            run_ids,
        ).fetchall()
        listening = annotations.listening(conn, run_ids)
    finally:
        conn.close()
    starts = {r["id"]: r["created_at"].replace(" ", "T") + "Z" for r in rows if r["created_at"]}
    return {run_id: {"started_at": starts.get(run_id), "listening": listening.get(run_id, False)}
            for run_id in run_ids}


async def _active_rows(channel: str, managers: list[tuple[str, Any]]) -> list[dict]:
    """One channel's live runs as the /runs/active rows. ``managers`` are
    its (run_id, ProcessManager) pairs, in start order. Only the db read
    leaves the event loop: the app state is read on it."""
    from cell_placement import find_cell
    from collimate.harness_catalog import HARNESSES
    from served_surfaces import for_run
    from state import get_channel_by_id
    facts = await asyncio.to_thread(
        _run_facts, channel, [run_id for run_id, _manager in managers])
    owner = get_channel_by_id(channel)
    rows = []
    for run_id, manager in managers:
        _stack, cell = find_cell(owner, manager.cell_id)
        harness = getattr(manager, "harness", None)
        rows.append({
            "run_id": run_id,
            "cell_id": manager.cell_id or "",
            # The cell's name on its canvas ('' once it left it), for a
            # client that holds only another channel's metadata.
            "cell_name": cell.name if cell is not None else "",
            "runner_id": manager.runner_id,
            "runner_name": manager.runner_name,
            "label": manager.label,
            "folder": manager.folder,
            "generator_name": manager.generator_name,
            "started_at": facts.get(run_id, {}).get("started_at"),
            # Whether the run reads its annotation context (it has polled it).
            "listening": facts.get(run_id, {}).get("listening", False),
            "surfaces": [surface.public() for surface in for_run(run_id)],
            # The live phase: a missed run_phase broadcast is repaired on
            # reload. A final status is the phase its finalize is about to
            # apply (it sets the status first).
            "phase": (manager.final_status
                      or manager.current_phase or ""),
            "phase_message": manager.current_phase_message or "",
            "stopping": bool(manager.stop_requested
                             or manager.final_status),
            # The grant the run runs under, whether it was raised to its
            # harness's minimum (runners.adopt_harness_grant), and that
            # harness and its label; None / False / None / None for a run no
            # harness binds.
            "effective_grant": getattr(manager, "effective_grant", None),
            "adopted": bool(getattr(manager, "adopted", False)),
            "harness": harness,
            "harness_label": HARNESSES[harness]["label"] if harness in HARNESSES else None,
            # A CLI run's harness CLI: on this machine (host) or in its
            # container; None for any other run.
            "cli_locus": getattr(manager, "cli_locus", None),
        })
    return rows


@router.get("/api/channels/{channel}/runs/active")
async def list_active_runs(channel: str):
    """The channel's live runs with what the frontend needs to reattach:
    which cell each runs on, its runner and label, its run folder (every
    file there is the run's locked file), its surfaces (starting or live),
    its live phase, when it started, and ``listening`` (the run has polled
    its annotation context). An accepted run whose spawn has not
    registered yet is listed too, ``queued``.

    A run stays listed until its run-end cleanup is done (the reaper is
    what removes it); ``stopping`` marks one whose end is under way — a stop
    was requested or its status is final — so a reload does not attach it
    as live.

    Declared before the ``/runs/{run_id}`` route so ``active`` isn't captured
    as a run_id path parameter.
    """
    from state import require_channel
    ch = str(require_channel(channel).id)
    managers = [(run_id, manager) for run_id, manager in live_and_starting()
                if canonical_channel_id(manager.channel or "") == ch]
    return await _active_rows(ch, managers)


def live_and_starting() -> list[tuple[str, Any]]:
    """The registered runs in start order, then the accepted runs whose
    spawn has not registered yet (phase ``queued``)."""
    from routes.runs.create import starting_runs
    return [*active_managers.items(), *starting_runs()]


# Channel-free: included without the channel dependency (routes/runs/__init__).
everywhere_router = APIRouter()


@everywhere_router.get("/api/runs/active")
async def list_active_runs_everywhere():
    """Every channel's live runs, ``[{channel, channel_name, runs}]`` with
    ``runs`` the channel's ``/runs/active`` rows, from the same registry.
    The top bar's running count and runs menu read it; the channel stream's
    ``runs_active_changed`` says when to read it again."""
    by_channel: dict[str, list[tuple[str, Any]]] = {}
    for run_id, manager in live_and_starting():
        by_channel.setdefault(canonical_channel_id(manager.channel or ""), []).append((run_id, manager))

    from state import get_channel_by_id
    out = []
    for ch, managers in by_channel.items():
        owner = get_channel_by_id(ch)
        if owner is None:
            continue
        runs = await _active_rows(ch, managers)
        out.append({
            "channel": ch,
            "channel_name": owner.name,
            "runs": runs,
        })
    return out


@router.get("/api/channels/{channel}/cells/{cell_id}/surfaces/{filepath:path}")
async def get_surface(channel: str, cell_id: str, filepath: str):
    """The coordinates of the run surface at this exact cell path:
    ``state: 'starting'`` (claimed by an accepted run that has not served it
    yet; no session) or ``state: 'live'`` (with its session)."""
    from served_surfaces import get
    from state import require_channel
    owner = require_channel(channel)
    surface = get(cell_id, filepath)
    if surface is None or str(require_channel(surface.channel).id) != str(owner.id):
        raise HTTPException(404, 'No run surface at this path')
    return surface.coordinates()


@router.get("/api/channels/{channel}/runs/{run_id}")
async def get_run(channel: str, run_id: str):
    """Return a run record and the version rows it committed. The record carries
    how the run ended: ``ended_at`` (UTC ISO with offset; None while it
    runs), ``note`` and ``outcome`` ('pass' / 'fail' / None)."""
    def _query():
        conn = open_channel_db(channel)
        try:
            run_row = conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            if not run_row:
                return None
            cells = conn.execute(
                "SELECT id, generator_name, generator_cell_id, sequence_num, created_at "
                "FROM cells WHERE run_id=? "
                "ORDER BY sequence_num",
                (run_id,),
            ).fetchall()
            run = dict(run_row)
            run["ended_at"] = iso(run["ended_at"])
            return {
                "run": run,
                "cells": [dict(c) for c in cells],
            }
        finally:
            conn.close()
    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "Run not found")
    return result


@router.get("/api/channels/{channel}/runs/{run_id}/events_since")
async def fetch_run_events_since(channel: str, run_id: str, since_id: int = 0):
    """One-shot backfill of a run's persisted events.

    Replaces the long-lived per-run SSE stream for live delivery: live
    events now flow through the per-channel state SSE as ``run_events``
    messages (see process_manager._broadcast_run_events). This endpoint
    exists only to backfill history on ``attachToRun`` / reload so the
    frontend can reconstruct the log before it starts consuming live
    broadcasts.

    The latest 5,000 rows bound reload work. The last outcome event of each
    kind also survives truncation, so stopping and completion notifications
    do not depend on how many ordinary log entries followed them. The cursor
    and omission count let the client resume live delivery and label its tail.
    """
    def _query():
        conn = open_channel_db(channel)
        try:
            rows = conn.execute(
                "SELECT id, event_type, payload, created_at "
                "FROM run_events WHERE run_id=? AND id>? ORDER BY id DESC LIMIT 5000",
                (run_id, since_id),
            ).fetchall()
            count = conn.execute(
                "SELECT count(*) FROM run_events WHERE run_id=? AND id>?",
                (run_id, since_id),
            ).fetchone()[0]
            last_event_id = rows[0]['id'] if rows else since_id
            if count > len(rows):
                outcomes = conn.execute(
                    "SELECT id, event_type, payload, created_at FROM run_events WHERE id IN ("
                    "SELECT max(id) FROM run_events WHERE run_id=? AND id>? "
                    "AND event_type IN ('stop','no_output','finish','timeout') GROUP BY event_type)",
                    (run_id, since_id),
                ).fetchall()
                by_id = {r['id']: r for r in [*rows, *outcomes]}
                rows = sorted(by_id.values(), key=lambda r: r['id'])
            else:
                rows.reverse()
            run_row = conn.execute(
                "SELECT status, phase, failure_reason FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            return rows, run_row, count - len(rows), last_event_id
        finally:
            conn.close()
    def _response():
        rows, run_row, dropped, last_event_id = _query()

        status = run_row["status"] if run_row else None
        # Authoritative current phase for reconnect: the client seeds its
        # phase indicator from this in the same round-trip it already makes to
        # backfill events, so a reload mid-run recovers spinning_up/running
        # without replaying the phase event rows. May be NULL for runs inserted
        # without a phase (generator edits, checkpoints), and is None
        # when there is no run row at all.
        phase = run_row["phase"] if run_row else None
        is_active = run_id in active_managers
        # Authoritative terminal flag — the frontend used to re-derive this
        # from !is_active && status!='running', which disagreed with the SSE
        # path's definition (status in the terminal set). Emit it here so both
        # consumers read from the same source. TERMINAL_PHASES mirrors
        # runs.status's terminal vocabulary (completed/stopped/failed/
        # timed_out/interrupted).
        from process_manager.phases import is_terminal as _phase_is_terminal
        is_terminal = _phase_is_terminal(status) and not is_active
        return JSONResponse({
            "events": [
                {
                    "id": r["id"],
                    "type": r["event_type"],
                    "payload": r["payload"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
            "status": status,
            "phase": phase,
            "failure_reason": run_row["failure_reason"] if run_row else None,
            "is_active": is_active,
            "is_terminal": is_terminal,
            "truncated": dropped > 0,
            "dropped_logs": dropped,
            "last_event_id": last_event_id,
        })

    return await asyncio.to_thread(_response)


@router.get("/api/channels/{channel}/runs/{run_id}/artifacts")
async def list_run_artifacts(channel: str, run_id: str):
    """The run's Config/Logs artifact listing: {run_id, status, artifacts:
    [{category, path, blob_hash, size}]}. Content is served by
    GET /api/blob/{hash} (immutable-cached — a finished run's artifacts
    never change)."""

    def _q():
        conn = open_channel_db(channel)
        try:
            run = conn.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            if run is None:
                return None
            rows = conn.execute(
                "SELECT ra.category, ra.filepath, ra.blob_hash, b.size "
                "FROM run_artifacts ra JOIN blobs b ON ra.blob_hash = b.hash "
                "WHERE ra.run_id=? ORDER BY ra.category, ra.filepath",
                (run_id,),
            ).fetchall()
            return {
                "run_id": run_id,
                "status": run["status"],
                "artifacts": [
                    {
                        "category": r["category"],
                        "path": r["filepath"],
                        "blob_hash": r["blob_hash"],
                        "size": r["size"],
                    }
                    for r in rows
                ],
            }
        finally:
            conn.close()

    result = await asyncio.to_thread(_q)
    if result is None:
        raise HTTPException(404, f"No run {run_id}")
    return result
