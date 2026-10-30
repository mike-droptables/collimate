"""The suggested channel setups the New channel page offers (suggested.py)."""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

import suggested

router = APIRouter()


class SuggestedSource(BaseModel):
    """A local content folder (holding suggested/) to read suggestions from; None drops the folder."""
    model_config = ConfigDict(extra="forbid")
    path: str | None


@router.get("/api/suggested")
async def get_suggested(refresh: bool = False) -> dict:
    """The catalog, from the folder override, else the latest release's
    content fetched by the last refresh, else the content installed with the
    app. ``refresh`` fetches the latest release's content first (no effect
    on a folder); a failed fetch keeps the source it had beside the error.
    Only a refresh touches the network."""
    return await asyncio.to_thread(suggested.catalog, refresh=refresh)


@router.put("/api/suggested/source")
async def put_suggested_source(body: SuggestedSource) -> dict:
    """Read suggestions from ``path`` (a folder holding ``suggested/``), or
    for None stop reading a folder; answers the new catalog. A folder that
    is not one, or whose suggestions cannot be read, is a 400 and changes
    nothing."""
    try:
        return await asyncio.to_thread(suggested.set_source, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
