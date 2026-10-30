"""UUID-scoped, last-committed-writer display records for live cells."""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from cell_display import CellDisplay, KEY_PREFIX, read_display, validate_display, write_display
from cell_placement import iter_stacks
from channel_db import open_channel_db
from state import require_channel

router = APIRouter()


def _live_ids(channel: str) -> set[str]:
    ch = require_channel(str(channel))
    return {str(cell.id) for stack in iter_stacks(ch) for cell in stack.cells}


def _require_live(conn, channel: str, cell_id: UUID) -> None:
    # Removal's row and display transfer commit together. This SQL check
    # rejects a late PUT even before the short in-memory publication finishes.
    # Never acquire state_lock while holding this channel writer.
    if conn.execute("SELECT 1 FROM removed_cells WHERE cell_id=?", (str(cell_id),)).fetchone():
        raise HTTPException(409, "Cell is removed; display cannot be changed")
    if str(cell_id) not in _live_ids(channel):
        raise HTTPException(404, "Cell is not live in this channel")


@router.get("/api/channels/{channel}/cell_display")
async def list_cell_display(channel: str) -> dict:
    def work():
        live = _live_ids(channel)
        conn = open_channel_db(str(channel))
        try:
            removed = {row[0] for row in conn.execute("SELECT cell_id FROM removed_cells")}
            ids = [row[0][len(KEY_PREFIX):] for row in conn.execute(
                "SELECT key FROM ui_state WHERE substr(key,1,?)=?", (len(KEY_PREFIX), KEY_PREFIX))]
            try:
                return {cell_id: read_display(conn, cell_id) for cell_id in ids
                        if cell_id in live and cell_id not in removed}
            except (ValueError, ValidationError) as exc:
                raise HTTPException(409, "Stored cell display is invalid") from exc
        finally:
            conn.close()
    return await asyncio.to_thread(work)


@router.put("/api/channels/{channel}/cell_display/{cell_id}")
async def put_cell_display(channel: str, cell_id: UUID, body: CellDisplay) -> dict:
    try:
        display = validate_display(body.model_dump(mode="json", exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    def work():
        require_channel(str(channel))
        conn = open_channel_db(str(channel))
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_live(conn, channel, cell_id)
            write_display(conn, str(cell_id), display)
            conn.commit()
            return {"ok": True, "cell_id": str(cell_id)}
        finally:
            conn.close()
    return await asyncio.to_thread(work)


@router.delete("/api/channels/{channel}/cell_display/{cell_id}")
async def delete_cell_display(channel: str, cell_id: UUID) -> dict:
    def work():
        require_channel(str(channel))
        conn = open_channel_db(str(channel))
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_live(conn, channel, cell_id)
            deleted = conn.execute("DELETE FROM ui_state WHERE key=?", (KEY_PREFIX + str(cell_id),)).rowcount == 1
            conn.commit()
            return {"ok": True, "cell_id": str(cell_id), "deleted": deleted}
        finally:
            conn.close()
    return await asyncio.to_thread(work)
