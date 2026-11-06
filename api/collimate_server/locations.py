"""Channel locations as the New / manage channels page sees them (plan 1105
S2, S2a): the listing, the check of a folder before it is added, add, the
one default, removal of an empty location, and claim.

The folders, markers and the index are ``channel_paths``'; this module
validates, answers the wire shapes and brings the channels an add or a
claim indexes into the live catalog. Every refusal is a
:class:`LocationRefused` with a code; the routes answer it as
``{"detail": {"code", "message", ...}}``.

Validation codes (``check`` and add): ``not_absolute``, ``not_directory``,
``not_writable``, ``inside_data_dir``, ``overlaps_location``,
``not_empty`` (an existing folder without a marker that holds anything but
channel folders: indexing it would park the rest in ``.orphaned``), and
``location_owned`` (its marker names another installation: added only with
``claim``). A network filesystem is a warning, never a refusal.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import collimate_server.channel_paths as channel_paths

logger = logging.getLogger(__name__)

NETWORK_MOUNT_TYPES = ("nfs", "cifs", "smb", "fuse.sshfs")


class LocationRefused(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self), **self.extra}


# --- checks ---------------------------------------------------------------------------------------


def _mount_type(path: Path) -> str | None:
    """The filesystem type of the mount holding ``path`` (Linux
    ``/proc/self/mounts``), None when it cannot be told."""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as mounts:
            rows = [line.split() for line in mounts]
    except OSError:
        return None
    target = os.path.realpath(path)
    best, kind = "", None
    for row in rows:
        if len(row) < 3:
            continue
        point = row[1].replace("\\040", " ")
        if (target == point or target.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
            best, kind = point, row[2]
    return kind


def network_warning(path: Path) -> dict | None:
    kind = _mount_type(path)
    if kind and (kind.startswith(("nfs", "smb")) or kind in ("cifs", "fuse.sshfs")):
        return {"code": "network_filesystem",
                "message": f"{path} is on a network filesystem ({kind}). Channels there are slower, and a "
                           "dropped connection makes them unavailable until it is back."}
    return None


def _existing_ancestor(path: Path) -> Path:
    probe = path
    while not os.path.lexists(probe):
        probe = probe.parent
    return probe


def _within(inner: str, outer: str) -> bool:
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def _overlaps(a: str, b: str) -> bool:
    return _within(a, b) or _within(b, a)


def _holds_only_channels(path: Path) -> bool:
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if not entry.is_dir(follow_symlinks=False):
            return False
        try:
            channel_paths.read_manifest(Path(entry.path))
        except (OSError, ValueError):
            return False
    return True


@dataclass
class Checked:
    path: Path
    creates: bool
    warning: dict | None = None
    #: A marker naming another installation: what ``owned_by`` reports.
    owner: dict | None = None
    #: Our own marker, not listed (a reinstall that kept the folder).
    ours: bool = False
    error: LocationRefused | None = None
    extra: dict = field(default_factory=dict)

    def wire(self) -> dict:
        out: dict = {"ok": self.error is None, "path": str(self.path), "creates": self.creates}
        if self.warning is not None:
            out["warning"] = self.warning
        if self.owner is not None:
            out["owned_by"] = self.owner
        if self.error is not None:
            out["error"] = {"code": self.error.code, "message": str(self.error)}
        return out


def check(raw: str) -> Checked:
    """Validate a folder before it is added (S2a "Location validation"). The
    result carries the first refusal in ``error``, never raises for one."""
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        return Checked(Path(raw or "."), False,
                       error=LocationRefused(400, "not_absolute", "Give the folder's absolute path."))
    path = Path(os.path.normpath(raw))
    exists = os.path.lexists(path)
    if exists and not path.is_dir():
        return Checked(path, False, error=LocationRefused(400, "not_directory", f"{path} is not a folder."))
    ancestor = path if exists else _existing_ancestor(path)
    if not ancestor.is_dir():
        return Checked(path, not exists,
                       error=LocationRefused(400, "not_directory", f"{ancestor} is not a folder."))
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return Checked(path, not exists,
                       error=LocationRefused(400, "not_writable", f"{ancestor} is not writable by the server."))
    result = Checked(path, not exists, warning=network_warning(ancestor))
    real = os.path.realpath(path)
    data = os.path.realpath(".collimate")
    if _overlaps(real, data):
        result.error = LocationRefused(400, "inside_data_dir",
                                       f"{path} overlaps the server's data folder {data}.")
        return result
    for listed in channel_paths.locations():
        if _overlaps(real, os.path.realpath(listed.path)):
            result.error = LocationRefused(400, "overlaps_location",
                                           f"{path} overlaps the location {listed.path}.", location_id=listed.id)
            return result
    for found in channel_paths.homes():
        if _overlaps(real, os.path.realpath(found.path)):
            result.error = LocationRefused(400, "overlaps_location",
                                           f"{path} overlaps the channel folder {found.path}.")
            return result
    marker = channel_paths.read_marker(path) if exists else None
    if marker is not None and marker.get("installation_id") != channel_paths.installation_id():
        owner = marker.get("data_dir")
        result.owner = {"installation_id": marker.get("installation_id"), "data_dir": owner,
                        "channel_count": channel_paths.channel_count(path),
                        "owner_running": channel_paths.owner_running(owner)}
        result.error = LocationRefused(
            409, "location_owned",
            f"{path} belongs to another Collimate installation (data dir {owner or 'unknown'}, "
            f"{result.owner['channel_count']} channels). Claim it to add it.",
            owned_by=result.owner)
        return result
    if marker is not None:
        result.ours = True
    elif exists and not _holds_only_channels(path):
        result.error = LocationRefused(400, "not_empty",
                                       f"{path} holds other files or folders. Choose an empty folder, or "
                                       "one that holds only channel folders.")
    return result


# --- the listing ----------------------------------------------------------------------------------


def wire(entry: channel_paths.Location) -> dict:
    """One location for the page. A foreign one reports its channel count
    from a scan, and nothing else of its contents."""
    out: dict = {"id": entry.id, "path": str(entry.path), "is_default": entry.is_default, "state": entry.state,
                 "channel_count": 0, "bytes": None, "free_bytes": None, "total_bytes": None}
    if entry.state == "foreign":
        out["owner_data_dir"] = entry.owner_data_dir
        out["channel_count"] = channel_paths.channel_count(entry.path)
        out["actions"] = ["claim"]
        return out
    if entry.state == "missing":
        out["actions"] = ["remove"] if not entry.is_default else []
        return out
    try:
        usage = shutil.disk_usage(entry.path)
        out["free_bytes"], out["total_bytes"] = usage.free, usage.total
    except OSError:
        pass
    if not os.access(entry.path, os.W_OK | os.X_OK):
        out["state"] = "unwritable"
    out["channel_count"] = sum(1 for found in channel_paths.channels_in(entry.id) if found.state != "creating")
    warning = network_warning(entry.path)
    if warning is not None:
        out["warning"] = warning
    actions = []
    if not entry.is_default and out["state"] == "ok":
        actions.append("set_default")
    if not entry.is_default and out["channel_count"] == 0:
        actions.append("remove")
    out["actions"] = actions
    return out


def listing() -> list[dict]:
    """Every listed location, its state re-read from disk (a location
    claimed away while this server ran shows ``foreign``; a missing one
    that is back is indexed again)."""
    refresh()
    return [wire(entry) for entry in channel_paths.locations()]


def refresh() -> None:
    """Bring the index and the live catalog up to the locations' folders."""
    changed = channel_paths.refresh_locations()
    _drop(changed.dropped)
    _adopt(changed.indexed)


def _require(location_id: str) -> channel_paths.Location:
    refresh()
    found = channel_paths.location(location_id)
    if found is None:
        raise LocationRefused(404, "location_not_found", "No such location.")
    return found


# --- adoption into the live catalog ---------------------------------------------------------------


def _adopt(channel_ids: list[str]) -> list[str]:
    """Load channels an add or a claim indexed into the live catalog, as a
    restart would (``main.restore_app_state``); one whose saved state does
    not open is left unavailable. Answers the ids loaded."""
    import sqlite3
    from collimate_server.genesis import ensure_capabilities_stack, load_channel, reconcile_restored_cell_files
    from collimate_server.state import persist_state, state, state_lock
    loaded = []
    for channel_id in channel_ids:
        if any(str(channel.id) == channel_id for channel in state.channels):
            continue
        try:
            channel = load_channel(channel_id)
            reconcile_restored_cell_files(channel)
        except (ValueError, OSError, sqlite3.Error) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            channel_paths.mark_unavailable(channel_id, f"its saved state does not open: {reason}")
            logger.error("the adopted channel %s is unavailable, its folder untouched: %s", channel_id, reason)
            continue
        ensure_capabilities_stack(channel)
        with state_lock:
            state.channels.append(channel)
        loaded.append(channel_id)
    if loaded:
        persist_state()
    return loaded


def _drop(channel_ids: list[str]) -> None:
    """Take channels whose location left this installation out of the live
    catalog (the active one falls back to the first left)."""
    if not channel_ids:
        return
    from collimate_server.state import persist_state, state, state_lock
    gone = set(channel_ids)
    with state_lock:
        state.channels = [channel for channel in state.channels if str(channel.id) not in gone]
        if state.active_channel_id is not None and str(state.active_channel_id) in gone:
            state.active_channel_id = state.channels[0].id if state.channels else None
    persist_state()


# --- changes --------------------------------------------------------------------------------------


def _claim_refusal(exc: Exception) -> LocationRefused:
    if isinstance(exc, channel_paths.OwnerRunning):
        return LocationRefused(409, "owner_running", str(exc), owner_data_dir=exc.data_dir)
    if isinstance(exc, channel_paths.OwnerUnchecked):
        return LocationRefused(409, "owner_unchecked", str(exc), owner_data_dir=exc.data_dir)
    raise exc


def add(raw: str, *, claim: bool = False, default: bool = False, confirm_unchecked: bool = False) -> dict:
    """Add the folder as a location: ``{location, claimed?}``. A folder
    another installation owns is added only with ``claim`` (which then
    refuses while its owner runs, and needs ``confirm_unchecked`` when the
    owner cannot be checked)."""
    checked = check(raw)
    if checked.error is not None and not (claim and checked.error.code == "location_owned"):
        raise checked.error
    if checked.owner is not None:
        try:
            claimed = channel_paths.claim(checked.path, confirm_unchecked=confirm_unchecked, default=default)
        except (channel_paths.OwnerRunning, channel_paths.OwnerUnchecked) as exc:
            raise _claim_refusal(exc) from None
        loaded = _adopt(claimed.adopted)
        entry = channel_paths.location(claimed.location_id)
        assert entry is not None
        return {"location": wire(entry), "claimed": {"adopted": loaded, "skipped": claimed.skipped}}
    try:
        added = channel_paths.add_location(checked.path, default=default)
    except channel_paths.LocationOwned as exc:
        raise LocationRefused(409, "location_owned", str(exc), owner_data_dir=exc.owner_data_dir) from None
    loaded = _adopt(added.adopted)
    entry = channel_paths.location(added.location_id)
    assert entry is not None
    out: dict = {"location": wire(entry)}
    if added.adopted or added.skipped:
        out["claimed"] = {"adopted": loaded, "skipped": added.skipped}
    return out


def claim(location_id: str, *, confirm_unchecked: bool = False) -> dict:
    """Claim a listed ``foreign`` location: ``{location, adopted, skipped}``."""
    entry = _require(location_id)
    if entry.state != "foreign":
        raise LocationRefused(409, "not_foreign", "This installation already owns the location.")
    try:
        claimed = channel_paths.claim(entry.path, confirm_unchecked=confirm_unchecked, default=entry.is_default)
    except (channel_paths.OwnerRunning, channel_paths.OwnerUnchecked) as exc:
        raise _claim_refusal(exc) from None
    except ValueError as exc:
        raise LocationRefused(409, "not_foreign", str(exc)) from None
    loaded = _adopt(claimed.adopted)
    found = channel_paths.location(claimed.location_id)
    assert found is not None
    return {"location": wire(found), "adopted": loaded, "skipped": claimed.skipped}


def set_default(location_id: str) -> dict:
    entry = _require(location_id)
    if entry.state != "ok" or not entry.path.is_dir():
        raise LocationRefused(409, f"location_{entry.state}",
                              f"The location {entry.path} is {'owned by another installation' if entry.state == 'foreign' else 'not available'}; "
                              "it cannot be the default.")
    if not os.access(entry.path, os.W_OK | os.X_OK):
        raise LocationRefused(409, "location_unwritable", f"The location {entry.path} is not writable.")
    return wire(channel_paths.set_default(location_id))


def remove(location_id: str) -> None:
    """Drop a location: never the default (so never the last), and only
    one of ours holding no channel. A foreign or missing one is just
    dropped from the list."""
    entry = _require(location_id)
    if entry.is_default:
        raise LocationRefused(409, "location_is_default",
                              "The default location cannot be removed. Make another location the default first.")
    if entry.state == "ok":
        held = max(len(channel_paths.channels_in(location_id)), channel_paths.channel_count(entry.path))
        if held:
            raise LocationRefused(409, "location_not_empty",
                                  "The location still holds channels. Move or delete them first.",
                                  channel_count=held)
        from collimate_server.channel_move import active_job
        moving = active_job()
        if moving is not None and moving.to_location_id == location_id:
            raise LocationRefused(409, "location_not_empty", "A channel is being moved into the location.")
    dropped = [found.channel_id for found in channel_paths.channels_in(location_id)]
    channel_paths.remove_location(location_id)
    _drop(dropped)
