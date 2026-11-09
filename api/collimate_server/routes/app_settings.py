"""The server-wide settings routes (``app_settings``): the document, and the
theme pictures it names.

The routes name no channel, so the channel guard passes them, and editing
the library from an archived channel works. A picture is a body in the
installation's store (``cas.app_store()``); the image routes are the
channel blob routes (``routes/blobs.py``) pointed at that store. The GET
serves any body of the store, not only the pictures the library names: the
editor shows an upload before its debounced save names it, and the store
holds nothing a client may not read (renderer bundles, handler builds, the
module mirror).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException, Request

from collimate_server.cas import app_store
from collimate_server.routes.blobs import serve_cas_blob, stream_upload

router = APIRouter()


@router.get("/api/app-settings")
async def get_app_settings() -> dict:
    """The server's settings document ``{version, kind: "app-settings",
    theme: {defaultThemeId, userThemes}}``; the defaults while none is
    stored."""
    from collimate_server.app_settings import load_app_settings
    return await asyncio.to_thread(load_app_settings)


@router.put("/api/app-settings")
async def put_app_settings(partial: dict = Body(...)) -> dict:
    """Replace each provided section wholesale (``{theme?}``); answers the
    stored document. 400 for an unknown section, a section that is not an
    object, or another ``kind``; 422 for a body that is not an object. Every
    channel stream then hears ``app_settings_changed``."""
    from collimate_server.app_settings import merge_app_settings
    from collimate_server.routes.system import broadcast_app_settings_changed
    try:
        doc = await asyncio.to_thread(merge_app_settings, partial)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    broadcast_app_settings_changed()
    return doc


@router.post("/api/app-settings/images")
async def upload_app_settings_image(request: Request) -> dict:
    """Stream a raw request body (a theme's scene picture) into the
    installation's store; answers ``{blob_hash, size, encoding}``. Written to
    disk as it arrives (the route is exempt from the buffered-body cap) and
    capped at ``IMPORT_MAX_FILE_BYTES`` (413); 507 when it cannot be stored."""
    return await stream_upload(app_store(), request)


@router.get("/api/app-settings/images/{blob_hash}")
async def get_app_settings_image(blob_hash: str, request: Request):
    """Serve a body of the installation's store by its SHA-256 hex digest:
    400 for a malformed hash, 404 for one the store does not hold."""
    return await serve_cas_blob(request, blob_hash, app_store, missing="Image not found")
