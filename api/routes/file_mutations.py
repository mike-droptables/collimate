"""Conditional cell-file mutations and durable conflict discovery."""
import asyncio

from fastapi import APIRouter
from persistence import FileOperation, FileMove, mutate_files, move_files

router = APIRouter()


@router.post('/api/channels/{channel}/cells/{cell_id}/files/mutate')
async def mutate_cell_files(channel: str, cell_id: str, operation: FileOperation):
    return await asyncio.to_thread(mutate_files, channel, cell_id, operation)


@router.post("/api/channels/{channel}/files/move")
async def move_cell_files(channel: str, operation: FileMove):
    return await asyncio.to_thread(move_files, channel, operation)
