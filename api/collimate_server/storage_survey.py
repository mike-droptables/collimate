"""The storage survey (plan 1105 S8): every size the New / manage channels
page shows, measured off the event loop and cached.

**Measuring.** :func:`measure` walks with ``os.scandir`` and follows no
symlink. A file counts its allocated bytes (``st_blocks * 512``; its
length where the platform has no blocks) and every inode counts once per
measurement, so a file hardlinked twice under one tree counts once.

**What is measured** (:func:`survey`):

- each indexed channel's folder, split into ``channel_db`` (with its WAL
  and shm), ``git`` (``git/`` and ``git-rescue/``), ``objects``,
  ``objects_trash``, ``cell_state`` and ``other``;
- each owned location's other contents (its marker, ``.orphaned/``, a
  ``.moving`` copy or a ``.deleting-*`` folder), and how many channel
  folders a foreign location holds (nothing else of its contents);
- the data dir's parts: ``app_db``, ``app_store`` and ``staged_store``
  (each with its trash), ``envs`` (the stable generator trees and the uv
  environments ``env_uses`` recorded), ``scratch`` (``tmp/``, the live
  runs' workspaces in it, ``pycache/``), ``previous_layout`` (the previous
  storage layout's trees, which can be removed by hand) and ``other``
  (everything else under ``.collimate``);
- report only: uv's cache and the agent adapters
  (``~/.cache/collimate/acp-adapters``), both shared with every server on
  the machine, and the Collimate-built Docker images (``docker_inventory``).

**The cache.** A survey's result is kept in memory and in ``app.db``
(``ui_state`` ``storage.sizes``), each entry with the time it was measured
(``computed_at``), so it survives a restart. :func:`request_survey` starts
a survey on a worker thread; requests while one runs join it. Channel
creation and deletion, the channel size route and the cleanups update
their own entries; a survey that finishes keeps any entry measured after
its own measurement of it. Nothing is measured at startup: the page asks
for a survey when the cache is cold or older than ten minutes.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import shutil
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import collimate_server.channel_paths as channel_paths
from collimate_server.database import read_global_json, write_global_json

logger = logging.getLogger(__name__)

#: The ``app.db`` ``ui_state`` row holding the cached sizes.
SIZES_KEY = "storage.sizes"
#: The Docker row's ``unused_bytes``: images with no use in this many days.
UNUSED_DAYS = 30

DATA = ".collimate"
APP_DB = ("app.db", "app.db-wal", "app.db-shm")
PREVIOUS_LAYOUT = ("channels", "objects", "objects.trash")
TMP = os.path.join(DATA, "tmp")
PYCACHE = os.path.join(DATA, "pycache")
_NAMED = frozenset({*APP_DB, "app-objects", "app-objects.trash", "staged-objects", "staged-objects.trash",
                    "envs", "tmp", "pycache", *PREVIOUS_LAYOUT})

#: A channel folder's entries and the part each belongs to (else ``other``).
CHANNEL_PARTS = {
    channel_paths.DB_FILE: "channel_db", f"{channel_paths.DB_FILE}-wal": "channel_db",
    f"{channel_paths.DB_FILE}-shm": "channel_db",
    channel_paths.GIT_DIR: "git", channel_paths.GIT_RESCUE_DIR: "git",
    channel_paths.OBJECTS_DIR: "objects", f"{channel_paths.OBJECTS_DIR}.trash": "objects_trash",
    channel_paths.CELL_STATE_DIR: "cell_state",
}
PART_NAMES = ("channel_db", "git", "objects", "objects_trash", "cell_state", "other")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

class Stopped(Exception):
    """The survey was asked to stop (the server is shutting down)."""


@dataclass
class Tally:
    #: Allocated bytes, each inode once.
    bytes: int = 0
    #: Regular files, each inode once.
    files: int = 0
    #: What removing the trees frees: everything but the files that have
    #: links outside them.
    reclaimable_bytes: int = 0


def _allocated(found: os.stat_result) -> int:
    blocks = getattr(found, "st_blocks", None)
    return blocks * 512 if blocks is not None else found.st_size


def measure(paths: Iterable[str | os.PathLike], *, stop: threading.Event | None = None) -> Tally:
    """The size of the trees at ``paths`` together (a missing one adds
    nothing), no symlink followed, each inode once. Raises :class:`Stopped`
    when ``stop`` is set during the walk."""
    tally = Tally()
    # (dev, ino) of a multiply-linked file → [allocated, links, links seen]
    linked: dict[tuple[int, int], list[int]] = {}

    def count(found: os.stat_result) -> None:
        allocated = _allocated(found)
        if not stat.S_ISDIR(found.st_mode) and found.st_nlink > 1:
            key = (found.st_dev, found.st_ino)
            seen = linked.get(key)
            if seen is not None:
                seen[2] += 1
                return
            linked[key] = [allocated, found.st_nlink, 1]
        else:
            tally.reclaimable_bytes += allocated
        tally.bytes += allocated
        if stat.S_ISREG(found.st_mode):
            tally.files += 1

    for top in paths:
        try:
            found = os.lstat(top)
        except OSError:
            continue
        count(found)
        if not stat.S_ISDIR(found.st_mode):
            continue
        pending = [os.fspath(top)]
        while pending:
            if stop is not None and stop.is_set():
                raise Stopped()
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            found = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        count(found)
                        if stat.S_ISDIR(found.st_mode):
                            pending.append(entry.path)
            except OSError:
                continue
    tally.reclaimable_bytes += sum(allocated for allocated, links, seen in linked.values() if seen >= links)
    return tally


def measure_channel_folder(folder: Path, *, stop: threading.Event | None = None) -> dict:
    """``{bytes, files, parts: {channel_db, git, objects, objects_trash,
    cell_state, other}, computed_at}`` of one channel folder. Raises OSError
    when the folder cannot be read."""
    groups: dict[str, list[Path]] = {name: [] for name in PART_NAMES}
    own = os.lstat(folder)
    for name in os.listdir(folder):
        groups[CHANNEL_PARTS.get(name, "other")].append(folder / name)
    parts = {name: measure(paths, stop=stop) for name, paths in groups.items()}
    return {"bytes": _allocated(own) + sum(part.bytes for part in parts.values()),
            "files": sum(part.files for part in parts.values()),
            "parts": {name: part.bytes for name, part in parts.items()},
            "computed_at": now_iso()}


# ---------------------------------------------------------------------------
# The parts of the survey
# ---------------------------------------------------------------------------

def _store_part(root: str, stop: threading.Event | None) -> dict:
    return {"bytes": measure([os.path.join(DATA, root)], stop=stop).bytes,
            "trash_bytes": measure([os.path.join(DATA, f"{root}.trash")], stop=stop).bytes}


def _app_db(stop: threading.Event | None) -> dict:
    return {"bytes": measure([os.path.join(DATA, name) for name in APP_DB], stop=stop).bytes}


def _app_store(stop: threading.Event | None) -> dict:
    return _store_part("app-objects", stop)


def _staged_store(stop: threading.Event | None) -> dict:
    return _store_part("staged-objects", stop)


def _envs(stop: threading.Event | None) -> dict:
    """The stable trees under ``envs/`` and every recorded uv environment."""
    import collimate_server.env_uses as env_uses
    from collimate_server.run_staging import ENVS_DIR
    rows = env_uses.rows()
    size = measure([ENVS_DIR, *(row["uv_env"] for row in rows if row["uv_env"])], stop=stop)
    return {"count": len(rows), "bytes": size.bytes, "reclaimable_bytes": size.reclaimable_bytes}


def live_workspaces() -> set[str]:
    """The real paths of the registered runs' workspaces."""
    from collimate_server.run_registry import active_managers
    found = set()
    for manager in list(active_managers.values()):
        workspace = getattr(manager, "workspace_dir", None)
        if workspace:
            found.add(os.path.realpath(workspace))
    return found


def _scratch(stop: threading.Event | None) -> dict:
    """``tmp/`` (``tmp_live_bytes``: the live runs' workspaces in it) and
    ``pycache/``."""
    live = live_workspaces()
    try:
        children = [os.path.join(TMP, name) for name in os.listdir(TMP)]
    except OSError:
        children = []
    in_use = [path for path in children if os.path.realpath(path) in live]
    total = measure([TMP], stop=stop).bytes if children or os.path.isdir(TMP) else 0
    return {"tmp_bytes": total, "tmp_live_bytes": measure(in_use, stop=stop).bytes,
            "pycache_bytes": measure([PYCACHE], stop=stop).bytes}


def previous_layout_paths() -> list[str]:
    """The previous storage layout's trees that are still there."""
    return [os.path.abspath(os.path.join(DATA, name)) for name in PREVIOUS_LAYOUT
            if os.path.lexists(os.path.join(DATA, name))]


def _previous_layout(stop: threading.Event | None) -> dict:
    paths = previous_layout_paths()
    return {"paths": paths, "bytes": measure(paths, stop=stop).bytes}


def _other(stop: threading.Event | None) -> dict:
    """Everything under ``.collimate`` no other part counts (imports,
    verifier workers, the lock, keys and the like)."""
    try:
        names = [name for name in os.listdir(DATA) if name not in _NAMED]
    except OSError:
        return {"bytes": 0}
    own = _allocated(os.lstat(DATA))
    return {"bytes": own + measure([os.path.join(DATA, name) for name in names], stop=stop).bytes}


def uv_cache_path() -> str | None:
    """Where the uv the runs use keeps its cache, or None when it does not say."""
    import collimate_server.env_uses as env_uses
    from collimate_server.process_manager.native_tools import native_uv
    return env_uses.uv_cache_dir(native_uv(), env_uses.uv_query_env())


def adapters_path() -> str:
    """The agent adapters' install cache every server on the machine shares."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "collimate", "acp-adapters")


def _uv_cache(stop: threading.Event | None) -> dict:
    path = uv_cache_path()
    if path is None:
        return {"path": None, "bytes": 0, "reason": "uv did not say where its cache is"}
    return {"path": path, "bytes": measure([path], stop=stop).bytes}


def _adapters(stop: threading.Event | None) -> dict:
    path = adapters_path()
    return {"path": path, "bytes": measure([path], stop=stop).bytes}


def _docker(stop: threading.Event | None) -> dict:
    """The Collimate-built images: ``images_bytes`` what they alone hold
    (``exact``, else at least that), ``unused_bytes`` what deleting the
    ones not used here in :data:`UNUSED_DAYS` days frees, the build cache
    (reported only)."""
    from collimate_server import image_uses
    from collimate_server.docker_inventory import (
        DockerUnavailable, candidates, docker_client, measure as layer_model, read_inventory, reclaim)
    try:
        client = docker_client()
    except DockerUnavailable as exc:
        return {"available": False, "reason": str(exc)}
    try:
        inventory = read_inventory(client, image_uses.last_uses())
        model = layer_model(client, inventory, inventory.images)
        images_bytes, exact = reclaim(inventory.images, model)
        unused = candidates(inventory, "unused", UNUSED_DAYS, datetime.now(timezone.utc))
        unused_bytes, unused_exact = reclaim(unused, model)
    except Exception as exc:
        logger.warning("storage survey: reading the Docker images failed: %s", exc)
        return {"available": False, "reason": f"reading the images failed: {exc}"}
    finally:
        client.close()
    return {"available": True, "count": len(inventory.images), "images_bytes": images_bytes, "exact": exact,
            "unused_days": UNUSED_DAYS, "unused_count": len(unused), "unused_bytes": unused_bytes,
            "unused_exact": unused_exact, "build_cache_bytes": inventory.build_cache_bytes}


#: The survey's parts outside the channel locations, by name.
PARTS: dict[str, Callable[[threading.Event | None], dict]] = {
    "app_db": _app_db, "app_store": _app_store, "staged_store": _staged_store, "envs": _envs,
    "scratch": _scratch, "previous_layout": _previous_layout, "other": _other,
    "uv_cache": _uv_cache, "adapters": _adapters, "docker": _docker,
}
#: The parts every server on the machine shares, measured last: a survey
#: publishes the server's own sizes first (uv's cache can take many minutes
#: to walk).
MACHINE_PARTS = ("uv_cache", "adapters", "docker")
#: The parts, and their figures, that count in the server's total (uv's
#: cache, the adapters and Docker are machine-wide and reported apart).
COUNTED = {"app_db": ("bytes",), "app_store": ("bytes", "trash_bytes"), "staged_store": ("bytes", "trash_bytes"),
           "envs": ("bytes",), "scratch": ("tmp_bytes", "pycache_bytes"), "previous_layout": ("bytes",),
           "other": ("bytes",)}


def _location(location: channel_paths.Location, stop: threading.Event | None) -> dict:
    """An owned location's contents that are not indexed channel folders;
    a foreign one's channel count only."""
    if location.state == "foreign":
        return {"channel_count": _channel_count(location.path), "computed_at": now_iso()}
    if location.state != "ok":
        return {"computed_at": now_iso()}
    folders = {home.path.name for home in channel_paths.homes()
               if home.location_id == location.id and home.state != "creating"}
    try:
        own = _allocated(os.lstat(location.path))
        others = [location.path / name for name in os.listdir(location.path) if name not in folders]
    except OSError:
        return {"computed_at": now_iso()}
    return {"other_bytes": own + measure(others, stop=stop).bytes, "computed_at": now_iso()}


def _channel_count(path: Path) -> int:
    count = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            channel_paths.read_manifest(Path(entry.path))
        except (OSError, ValueError):
            continue
        count += 1
    return count


def _timed(entry: dict) -> dict:
    return {**entry, "computed_at": entry.get("computed_at") or now_iso()}


def _empty() -> dict:
    return {"computed_at": None, "channels": {}, "locations": {}, "parts": {}}


def survey(stop: threading.Event | None = None,
           publish: Callable[[dict, tuple[str, ...]], None] | None = None) -> dict:
    """Measure everything (blocking; :func:`request_survey` runs it on a
    worker thread). ``publish(sizes, pending)`` is called with the server's
    own sizes once they are measured, then after each machine-wide part,
    ``pending`` naming the parts still to come."""
    sizes = _empty()
    for location in channel_paths.locations():
        sizes["locations"][location.id] = _location(location, stop)
    for home in channel_paths.homes():
        if home.state == "creating":
            continue
        try:
            sizes["channels"][home.channel_id] = {"location_id": home.location_id,
                                                  **measure_channel_folder(home.path, stop=stop)}
        except OSError:
            continue
    for name, part in PARTS.items():
        if name not in MACHINE_PARTS:
            sizes["parts"][name] = _timed(part(stop))
    sizes["computed_at"] = now_iso()
    for done, name in enumerate(MACHINE_PARTS):
        if publish is not None:
            publish(sizes, MACHINE_PARTS[done:])
        sizes["parts"][name] = _timed(PARTS[name](stop))
    return sizes


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

@dataclass
class _Survey:
    sizes: dict | None = None
    loaded: bool = False
    task: asyncio.Task | None = None
    #: idle, running or failed (the last survey raised; ``error`` says why).
    state: str = "idle"
    started_at: str | None = None
    error: str | None = None
    #: While running: the parts the survey has not measured yet.
    pending: tuple[str, ...] = ()
    stop: threading.Event = field(default_factory=threading.Event)
    updates: set[asyncio.Task] = field(default_factory=set)


_surveys: dict[str, _Survey] = {}
# Guards every cache's sizes and their app.db row.
_lock = threading.Lock()


def _key() -> str:
    return os.path.abspath(os.getcwd())


def _current() -> _Survey:
    key = _key()
    with _lock:
        found = _surveys.get(key)
        if found is None:
            found = _surveys[key] = _Survey()
        return found


def _loaded(found: _Survey) -> dict:
    """The cache's sizes, read from ``app.db`` the first time (``_lock`` held)."""
    if not found.loaded:
        stored = read_global_json(SIZES_KEY)
        found.sizes = stored if isinstance(stored, dict) and isinstance(stored.get("channels"), dict) else None
        found.loaded = True
    return found.sizes or _empty()


def cached() -> dict:
    """A copy of the cached sizes (empty before the first survey)."""
    found = _current()
    with _lock:
        return copy.deepcopy(_loaded(found))


def channel_sizes() -> dict:
    """The cached channel entries by id (a copy)."""
    found = _current()
    with _lock:
        return copy.deepcopy(_loaded(found)["channels"])


def _merge(fresh: dict, old: dict, started_at: str) -> dict:
    """``fresh``, keeping each of ``old``'s entries measured after
    ``fresh``'s measurement of it (or, for one ``fresh`` lacks, after the
    survey started)."""
    for section in ("channels", "locations", "parts"):
        for key, entry in old.get(section, {}).items():
            measured = entry.get("computed_at") or ""
            ours = fresh[section].get(key)
            if measured > ((ours.get("computed_at") or "") if ours else started_at):
                fresh[section][key] = entry
    return fresh


def _patch(section: str, key: str, entry: dict | None) -> None:
    found = _current()
    with _lock:
        sizes = _loaded(found)
        if entry is None:
            if key not in sizes[section]:
                return
            sizes[section].pop(key)
        else:
            sizes[section][key] = entry
        found.sizes = sizes
        write_global_json(SIZES_KEY, sizes)


def _store(found: _Survey, fresh: dict, started_at: str, pending: tuple[str, ...] = ()) -> None:
    """Merge a survey's sizes into the cache; a part still ``pending``
    keeps its last measurement meanwhile."""
    with _lock:
        old = _loaded(found)
        fresh = copy.deepcopy(fresh)
        for name in pending:
            if name in old["parts"]:
                fresh["parts"].setdefault(name, old["parts"][name])
        merged = _merge(fresh, old, started_at)
        found.sizes = merged
        write_global_json(SIZES_KEY, merged)
        found.pending = pending


def _run(found: _Survey, started_at: str) -> None:
    try:
        fresh = survey(found.stop, lambda sizes, pending: _store(found, sizes, started_at, pending))
    except Stopped:
        found.state, found.pending = "idle", ()
        return
    except Exception as exc:
        logger.exception("storage survey failed")
        found.state, found.error, found.pending = "failed", str(exc), ()
        return
    _store(found, fresh, started_at)
    found.state = "idle"


def status(found: _Survey | None = None) -> dict:
    """``{state, computed_at, started_at?, pending?, error?}``: while a
    survey runs, ``pending`` names the parts it has still to measure (the
    machine-wide ones come last; the rest are in the cache by then)."""
    found = found or _current()
    with _lock:
        computed_at = _loaded(found).get("computed_at")
        pending = found.pending
    out: dict = {"state": found.state, "computed_at": computed_at}
    if found.state == "running":
        out["started_at"] = found.started_at
        out["pending"] = list(pending)
    if found.error and found.state == "failed":
        out["error"] = found.error
    return out


def request_survey() -> asyncio.Task:
    """Start a survey on a worker thread, or join the one running (call on
    the event loop)."""
    from collimate_server.task_utils import track_task
    found = _current()
    if found.task is None or found.task.done():
        found.state, found.started_at, found.error, found.pending = "running", now_iso(), None, tuple(PARTS)
        found.task = track_task(asyncio.to_thread(_run, found, found.started_at), name="storage-survey")
    return found.task


def update_channel(channel_id) -> dict:
    """Measure one channel's folder now and cache it (blocking). Raises
    :class:`channel_paths.ChannelNotFound` for a channel with no folder,
    :class:`channel_paths.ChannelUnavailable` when its folder is gone."""
    try:
        found = channel_paths.home(channel_id)
    except ValueError:
        raise channel_paths.ChannelNotFound(str(channel_id), "not a channel id") from None
    if found is None or found.state == "creating":
        raise channel_paths.ChannelNotFound(str(channel_id), "it has no channel folder")
    try:
        entry = {"location_id": found.location_id, **measure_channel_folder(found.path)}
    except FileNotFoundError:
        raise channel_paths.ChannelUnavailable(found.channel_id, f"its folder {found.path} is missing") from None
    _patch("channels", found.channel_id, entry)
    return entry


def forget_channel(channel_id: str) -> None:
    """Drop a deleted channel's entry (blocking)."""
    _patch("channels", str(channel_id), None)


def update_parts(*names: str, stop: threading.Event | None = None) -> None:
    """Measure the named parts now and cache them (blocking)."""
    for name in names:
        _patch("parts", name, _timed(PARTS[name](stop)))


def update_later(*, channels: Iterable[str] = (), parts: Iterable[str] = ()) -> None:
    """Re-measure ``channels`` and ``parts`` on a worker thread after a
    cleanup (call on the event loop; :func:`shutdown` waits for it)."""
    from collimate_server.task_utils import track_task
    found = _current()
    channels, parts = list(channels), list(parts)

    def work() -> None:
        try:
            for channel_id in channels:
                try:
                    update_channel(channel_id)
                except (channel_paths.ChannelUnavailable, OSError):
                    continue
            update_parts(*parts, stop=found.stop)
        except Stopped:
            pass
        except Exception:
            logger.exception("storage survey: updating %s failed", [*channels, *parts])
    task = track_task(asyncio.to_thread(work), name="storage-update")
    found.updates.add(task)
    task.add_done_callback(found.updates.discard)


async def shutdown() -> None:
    """Stop the survey and the updates, wait for their threads, and forget
    the in-memory cache (the next start reads ``app.db``)."""
    key = _key()
    with _lock:
        found = _surveys.get(key)
    if found is None:
        return
    found.stop.set()
    for task in [found.task, *found.updates]:
        if task is not None and not task.done():
            try:
                await asyncio.shield(task)
            except Exception:
                pass
    with _lock:
        if _surveys.get(key) is found:
            del _surveys[key]


# ---------------------------------------------------------------------------
# What the routes answer
# ---------------------------------------------------------------------------

def _listed_homes() -> list[channel_paths.Home]:
    return [home for home in channel_paths.homes() if home.state != "creating"]


def manage_listing() -> list[dict]:
    """``GET /api/channels/manage``'s rows: every indexed channel, archived
    ones included, by name, each ``archive.manage_entry`` (the row the
    archive routes answer)."""
    from collimate_server.archive import manage_entry
    sizes = channel_sizes()
    rows = [manage_entry(home.channel_id, sizes) for home in _listed_homes()]
    rows.sort(key=lambda row: (row["name"].casefold(), row["id"]))
    return rows


def _disk(path: Path) -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {}
    return {"free_bytes": usage.free, "total_bytes": usage.total}


def overview() -> dict:
    """``GET /api/storage`` without its server block and survey state: the
    locations with their channels, the data dir's parts, the machine-wide
    caches and Docker, from the cache; a part not yet measured is absent.
    Free and total space are read now."""
    from collimate_server.archive import is_archived
    from collimate_server.cas_gc import last_collection_at
    from collimate_server.run_registry import active_managers
    sizes = cached()
    channel_sizes = sizes["channels"]
    homes = _listed_homes()
    total = 0
    locations = []
    for location in channel_paths.locations():
        measured = sizes["locations"].get(location.id, {})
        row: dict = {"id": location.id, "path": str(location.path), "is_default": location.is_default,
                     "state": location.state}
        if location.state == "foreign":
            row["owner_data_dir"] = location.owner_data_dir
            if "channel_count" in measured:
                row["channel_count"] = measured["channel_count"]
            locations.append(row)
            continue
        channels = []
        for home in homes:
            if home.location_id != location.id:
                continue
            size = channel_sizes.get(home.channel_id) or {}
            channels.append({"id": home.channel_id, "name": home.manifest.get("name", ""),
                             "archived": is_archived(home.channel_id), "bytes": size.get("bytes"),
                             "files": size.get("files"), "parts": size.get("parts"),
                             "computed_at": size.get("computed_at")})
        channels.sort(key=lambda channel: -(channel["bytes"] or 0))
        known = sum(channel["bytes"] or 0 for channel in channels) + (measured.get("other_bytes") or 0)
        total += known
        row.update({"channel_count": len(channels), "bytes": known, "other_bytes": measured.get("other_bytes"),
                    "channels": channels, **(_disk(location.path) if location.state == "ok" else {})})
        locations.append(row)
    parts = sizes["parts"]
    previous = previous_layout_paths()
    for name, figures in COUNTED.items():
        if name == "previous_layout" and not previous:
            continue
        part = parts.get(name) or {}
        total += sum(part.get(figure) or 0 for figure in figures)
    out: dict = {"totals": {"bytes": total, "computed_at": sizes.get("computed_at")}, "locations": locations,
                 "last_collection_at": last_collection_at()}
    for name in PARTS:
        if name in parts and name != "previous_layout":
            out[name] = parts[name]
    if "scratch" in out:
        out["scratch"] = {**out["scratch"], "live_runs": len(active_managers)}
    if previous:
        out["previous_layout"] = {**parts.get("previous_layout", {}), "paths": previous,
                                  "note": "From the previous storage layout: no longer used, and can be "
                                          "removed by hand."}
    return out
