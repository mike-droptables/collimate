"""The window server's storage routes (``/api/storage/*``), behind the
New / manage channels page (plan 1105)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import collimate_server.env_uses as env_uses
import collimate_server.storage_survey as storage_survey

router = APIRouter()


# -- overview and maintenance --

def _server() -> dict:
    import os
    import collimate_server.channel_paths as channel_paths
    from collimate_server.app_version import app_version
    from collimate_server.instance import instance_id
    return {"data_dir": os.path.abspath(os.getcwd()), "instance": instance_id(),
            "installation_id": channel_paths.installation_id(), "version": app_version()}


@router.get("/api/storage")
async def storage_overview() -> dict:
    """What this server stores and where, from the survey's cache:
    ``{server: {data_dir, instance, installation_id, version}, survey:
    {state, computed_at, started_at?, error?}, totals: {bytes}, locations:
    [{id, path, is_default, state, owner_data_dir?, channel_count, bytes,
    other_bytes, free_bytes, total_bytes, channels: [{id, name, archived,
    bytes, files, parts, computed_at}]}], app_db, app_store, staged_store,
    envs, scratch: {tmp_bytes, tmp_live_bytes, pycache_bytes, live_runs},
    other, uv_cache, adapters, docker, previous_layout?,
    last_collection_at}``. Sizes are allocated bytes, each entry with its
    ``computed_at``; a part not measured yet is absent. With nothing cached
    yet a survey starts (``survey.state`` running). ``totals`` counts the
    locations and the data dir, not uv's cache, the adapters or Docker,
    which every server on the machine shares."""
    if await asyncio.to_thread(lambda: storage_survey.cached()["computed_at"] is None):
        storage_survey.request_survey()

    def read() -> dict:
        # The state first: an idle survey's sizes are stored before it is idle.
        survey = storage_survey.status()
        return {"server": _server(), "survey": survey, **storage_survey.overview()}
    return await asyncio.to_thread(read)


@router.post("/api/storage/refresh", status_code=202)
async def refresh_storage() -> dict:
    """Start a survey of every size, or join the one running: ``{survey}``."""
    storage_survey.request_survey()
    return {"survey": await asyncio.to_thread(storage_survey.status)}


@router.post("/api/storage/scratch/clear")
async def clear_scratch() -> dict:
    """Clear scratch: the run workspaces their receipts prove abandoned and
    the other leftovers in ``.collimate/tmp``, and ``.collimate/pycache``:
    ``{bytes_freed, removed, kept: [{name, reason}]}``. 409 with the reason
    while a run is starting, live or finishing, or an export is in
    progress."""
    import collimate_server.scratch as scratch
    reason = scratch.busy_reason()
    if reason is not None:
        raise HTTPException(409, reason)
    result = await asyncio.to_thread(scratch.clear)
    storage_survey.update_later(parts=("scratch",))
    return result


@router.post("/api/storage/collect")
async def collect_now() -> dict:
    """Collect every body store now, then the git stores: unreferenced
    bodies past the one-day publication floor are removed at once (no trash
    grace). ``{channels: [{id, swept, purged_sweeps, freed_bytes, ...}],
    app_store, staging, git, freed_bytes, last_collection_at}``."""
    import collimate_server.cas_gc as cas_gc
    result = await cas_gc.collect_now()
    storage_survey.update_later(channels=[outcome["id"] for outcome in result["channels"]],
                                parts=("app_store", "staged_store"))
    return result


# -- docker --

class DockerPreviewRequest(BaseModel):
    mode: Literal["all", "unused"]
    #: For ``unused``: the images with no recorded use in this many days.
    days: int | None = Field(default=None, ge=0)


class DockerDeleteRequest(BaseModel):
    preview_id: str


def _docker_client():
    from collimate_server.docker_inventory import DockerUnavailable, docker_client
    try:
        return docker_client()
    except DockerUnavailable as exc:
        raise HTTPException(503, f"Docker is not reachable: {exc}") from None


@router.get("/api/storage/docker")
async def docker_images() -> dict:
    """The Collimate-built images (repository ``collimate-*``) on the
    machine's Docker daemon: ``{available, images: [{id, refs, other_refs,
    size, shared_size, unique_size, containers, created_at, last_used_at?}],
    build_cache_bytes}``, or ``{available: false, reason}``. ``last_used_at``
    is this server's last recorded use; the daemon is machine-wide, so
    another server's images show as unused here, and deleting one makes
    that server rebuild it. The build cache is reported only."""
    from collimate_server.docker_inventory import listing
    return await asyncio.to_thread(listing)


@router.post("/api/storage/docker/preview")
async def docker_preview(body: DockerPreviewRequest) -> dict:
    """Select ``all`` candidates, or those ``unused`` in the last ``days``
    (never one with a container or a tag outside ``collimate-*``):
    ``{preview_id, count, bytes, exact, expires_at, images}``. ``bytes`` is
    exact when ``exact``, else a lower bound. The preview may be deleted
    from once, within ten minutes. 503 when Docker is not reachable."""
    from collimate_server.docker_inventory import make_preview
    if body.mode == "unused" and body.days is None:
        raise HTTPException(400, "Say how many days: unused selects the images not used in that many days")

    def _preview() -> dict:
        client = _docker_client()
        try:
            return make_preview(client, body.mode, body.days, datetime.now(timezone.utc)).wire()
        finally:
            client.close()
    return await asyncio.to_thread(_preview)


@router.post("/api/storage/docker/delete")
async def docker_delete(body: DockerDeleteRequest) -> dict:
    """Delete a preview's images, each re-checked first (still no
    container, no foreign tag, not used since the preview, no run preparing
    it), by removing its ``collimate-*`` tags without force: ``{removed:
    [ref], failed: [{ref, reason}], bytes_freed_estimate, exact}``. An image
    another server uses is rebuilt there on its next use. 404 for an unknown,
    used or expired preview; 503 when Docker is not reachable."""
    from collimate_server.docker_inventory import delete, take_preview

    def _delete() -> dict:
        client = _docker_client()
        try:
            preview = take_preview(body.preview_id)
            if preview is None:
                raise HTTPException(404, "This preview has expired or was used; preview again")
            return delete(client, preview, datetime.now(timezone.utc))
        finally:
            client.close()
    result = await asyncio.to_thread(_delete)
    storage_survey.update_later(parts=("docker",))
    return result


# -- run environments --

class EnvsPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "unused"]
    #: With ``unused``: a key last used more than this many days ago.
    days: int | None = Field(default=None, ge=0)


class EnvsDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str


def runs_live() -> bool:
    """Whether any run on this server is starting, live or finishing: a
    registered run (until its run-end cleanup is done), a start holding a
    cell, or a channel a start or stop has marked busy."""
    from collimate_server.run_registry import active_managers, busy_channels, held_cell_ids
    return bool(active_managers) or bool(busy_channels()) or bool(held_cell_ids())


@router.get("/api/storage/envs")
async def list_envs():
    """Every run environment this server recorded: its key, generator,
    stable tree, uv environment once known, allocated ``bytes``,
    ``reclaimable_bytes`` (what deleting it frees), last use and whether a
    run uses it now."""
    return {"envs": await asyncio.to_thread(env_uses.listing)}


@router.post("/api/storage/envs/preview")
async def preview_envs(body: EnvsPreviewRequest):
    """What a deletion would remove now (``all``, or ``unused`` for
    ``days``; live keys never), under a ``preview_id`` valid ten minutes."""
    if body.mode == "unused" and body.days is None:
        raise HTTPException(400, "Say how many days: unused selects the environments not used in that many days")
    return await asyncio.to_thread(env_uses.preview, body.mode, body.days)


@router.post("/api/storage/envs/delete")
async def delete_envs(body: EnvsDeleteRequest):
    """Delete what the preview ``preview_id`` named and is still selected:
    each key's uv environment, then its tree, then its record. 404 for an
    unknown, used or expired preview."""
    try:
        result = await asyncio.to_thread(env_uses.delete, body.preview_id)
    except LookupError as missing:
        raise HTTPException(404, str(missing)) from None
    storage_survey.update_later(parts=("envs",))
    return result


@router.post("/api/storage/uv/prune")
async def prune_uv():
    """``uv cache prune``: uv's own removal of dangling cache entries and
    unused environments, for every tool on this machine that uses uv
    (environments rebuild on demand). Refused while any run is live."""
    if runs_live():
        raise HTTPException(409, "Runs are still running or finishing on this server. Stop them "
                                 "and let them finish before pruning uv's cache.")
    result = await asyncio.to_thread(env_uses.prune_uv_cache)
    storage_survey.update_later(parts=("envs", "uv_cache"))
    return result


# -- locations (plan 1105 S2, S2a) --

class LocationPath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: The folder's absolute path on the server.
    path: str


class AddLocationRequest(LocationPath):
    #: Take ownership of a folder another installation owns (S2a).
    claim: bool = False
    #: Make the new location the default for new channels.
    default: bool = False
    #: With ``claim``: claim even though the owner cannot be checked (D18).
    confirm_unchecked: bool = False


class ClaimLocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Claim even though the owner cannot be checked (D18).
    confirm_unchecked: bool = False


def _refused(exc) -> HTTPException:
    return HTTPException(exc.status, exc.detail())


@router.get("/api/storage/locations")
async def list_locations() -> dict:
    """``{locations: [{id, path, is_default, state: ok|missing|unwritable|
    foreign, owner_data_dir?, channel_count, bytes, free_bytes, total_bytes,
    warning?, actions}]}``, each re-read from disk. A ``foreign`` location
    (claimed away by another installation) reports only its channel count;
    its channels are not listed and Claim is its only action. ``bytes`` is
    null here (the storage survey sizes folders)."""
    import collimate_server.locations as locations
    return {"locations": await asyncio.to_thread(locations.listing)}


@router.post("/api/storage/locations/check")
async def check_location(body: LocationPath) -> dict:
    """Validate a folder before adding it: ``{ok, path, creates, warning?,
    owned_by?: {installation_id, data_dir, channel_count, owner_running},
    error?: {code, message}}``. Codes: ``not_absolute``, ``not_directory``,
    ``not_writable``, ``inside_data_dir``, ``overlaps_location``,
    ``not_empty``, ``location_owned``. A network filesystem is a warning."""
    import collimate_server.locations as locations
    return (await asyncio.to_thread(locations.check, body.path)).wire()


@router.post("/api/storage/locations", status_code=201)
async def add_location(body: AddLocationRequest) -> dict:
    """Add a location: ``201 {location, claimed?: {adopted, skipped}}``.
    400 with a validation code; 409 ``location_owned`` for a folder another
    installation owns unless ``claim``; with ``claim``, 409
    ``owner_running`` while its owner's server runs and ``owner_unchecked``
    without ``confirm_unchecked`` when the owner cannot be checked."""
    import collimate_server.locations as locations
    try:
        return await asyncio.to_thread(locations.add, body.path, claim=body.claim, default=body.default,
                                       confirm_unchecked=body.confirm_unchecked)
    except locations.LocationRefused as exc:
        raise _refused(exc) from None


@router.post("/api/storage/locations/{location_id}/claim")
async def claim_location(location_id: str, body: ClaimLocationRequest | None = None) -> dict:
    """Claim a listed ``foreign`` location: its marker names this
    installation and its channels are adopted, ``{location, adopted,
    skipped: [{id, reason: duplicate_channel_id}]}``. 409 ``owner_running``
    while its owner's server runs; ``owner_unchecked`` without
    ``confirm_unchecked`` when the owner cannot be checked; ``not_foreign``."""
    import collimate_server.locations as locations
    confirm = bool(body and body.confirm_unchecked)
    try:
        return await asyncio.to_thread(locations.claim, location_id, confirm_unchecked=confirm)
    except locations.LocationRefused as exc:
        raise _refused(exc) from None


@router.put("/api/storage/locations/{location_id}/default")
async def set_default_location(location_id: str) -> dict:
    """Make the location the default for new channels (exactly one is);
    no channel moves. 409 for a ``foreign``, ``missing`` or unwritable one."""
    import collimate_server.locations as locations
    try:
        return await asyncio.to_thread(locations.set_default, location_id)
    except locations.LocationRefused as exc:
        raise _refused(exc) from None


@router.delete("/api/storage/locations/{location_id}", status_code=204)
async def delete_location(location_id: str) -> None:
    """Remove a location: 409 ``location_is_default`` for the default (so
    never the last one) and ``location_not_empty`` while it holds a
    channel. Our marker goes, then the folder if that leaves it empty. A
    ``foreign`` or ``missing`` entry is just dropped from the list."""
    import collimate_server.locations as locations
    try:
        await asyncio.to_thread(locations.remove, location_id)
    except locations.LocationRefused as exc:
        raise _refused(exc) from None


# -- channel move (plan 1105 S7) --

class MoveChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: str


@router.post("/api/channels/{channel}/move", status_code=202)
async def move_channel(channel: str, body: MoveChannelRequest) -> dict:
    """Move the channel's folder to another owned location: ``202
    {job_id}``, progress at ``GET /api/storage/jobs/{job_id}``. One move
    per server. 409 with a code while runs are live (``runs_live``), pins
    are held (``pins_held``), its collection is in flight, it is being
    created, changed or moved, for a foreign, missing or unwritable
    destination, the location it is in, or too little free space there
    (its size plus 5 %). Its writes are refused (``channel_moving``) until
    the job ends; an archived channel moves like any other."""
    import collimate_server.channel_move as channel_move
    import collimate_server.channel_paths as channel_paths
    from collimate_server.routes.channels import _refuse_while_runs_live
    from collimate_server.state import require_channel
    owner = require_channel(channel)
    channel_id = str(owner.id)
    size, files = await asyncio.to_thread(channel_move.folder_size, channel_paths.channel_dir(channel_id))
    # From here to begin(): no await, so no run can start in between.
    try:
        _refuse_while_runs_live(owner, "moving")
    except HTTPException as exc:
        raise HTTPException(409, {"code": "runs_live", "message": exc.detail}) from None
    try:
        channel_move.check(channel_id, body.location_id, size)
        job = channel_move.begin(channel_id, body.location_id, size, files)
    except channel_move.MoveRefused as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from None
    channel_move.start_thread(job)
    return {"job_id": job.id}


@router.get("/api/storage/jobs/{job_id}")
async def storage_job(job_id: str) -> dict:
    """A move job: ``{id, kind: "move", channel_id, from_location_id,
    to_location_id, state: copying|verifying|switching|cleaning|done|failed,
    method: rename|copy|null, bytes_done, bytes_total, files_done,
    files_total, started_at, finished_at, error?: {code, message},
    warning?}``. A finished job stays readable for ten minutes; 404 after."""
    import collimate_server.channel_move as channel_move
    found = channel_move.job(job_id)
    if found is None:
        raise HTTPException(404, "No such job (a finished job is kept for ten minutes)")
    return found.wire()
