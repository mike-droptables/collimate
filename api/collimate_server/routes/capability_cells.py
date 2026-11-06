"""Routes over one Capabilities cell as a whole: where it goes and where it
came from.

- ``POST …/capabilities/cells/{cell_id}/copy/review`` and ``…/copy``: Copy
  to channel (``cell_copy``);
- ``POST …/capabilities/cells/{cell_id}/publish/review`` and ``…/publish``:
  Publish to catalog (``user_catalog``);
- ``GET …/capabilities/updates``: what each cell follows and whether its
  upstream changed; ``POST …/capabilities/cells/{cell_id}/replace/review``
  and ``…/replace``: Review and replace (``definition_updates``).
"""
from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class CopyReviewRequest(BaseModel):
    """The channel a Capabilities cell is copied to."""
    model_config = ConfigDict(extra="forbid")
    target_channel_id: str = Field(min_length=1)


class CopyRequest(CopyReviewRequest):
    """The copy, as its review saw it (``token``)."""
    token: str = Field(min_length=1)


class Reviewed(BaseModel):
    """An action as its review saw it (``token``)."""
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1)


def _channels(channel: str, target: str):
    """The source and the target; an archived target refuses the copy (the
    source may be archived: a copy reads it)."""
    from collimate_server.channel_guard import require_writable
    from collimate_server.state import require_channel
    source, destination = require_channel(channel), require_channel(target)
    require_writable(destination.id)
    return source, destination


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/copy/review")
async def review_copy(channel: str, cell_id: UUID, body: CopyReviewRequest) -> dict:
    """What Copy to channel would land on the target: ``{source, target,
    cells: [{name, brought, packages}], libraries: [{id, resolves, cell}],
    skipped, token}`` (``cell_copy.plan_copy``). 404 for a cell that is not
    on Capabilities or an unknown channel; 400 for a refusal (a library that
    resolves nowhere, a definition admission refuses, the same channel); 409
    when the target already holds every package of the cell."""
    from collimate_server.cell_copy import plan_copy
    from collimate_server.channel_sources import admission_errors

    def _body() -> dict:
        source, target = _channels(channel, body.target_channel_id)
        with admission_errors():
            return plan_copy(source, str(cell_id), target).review

    return await asyncio.to_thread(_body)


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/copy")
async def copy_to_channel(channel: str, cell_id: UUID, body: CopyRequest) -> dict:
    """Copy the cell onto the target channel's Capabilities as its review
    showed (409 when the source changed since): ``{cell_id, cell_ids,
    package_skips}``."""
    from collimate_server.cell_copy import copy_cell
    from collimate_server.channel_sources import admission_errors
    from collimate_server.state import flush_persist_state

    def _body() -> dict:
        source, target = _channels(channel, body.target_channel_id)
        with admission_errors():
            return copy_cell(source, str(cell_id), target, body.token)

    result = await asyncio.to_thread(_body)
    await asyncio.to_thread(flush_persist_state)
    return result


@router.get("/api/channels/{channel}/capabilities/updates")
async def capability_updates(channel: str) -> dict:
    """What each Capabilities cell follows: ``{cells: [{cell_id, upstream:
    {kind, id, label}, status, edited}]}``, ``status`` one of ``current``,
    ``update`` (Update available), ``missing`` or ``mixed``; a cell that
    follows nothing is not listed."""
    from collimate_server.definition_updates import updates
    from collimate_server.state import require_channel
    return {"cells": await asyncio.to_thread(lambda: updates(require_channel(channel)))}


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/replace/review")
async def review_replace(channel: str, cell_id: UUID) -> dict:
    """What Review and replace makes of the cell: ``{cell, upstream, edited,
    packages: [package + change], runners, libraries, named_by, token}``.
    404 for a cell not on Capabilities; 409 for one with no update or whose
    replacement admission holds a package; 400 for a refusal."""
    from collimate_server.channel_sources import admission_errors
    from collimate_server.definition_updates import plan_replace
    from collimate_server.state import require_channel

    def _body() -> dict:
        with admission_errors():
            return plan_replace(require_channel(channel), str(cell_id)).review

    return await asyncio.to_thread(_body)


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/replace")
async def replace_from_upstream(channel: str, cell_id: UUID, body: Reviewed) -> dict:
    """Replace the cell with its upstream's copy as reviewed, keeping its
    runners (409 when the cell or its upstream changed since): ``{cell_id,
    version_id}``."""
    from collimate_server.channel_sources import admission_errors
    from collimate_server.definition_updates import replace
    from collimate_server.state import flush_persist_state, require_channel

    def _body() -> dict:
        with admission_errors():
            return replace(require_channel(channel), str(cell_id), body.token)

    result = await asyncio.to_thread(_body)
    await asyncio.to_thread(flush_persist_state)
    return result


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/publish/review")
async def review_publish(channel: str, cell_id: UUID) -> dict:
    """What Publish to catalog writes: ``{name, entry, unchanged, packages,
    libraries: [{id, resolves, entry, cell}], token}`` (``entry`` the entry
    it republishes, or null for a new one). 404 for a cell not on
    Capabilities, 400 for one with no definition."""
    from collimate_server.state import require_channel
    from collimate_server.user_catalog import plan_publish
    return await asyncio.to_thread(lambda: plan_publish(require_channel(channel), str(cell_id)).review)


@router.post("/api/channels/{channel}/capabilities/cells/{cell_id}/publish")
async def publish_to_catalog(channel: str, cell_id: UUID, body: Reviewed) -> dict:
    """Publish the cell to the user catalog as reviewed (409 when it or its
    entry changed since, or the entry already holds it): the entry."""
    from collimate_server.state import require_channel
    from collimate_server.user_catalog import publish
    return await asyncio.to_thread(lambda: publish(require_channel(channel), str(cell_id), body.token))
