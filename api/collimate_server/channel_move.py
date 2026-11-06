"""Moving a channel's folder to another location (plan 1105 S7).

``POST /api/channels/{id}/move {location_id}`` checks every refusal at
once (:func:`start`), marks the channel moving and answers a job that
``GET /api/storage/jobs/{job_id}`` reports. One move runs per server; a
finished job stays readable for ten minutes.

The job (:func:`run`):

1. Checkpoint the channel's DB (TRUNCATE) and wait for its collection.
2. **Same filesystem**: under ``channel_files_lock`` and the store lock,
   ``os.rename(<src>/<uuid>, <dest>/<uuid>)``, fsync both parents, point
   the index at it. Done. A rename refused with EXDEV (two mounts of one
   filesystem) goes on as a copy.
3. **Copy** into ``<dest>/<uuid>.moving/``, each file and directory
   fsynced, the source's file sizes and times noted. ``channel.db-wal``
   and ``-shm`` stay behind (the WAL is empty after the checkpoint).
4. **Verify** by walking both trees: the same relative paths, kinds and
   sizes; every body under ``objects/`` hashes to its name; ``channel.db``,
   ``channel.json`` and each git pack match by sha256. A mismatch removes
   the copy and keeps the source.
5. **Switch** under both locks: the source is checked unchanged since the
   copy (and its WAL still empty); :data:`channel_paths.MOVE_RECORD` is
   written into the copy; ``<uuid>.moving`` is renamed to ``<uuid>``; the
   index points at it; the source is renamed to ``.deleting-<uuid>-<rand>``.
6. **Clean up**: the record goes, then the ``.deleting-*`` folder.

Crash recovery at startup (``channel_paths.establish``): a ``.moving`` is
removed (a crash in 3-5 before the rename), a folder with a record whose
source still exists wins and the source goes (a crash between step 5's two
renames), and a ``.deleting-*`` is removed (a crash in step 6). The folders
are the only truth, so each crash point leaves exactly one copy.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import collimate_server.channel_paths as channel_paths

logger = logging.getLogger(__name__)

#: How long a finished job stays readable.
KEEP_SECONDS = 600
#: The free space a cross-filesystem move needs beyond the folder's size.
SPACE_MARGIN = 0.05
#: Top-level files of a channel folder that never travel: SQLite's WAL
#: (empty after the TRUNCATE checkpoint) and shared-memory index, and the
#: record of an earlier move.
LEFT_BEHIND = frozenset({"channel.db-wal", "channel.db-shm", channel_paths.MOVE_RECORD})
_BODY = re.compile(r"objects/[0-9a-f]{2}/[0-9a-f]{62}")
_CHUNK = 1 << 20


class MoveRefused(Exception):
    """A move refused before it starts: 409 with the code."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


class MoveFailed(Exception):
    """A move that stopped with the source kept."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MoveJob:
    id: str
    channel_id: str
    from_location_id: str
    to_location_id: str
    bytes_total: int
    files_total: int
    # copying | verifying | switching | cleaning | done | failed
    state: str = "copying"
    #: rename (one filesystem) or copy; None until the job knows.
    method: str | None = None
    bytes_done: int = 0
    files_done: int = 0
    error: dict | None = None
    warning: str | None = None
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    finished: float | None = None
    #: The source files' (size, mtime_ns) when copied, checked at the switch.
    noted: dict[str, tuple[int, int]] = field(default_factory=dict)

    def wire(self) -> dict:
        out = {"id": self.id, "kind": "move", "channel_id": self.channel_id,
               "from_location_id": self.from_location_id, "to_location_id": self.to_location_id,
               "state": self.state, "method": self.method,
               "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
               "files_done": self.files_done, "files_total": self.files_total,
               "started_at": self.started_at, "finished_at": self.finished_at}
        if self.error is not None:
            out["error"] = self.error
        if self.warning is not None:
            out["warning"] = self.warning
        return out


_jobs: dict[str, MoveJob] = {}
_jobs_lock = threading.Lock()


def _prune() -> None:
    cutoff = time.monotonic() - KEEP_SECONDS
    for job_id in [job_id for job_id, job in _jobs.items() if job.finished is not None and job.finished < cutoff]:
        del _jobs[job_id]


def job(job_id: str) -> MoveJob | None:
    with _jobs_lock:
        _prune()
        return _jobs.get(job_id)


def active_job() -> MoveJob | None:
    with _jobs_lock:
        return next((found for found in _jobs.values() if found.finished is None), None)


def _crash_point(step: str) -> None:
    """Where a crash of the server between two durable steps would land
    (the tests stop the job here)."""


# --- sizes and trees --------------------------------------------------------------------------------


def folder_size(folder: Path) -> tuple[int, int]:
    """(bytes, files) a copy of ``folder`` carries: the regular files' sizes."""
    total = files = 0
    for entry in _walk(folder):
        if entry.kind == "f":
            total += entry.size
            files += 1
    return total, files


@dataclass(frozen=True)
class _Entry:
    rel: str
    #: d (a directory), l (a link) or f (a regular file)
    kind: str
    size: int = 0
    mtime_ns: int = 0
    target: str = ""


def _walk(top: Path):
    """Every :class:`_Entry` under ``top`` that travels, parents first.
    Sockets and other special files are not part of a channel and are
    skipped, as are the top-level :data:`LEFT_BEHIND` files."""
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        directory = top / rel_dir if rel_dir else top
        with os.scandir(directory) as entries:
            found = sorted(entries, key=lambda entry: entry.name)
        for entry in found:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            if not rel_dir and entry.name in LEFT_BEHIND:
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                yield _Entry(rel, "l", target=os.readlink(entry.path))
            elif stat.S_ISDIR(info.st_mode):
                yield _Entry(rel, "d")
                stack.append(rel)
            elif stat.S_ISREG(info.st_mode):
                yield _Entry(rel, "f", info.st_size, info.st_mtime_ns)


def _inventory(top: Path) -> dict[str, tuple]:
    """The tree's shape: each path's kind and size (or link target)."""
    return {entry.rel: (entry.kind, entry.size, entry.target) for entry in _walk(top)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    from collimate_server.cas import fsync_directory
    fsync_directory(path)


# --- refusals and the start -------------------------------------------------------------------------


def _checkpoint(channel_id: str) -> None:
    """TRUNCATE-checkpoint the channel's DB: its WAL folds into channel.db."""
    from collimate_server.database import get_db_connection
    conn = get_db_connection(channel_id)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def _same_filesystem(source: Path, destination: Path) -> bool:
    return os.stat(source).st_dev == os.stat(destination).st_dev


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def needed_bytes(size: int) -> int:
    return int(size * (1 + SPACE_MARGIN))


def check(channel_id: str, location_id: str, size: int) -> tuple[Path, channel_paths.Location]:
    """Every refusal of S7 step 1 that does not need the live state, as
    :class:`MoveRefused`: another move under way, the channel being created
    or changed, a held pin, an in-flight collection, a destination that is
    not an owned, available location or is the channel's own, a folder of
    the channel already there, too little free space on another filesystem.
    Answers the source folder and the destination."""
    from collimate_server.cas import channel_store
    from collimate_server.cas_gc import _collection_lock
    from collimate_server.channel_guard import is_moving
    from collimate_server.database import changing_channels, creating_channels
    if active_job() is not None:
        raise MoveRefused("move_in_progress", "Another channel is being moved. Wait until it is done.")
    if is_moving(channel_id) or channel_id in creating_channels or channel_id in changing_channels:
        raise MoveRefused("channel_busy", "The channel is being created, changed or moved. Try again when it is done.")
    source = channel_paths.channel_dir(channel_id)
    destination = channel_paths.location(location_id)
    if destination is None:
        raise MoveRefused("location_not_found", "No such location.", status=404)
    if destination.state == "foreign":
        raise MoveRefused("location_foreign", f"The location {destination.path} is owned by another "
                                              "installation. Claim it before moving a channel there.")
    if destination.state != "ok" or not destination.path.is_dir():
        raise MoveRefused("location_missing", f"The location {destination.path} is not available.")
    if not os.access(destination.path, os.W_OK | os.X_OK):
        raise MoveRefused("location_unwritable", f"The location {destination.path} is not writable.")
    if channel_paths.home_location(channel_id).id == location_id:
        raise MoveRefused("same_location", "The channel is already in that location.")
    if (destination.path / channel_id).exists():
        raise MoveRefused("destination_exists", f"{destination.path / channel_id} already exists.")
    store = channel_store(channel_id)
    if store.holds_pins():
        raise MoveRefused("pins_held", "An export of this channel is in progress. Try again when it is done.")
    collecting = _collection_lock(store)
    if not collecting.acquire(blocking=False):
        raise MoveRefused("collection_in_flight", "The channel's storage is being collected. Try again in a moment.")
    collecting.release()
    if not _same_filesystem(source.parent, destination.path):
        free = _free_bytes(destination.path)
        if free < needed_bytes(size):
            raise MoveRefused("insufficient_space",
                              f"The location {destination.path} has {free} bytes free; the channel needs "
                              f"{needed_bytes(size)} (its size plus 5 %).")
    return source, destination


def begin(channel_id: str, location_id: str, size: int, files: int) -> MoveJob:
    """After :func:`check` and the live-run check, with no await between:
    mark the channel moving (``channel_guard`` refuses its writes, and it is
    in ``changing_channels``, so collection, the git store and the workspace
    sweep leave it alone) and register the job."""
    from collimate_server.channel_guard import mark_moving
    from collimate_server.database import changing_channels
    with _jobs_lock:
        if any(found.finished is None for found in _jobs.values()):
            raise MoveRefused("move_in_progress", "Another channel is being moved. Wait until it is done.")
        if not mark_moving(channel_id):
            raise MoveRefused("channel_busy", "The channel is already being moved.")
        changing_channels.add(channel_id)
        found = MoveJob(str(uuid.uuid4()), channel_id, channel_paths.home_location(channel_id).id,
                        location_id, size, files)
        _jobs[found.id] = found
    return found


def start_thread(found: MoveJob) -> threading.Thread:
    thread = threading.Thread(target=run, args=(found,), name=f"move-{found.channel_id[:8]}", daemon=True)
    thread.start()
    return thread


# --- the job ----------------------------------------------------------------------------------------


def run(found: MoveJob) -> None:
    """The move itself, in a worker thread. Every failure ends the job
    ``failed`` with the source kept (and any copy removed)."""
    staging: Path | None = None
    switched = False
    try:
        channel_id = found.channel_id
        _checkpoint(channel_id)
        _wait_for_collection(channel_id)
        source = channel_paths.channel_dir(channel_id)
        destination = channel_paths.location(found.to_location_id)
        if destination is None or destination.state != "ok":
            raise MoveFailed("location_missing", "The destination location went away.")
        final = destination.path / channel_id
        if _same_filesystem(source.parent, destination.path):
            found.method = "rename"
            try:
                _rename(found, source, final)
                switched = True
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
        if not switched:
            found.method = "copy"
            free = _free_bytes(destination.path)
            if free < needed_bytes(found.bytes_total):
                raise MoveFailed("insufficient_space",
                                 f"The location {destination.path} has {free} bytes free; the channel needs "
                                 f"{needed_bytes(found.bytes_total)}.")
            staging = destination.path / f"{channel_id}{channel_paths.STAGING_SUFFIX}"
            _copy(found, source, staging)
            _crash_point("copied")
            found.state = "verifying"
            _verify(source, staging)
            _crash_point("verified")
            found.state = "switching"
            retired = _switch(found, source, staging, final)
            switched = True
            staging = None
            found.state = "cleaning"
            _clean(found, final, retired)
        found.state = "done"
    except Exception as exc:
        code = getattr(exc, "code", None) or ("io_error" if isinstance(exc, OSError) else "move_failed")
        logger.warning("moving the channel %s failed (%s): %s", found.channel_id, code, exc)
        found.state, found.error = "failed", {"code": code, "message": str(exc)}
        if staging is not None and not switched:
            import collimate_server.readonly_trees as readonly_trees
            readonly_trees.remove_tree(str(staging), ignore_errors=True)
    finally:
        _release(found)


def _wait_for_collection(channel_id: str) -> None:
    from collimate_server.cas import channel_store
    from collimate_server.cas_gc import _collection_lock
    with _collection_lock(channel_store(channel_id)):
        pass


def _release(found: MoveJob) -> None:
    from collimate_server.channel_guard import clear_moving
    from collimate_server.database import changing_channels
    changing_channels.discard(found.channel_id)
    clear_moving(found.channel_id)
    with _jobs_lock:
        found.finished_at, found.finished = _now(), time.monotonic()
    try:
        from collimate_server.state import persist_state
        persist_state()
    except Exception:
        logger.exception("persisting after the move of %s failed", found.channel_id)


@contextlib.contextmanager
def _locks(channel_id: str):
    """``channel_files_lock`` then the store lock (the plan's lock order)."""
    import collimate_server.git_store as git_store
    from collimate_server.database import channel_files_lock
    with channel_files_lock, git_store.store_lock(channel_id):
        yield


def _forget(channel_id: str) -> None:
    from collimate_server.cell_metadata import forget_layout
    from collimate_server.database import forget_db
    forget_db(channel_id)
    forget_layout(channel_id)


def _rename(found: MoveJob, source: Path, final: Path) -> None:
    """Step 3: one rename on one filesystem."""
    found.state = "switching"
    with _locks(found.channel_id):
        _checkpoint(found.channel_id)
        _forget(found.channel_id)
        os.rename(source, final)
        _fsync_dir(final.parent)
        _fsync_dir(source.parent)
        channel_paths.relocate(found.channel_id, final, found.to_location_id)
    found.bytes_done, found.files_done = found.bytes_total, found.files_total


def _copy(found: MoveJob, source: Path, staging: Path) -> None:
    """Step 4's copy: every travelling entry into ``staging``, each file
    fsynced as written, each directory fsynced once filled and given its
    source's mode last (a read-only git directory stays writable until its
    files are in)."""
    import collimate_server.readonly_trees as readonly_trees
    if os.path.lexists(staging):
        readonly_trees.remove_tree(str(staging))
    staging.mkdir(mode=0o700)
    _fsync_dir(staging.parent)
    found.state = "copying"
    directories: list[tuple[Path, int]] = [(staging, os.stat(source).st_mode)]
    for entry in _walk(source):
        origin, target = source / entry.rel, staging / entry.rel
        if entry.kind == "d":
            target.mkdir(mode=0o700)
            directories.append((target, os.lstat(origin).st_mode))
        elif entry.kind == "l":
            os.symlink(entry.target, target)
        else:
            found.noted[entry.rel] = (entry.size, entry.mtime_ns)
            with open(origin, "rb") as reader, open(target, "xb") as writer:
                for chunk in iter(lambda: reader.read(_CHUNK), b""):
                    writer.write(chunk)
                    found.bytes_done += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(target, stat.S_IMODE(os.lstat(origin).st_mode))
            found.files_done += 1
            _crash_point("copying")
    for directory, mode in reversed(directories):
        _fsync_dir(directory)
        os.chmod(directory, stat.S_IMODE(mode) | stat.S_IRWXU)


def _verify(source: Path, copy: Path) -> None:
    """Step 4's verify: the same tree, every body hashing to its name, the
    DB, the manifest and each git pack matching the source by sha256."""
    if _inventory(source) != _inventory(copy):
        raise MoveFailed("verify_failed", "The copy does not have the source's files and sizes; the source is kept.")
    for entry in _walk(copy):
        if entry.kind != "f":
            continue
        rel = entry.rel
        if _BODY.fullmatch(rel):
            if _sha256(copy / rel) != rel[8:10] + rel[11:]:
                raise MoveFailed("verify_failed", f"The copied body {rel} does not hash to its name; "
                                                  "the source is kept.")
        elif rel in ("channel.db", channel_paths.MANIFEST) or (rel.startswith("git/") and rel.endswith(".pack")):
            if _sha256(copy / rel) != _sha256(source / rel):
                raise MoveFailed("verify_failed", f"The copied {rel} differs from the source; the source is kept.")


def _unchanged(found: MoveJob, source: Path) -> bool:
    """Whether the source's files are those the copy read: none added,
    removed, resized or rewritten since."""
    now = {entry.rel: (entry.size, entry.mtime_ns) for entry in _walk(source) if entry.kind == "f"}
    if now != found.noted:
        return False
    wal = source / "channel.db-wal"
    return not wal.exists() or wal.stat().st_size == 0


def _switch(found: MoveJob, source: Path, staging: Path, final: Path) -> Path:
    """Step 5, under both locks: the finished copy becomes the channel and
    the source is renamed out of the way, so outside the lock at most one
    non-dotted copy exists and a crash in between leaves the record that
    makes the copy win."""
    with _locks(found.channel_id):
        _checkpoint(found.channel_id)
        if not _unchanged(found, source):
            raise MoveFailed("channel_changed", "The channel changed while it was copied; the source is kept. "
                                                "Move it again.")
        if os.path.lexists(final):
            raise MoveFailed("destination_exists", f"{final} appeared during the move; the source is kept.")
        channel_paths.write_json_atomic(staging / channel_paths.MOVE_RECORD, {
            "channel_id": found.channel_id, "from_location_id": found.from_location_id,
            "from_path": str(source), "to_location_id": found.to_location_id, "moved_at": _now()})
        _crash_point("recorded")
        _forget(found.channel_id)
        os.rename(staging, final)
        _fsync_dir(final.parent)
        channel_paths.relocate(found.channel_id, final, found.to_location_id)
        _crash_point("renamed_destination")
        retired = source.parent / f"{channel_paths.DELETING_PREFIX}{found.channel_id}-{secrets.token_hex(4)}"
        try:
            os.rename(source, retired)
            _fsync_dir(source.parent)
        except OSError as exc:
            found.warning = (f"The old copy {source} could not be removed ({exc}); "
                             "the next start removes it.")
            logger.warning("move of %s: %s", found.channel_id, found.warning)
            return source
        _crash_point("renamed_source")
    return retired


def _clean(found: MoveJob, final: Path, retired: Path) -> None:
    """Step 6: the record goes, then the old copy."""
    import collimate_server.readonly_trees as readonly_trees
    if retired.name.startswith(channel_paths.DELETING_PREFIX):
        (final / channel_paths.MOVE_RECORD).unlink(missing_ok=True)
        _fsync_dir(final)
        _crash_point("unrecorded")
        try:
            readonly_trees.remove_tree(str(retired))
        except OSError as exc:
            found.warning = f"The old copy {retired} could not be removed ({exc}); the next start removes it."
            logger.warning("move of %s: %s", found.channel_id, found.warning)
