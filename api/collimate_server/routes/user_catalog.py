"""The user catalog's routes (``user_catalog``): its entries as a channel
adds them, removing one, and the saved suggestions.

Publishing a cell is a route of that cell (``routes/capability_cells``).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from collimate_server.user_catalog import SuggestionBody

router = APIRouter()


def _admitting(body):
    from collimate_server.channel_sources import admission_errors
    with admission_errors():
        return body()


@router.get("/api/channels/{channel}/capabilities/catalog/entries")
async def catalog_entries(channel: str) -> dict:
    """The user catalog for the channel's Add from catalog: ``{entries: [{id,
    name, section, libraries, packages, revision, published_at, app_version,
    held}]}``; ``held`` exactly when the add answers 409 'Already in this
    channel'."""
    from collimate_server.state import require_channel
    from collimate_server.user_catalog import catalog_listing
    return await asyncio.to_thread(lambda: _admitting(lambda: catalog_listing(require_channel(channel))))


@router.post("/api/channels/{channel}/capabilities/catalog/entries/{entry_id}/add")
async def add_catalog_entry(channel: str, entry_id: str) -> dict:
    """Add the entry (and each library entry it needs) to the end of
    Capabilities, following the entry. 404 for an unknown entry, 409
    'Already in this channel' or 'Nothing to add: …', 400 for a library
    that resolves nowhere. Answers the layout with ``{cell_id, cell_ids,
    package_skips}``."""
    from collimate_server.routes.capabilities_stack import _with_layout
    from collimate_server.state import flush_persist_state, require_channel
    from collimate_server.user_catalog import add_entry

    def _body() -> dict:
        return _with_layout(channel, _admitting(lambda: add_entry(require_channel(channel), entry_id)))

    result = await asyncio.to_thread(_body)
    await asyncio.to_thread(flush_persist_state)
    return result


@router.delete("/api/catalog/entries/{entry_id}")
async def delete_catalog_entry(entry_id: str) -> dict:
    """Take an entry out of the catalog (404 unknown, 409 while a saved
    suggestion lists it); cells added from it stay where they are."""
    from collimate_server.user_catalog import remove_entry
    await asyncio.to_thread(remove_entry, entry_id)
    return {"ok": True}


@router.get("/api/catalog/suggestions")
async def saved_suggestions() -> dict:
    """The saved suggestions as the New channel page reads a suggestion:
    ``{suggestions: [{id, name, description, saved, items, generators,
    renderers, libraries, tutorial, runner_generators, runners, problem}]}``."""
    from collimate_server.user_catalog import offered_suggestions
    return {"suggestions": await asyncio.to_thread(offered_suggestions)}


@router.post("/api/catalog/suggestions")
async def create_saved_suggestion(body: SuggestionBody) -> dict:
    """Save a suggestion of catalog cells and runner drafts; answers it with
    ``added`` (the library entries listed because its entries need them).
    400 for a cell or runner it cannot hold."""
    from collimate_server.user_catalog import save_suggestion
    return await asyncio.to_thread(lambda: _admitting(lambda: save_suggestion(body)))


@router.put("/api/catalog/suggestions/{suggestion_id}")
async def replace_saved_suggestion(suggestion_id: str, body: SuggestionBody) -> dict:
    """Replace a saved suggestion (404 unknown); as the create."""
    from collimate_server.user_catalog import save_suggestion
    return await asyncio.to_thread(lambda: _admitting(lambda: save_suggestion(body, suggestion_id)))


@router.delete("/api/catalog/suggestions/{suggestion_id}")
async def delete_saved_suggestion(suggestion_id: str) -> dict:
    """Delete a saved suggestion (404 unknown)."""
    from collimate_server.user_catalog import delete_suggestion
    await asyncio.to_thread(delete_suggestion, suggestion_id)
    return {"ok": True}
