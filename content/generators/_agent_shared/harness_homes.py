# SPDX-License-Identifier: MIT
"""Private per-cell harness homes and bounded, credential-free session copies."""
from __future__ import annotations
import atexit
import contextlib
import fcntl
import fnmatch
import json
import os
import shutil
import stat
import tempfile
import threading
import gen
from collimate import fs
from .harnesses import HARNESS
from .logging import safe_log as _log

def _cache_root() -> str:
    """The Collimate-owned host cache root for adapter installs and session
    history. Credential-bearing homes live in the private run workspace."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "collimate")


def _private_cache_dir(root: str, path: str) -> None:
    """Create ``path`` and every level from ``root`` down to it as owner-only
    (0700), tightening levels that already exist.

    Session state is copied with its vendor modes (often 0644) and can quote
    prompts, tool output or pasted keys; a 0700 tree keeps other local users
    out whatever the files' own modes are. Only ``root`` and what lies below
    it are touched: a path outside it gets its own directory made private and
    nothing above it is ever changed."""
    root = os.path.abspath(root)
    path = os.path.abspath(path)
    levels = [path]
    if os.path.commonpath([root, path]) == root:
        levels = [root]
        relative = os.path.relpath(path, root)
        if relative != ".":
            for part in relative.split(os.sep):
                levels.append(os.path.join(levels[-1], part))
    for level in levels:
        os.makedirs(level, mode=0o700, exist_ok=True)
        if not os.path.islink(level):
            os.chmod(level, 0o700)


def cell_key() -> str:
    """A stable per-cell key for the harness home: the run's ``cell_id`` for
    a run in a cell (several runs of one cell share it, serialized by the
    state dir's lock); a run (fresh cell, no continuity to
    keep) falls back to its run id — a per-run home that is simply never
    resumed."""
    info = gen.run_info()
    return info.cell_id or f"run-{info.run_id or 'dev'}"


# The harness session surfaces kept in the cell's session store between runs
# (the harness's own history; no run loads a previous run's session on its
# own — ``_agent/session.json`` is a run log, never carried into a later
# run). Vendor homes also
# contain credentials and config files with API keys; those stay in the
# disposable runtime home and can never enter the session cache.
_SESSION_PATHS = {name: tuple(row.session.paths) for name, row in HARNESS.items() if row.session.paths}
# A harness whose own process runs on this machine while its commands run in
# a sandbox container that mounts the run workspace (``Commands.sandboxed``)
# keeps its home outside the workspace, under the host cache, which no
# container mounts: a sandboxed command never reaches the files the host
# process reads and writes (a link planted in its session store would turn
# the next save into a write anywhere on this machine).
_OUTSIDE_WORKSPACE = frozenset(name for name, row in HARNESS.items() if row.commands.sandboxed)
_HARNESS_HOMES = {}
_HARNESS_HOME_LOCK = threading.RLock()
# Backends already reported as having no _SESSION_PATHS row (once per run).
_NO_SESSION_PATHS_LOGGED: set = set()


def _session_paths(backend: str) -> tuple:
    """``backend``'s session surfaces (:data:`_SESSION_PATHS`). A backend with
    no row keeps no session history in the cell's store; that is logged once
    per run rather than left silent."""
    paths = _SESSION_PATHS.get(backend)
    if paths is None:
        if backend not in _NO_SESSION_PATHS_LOGGED:
            _NO_SESSION_PATHS_LOGGED.add(backend)
            _log(f"acp[{backend}]: no session-store paths are listed for this "
                 "harness — its session history is not kept in the cell's "
                 "store after this run")
        return ()
    return paths


# Files never carried out of a vendor home, whatever directory holds them.
_CREDENTIAL_NAMES = frozenset({
    "auth.json", "credentials.json", ".credentials.json", "providers.json",
    "antigravity-oauth-token", "acp_token.json", "acp_business_token.json", ".env",
})
_O_NOCTTY = getattr(os, "O_NOCTTY", 0)


def _standing_grant(backend: str, name: str) -> bool:
    """A harness file that stores "always allow" grants — never carried to a
    later run, where it would approve commands the grant says to ask about."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in HARNESS[backend].session.standing_grants) if backend in HARNESS else False


def _open_entry(dir_fd: int, name: str) -> "tuple | None":
    """``(fd, stat)`` for ``name`` inside ``dir_fd`` when it is a directory
    or a regular file, else None (a symlink, a FIFO, a device, or gone).

    Never follows a link: the entry is lstat'ed through ``dir_fd``, opened
    with ``O_NOFOLLOW``, and the opened fd must be the inode lstat saw — an
    entry swapped between the two calls is skipped, not read."""
    try:
        seen = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return None
    is_dir = stat.S_ISDIR(seen.st_mode)
    if not (is_dir or stat.S_ISREG(seen.st_mode)):
        return None
    flags = os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK | _O_NOCTTY
    try:
        fd = os.open(name, flags | (fs.O_DIRECTORY if is_dir else 0), dir_fd=dir_fd)
    except OSError:
        return None
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (seen.st_dev, seen.st_ino):
        os.close(fd)
        return None
    os.set_blocking(fd, True)
    return fd, st


# The temp file a copy writes before renaming it over its target. A copy cut
# by a kill leaves one behind; directory copies never carry it.
_COPY_TMP_SUFFIX = ".collimate-tmp"


def _append_jsonl(fd: int, st: os.stat_result, dst: str, have: os.stat_result) -> bool:
    """Append a grown JSONL only when its last copied block still matches.

    A partial append is rolled back on an ordinary failure; a killed copy is
    detected by the missing record boundary and atomically replaced next time.
    Database files never enter this path.
    """
    size = have.st_size
    if not dst.endswith(".jsonl") or not (0 < size < st.st_size):
        return False
    out_fd = os.open(dst, os.O_RDWR | fs.O_NOFOLLOW)
    try:
        opened = os.fstat(out_fd)
        if (opened.st_ino, opened.st_size, opened.st_mtime_ns) != (have.st_ino, size, have.st_mtime_ns):
            return False
        block_start = max(0, size - 65536)
        before = os.pread(out_fd, size - block_start, block_start)
        if not before.endswith(b"\n") or before != os.pread(fd, len(before), block_start):
            return False
        # Copy exactly the bytes observed at open, even while a harness writes.
        class Suffix:
            def __init__(self, source):
                self.source = source
                self.remaining = st.st_size - size

            def read(self, n=-1):
                n = self.remaining if n < 0 else min(n, self.remaining)
                value = self.source.read(n)
                self.remaining -= len(value)
                return value

        try:
            with os.fdopen(os.dup(fd), "rb") as source, os.fdopen(os.dup(out_fd), "r+b") as out:
                source.seek(size)
                out.seek(size)
                shutil.copyfileobj(Suffix(source), out)
                out.flush()
                now = os.fstat(fd)
                if (now.st_size, now.st_mtime_ns, now.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                    os.ftruncate(out_fd, size)
                    return False
                os.fchmod(out_fd, stat.S_IMODE(st.st_mode) & 0o777)
                os.utime(out_fd, ns=(st.st_atime_ns, st.st_mtime_ns))
            return True
        except BaseException:
            os.ftruncate(out_fd, size)
            raise
    finally:
        os.close(out_fd)


def _stage_file(fd: int, st: os.stat_result, dst: str) -> "str | None":
    """Write the open regular file ``fd`` to a temp file beside ``dst`` with
    its permission bits and times, and return the temp's path; None when
    ``dst`` already holds it unchanged (the times are copied, so size + mtime
    identify it) — the per-turn sync then costs only what the turn wrote.

    Only a group commit (:func:`_commit_group`) renames the temp over
    ``dst``, so a kill mid-copy (the per-turn sync runs because one can land
    at any time) leaves the previous full copy in place, never a truncated
    one. A directory at ``dst`` (the source swapped one for a file) raises
    :class:`IsADirectoryError` before anything is written: no rename could
    replace it."""
    from .session_scrub import stage_text
    handled, sanitized = stage_text(fd, st, dst)
    if handled:
        return sanitized
    try:
        have = os.stat(dst, follow_symlinks=False)
    except OSError:
        have = None
    if have is not None and stat.S_ISDIR(have.st_mode):
        raise IsADirectoryError(f"a directory holds the place of {dst}")
    if (have is not None and stat.S_ISREG(have.st_mode)
            and have.st_size == st.st_size and have.st_mtime_ns == st.st_mtime_ns):
        return None
    if (have is not None and stat.S_ISREG(have.st_mode)
            and _append_jsonl(fd, st, dst, have)):
        return None
    os.lseek(fd, 0, os.SEEK_SET)
    tmp = f"{dst}.{os.getpid()}{_COPY_TMP_SUFFIX}"
    with contextlib.suppress(OSError):
        os.unlink(tmp)
    out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(os.dup(fd), "rb") as src, os.fdopen(out_fd, "wb") as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fchmod(out.fileno(), stat.S_IMODE(st.st_mode) & 0o777)
            os.utime(out.fileno(), ns=(st.st_atime_ns, st.st_mtime_ns))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _discard(staged: list) -> None:
    """Remove the temps of a group that is not committed."""
    for tmp, _dst in staged:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _is_regular(path: str) -> bool:
    try:
        return stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return False


# SQLite's sidecars: the write-ahead log, its shared-memory index and the
# rollback journal. SQLite replays a -wal (or rolls back a -journal) over
# whatever database sits beside it, so a sidecar from another moment than
# its database loses committed rows. A regular file and its regular-file
# sidecars are copied as one unit (:func:`_copy_file_group`).
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# How many times one sync copies a database that keeps changing while its
# group is copied, before it keeps the previous copy for the next sync.
_GROUP_COPY_ATTEMPTS = 3
# The group commit in flight in a copy's target (the state dir, or a home
# being seeded): the renames and removals that swap a database and its
# sidecars in as one unit. Written once every temp it names is complete and
# removed once applied, so a kill in between leaves it for the next copy
# into or out of that state dir to finish (:func:`_finish_group_commit`).
# Outside every _SESSION_PATHS entry, so the copy never carries it.
GROUP_COMMIT_FILE = ".collimate-commit"


def _apply_group(replace: list, remove: list) -> None:
    """Rename each staged temp over its target and remove each stale
    sidecar (a regular file only: a directory or link of that name is not
    one). Idempotent: a temp already renamed is gone, and so is a sidecar
    already removed."""
    for tmp, dst in replace:
        with contextlib.suppress(FileNotFoundError):
            os.replace(tmp, dst)
    for path in remove:
        if _is_regular(path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


def _commit_group(root: str, replace: list, remove: list) -> None:
    """Swap one group's staged temps (``[(temp, target)]``) in and its stale
    sidecars out. A single change is one rename or unlink, atomic by itself;
    several are first recorded in ``root``'s :data:`GROUP_COMMIT_FILE`, so a
    kill partway leaves the record for the next copy to finish — never a
    database beside a log from another moment."""
    if len(replace) + len(remove) < 2:
        try:
            _apply_group(replace, remove)
        except BaseException:
            _discard(replace)
            raise
        return
    marker = os.path.join(root, GROUP_COMMIT_FILE)
    tmp = f"{marker}.{os.getpid()}{_COPY_TMP_SUFFIX}"
    try:
        record = json.dumps({
            "replace": [[os.path.relpath(temp, root), os.path.relpath(dst, root)]
                        for temp, dst in replace],
            "remove": [os.path.relpath(path, root) for path in remove],
        }).encode()
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(record)
        os.replace(tmp, marker)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        _discard(replace)
        raise
    # The record now stands for the group: a failure from here leaves it,
    # and the temps it names, for the next copy to finish.
    _apply_group(replace, remove)
    os.unlink(marker)


def _inside(root: str, relative) -> str:
    """``relative`` joined under ``root``; :class:`ValueError` for a path
    that would leave it."""
    if (not isinstance(relative, str) or not relative or os.path.isabs(relative)
            or os.pardir in relative.split(os.sep)):
        raise ValueError(f"not a path inside the store: {relative!r}")
    return os.path.join(root, relative)


def _finish_group_commit(root: str) -> None:
    """Finish the group commit a kill cut short in ``root``: every temp its
    record names was complete before the record was written, so the record
    is applied again, then removed. A record that cannot be read or applied
    is logged and removed — kept, it would fail every later copy."""
    marker = os.path.join(root, GROUP_COMMIT_FILE)
    try:
        fd = os.open(marker, os.O_RDONLY | fs.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        with os.fdopen(fd, "rb") as fh:
            record = json.loads(fh.read())
        replace = [(_inside(root, temp), _inside(root, dst))
                   for temp, dst in record["replace"]]
        remove = [_inside(root, path) for path in record["remove"]]
        _apply_group(replace, remove)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        _log(f"the session store's unfinished group commit in {root} could "
             f"not be finished ({exc}); dropping it")
    os.unlink(marker)


def _copy_file_group(parent_fd: int, name: str, fd: int, st: os.stat_result,
                     dst: str, root: str, listed: "frozenset | None" = None) -> set:
    """Copy the regular file ``name`` in ``parent_fd`` (open as ``fd``,
    lstat'ed as ``st``) to ``dst`` together with its regular-file SQLite
    sidecars (:data:`_SQLITE_SIDECARS`), so the target never pairs a
    database with a log from another moment. Returns the names of the
    sidecars it carried, which the caller's walk then skips.

    * What changed is staged first and swapped in by one group commit
      (:func:`_commit_group`, recorded in ``root``, the copy's target): a
      kill while staging leaves the previous group whole, and one
      mid-commit leaves a record the next copy finishes.
    * When the database changed, a sidecar the source no longer has is
      removed from the target — a harness that closes cleanly checkpoints
      its -wal into the database and deletes it.
    * Once its group is staged, the database is lstat'ed again. The
      per-turn sync runs with the harness live, and a checkpoint landing
      between the database and log copies pairs a database that lacks the
      checkpointed pages with a log that no longer holds them. A database
      that moved has its staged group dropped and copied again, at most
      :data:`_GROUP_COPY_ATTEMPTS` times; after that the previous group is
      kept for the next sync.

    ``listed`` is the source dir's listing: on the first attempt it tells
    which sidecars exist without a syscall; without it, and on a retry,
    each is lstat'ed. Beyond the file's own copy, an unchanged file with no
    sidecars costs nothing, and a changed one an lstat of each possible
    sidecar in the target plus one more of the database."""
    from .secret_hygiene import refresh, snapshot
    from .session_scrub import opaque, contaminated
    refresh()
    carried: set = set()
    is_opaque = snapshot()[1] is not None and opaque(fd, name)
    for attempt in range(_GROUP_COPY_ATTEMPTS):
        staged: list = []                  # [(temp, target)]
        remove: list = []
        with contextlib.ExitStack() as held:
            if attempt:
                listed = None
                entry = _open_entry(parent_fd, name)
                if entry is None:
                    return carried
                fd, st = entry
                held.callback(os.close, fd)
                if not stat.S_ISREG(st.st_mode):
                    return carried
            try:
                tmp = _stage_file(fd, st, dst)
                changed = tmp is not None
                if changed:
                    staged.append((tmp, dst))
                for suffix in _SQLITE_SIDECARS:
                    side = name + suffix
                    entry = (_open_entry(parent_fd, side)
                             if listed is None or side in listed else None)
                    if entry is None or not stat.S_ISREG(entry[1].st_mode):
                        if entry is not None:
                            os.close(entry[0])
                        if changed and _is_regular(dst + suffix):
                            remove.append(dst + suffix)
                        continue
                    carried.add(side)
                    side_fd, side_st = entry
                    try:
                        tmp = _stage_file(side_fd, side_st, dst + suffix)
                    finally:
                        os.close(side_fd)
                    if tmp is not None:
                        staged.append((tmp, dst + suffix))
            except IsADirectoryError as exc:
                _discard(staged)
                _log(f"the session store's {name} was not copied ({exc})")
                return carried
            except BaseException:
                _discard(staged)
                raise
        if is_opaque:
            # Scan the exact staged bytes to be published, not a source that
            # can change between the scan and the copy. Source sidecars are
            # still opened only once inside the existing verified group.
            replacements = {target: temp for temp, target in staged}
            dirty = False
            try:
                for target in [dst] + [dst + suffix for suffix in _SQLITE_SIDECARS]:
                    if target in remove:
                        continue
                    path = replacements.get(target, target)
                    try:
                        check = os.open(path, os.O_RDONLY | fs.O_NOFOLLOW)
                    except FileNotFoundError:
                        continue
                    try:
                        dirty = contaminated(check) or dirty
                    finally:
                        os.close(check)
            except BaseException:
                _discard(staged)
                raise
            if dirty:
                _discard(staged)
                _commit_group(root, [], [dst] + [dst + suffix for suffix in _SQLITE_SIDECARS])
                group = os.path.relpath(dst, root)
                _log(f"{group}: omitted session cache group containing a selected credential; resume history may be unavailable")
                return carried
        if not staged and not remove:
            return carried
        try:
            now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            now = None
        if now is not None and ((now.st_ino, now.st_size, now.st_mtime_ns)
                                == (st.st_ino, st.st_size, st.st_mtime_ns)):
            _commit_group(root, staged, remove)
            return carried
        _discard(staged)
        if now is None:
            return carried                 # gone: the previous copy stays
    _log(f"the session store's {name} kept changing while it was copied; its "
         "previous copy is kept and the next sync copies it again")
    return carried


def _left_out(backend: str, name: str) -> bool:
    """Whether the walk leaves ``name`` behind: a credential or a standing
    grant, a SQLite sidecar of one (it holds that file's pages), or a temp
    a cut copy left."""
    if name.endswith(_COPY_TMP_SUFFIX):
        return True
    bases = [name] + [name[:-len(suffix)] for suffix in _SQLITE_SIDECARS
                      if name.endswith(suffix)]
    return any(base in _CREDENTIAL_NAMES or _standing_grant(backend, base)
               for base in bases)


def _copy_dir_fd(dir_fd: int, dst: str, backend: str, root: str) -> None:
    """Copy the open directory ``dir_fd`` into ``dst`` (inside ``root``, the
    copy's target): directories and regular files only, each reached through
    its parent's fd, credentials and standing grants left out, each file
    with its SQLite sidecars."""
    os.makedirs(dst, exist_ok=True)
    names = sorted(os.listdir(dir_fd))
    listed = frozenset(names)
    # Sidecars already copied with their database, which sorts before them.
    carried: set = set()
    for name in names:
        if name in carried or _left_out(backend, name):
            continue
        entry = _open_entry(dir_fd, name)
        if entry is None:
            continue
        fd, st = entry
        try:
            if stat.S_ISDIR(st.st_mode):
                _copy_dir_fd(fd, os.path.join(dst, name), backend, root)
            else:
                carried |= _copy_file_group(dir_fd, name, fd, st,
                                            os.path.join(dst, name), root, listed)
        finally:
            os.close(fd)


def _copy_session_state(source: str, target: str, backend: str) -> None:
    """Copy ``backend``'s session surfaces (:data:`_SESSION_PATHS`) from
    ``source`` to ``target``, first finishing a group commit a kill cut
    short in ``target`` (:func:`_finish_group_commit`).

    The copy adds and replaces, and removes only a SQLite sidecar its
    database no longer has (:func:`_copy_file_group`): several runs of one
    cell copy their homes back into one state dir, so a file one home lacks
    may be another run's session.

    A container harness owns its bind-mounted home and can plant a link at
    any level, or swap a directory for one while this copy runs (the turn
    boundary sync runs with the container still up). So the walk is
    fd-based: every component is opened relative to its parent's fd with
    ``O_NOFOLLOW`` and copied from that fd, and no path is ever re-resolved
    — nothing outside ``source`` can be reached. Without ``dir_fd`` support
    no session state is carried."""
    if not fs.FD_SAFE:
        return
    _finish_group_commit(target)
    for relative in _session_paths(backend):
        parts = relative.split("/")
        try:
            parent = fs.open_parent(source, parts, create=False)
        except OSError:
            continue
        try:
            entry = _open_entry(parent, parts[-1])
            if entry is None:
                continue
            fd, st = entry
            dst = os.path.join(target, *parts)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if stat.S_ISDIR(st.st_mode):
                    _copy_dir_fd(fd, dst, backend, target)
                else:
                    _copy_file_group(parent, parts[-1], fd, st, dst, target)
            finally:
                os.close(fd)
        finally:
            os.close(parent)


# The temp homes carry the owning process's pid, so a later run can tell a
# home orphaned by a SIGKILL (Force Stop, or Stop's escalation — neither runs
# close_harness_homes) from one a concurrent run is still using.
_HOME_PREFIX = "collimate-harness-"


def _sweep_orphaned_homes(root: str) -> None:
    """Remove temp homes whose process is gone. Their session state was
    already persisted at each turn boundary (sync_harness_homes); what is
    left is per-run config and credential mountpoints nobody will reap."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(_HOME_PREFIX):
            continue
        pid = name[len(_HOME_PREFIX):].split("-", 1)[0]
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            os.kill(int(pid), 0)
            continue                       # still running: not ours to touch
        except ProcessLookupError:
            pass
        except OSError:
            continue                       # exists under another user
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _state_dir(backend: str) -> "tuple[str, str]":
    """``(cache root, state dir)`` for this cell's ``backend``: the host-side
    ``<cache>/agent-state/<cell>/<backend>/`` that keeps the harness's session
    store between runs. It is never mounted into a container — a run's
    harness home is a disposable copy of the listed session surfaces."""
    root = _cache_root()
    return root, os.path.join(root, "agent-state", cell_key(), backend)


# The lock file of a shared cache dir. Several runs of one cell can run the
# same backend at once, and each copies its session store back into the
# shared state dir and rewrites the resume record there; several runs can
# also reach a host adapter's first install into one prefix at once
# (ensure_adapter). Outside every _SESSION_PATHS entry, so the copy never
# carries it.
STATE_LOCK_FILE = ".lock"


@contextlib.contextmanager
def _state_lock(root: str, state: str, *, shared: bool = False):
    """Hold the exclusive ``flock`` on the lock file in ``state`` (a cache
    dir under ``root``) for a block — a read-modify-write of the resume
    record, a store copy-back, or an adapter install — so two concurrent
    runs never interleave them; ``shared`` holders run side by side and
    exclude only an exclusive one (an adapter preflight beside an install).
    Creates ``state`` owner-only from ``root`` down; raises
    :class:`OSError` when it cannot."""
    _private_cache_dir(root, state)
    fd = os.open(os.path.join(state, STATE_LOCK_FILE),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                       # closing the fd releases the lock


_CACHE_SCRUB_REVISION = {}


def _scrub_cached_state(state, backend):
    """Once per selected-token revision, sanitize even older cached sessions.

    Caller holds the existing state lock. Self-copy keeps the same no-follow
    whitelist and transaction machinery, while removing contaminated opaque
    groups before they can seed a new home or survive an unrelated sync.
    """
    from .secret_hygiene import refresh, snapshot
    refresh()
    revision, pattern, _width = snapshot()
    key = (state, backend)
    if pattern is not None and _CACHE_SCRUB_REVISION.get(key) != revision:
        _copy_session_state(state, state, backend)
        _CACHE_SCRUB_REVISION[key] = revision


def harness_home(backend: str) -> str:
    """Disposable vendor home with explicitly persisted nonsecret session state."""
    from collimate.fs import open_parent

    workspace = os.path.realpath(gen.workspace_path())
    key = (workspace, cell_key(), backend)
    with _HARNESS_HOME_LOCK:
        if key not in _HARNESS_HOMES:
            if backend in _OUTSIDE_WORKSPACE:
                homes = os.path.join(_cache_root(), "homes")
                _private_cache_dir(_cache_root(), homes)
            else:
                # Refuse a planted symlink at either private directory. Public
                # workspace/export APIs omit .collrun; the host preserves its modes.
                for parts in ([".collrun", "placeholder"],
                              [".collrun", "homes", "placeholder"]):
                    fd = open_parent(workspace, parts, create=True)
                    try:
                        os.fchmod(fd, 0o700)
                    finally:
                        os.close(fd)
                homes = os.path.join(workspace, ".collrun", "homes")
            _sweep_orphaned_homes(homes)
            temporary = tempfile.TemporaryDirectory(
                prefix=f"{_HOME_PREFIX}{os.getpid()}-", dir=homes)
            root, state = _state_dir(backend)
            # Seed under the state dir's lock: another run of this cell may
            # be copying its store back into it right now. A cache the lock
            # cannot be taken in is seeded unlocked, as before. A copy-back a
            # kill cut short mid-commit is finished before the store is read.
            with contextlib.ExitStack() as held:
                try:
                    held.enter_context(_state_lock(root, state))
                except OSError:
                    pass
                else:
                    _finish_group_commit(state)
                _scrub_cached_state(state, backend)
                _copy_session_state(state, temporary.name, backend)
            # The root is fixed here: the environment may differ by the time
            # the home is closed (atexit), and the tree to tighten is this one.
            _HARNESS_HOMES[key] = (temporary, state, root)
        return _HARNESS_HOMES[key][0].name


# The loosest commands level each of this cell's sessions may have run under,
# kept by the generator in the host-side state dir: the resume decision's
# source (resumable_acp_session_id). The name is outside every _SESSION_PATHS
# entry, so the store copy-back never overwrites it, and the state dir is never
# mounted, so the agent cannot write it. ``{"sessions": {session id: level}}``,
# newest last, the oldest dropped past _RESUME_LEVELS_KEEP.
RESUME_LEVELS_FILE = "collimate-resume-levels.json"
_RESUME_LEVELS_KEEP = 64


def _resume_levels(backend: str) -> dict:
    """``{session id: commands level}`` from this cell's record, or ``{}``."""
    _root, state = _state_dir(backend)
    try:
        with open(os.path.join(state, RESUME_LEVELS_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return dict(sessions) if isinstance(sessions, dict) else {}


def _level_rank(level, *, unknown: int) -> int:
    """``level``'s index in the commands ladder (strictest first), or
    ``unknown`` for a value that is not a level."""
    from collimate.permissions import COMMAND_LEVELS

    return COMMAND_LEVELS.index(level) if level in COMMAND_LEVELS else unknown


def _looser_level(a: str, b: str) -> str:
    """The looser of two commands levels; a value that is not a level counts
    as the loosest."""
    from collimate.permissions import COMMAND_LEVELS

    loosest = len(COMMAND_LEVELS) - 1
    return COMMAND_LEVELS[max(_level_rank(a, unknown=loosest),
                              _level_rank(b, unknown=loosest))]


def record_resume_level(backend: str, harness_session_id: str, commands: str) -> None:
    """Record that a ``backend`` leg of this cell runs under ``commands``, on
    ``harness_session_id`` (new or loaded; "" for a leg with no session id).

    A leg can write the whole session store, not only its own session (an
    allow leg can rewrite another session's stored extensions), so every
    recorded session is raised to at least ``commands`` and ``harness_session_id`` is
    recorded at ``commands``, newest last. The record is the resume
    decision's source, so it fails closed: a record that cannot be written is
    removed, which leaves every session unknown and never resumed at a
    stricter level, and one that cannot be removed either raises, so the leg
    does not start."""
    from collimate.permissions import COMMAND_LEVELS

    root, state = _state_dir(backend)
    # Not a level: the loosest.
    level = COMMAND_LEVELS[_level_rank(commands, unknown=len(COMMAND_LEVELS) - 1)]
    path = os.path.join(state, RESUME_LEVELS_FILE)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        # Read, raise and rewrite under the state dir's lock: another run of
        # this cell on the same backend may be recording its own leg.
        with _state_lock(root, state):
            levels = {sid: _looser_level(stored, level)
                      for sid, stored in _resume_levels(backend).items()}
            if harness_session_id:
                levels.pop(harness_session_id, None)
                levels[harness_session_id] = level
            while len(levels) > _RESUME_LEVELS_KEEP:
                levels.pop(next(iter(levels)))
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"sessions": levels}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        return
    except OSError as exc:
        _log(f"acp[{backend}]: could not record the commands level of this "
             f"cell's sessions ({exc}) — dropping the record, so no earlier "
             "session is resumed")
    for leftover in (tmp, path):
        with contextlib.suppress(OSError):
            os.unlink(leftover)
    if os.path.lexists(path):
        raise RuntimeError(
            f"the {backend} resume record {path} could neither be updated nor "
            "removed — refusing to run a leg its resume decision would not see")


def sync_harness_homes() -> None:
    """Persist every open home's session state now, without closing it. Run
    at each turn boundary: a SIGKILL never reaches close_harness_homes, and
    the cell's session store would otherwise miss this run's session.
    Best-effort — a failed copy never fails the turn."""
    with _HARNESS_HOME_LOCK:
        for (_, _, backend), (temporary, state, root) in list(_HARNESS_HOMES.items()):
            try:
                with _state_lock(root, state):
                    _scrub_cached_state(state, backend)
                    _copy_session_state(temporary.name, state, backend)
            except Exception as exc:  # noqa: BLE001 — best-effort, never fails the turn
                _log(f"acp[{backend}]: this turn's session store was not "
                     f"persisted ({exc})")


def close_harness_homes() -> None:
    """Call after CLI containers stop; no credentials are copied to the cache."""
    with _HARNESS_HOME_LOCK:
        try:
            for (_, _, backend), (temporary, state, root) in _HARNESS_HOMES.items():
                try:
                    with _state_lock(root, state):
                        _scrub_cached_state(state, backend)
                        _copy_session_state(temporary.name, state, backend)
                except Exception as exc:  # noqa: BLE001 — teardown never raises
                    _log(f"acp[{backend}]: session store not persisted at "
                         f"close ({exc})")
                finally:
                    with contextlib.suppress(OSError):
                        temporary.cleanup()
        finally:
            _HARNESS_HOMES.clear()


atexit.register(close_harness_homes)
