"""The suggested channel setups the New channel page offers (suggested.py)."""
import asyncio

from fastapi import APIRouter

import collimate_server.suggested as suggested

router = APIRouter()


@router.get("/api/suggested")
async def get_suggested() -> dict:
    """The catalog of the content tree installed with the server (a
    checkout's own ``content/`` when it runs from one). Never touches the
    network."""
    return await asyncio.to_thread(suggested.catalog)
