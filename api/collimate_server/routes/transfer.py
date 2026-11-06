"""Import and export routes: zip expansion, the import commit and channel export.

Single files stream into a channel's body store through ``POST
/api/channels/{channel}/blob/stream`` (routes/blobs.py); an import into an
existing cell goes through the conditional file-mutation route with those
blob hashes.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from collimate_server.transfer_export import Scope
from collimate_server.transfer_import import ImportRequest

router = APIRouter()


async def _expand_upload(request: Request, store) -> dict:
    from collimate_server.routes.blobs import consume_request_body
    from collimate_server.transfer_import import expand_zip, receive_zip
    path = await consume_request_body(request, receive_zip)
    try:
        return await asyncio.to_thread(expand_zip, path, store)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@router.post("/api/channels/{channel}/import/zip")
async def import_zip(channel: str, request: Request) -> dict:
    """Expand a raw zip body into the channel's body store, for an import
    preview; nothing is committed.

    The body is written to a temp file under the data dir as it arrives,
    expanded member by member and removed. Answers ``{entries, dirs,
    repositories, skipped}`` (``repositories``: each ``<dir>/.git`` set
    aside as ``{dir, token}``, never a file entry); 400 for a refused
    structure, 413 for a cap.
    """
    from collimate_server.cas import channel_store
    from collimate_server.state import require_channel
    return await _expand_upload(request, channel_store(require_channel(channel).id))


@router.post("/api/import/zip")
async def import_zip_for_new_channel(request: Request) -> dict:
    """Expand a raw zip body for the new-channel page, whose channel does not
    exist yet: into the staging store (``cas.staging_store``), from which the
    channel's creation copies the bodies it takes (``channel_sources``).
    Answers as :func:`import_zip`."""
    from collimate_server.cas import staging_store
    return await _expand_upload(request, staging_store())


@router.post("/api/channels/{channel}/import/repository")
async def import_repository(channel: str, request: Request) -> dict:
    """Set one repository's ``.git`` aside for an import into the channel to
    adopt: a ``multipart/form-data`` upload whose ``file`` is a zip of the
    ``.git`` with its members under ``<dir>/.git/``. Answers ``{dir,
    token}`` (``dir`` the folder that held it, ``""`` the root); an import
    cell names it by ``token`` in ``repositories``. 400 for anything but one
    ``.git`` (a gitfile included), 413 for a cap. Uploads left a day are
    swept."""
    from collimate_server.cas import channel_store, remove_stale_upload_dirs
    from collimate_server.routes.blobs import consume_request_body
    from collimate_server.state import require_channel
    from collimate_server.transfer_import import IMPORTS_DIR, expand_zip, receive_form_file
    store = channel_store(require_channel(channel).id)
    await asyncio.to_thread(remove_stale_upload_dirs, IMPORTS_DIR)
    content_type = request.headers.get("content-type", "")
    path = await consume_request_body(request, lambda chunks: receive_form_file(chunks, content_type))
    try:
        return await asyncio.to_thread(expand_zip, path, store, repository=True)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@router.post("/api/channels/{channel}/import")
def import_cells(channel: str, body: ImportRequest) -> dict:
    """Create new stacks and cells from uploaded blobs in one publication,
    each cell adopting its uploaded repositories (``repositories``:
    ``{repo_path, token}``).

    Answers ``{stack_ids, cell_ids, skipped}``, ``skipped`` the
    repositories git would not adopt (``{path: "<repo_path>/.git",
    reason}``; their files imported); 400 for an invalid, reserved or run
    path, an empty stack or a bad token, 409 for an unknown blob hash or an
    uploaded ``.git`` that is gone.
    """
    from collimate_server.transfer_import import commit_import
    return commit_import(channel, body)


def _freeze(channel: str, scope: Scope, cell_id: str | None, stack_id: str | None, tag: str | None,
            omit_git_history: bool):
    """Freeze the scope's cells, build its tree and stage its repositories'
    ``.git`` (``omit_git_history``: without reflogs and the stash); the
    caller releases the returned snapshot and the tree's staging."""
    import shutil
    from collimate_server.cell_placement import BUILTIN_REFUSAL, capabilities_stack_cells, find_cell
    from collimate_server.lifecycle_snapshots import export_snapshot, freeze_tag_cells
    from collimate_server.state import require_channel, state_lock
    from collimate_server.transfer_export import build_tree, make_staging, stage_repositories
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
        tree = build_tree(snapshot, scope, cell_id=cell_id, stack_id=stack_id, tag=tag)
    except LookupError as exc:
        snapshot.release()
        raise HTTPException(409, f"{exc} after the export froze; retry") from exc
    except BaseException:
        snapshot.release()
        raise
    if not tree.repositories and not tree.left_out:
        return snapshot, tree
    _token, staging = make_staging()
    try:
        return snapshot, stage_repositories(tree, snapshot, staging, omit_history=omit_git_history)
    except BaseException:
        snapshot.release()
        shutil.rmtree(staging, ignore_errors=True)
        raise


_OMIT_GIT_HISTORY = Query(False, description="Omit local git history (reflogs, stash)")


@router.get("/api/channels/{channel}/export/manifest")
def export_manifest(channel: str, scope: Scope, cell_id: str | None = None, stack_id: str | None = None,
                    tag: str | None = None, omit_git_history: bool = _OMIT_GIT_HISTORY) -> dict:
    """The folder tree of a frozen export: ``{root, entries, dirs, token,
    notes}``. A cell file's entry is ``{path, body_hash, size, encoding}``,
    its body fetched from ``GET /api/blob/{body_hash}``; a file the export
    staged (a repository's ``.git``) is ``{path, export_file, size}``,
    fetched from ``GET …/export/{token}/file?path=<export_file>`` until
    ``DELETE …/export/{token}`` or an hour has passed (``token`` is null
    when nothing was staged). ``notes`` say what the export left behind.
    ``scope`` is ``cell`` (with ``cell_id``), ``stack`` (with
    ``stack_id``), ``all``, ``packages`` (the capability cells), ``main``
    (the standard main stacks) or ``tag`` (with ``tag``: the live and
    removed cells carrying it; 404 when none does)."""
    import shutil
    from collimate_server.state import channel_db_name
    from collimate_server.transfer_export import STAGING_PREFIX, StagedEntry, export_manifest as manifest, hold_staging
    snapshot, tree = _freeze(channel, scope, cell_id, stack_id, tag, omit_git_history)
    try:
        token = None
        if tree.staging is not None:
            if any(isinstance(entry, StagedEntry) for entry in tree.entries):
                token = os.path.basename(tree.staging).removeprefix(STAGING_PREFIX)
                hold_staging(channel_db_name(snapshot.channel), token, tree.staging)
            else:
                shutil.rmtree(tree.staging, ignore_errors=True)
        return manifest(tree, token)
    finally:
        snapshot.release()


@router.get("/api/channels/{channel}/export/{token}/file")
def export_file(channel: str, token: str, path: str = Query(...)):
    """One file a folder export staged (its manifest entry's
    ``export_file``); 404 for an unknown or released token or name."""
    from fastapi.responses import FileResponse
    from collimate_server.state import channel_db_name, require_channel
    from collimate_server.transfer_export import staged_file
    try:
        found = staged_file(channel_db_name(require_channel(channel)), token, path)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(found, media_type="application/octet-stream")


@router.delete("/api/channels/{channel}/export/{token}", status_code=204)
def release_export(channel: str, token: str) -> Response:
    """Release a folder export's staged files once they are written; 204,
    also for a token already released or swept."""
    from collimate_server.state import channel_db_name, require_channel
    from collimate_server.transfer_export import release_staging, staging_channel
    if staging_channel(token) == channel_db_name(require_channel(channel)):
        release_staging(token)
    return Response(status_code=204)


def _download_name(scope: Scope, channel_name: str, tree_root: str) -> str:
    """The zip's file name (without ``.zip``): the tree's root folder for
    ``cell``, ``stack`` and ``all``; for the scopes the New channel page
    exports, the channel and what was exported, so two exports of one
    channel never share a name: ``<channel> packages``, ``<channel> main
    cells`` and ``<channel> <tag> cells``."""
    from collimate_server.transfer_export import safe_name
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
                     tag: str | None = None, omit_git_history: bool = _OMIT_GIT_HISTORY):
    """Stream a frozen export as a zip; the same tree as the manifest, named
    by :func:`_download_name`, each repository's staged ``.git`` written
    into it and its notes in the cell's provenance file."""
    from collimate_server.transfer_export import ZipStream
    snapshot, tree = await asyncio.to_thread(_freeze, channel, scope, cell_id, stack_id, tag, omit_git_history)
    return _ZipResponse(ZipStream(tree, snapshot), _download_name(scope, snapshot.channel.name, tree.root))
