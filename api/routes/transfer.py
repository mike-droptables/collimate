"""Import and export routes: zip expansion, the import commit and channel export.

Single files stream into the CAS through ``POST /api/blob/stream``
(routes/blobs.py); an import into an existing cell goes through the
conditional file-mutation route with those blob hashes.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from transfer_export import Scope
from transfer_import import ImportRequest

router = APIRouter()


@router.post("/api/import/zip")
async def import_zip(request: Request) -> dict:
    """Expand a raw zip body into the CAS, for an import preview or a new
    channel's sources; nothing is committed and no channel is involved.

    The body is written to a temp file under the data dir as it arrives,
    expanded member by member and removed. Answers ``{entries, dirs,
    skipped}``; 400 for a refused structure, 413 for a cap.
    """
    from routes.blobs import consume_request_body
    from transfer_import import expand_zip, receive_zip
    path = await consume_request_body(request, receive_zip)
    try:
        return await asyncio.to_thread(expand_zip, path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@router.post("/api/channels/{channel}/import")
def import_cells(channel: str, body: ImportRequest) -> dict:
    """Create new stacks and cells from uploaded blobs in one publication.

    Answers ``{stack_ids, cell_ids}``; 400 for an invalid, reserved or run
    path or an empty stack, 409 for an unknown blob hash.
    """
    from transfer_import import commit_import
    return commit_import(channel, body)


def _freeze(channel: str, scope: Scope, cell_id: str | None, stack_id: str | None, tag: str | None):
    """Freeze the scope's cells and build its tree; the caller releases the
    returned snapshot."""
    from cell_placement import BUILTIN_REFUSAL, capabilities_stack_cells, find_cell
    from lifecycle_snapshots import export_snapshot, freeze_tag_cells
    from state import require_channel, state_lock
    from transfer_export import build_tree
    ch = require_channel(channel)
    if scope == "tag" and not tag:
        raise HTTPException(400, "tag is required for scope=tag")
    with state_lock:
        if scope == "cell":
            if not cell_id:
                raise HTTPException(400, "cell_id is required for scope=cell")
            cell = find_cell(ch, cell_id)[1]
            if cell is None:
                raise HTTPException(404, "Cell not found")
            if cell.builtin:
                raise HTTPException(400, BUILTIN_REFUSAL)
            cell_ids: list[str] | None = [cell_id]
        elif scope == "stack":
            if not stack_id:
                raise HTTPException(400, "stack_id is required for scope=stack")
            stack = next((s for s in ch.main_stacks + ch.nav_stacks if str(s.id) == stack_id), None)
            if stack is None:
                raise HTTPException(404, "Stack not found")
            cell_ids = [str(c.id) for c in stack.cells if not c.builtin]
        elif scope == "packages":
            cell_ids = [str(c.id) for c in capabilities_stack_cells(ch)]
        elif scope == "main":
            cell_ids = [str(c.id) for s in ch.main_stacks if s.stack_type == "standard" for c in s.cells]
        else:
            cell_ids = None
    try:
        snapshot = freeze_tag_cells(ch, tag or "") if scope == "tag" else export_snapshot(ch, cell_ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, "Stored content is missing or corrupt") from exc
    if scope == "tag" and not snapshot.files_by_cell and not snapshot.removed:
        snapshot.release()
        raise HTTPException(404, "No cell carries this tag")
    try:
        return snapshot, build_tree(snapshot, scope, cell_id=cell_id, stack_id=stack_id, tag=tag)
    except LookupError as exc:
        snapshot.release()
        raise HTTPException(409, f"{exc} after the export froze; retry") from exc
    except BaseException:
        snapshot.release()
        raise


@router.get("/api/channels/{channel}/export/manifest")
def export_manifest(channel: str, scope: Scope, cell_id: str | None = None, stack_id: str | None = None,
                    tag: str | None = None) -> dict:
    """The folder tree of a frozen export: ``{root, entries: [{path,
    body_hash, size, encoding}], dirs}``. Each body is then fetched from
    ``GET /api/blob/{body_hash}``. ``scope`` is ``cell`` (with ``cell_id``),
    ``stack`` (with ``stack_id``), ``all``, ``packages`` (the capability
    cells), ``main`` (the standard main stacks) or ``tag`` (with
    ``tag``: the live and removed cells carrying it; 404 when none does)."""
    from transfer_export import export_manifest as manifest
    snapshot, tree = _freeze(channel, scope, cell_id, stack_id, tag)
    try:
        return manifest(tree)
    finally:
        snapshot.release()


def _download_name(scope: Scope, channel_name: str, tree_root: str) -> str:
    """The zip's file name (without ``.zip``): the tree's root folder for
    ``cell``, ``stack`` and ``all``; for the scopes the New channel page
    exports, the channel and what was exported, so two exports of one
    channel never share a name: ``<channel> packages``, ``<channel> main
    cells`` and ``<channel> <tag> cells``."""
    from transfer_export import safe_name
    if scope == "packages":
        return f"{tree_root} packages"
    if scope == "main":
        return f"{tree_root} main cells"
    if scope == "tag":
        return f"{safe_name(channel_name, 'Channel')} {tree_root} cells"
    return tree_root


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f'attachment; filename="{ascii_name}.zip"; filename*=UTF-8\'\'{quote(name + ".zip")}'


class _ZipResponse(StreamingResponse):
    """Closes its zip stream however the response ends, even when the body
    was never iterated (the client left before the headers went out)."""

    def __init__(self, stream, name: str):
        async def body():
            while (piece := await asyncio.to_thread(stream.next_chunk)) is not None:
                yield piece
        super().__init__(body(), media_type="application/zip",
                         headers={"Content-Disposition": _content_disposition(name)})
        self._stream = stream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stream.close()


@router.get("/api/channels/{channel}/export.zip")
async def export_zip(channel: str, scope: Scope, cell_id: str | None = None, stack_id: str | None = None,
                     tag: str | None = None):
    """Stream a frozen export as a zip; the same tree as the manifest, named
    by :func:`_download_name`."""
    from transfer_export import ZipStream
    snapshot, tree = await asyncio.to_thread(_freeze, channel, scope, cell_id, stack_id, tag)
    return _ZipResponse(ZipStream(tree, snapshot), _download_name(scope, snapshot.channel.name, tree.root))
