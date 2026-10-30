"""Per-cell panel sessions — the ``pick_sessions`` row of a channel DB's
``ui_state`` table.

A cell panel (kind ``runners``, ``files`` or ``handlers``) renders INSIDE a
cell, over its body, one session per cell, so an open panel and its search
query have to outlive a component unmount (opening the overview tears the
whole CellEditor subtree down) AND a browser refresh, on any machine that opens the
channel.

Where this state deliberately does NOT live:

- **not in ``.coll*`` bundles** — a half-filled panel is per-user scratch, not
  channel content, so ``channel_settings`` never gains a ``pick_sessions``
  section and no exporter touches this row.
- **not in a cell's history** — checkpoints contain accepted file revisions;
  panel sessions remain separate UI state.
- **not in localStorage** — it is channel state, not browser state.

Shape: ``{cellId: sessionObject}``, keyed by the STABLE canvas cell id (the id
a run's ``cell_id`` uses), not a version-row id. The session object
itself is stored VERBATIM — the frontend owns its schema (``PickSession`` in
``frontend/src/lib/types.ts``); the backend only enforces "a JSON object, not
absurdly large" so a schema change doesn't need a matching backend release.

Entries for cells that no longer exist are pruned on read (§``list_pick_sessions``)
rather than by a background sweep: cells vanish through many paths (stack
removal, channel import, history restore) and a read-time prune is the one
place guaranteed to run before the map is ever used.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Body, HTTPException

import channel_config
import state
from channel_config import PICK_SESSIONS_KEY

router = APIRouter()

# Per-session cap on the serialized JSON. A pick session is a search query
# and a timestamp, so 4 KiB is generous headroom; the cap keeps a runaway
# client from bloating a row that is read whole on every channel hydration.
# Rejected with 400, not 413: it is a shape error the client can see and
# fix, and the global body-size middleware's 413 means something else
# entirely (a 64 MB request).
MAX_SESSION_BYTES = 4 * 1024


def _db_name(channel: str) -> str:
    """Resolve a live registered UUID before any panel-state database access."""
    return state.channel_db_name(state.require_channel(channel))


def _read_map(db_name: str) -> dict:
    """The stored ``{cellId: session}`` map, defensively normalized — a
    hand-edited or half-written row must not break channel hydration."""
    data = channel_config.read_ui_state_json(db_name, PICK_SESSIONS_KEY, {})
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, dict)
    }


def _live_cell_ids(channel: str) -> set[str] | None:
    """Every cell id currently on the channel's canvas (main + nav stacks), or
    None if a previously admitted channel has been removed before publication."""
    ch = state.get_channel_by_id(channel)
    if ch is None:
        return None
    return {
        str(c.id)
        for stk in ch.main_stacks + ch.nav_stacks
        for c in stk.cells
    }


@router.get("/api/channels/{channel}/pick_sessions")
async def list_pick_sessions(channel: str):
    """The channel's ``{cellId: session}`` map, pruned of entries whose cell is
    gone. The pruned map is persisted when the prune removed anything, so a
    deleted cell's session doesn't get re-read (and re-pruned) forever."""

    def _work():
        db = _db_name(channel)
        sessions = _read_map(db)
        live = _live_cell_ids(channel)
        if live is None or not sessions:
            # Do not prune against a canvas that disappeared after admission.
            return sessions
        kept = {k: v for k, v in sessions.items() if k in live}
        if len(kept) == len(sessions):
            return kept
        cfg = channel_config
        with cfg.ui_state_merge_lock():
            # Re-read under the lock: a PUT that landed between our read and
            # the lock must survive the prune (it is by definition a session
            # for a cell the client is looking at right now). The live-cell
            # set is ALSO recomputed here — a cell created in that same
            # window would otherwise have its just-saved session filtered
            # against the stale snapshot.
            current = _read_map(db)
            live_now = _live_cell_ids(channel)
            if live_now is None:
                return current
            merged = {k: v for k, v in current.items() if k in live_now}
            cfg.write_ui_state_json(db, PICK_SESSIONS_KEY, merged)
            return merged

    return await asyncio.to_thread(_work)


@router.put("/api/channels/{channel}/pick_sessions/{cell_id}")
async def save_pick_session(channel: str, cell_id: str, body: Any = Body(None)):
    """Upsert one cell's session. The body IS the session object, stored
    verbatim (the frontend owns its schema).

    Untyped-and-optional ``Body`` on purpose: a ``dict`` annotation would make
    FastAPI answer 422 for a non-object body, and a required ``Body(...)``
    would answer 422 for a literal ``null`` / absent body. This contract
    promises one 400 for every "that isn't a session object" case."""
    if not isinstance(body, dict):
        raise HTTPException(400, "pick session body must be a JSON object")
    try:
        # allow_nan=False: Python's json would happily store NaN/Infinity,
        # which is not JSON — every later GET of the whole map would then
        # fail to serialize, permanently breaking channel hydration.
        size = len(json.dumps(body, allow_nan=False).encode("utf-8"))
    except ValueError:
        raise HTTPException(400, "pick session body must be strict JSON (no NaN/Infinity)")
    if size > MAX_SESSION_BYTES:
        raise HTTPException(
            400,
            f"pick session too large ({size} bytes; limit {MAX_SESSION_BYTES})",
        )

    def _work():
        db = _db_name(channel)
        cfg = channel_config
        with cfg.ui_state_merge_lock():
            sessions = _read_map(db)
            sessions[cell_id] = body
            cfg.write_ui_state_json(db, PICK_SESSIONS_KEY, sessions)
        return {"ok": True}

    return await asyncio.to_thread(_work)


@router.delete("/api/channels/{channel}/pick_sessions/{cell_id}")
async def delete_pick_session(channel: str, cell_id: str):
    """Drop one cell's session. Idempotent — closing a panel that was never
    saved (or was already pruned) is a 200, never a 404, so the client's close
    path has nothing to special-case."""

    def _work():
        db = _db_name(channel)
        cfg = channel_config
        with cfg.ui_state_merge_lock():
            sessions = _read_map(db)
            existed = cell_id in sessions
            if existed:
                sessions.pop(cell_id, None)
                cfg.write_ui_state_json(db, PICK_SESSIONS_KEY, sessions)
        return {"ok": True, "deleted": existed}

    return await asyncio.to_thread(_work)
