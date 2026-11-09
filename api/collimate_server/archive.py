"""Archived channels (plan 1105 S6): the flag, its two writers and the
choice of the channel that follows an archived active one.

The flag is ``archived`` (with ``archived_at``) in the channel's
``channel.json``, so it travels with the folder. The channel index's
manifest mirrors it, and :func:`is_archived` reads that mirror;
``channel_guard`` refuses every write to an archived channel from it.

:func:`archive` and :func:`unarchive` rewrite the manifest atomically under
``database.channel_files_lock``. The archive route marks the channel
:func:`archiving` on the event loop before its write leaves the loop, so a
run start that marks the channel busy after the route's live-runs check
still sees it archived.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID

import collimate_server.channel_paths as channel_paths

# Channels whose archive is being written: archived to every reader.
_archiving: set[str] = set()
# Channel ids, most recently selected first (a select, a create, the
# restored active channel).
_selected: list[str] = []


def is_archived(channel_id) -> bool:
    """Whether the channel is archived. False for an id with no channel
    folder (or not a channel id)."""
    try:
        found = channel_paths.home(channel_id)
    except ValueError:
        return False
    if found is None:
        return False
    return found.channel_id in _archiving or bool(found.manifest.get("archived"))


@contextmanager
def archiving(channel_id: str):
    """Read the channel as archived while its archive is written."""
    _archiving.add(channel_id)
    try:
        yield
    finally:
        _archiving.discard(channel_id)


def _set(channel_id: str, archived: bool) -> dict:
    from collimate_server.database import channel_files_lock
    with channel_files_lock:
        folder = channel_paths.channel_dir(channel_id)
        found = channel_paths.home(channel_id)
        assert found is not None
        updated = {**found.manifest, "archived": archived,
                   "archived_at": datetime.now(timezone.utc).isoformat() if archived else None}
        channel_paths.write_manifest(folder, updated)
        found.manifest = updated
    return dict(updated)


def archive(channel_id: str) -> dict:
    """Rewrite the channel's ``channel.json`` archived, now; the manifest."""
    return _set(channel_id, True)


def unarchive(channel_id: str) -> dict:
    """Rewrite the channel's ``channel.json`` unarchived; the manifest."""
    return _set(channel_id, False)


def note_selected(channel_id: UUID | str) -> None:
    """Record that the channel was made active."""
    identity = str(channel_id)
    if identity in _selected:
        _selected.remove(identity)
    _selected.insert(0, identity)


def successor(channels, leaving: UUID | str | None) -> UUID | None:
    """The channel to make active when ``leaving`` stops being it (archived
    or deleted): the most recently selected channel of ``channels`` that is
    neither ``leaving`` nor archived, else the first such one, else None
    (the window shows the channels page)."""
    candidates = {str(channel.id): channel.id for channel in channels
                  if str(channel.id) != str(leaving) and not is_archived(channel.id)}
    recent = next((identity for identity in _selected if identity in candidates), None)
    if recent is not None:
        return candidates[recent]
    return next(iter(candidates.values()), None)


def channel_state(found: channel_paths.Home) -> tuple[str, str | None]:
    """``ok``, ``moving`` (``channel_guard.is_moving``: its folder is being
    copied to another location) or ``unavailable`` (it did not open, or its
    folder is gone), with the reason for the last."""
    from collimate_server.channel_guard import is_moving
    if found.state == "unavailable":
        return "unavailable", found.reason or "unavailable"
    if is_moving(found.channel_id):
        return "moving", None
    if not found.path.is_dir():
        return "unavailable", f"its folder {found.path} is missing"
    return "ok", None


def manage_entry(channel_id, sizes: dict | None = None) -> dict:
    """The channel's row of the manage listing (S8), which the archive and
    unarchive routes answer too: ``{id, name, archived, archived_at,
    location_id, path, state, reason, active, bytes, files, computed_at}``.
    The sizes are the storage survey's cache (null before it sized the
    folder); ``sizes`` is that cache's channels, read once by a listing."""
    import collimate_server.storage_survey as storage_survey
    from collimate_server.state import state
    found = channel_paths.home(channel_id)
    if found is None:
        raise channel_paths.ChannelNotFound(str(channel_id), "it has no channel folder")
    manifest = found.manifest
    state_name, reason = channel_state(found)
    size = (storage_survey.channel_sizes() if sizes is None else sizes).get(found.channel_id) or {}
    return {
        "id": found.channel_id,
        "name": manifest.get("name", ""),
        "archived": is_archived(found.channel_id),
        "archived_at": manifest.get("archived_at"),
        "location_id": found.location_id,
        "path": str(found.path),
        "state": state_name,
        "reason": reason,
        "active": str(state.active_channel_id) == found.channel_id,
        "bytes": size.get("bytes"),
        "files": size.get("files"),
        "computed_at": size.get("computed_at"),
    }
