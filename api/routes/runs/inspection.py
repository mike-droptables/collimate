"""Cell / file inspection endpoints."""

import asyncio

from fastapi import APIRouter

from channel_db import open_channel_db

router = APIRouter()


@router.get("/api/channels/{channel}/cells/{cell_id}/files")
async def list_cell_files(channel: str, cell_id: str):
    """Return current descriptors, including tombstone revisions for safe recreation."""
    def read():
        from persistence import snapshot_files
        conn=open_channel_db(channel)
        try:
            return {'files':snapshot_files(conn,cell_id,include_deleted=True)}
        finally:
            conn.close()
    return await asyncio.to_thread(read)
