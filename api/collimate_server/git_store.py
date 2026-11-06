"""The channel object store: one bare, objects-only git repository per
channel in its folder (``docs/plans/2026-10-31-git-in-cells/design.md``
§3.1, §4.8, §4.9, §7, §8).

``<channel folder>/git`` (``channel_paths.git_dir``) holds the objects
every repository of the channel's cells names, and nothing else: no refs at
rest (GC's ``refs/gc-roots/<gc-id>/*`` live for one sweep) and never a
``shallow`` file. :func:`ensure` is its only creator; rows
(``cell_repositories``) imply it. Rescued gitdirs live beside it in
``git-rescue/``. The store goes with its channel's folder.

Writes hold the store lock (:func:`store_lock`, ``channel-store:<uuid>``
in the cell-lock namespace), taken after any cell locks and never across
an await: every function here is synchronous, made for a worker thread.
Readers of committed rows (:func:`pack_for_export`, the copy's
``pack-objects``, :func:`ahead_behind`, a run's alternates) take no lock.
A harvest holds :func:`in_flight` from its walk to its row commit, and
:func:`collect` skips the channel while one does.

Ingest is the harvest's store side, one function per step it
orchestrates: :func:`quarantine` (step 1), :func:`band_guard` (step 5) and
:func:`ingest` (steps 6-8: walk, pack, ``index-pack --fsck-objects``,
check). A failure raises :class:`StoreFailed` carrying git's words, which
the harvest turns into a rescue (:func:`rescue`).

Every git process runs through :mod:`git_host`.
"""
from __future__ import annotations

import datetime
import errno
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid as uuid_module
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

from collimate import fs

import collimate_server.channel_paths as channel_paths
import collimate_server.git_host as git_host
import collimate_server.readonly_trees as readonly_trees
from collimate_server.cas import fsync_directory
from collimate_server.cell_commits import CellLock, cell_commit_lock
from collimate_server.database import canonical_channel_id, changing_channels, creating_channels
from collimate_server.repository_rows import branch_config, load_state
from collimate_server.repository_state import (
    BARE_FILES,
    NULL_OID,
    OPERATION_DIRS,
    READ_LIMIT,
    STAGED_RESOLUTIONS,
    GitdirObjects,
    Normalized,
    RepositoryState,
    format_refs,
    make_state,
    parse_refs,
    parse_reflog,
    parse_config_list,
    progress_name,
    progress_set,
    progress_shas,
    resolve_ref,
    roots,
    sanitize_config,
    scrub_bytes,
    scrub_text,
    shallow_of,
    split_config_key,
)

_logger = logging.getLogger(__name__)

# Quarantine caps (§4.8 step 1): bytes and files copied out of one run's
# objects, and the time the copy and its packs' indexing may take.
QUARANTINE_MAX_BYTES = 4 * 1024 ** 3
QUARANTINE_MAX_FILES = 100_000
QUARANTINE_SECONDS = 600.0

# The limit of one git child of an ingest, a copy, an export pack or a GC.
STORE_TIMEOUT = 3600.0

# ahead_behind's rev-list limit and the size of its cache.
AHEAD_BEHIND_TIMEOUT = 2.0
AHEAD_BEHIND_CACHE = 4096

# A rescue older than this is swept by collect (§4.9, Lifetime).
RESCUE_MAX_AGE_SECONDS = 30 * 86400
RESCUE_STDERR_CAP = 64 * 1024
RESCUE_RECORD = "rescue.json"

# The band a run's history must not hold (§4.8 step 5).
BAND = ".collrun"

_LAYOUT_DIRS = ("objects", "objects/info", "objects/pack", "refs")
_DIR_MODE = 0o755
_FILE_MODE = 0o644

_LOOSE_DIR = re.compile(r"[0-9a-f]{2}")
_LOOSE_FILE = re.compile(r"[0-9a-f]{38}")
_PACK_FILE = re.compile(r"pack-[0-9a-f]{40}\.pack")
_HEX40 = re.compile(r"[0-9a-f]{40}")
_QUARANTINE = re.compile(r"git-harvest-[0-9a-f]{8}_.*")
_STORE_LOCKS = ("packed-refs.lock", "HEAD.lock", "config.lock")
# What an interrupted index-pack (tmp_pack_*, tmp_idx_*) or repack
# (.tmp-<pid>-pack-*, which its own -d absorbs only as a .pack/.idx pair)
# leaves in objects/pack.
_PACK_TEMPORARIES = ("tmp_", ".tmp-")
# The scratch a killed process leaves in .collimate/tmp beside the
# quarantines: copy packs, shallow lists, export staging, git's temporary
# homes and the reader's and index lister's copies.
_SCRATCH = ("git-copy-", "git-shallow-", "export-", "git-home-", "git-read-", "git-index-")
_COPY_CHUNK = 1024 * 1024
# The objects an ingest's walk reached, in its quarantine, once the store
# holds them (Quarantine.landed).
_REACHED = "reached"


class StoreFailed(RuntimeError):
    """Git work over a store, a quarantine or a rescue did not succeed.
    ``reason`` is the one line a rescue, a log or a refused create shows,
    in git's words where git gave some; ``git_stderr`` is what git printed."""

    def __init__(self, reason: str, git_stderr: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.git_stderr = git_stderr


class StoreRefused(StoreFailed):
    """A store write the channel does not admit: it is being deleted or
    moved (``changing_channels``), it has no DB, or its store is not a
    directory the server user owns."""


def _git_words(exc: git_host.GitFailed) -> str:
    """``exc`` as one line: the subcommand and how it ended, then every
    ``error:`` and ``fatal:`` line git printed (an fsck id is on an error
    line before the closing fatal), or its last line."""
    if exc.returncode is None:
        return str(exc)
    told = [line.strip() for line in exc.stderr.splitlines() if line.strip()]
    marked = [line for line in told if line.startswith(("error:", "fatal:"))]
    shown = marked[-6:] if marked else told[-1:]
    sub = exc.subcommand
    head = f"git {sub} exited {exc.returncode}" if sub else f"git exited {exc.returncode}"
    return head + (": " + "; ".join(shown) if shown else "")


def _failed(what: str, exc: git_host.GitFailed) -> StoreFailed:
    return StoreFailed(f"{what}: {_git_words(exc)}", exc.stderr)


# --- paths and locks ------------------------------------------------------------------------------


def _basename(channel: str) -> str:
    """A channel DB's basename as a store name: a plain file name."""
    name = str(channel)
    if not name or name.startswith(".") or "/" in name or os.sep in name or "\0" in name:
        raise ValueError(f"not a channel basename: {name!r}")
    return name


def store_path(channel: str) -> str:
    """The absolute path of the channel's store (it may not exist): the
    path a materialized gitdir's alternates and a container bind name.
    Raises ``channel_paths.ChannelUnavailable`` for a channel with no
    available folder."""
    path = channel_paths.git_dir(_basename(channel))
    # The folder resolved, the store's own name kept: a link planted there is
    # refused by the checks, never followed.
    return os.path.join(os.path.realpath(path.parent), path.name)


def objects_path(channel: str) -> str:
    """The absolute path of the store's object directory: what a
    materialized gitdir's alternates name and a container binds read-only."""
    return os.path.join(store_path(channel), "objects")


def rescue_path(channel: str) -> str:
    """The absolute path of the channel's rescue area (it may not exist)."""
    path = channel_paths.git_rescue_dir(_basename(channel))
    return os.path.join(os.path.realpath(path.parent), path.name)


def store_lock(channel: str) -> CellLock:
    """The channel's store lock, ``channel-store:<uuid>`` in the cell-lock
    namespace: taken after any cell locks, never held across an await."""
    return cell_commit_lock(f"channel-store:{_basename(channel)}")


def _owned_directory(path: str) -> bool:
    """Whether ``path`` is a directory (not a symlink) the server user owns."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid()


def _writable(channel: str) -> str:
    """The store path, after the checks every store write makes: the
    channel is not in ``changing_channels`` and has its DB. The caller
    holds the store lock."""
    if channel in changing_channels:
        raise StoreRefused(f"the channel {channel} is being deleted or moved")
    try:
        if not channel_paths.db_path(channel).is_file():
            raise StoreRefused(f"the channel {channel} has no database")
    except channel_paths.ChannelUnavailable as exc:
        raise StoreRefused(f"the channel {channel} is unavailable: {exc.reason}") from None
    return store_path(channel)


def _existing_store(channel: str) -> str:
    """The store of a channel whose write is admitted and whose store is
    there; the caller holds the store lock."""
    path = _writable(channel)
    if not _owned_directory(path):
        raise StoreRefused(f"the channel store {channel}.git is missing or not owned by the server user")
    return path


# --- creation --------------------------------------------------------------------------------------


def ensure(channel: str) -> str:
    """The channel's store, made if missing; its absolute path.

    The only creator, under the store lock, idempotent. The layout is
    written by the host, never ``git init`` (P1), into
    ``<channel folder>/.creating-git-<n>``, then renamed into place and the
    folder fsynced: ``HEAD`` = ``ref: refs/heads/none``, ``config``
    a bare format-0 repository, ``objects/info/``, ``objects/pack/``,
    ``refs/``; directories 0755, files 0644. Refused (:class:`StoreRefused`)
    for a channel in ``changing_channels``, one without its DB, or a store
    path that is not a directory the server user owns."""
    identity = canonical_channel_id(channel)
    with store_lock(identity):
        path = _writable(identity)
        if os.path.lexists(path):
            if not _owned_directory(path):
                raise StoreRefused(f"the channel store {identity}.git is not a directory the server user owns")
            return path
        channels = os.path.dirname(path)
        for n in itertools.count():
            staging = os.path.join(channels, f".creating-git-{n}")
            try:
                os.mkdir(staging, _DIR_MODE)
            except FileExistsError:
                continue
            break
        try:
            _write_layout(staging)
            os.rename(staging, path)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        fsync_directory(channels)
        return path


def _write_layout(root: str) -> None:
    os.chmod(root, _DIR_MODE)
    for name in _LAYOUT_DIRS:
        directory = os.path.join(root, name)
        os.mkdir(directory, _DIR_MODE)
        os.chmod(directory, _DIR_MODE)
    for name, body in BARE_FILES.items():
        fd = os.open(os.path.join(root, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | fs.O_NOFOLLOW, _FILE_MODE)
        try:
            os.fchmod(fd, _FILE_MODE)
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
    for name in reversed(_LAYOUT_DIRS):
        fsync_directory(os.path.join(root, name))
    fsync_directory(root)


def purge(paths: Iterable[str]) -> None:
    """Remove trees set aside under the store lock, whatever their
    directories' modes (:func:`readonly_trees.remove_tree`); a failure is
    logged and left behind."""
    for path in paths:
        try:
            readonly_trees.remove_tree(path)
        except OSError as exc:
            _logger.warning("could not remove %s: %s", path, exc)


# --- the in-flight count --------------------------------------------------------------------------


_in_flight: dict[str, int] = {}
_in_flight_lock = threading.Lock()


@contextmanager
def in_flight(channel: str) -> Iterator[None]:
    """Count an ingest in flight on the channel for the block: a harvest
    holds it from its walk through ``index-pack`` to its row commit, so
    :func:`collect` never repacks under objects no row names yet. The count
    is raised under the store lock, held only for that: a collect already
    repacking finishes before the ingest's walk and ``pack-objects
    --local`` look at what the store holds, and a later one sees the
    count."""
    identity = canonical_channel_id(channel)
    with store_lock(identity), _in_flight_lock:
        _in_flight[identity] = _in_flight.get(identity, 0) + 1
    try:
        yield
    finally:
        with _in_flight_lock:
            left = _in_flight[identity] - 1
            if left:
                _in_flight[identity] = left
            else:
                del _in_flight[identity]


def ingest_in_flight(channel: str) -> bool:
    """Whether an ingest is in flight on the channel."""
    with _in_flight_lock:
        return _in_flight.get(str(channel), 0) > 0


# --- ingest (§4.8) --------------------------------------------------------------------------------


@dataclass
class Quarantine:
    """A harvest's quarantine: a host-written bare gitdir under
    ``.collimate/tmp`` whose alternates name the store, holding copies of
    one run repository's loose objects and packs (indexed here) and its
    ``shallow``. ``copied`` names the object files copied, relative to
    ``objects/``; ``ledger`` the files earlier quarantines of the lane
    landed. :attr:`objects` is the :class:`Objects` normalize filters roots
    and expires reflogs with; close it (or use ``with``) when done."""

    channel: str
    path: str
    copied: frozenset[str]
    ledger: frozenset[str] = frozenset()

    @property
    def shallow_file(self) -> str | None:
        shallow = os.path.join(self.path, "shallow")
        return shallow if os.path.isfile(shallow) else None

    @property
    def env(self) -> dict[str, str] | None:
        shallow = self.shallow_file
        return {"GIT_SHALLOW_FILE": shallow} if shallow else None

    @property
    def objects(self) -> GitdirObjects:
        return GitdirObjects(self.path, shallow_file=self.shallow_file)

    def landed(self) -> frozenset[str]:
        """The copied object files whose every object the commit of this
        :func:`ingest` roots: what the harvest's ledger takes after that
        commit, so a later quarantine copies them no more. An object counts
        when the ingest's walk reached it (so the store holds it and the
        rows root it), or when it is a loose object the ledger already
        holds (a run's repack of it). A file holding any other object (one
        the state does not reach, even if the store holds it for another
        row) stays out, and is copied again while the run keeps it."""
        reached = os.path.join(self.path, _REACHED)
        if not os.path.isfile(reached):
            return frozenset()
        names: dict[str, set[str]] = {}
        for rel in self.copied:
            if rel.startswith("pack/"):
                # Inside the quarantine: outside a repository, git 2.47's
                # show-index has no hash algorithm and crashes or lists nothing.
                with open(os.path.join(self.path, "objects", rel[:-len(".pack")] + ".idx"), "rb") as idx:
                    listed = git_host.run(["show-index"], input=idx, git_dir=self.path, timeout=STORE_TIMEOUT)
                names[rel] = {line.split(" ")[1] for line in listed.stdout.decode("ascii").splitlines() if line}
            else:
                names[rel] = {rel.replace("/", "")}
        wanted = set().union(*names.values()) if names else set()
        rooted = {rel.replace("/", "") for rel in self.ledger if not rel.startswith("pack/")} & wanted
        with open(reached, "rb") as fh:
            for line in fh:
                if (sha := line[:40].decode("ascii", "replace")) in wanted:
                    rooted.add(sha)
        return frozenset(rel for rel, shas in names.items() if shas <= rooted)

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Quarantine:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _open_gitdir(workspace: str, repo_path: str) -> int:
    """An fd on ``<workspace>/<repo_path>/.git``, no component followed
    through a symlink."""
    parts = fs.relative_parts(repo_path) if repo_path else []
    parent = fs.open_parent(workspace, [*parts, ".git"], create=False)
    try:
        return os.open(".git", os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _object_file(rel: str) -> bool:
    top, slash, rest = rel.partition("/")
    if not slash or "/" in rest:
        return False
    if top == "pack":
        return bool(_PACK_FILE.fullmatch(rest))
    return bool(_LOOSE_DIR.fullmatch(top) and _LOOSE_FILE.fullmatch(rest))


def _prune_objects(path: str, entry: os.DirEntry) -> bool:
    """The walk of a run's ``objects/`` enters fan-out directories and
    ``pack/`` and takes their object files only: never ``.idx``, ``.rev``,
    ``.bitmap``, midx, commit-graph or ``info/*``."""
    if "/" not in path:
        return not (path == "pack" or _LOOSE_DIR.fullmatch(path))
    return not _object_file(path)


def quarantine(channel: str, workspace: str, repo_path: str, *, run_id: str,
               ledger: Iterable[str] = ()) -> Quarantine:
    """Step 1: build a quarantine for ``<workspace>/<repo_path>/.git``.

    ``tempfile.mkdtemp(prefix=f"git-harvest-{run8}_", dir=.collimate/tmp)``
    gets a host-written ``config`` and ``HEAD``, ``objects/info/alternates``
    naming the store (made by :func:`ensure` if missing), copies of the
    run's regular loose objects and ``pack-<hex>.pack`` files not in
    ``ledger`` (the files earlier quarantines of this lane landed), each
    pack indexed with ``index-pack``, and the run's ``shallow``. Over
    :data:`QUARANTINE_MAX_BYTES`, :data:`QUARANTINE_MAX_FILES` or
    :data:`QUARANTINE_SECONDS` it fails; any failure removes the
    quarantine and raises :class:`StoreFailed`."""
    identity = canonical_channel_id(channel)
    store = ensure(identity)
    deadline = time.monotonic() + QUARANTINE_SECONDS
    done = frozenset(ledger)
    path = tempfile.mkdtemp(prefix=f"git-harvest-{run_id[:8]}_", dir=git_host.tmp_root())
    try:
        for name in ("objects/info", "objects/pack", "refs"):
            os.makedirs(os.path.join(path, name))
        for name, body in {**BARE_FILES,
                           "objects/info/alternates": os.path.join(store, "objects").encode() + b"\n"}.items():
            with open(os.path.join(path, name), "wb") as fh:
                fh.write(body)
        try:
            gitdir = _open_gitdir(workspace, repo_path)
        except OSError as exc:
            raise StoreFailed(f"gitdir unreadable: {exc.strerror or exc}") from None
        try:
            copied = _copy_objects(gitdir, path, done, deadline)
            _copy_shallow(gitdir, path)
        finally:
            os.close(gitdir)
        for rel in sorted(copied):
            if rel.startswith("pack/"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StoreFailed(f"objects not copied within {QUARANTINE_SECONDS / 60:.0f} minutes")
                try:
                    git_host.run(["index-pack", os.path.join(path, "objects", rel)], git_dir=path, timeout=remaining)
                except git_host.GitFailed as exc:
                    raise _failed(f"pack {rel[len('pack/'):]} unreadable", exc) from None
        return Quarantine(identity, path, frozenset(copied), done)
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


def _copy_objects(gitdir: int, quarantine_path: str, done: frozenset[str], deadline: float, *,
                  max_files: int | None = None, max_bytes: int | None = None) -> set[str]:
    """Copy the gitdir's loose objects and ``pack-<hex>.pack`` files not in
    ``done`` into ``quarantine_path`` as data; over ``max_files`` or
    ``max_bytes`` (the quarantine's caps when None) or past ``deadline`` it
    fails."""
    max_files = QUARANTINE_MAX_FILES if max_files is None else max_files
    max_bytes = QUARANTINE_MAX_BYTES if max_bytes is None else max_bytes
    try:
        objects = os.open("objects", os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=gitdir)
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise StoreFailed(f"objects unreadable: {exc.strerror or exc}") from None
    errors: list[OSError] = []
    copied: set[str] = set()
    total = 0
    try:
        for rel, fd in fs.walk_regular(".", dir_fd=objects, prune=_prune_objects, max_depth=2,
                                       onerror=errors.append):
            if rel in done:
                continue
            if len(copied) >= max_files:
                raise StoreFailed(f"objects over {max_files:,} files")
            target = os.path.join(quarantine_path, "objects", rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                while chunk := os.read(fd, _COPY_CHUNK):
                    total += len(chunk)
                    if total > max_bytes:
                        raise StoreFailed(f"objects over {max_bytes // 1024 ** 3} GiB")
                    if time.monotonic() > deadline:
                        raise StoreFailed(f"objects not copied within {QUARANTINE_SECONDS / 60:.0f} minutes")
                    out.write(chunk)
            copied.add(rel)
    finally:
        os.close(objects)
    if errors:
        failure = errors[0]
        raise StoreFailed(f"objects unreadable: {failure.filename or 'objects'}: {failure.strerror or failure}")
    return copied


def _copy_shallow(gitdir: int, quarantine_path: str) -> None:
    try:
        fd = os.open("shallow", os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=gitdir)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StoreFailed(f"shallow unreadable: {exc.strerror or exc}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StoreFailed("shallow is not a regular file")
        body = os.read(fd, READ_LIMIT + 1)
    finally:
        os.close(fd)
    if len(body) > READ_LIMIT:
        raise StoreFailed("shallow over 64 MiB")
    with open(os.path.join(quarantine_path, "shallow"), "wb") as fh:
        fh.write(body)


def _feed(lines: Iterable[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _haves(objects: GitdirObjects, state: RepositoryState, base: RepositoryState | None) -> dict[str, str]:
    """The base's roots that exist, with their types: what a run's
    history may stop at. GC keeps them (they are every row's roots), and
    the store holds each one's history down to the base's shallow
    boundary. None when ``state`` removed one of the base's cuts (``fetch
    --unshallow``, ``--deepen``): under the run's shallow file a have's
    walk then passes that cut into the history the run just fetched,
    which the store lacks, so the walk and pack take every object the
    state reaches (``--local`` still leaves out what the store holds)."""
    if base is None or shallow_of(base) - shallow_of(state):
        return {}
    return objects.exists(roots(base))


def _band_roots(state: RepositoryState) -> set[str]:
    """The roots :func:`band_guard` checks: every root but ``shallow``."""
    return roots(state) - shallow_of(state)


def _touches_band(git_dir: str, commits: Iterable[str], haves: Iterable[str], env: Mapping[str, str] | None) -> bool:
    """Whether history from ``commits`` but not from ``haves`` changes
    anything at or under the top-level band: one ``rev-list
    --full-history --stdin``, the haves as ``^`` lines (never on argv),
    then ``--`` and the band (P17)."""
    feed = [*sorted(set(commits)), *(f"^{have}" for have in sorted(set(haves))), "--", BAND]
    done = git_host.run(["rev-list", "--full-history", "--stdin"], git_dir=git_dir, env=env, input=_feed(feed),
                        timeout=STORE_TIMEOUT)
    return bool(done.stdout.strip())


def tree_holds_band(git_dir: str, tree: str) -> bool:
    """Whether the tree (or commit) ``tree`` holds anything at or under the
    top-level band."""
    done = git_host.run(["ls-tree", tree, "--", BAND], git_dir=git_dir, timeout=STORE_TIMEOUT)
    return bool(done.stdout.strip())


def _peeled(git_dir: str, tags: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Each annotated tag's peeled object and its type (``<sha>^{}``)."""
    wanted = sorted(set(tags))
    if not wanted:
        return {}
    done = git_host.run(["cat-file", "--batch-check"], git_dir=git_dir, input=_feed(f"{tag}^{{}}" for tag in wanted))
    peeled = {}
    for tag, line in zip(wanted, done.stdout.decode("ascii", "replace").splitlines()):
        fields = line.split(" ")
        if len(fields) == 3 and fields[1] != "missing":
            peeled[tag] = (fields[0], fields[1])
    return peeled


def _index_paths(body: bytes) -> list[str]:
    paths = []
    for line in body.split(b"\0"):
        _, tab, path = line.partition(b"\t")
        if tab:
            paths.append(path.decode("utf-8", "replace"))
    return paths


def band_guard(quarantine: Quarantine, state: RepositoryState, base: RepositoryState | None) -> Normalized:
    """Step 5: keep the band out of the state (R8, B15).

    The roots new since ``base`` (ref values and a detached HEAD; reflog,
    progress and ``index`` shas; not ``shallow``) are typed in the
    quarantine, a tag peeled. A commit hits when its history past the
    base's roots changes the top-level band (:func:`_touches_band`); a
    tree (``AUTO_MERGE``, a ref or tag at one) when ``ls-tree <tree> --
    .collrun`` lists anything, as rev-list ignores tree arguments (P17).
    An ``index`` line at or under the band hits all of ``index``.

    On a hit a ref keeps its base value and reflog (and goes when it has
    none), a detached HEAD too; a reflog entry naming a hit goes, its ref
    keeping a clean value; a progress file goes with its set; ``index``
    goes alone, keeping ``MERGE_HEAD``; one line each, so no hit stays a
    root. That covers ``refs/stash``'s third parent after ``stash --all``.
    A repository made in the run (no ``base``) whose history hits is
    refused, so the harvest rescues it. :class:`StoreFailed` when git
    cannot answer."""
    parts = state.named_parts()
    head = state.manifest()["head"]
    objects = quarantine.objects
    env = quarantine.env
    try:
        new = _band_roots(state) - (roots(base) if base is not None else set())
        types = objects.exists(new)
        peeled = _peeled(quarantine.path, [sha for sha, kind in types.items() if kind == "tag"])
        haves = _haves(objects, state, base)
        target = {sha: peeled.get(sha, (sha, kind)) for sha, kind in types.items()}
        commits = {sha for sha, (_, kind) in target.items() if kind == "commit"}
        commit_haves = [have for have, kind in haves.items() if kind == "commit"]
        hits = set()
        if commits and _touches_band(quarantine.path, {target[sha][0] for sha in commits}, commit_haves, env):
            hits |= {sha for sha in commits if _touches_band(quarantine.path, [target[sha][0]], commit_haves, env)}
        hits |= {sha for sha, (peel, kind) in target.items() if kind == "tree" and tree_holds_band(quarantine.path, peel)}
    except git_host.GitFailed as exc:
        raise _failed("history unreadable", exc) from None
    index = parts.get("index")
    index_hit = index is not None and any(path == BAND or path.startswith(BAND + "/") for path in _index_paths(index))
    if not hits and not index_hit:
        return Normalized(state)
    if hits and base is None:
        return Normalized(None, f"history holds {BAND}/: a repository made in this run cannot be kept")
    lines: list[str] = []
    warnings: list[str] = []
    if hits:
        assert base is not None
        head = _guard_refs(parts, head, base, hits, lines)
        for name in sorted(name for name in parts if name.startswith("progress/")):
            body = parts.get(name)
            if body is not None and progress_shas(name[len("progress/"):], body) & hits:
                group = progress_set(name[len("progress/"):])
                for other in [other for other in parts if other.startswith("progress/")
                              and progress_set(other[len("progress/"):]) == group]:
                    del parts[other]
                lines.append(f"progress {group}: history holds {BAND}/; not kept")
    if index_hit:
        del parts["index"]
        warnings.append(f"index holds {BAND}/; {STAGED_RESOLUTIONS}")
    return Normalized(make_state(head, parts), None, tuple(warnings), tuple(lines))


def _guard_refs(parts: dict[str, bytes], head: str, base: RepositoryState, hits: set[str], lines: list[str]) -> str:
    """Put each ref (and a detached HEAD) whose value is a hit back to its
    base value and reflog in ``parts``, or drop it with its reflog when the
    base has none; then drop the reflog entries that still name a hit (a
    clean ref, or HEAD, whose reflog remembers band history keeps its
    value). The head to keep."""
    refs = parse_refs(parts.get("refs", b""))
    base_refs = parse_refs(base.part("refs") or b"")
    base_parts = base.named_parts()

    def restore_reflog(name: str) -> None:
        key = f"reflogs/{name}"
        if key in base_parts:
            parts[key] = base_parts[key]
        else:
            parts.pop(key, None)

    for name in sorted(refs):
        if refs[name] in hits:
            if name in base_refs:
                refs[name] = base_refs[name]
                lines.append(f"ref {name}: history holds {BAND}/; base value kept")
            else:
                del refs[name]
                lines.append(f"ref {name}: history holds {BAND}/; not kept")
            restore_reflog(name)
    if head in hits:
        head = base.manifest()["head"]
        lines.append(f"HEAD: history holds {BAND}/; base value kept")
        restore_reflog("HEAD")
    for key in sorted(key for key in parts if key.startswith("reflogs/")):
        entries, _, _ = parse_reflog(parts[key])
        kept = [e for e in entries if not ({e.old, e.new} - {NULL_OID}) & hits]
        if len(kept) != len(entries):
            parts[key] = b"".join(e.line + b"\n" for e in kept)
            lines.append(f"reflog {key[len('reflogs/'):]}: history holds {BAND}/; "
                         f"{len(entries) - len(kept)} entries dropped")
    parts["refs"] = format_refs(refs)
    return head


@dataclass(frozen=True)
class Ingested:
    """What :func:`ingest` carried into the store: the number of objects
    its pack held (0: nothing new) and the dangling ``.gitmodules`` blobs
    ``index-pack`` named, which the store was then checked to hold."""

    objects: int
    gitmodules: tuple[str, ...] = ()


def _pack_count(path: str) -> int:
    with open(path, "rb") as fh:
        header = fh.read(12)
    if len(header) != 12 or header[:4] != b"PACK":
        raise StoreFailed("git pack-objects wrote no pack")
    return int.from_bytes(header[8:12], "big")


def ingest(quarantine: Quarantine, state: RepositoryState, base: RepositoryState | None) -> Ingested:
    """Steps 6-8: land ``state``'s objects in the store. Call it inside
    :func:`in_flight` for the channel, held until the rows commit.

    6. The walk: ``rev-list --objects --missing=error --stdin`` in the
       quarantine (``GIT_SHALLOW_FILE`` = its ``shallow``) over the roots,
       then the base's existing roots as ``^`` lines (P2, P3), none when
       the run removed a cut of the base (:func:`_haves`); the objects
       it reached are kept for :meth:`Quarantine.landed` once step 8 passed.
    7. ``pack-objects --revs --local -q --stdout`` (roots, ``--not``,
       haves) into a file; a zero-object pack is skipped (P5); else, under
       the store lock, ``index-pack --stdin --fsck-objects`` with the
       store-write flags, at fsck's strict severities (P4).
    8. ``cat-file --batch-check`` in the store alone over the roots and the
       dangling ``.gitmodules`` blobs index-pack named, then fsync
       ``objects/pack`` and ``objects``.

    :class:`StoreFailed` with git's words on any failure (a missing
    object, an fsck rejection such as ``hasDotgit``)."""
    channel = quarantine.channel
    if not ingest_in_flight(channel):
        raise RuntimeError(f"ingest into the store of {channel} outside git_store.in_flight")
    wanted = sorted(roots(state))
    env = quarantine.env
    walked = os.path.join(quarantine.path, f"{_REACHED}.walk")
    try:
        haves = sorted(_haves(quarantine.objects, state, base))
        with open(walked, "wb") as out:
            git_host.run(["rev-list", "--objects", "--no-object-names", "--missing=error", "--stdin"],
                         git_dir=quarantine.path, env=env, input=_feed([*wanted, *(f"^{have}" for have in haves)]),
                         output=out, timeout=STORE_TIMEOUT)
    except git_host.GitFailed as exc:
        raise _failed("history incomplete", exc) from None
    pack = os.path.join(quarantine.path, "ingest.pack")
    try:
        with open(pack, "wb") as out:
            git_host.run(["pack-objects", "--revs", "--local", "-q", "--stdout"], git_dir=quarantine.path, env=env,
                         input=_feed([*wanted, "--not", *haves]), output=out, timeout=STORE_TIMEOUT)
    except git_host.GitFailed as exc:
        raise _failed("objects not packed", exc) from None
    count = _pack_count(pack)
    with store_lock(channel):
        store = _existing_store(channel)
        gitmodules: tuple[str, ...] = ()
        if count:
            try:
                with open(pack, "rb") as stream:
                    done = git_host.run(["index-pack", "--stdin", "--fsck-objects"], git_dir=store, input=stream,
                                        store_write=True, timeout=STORE_TIMEOUT)
            except git_host.GitFailed as exc:
                raise _failed("objects refused", exc) from None
            gitmodules = _dangling_gitmodules(done.stdout)
        _check_store(store, [*wanted, *gitmodules])
    os.rename(walked, os.path.join(quarantine.path, _REACHED))
    return Ingested(count, gitmodules)


def _dangling_gitmodules(stdout: bytes) -> tuple[str, ...]:
    """The blob names ``index-pack`` prints after its ``pack\\t<hash>`` line."""
    lines = stdout.decode("ascii", "replace").splitlines()
    return tuple(line for line in lines[1:] if _HEX40.fullmatch(line))


def _check_store(store: str, shas: Iterable[str]) -> None:
    """Every sha in the store alone (the caller holds the store lock), then
    the fsyncs that make the new pack durable before any row names it."""
    wanted = set(shas)
    try:
        present = set(GitdirObjects(store).exists(wanted))
    except git_host.GitFailed as exc:
        raise _failed("the channel store is unreadable", exc) from None
    missing = wanted - present
    if missing:
        raise StoreFailed(f"history incomplete in the channel store: {len(missing)} objects missing, "
                          f"{sorted(missing)[0]} first")
    fsync_directory(os.path.join(store, "objects", "pack"))
    fsync_directory(os.path.join(store, "objects"))


# --- rescue (§4.9) --------------------------------------------------------------------------------


def _slot(repo_path: str) -> str:
    return hashlib.sha256(repo_path.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rescue(channel: str, cell_id: str, repo_path: str, gitdir: str, *, run_id: str, base_state_hash: str | None,
           reason: str, git_stderr: str = "", secrets: Iterable[str] = ()) -> str:
    """Keep a ``.git`` the harvest could not: rename ``gitdir`` (a real
    directory, never a symlink) to
    ``<uuid>.git-rescue/<cell_id>/<sha256(repo_path)[:16]>/.git`` beside
    ``rescue.json`` = ``{run_id, repo_path, base_state_hash, reason,
    git_stderr, at}``, ``git_stderr`` scrubbed of URL passwords and
    ``secrets`` (the bound accounts' secret forms) and capped at 64 KiB.
    The newest rescue of a (cell, path) replaces the one before. Under the
    store lock; :class:`StoreRefused` while the channel is being deleted
    or has no DB. The rescue's directory is returned."""
    identity = canonical_channel_id(channel)
    found = os.lstat(gitdir)
    if not stat.S_ISDIR(found.st_mode):
        raise StoreFailed(f"{repo_path or '.'}/.git is not a directory")
    record = {
        "run_id": run_id,
        "repo_path": repo_path,
        "base_state_hash": base_state_hash,
        "reason": scrub_text(reason, secrets),
        "git_stderr": scrub_text(git_stderr, secrets).encode("utf-8")[-RESCUE_STDERR_CAP:].decode("utf-8", "ignore"),
        "at": _now_iso(),
    }
    with store_lock(identity):
        _writable(identity)
        area = rescue_path(identity)
        cell_dir = os.path.join(area, _cell_name(cell_id))
        os.makedirs(cell_dir, mode=0o700, exist_ok=True)
        if not _owned_directory(area) or not _owned_directory(cell_dir):
            raise StoreRefused(f"the rescue area of {identity} is not owned by the server user")
        slot = os.path.join(cell_dir, _slot(repo_path))
        fresh = tempfile.mkdtemp(prefix=".new-", dir=cell_dir)
        kept = os.path.join(fresh, ".git")
        try:
            _move_tree(gitdir, kept)
            with open(os.path.join(fresh, RESCUE_RECORD), "w", encoding="utf-8") as fh:
                json.dump(record, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            fsync_directory(fresh)
            old = None
            if os.path.lexists(slot):
                old = tempfile.mkdtemp(prefix=".old-", dir=cell_dir)
                os.rename(slot, os.path.join(old, "slot"))
            os.rename(fresh, slot)
        except BaseException:
            # The run's .git goes back where it was rather than with the
            # half-made rescue.
            if os.path.lexists(kept) and not os.path.lexists(gitdir):
                _move_tree(kept, gitdir)
            shutil.rmtree(fresh, ignore_errors=True)
            raise
        fsync_directory(cell_dir)
        fsync_directory(area)
        if old is not None:
            purge([old])
    return slot


def _cell_name(cell_id: str) -> str:
    name = str(cell_id)
    if not name or name.startswith(".") or "/" in name or "\0" in name:
        raise ValueError(f"not a cell id: {name!r}")
    return name


def _move_tree(source: str, target: str) -> None:
    """Rename ``source`` to ``target``; from another file system, copy it (links
    as links) and remove the source."""
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copytree(source, target, symlinks=True)
        readonly_trees.remove_tree(source)


def rescued_cells(channel: str) -> set[str]:
    """The ids of the channel's cells that hold a rescue directory."""
    try:
        return {entry for entry in os.listdir(rescue_path(channel)) if not entry.startswith(".")}
    except (FileNotFoundError, NotADirectoryError):
        return set()


def _read_record(slot: str) -> dict | None:
    try:
        fd = os.open(os.path.join(slot, RESCUE_RECORD), os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        body = os.read(fd, 4 * RESCUE_STDERR_CAP)
    finally:
        os.close(fd)
    try:
        record = json.loads(body)
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("repo_path"), str):
        return None
    return record


def rescues_of(channel: str, cell_id: str) -> dict[str, dict]:
    """The cell's rescues as ``{repo_path: rescue.json}``, for the
    snapshot's error entries; a slot whose record does not name it is
    left out."""
    cell_dir = os.path.join(rescue_path(channel), _cell_name(cell_id))
    try:
        slots = sorted(os.listdir(cell_dir))
    except (FileNotFoundError, NotADirectoryError):
        return {}
    found = {}
    for slot in slots:
        record = None if slot.startswith(".") else _read_record(os.path.join(cell_dir, slot))
        if record is not None and _slot(record["repo_path"]) == slot:
            found[record["repo_path"]] = record
    return found


def rescue_gitdir(channel: str, cell_id: str, repo_path: str) -> str | None:
    """The rescued ``.git`` of (cell, path), or None when there is none."""
    slot = os.path.join(rescue_path(channel), _cell_name(cell_id), _slot(repo_path))
    gitdir = os.path.join(slot, ".git")
    record = _read_record(slot)
    if record is None or record["repo_path"] != repo_path or not _owned_directory(gitdir):
        return None
    return gitdir


def discard(channel: str, cell_id: str, repo_path: str) -> bool:
    """Remove the rescue of (cell, path), under the store lock (the caller
    holds the cell lock); whether there was one. The row and the route are
    the caller's."""
    identity = canonical_channel_id(channel)
    with store_lock(identity):
        cell_dir = os.path.join(rescue_path(identity), _cell_name(cell_id))
        slot = os.path.join(cell_dir, _slot(repo_path))
        if not os.path.lexists(slot):
            return False
        gone = tempfile.mkdtemp(prefix=".discard-", dir=cell_dir)
        os.rename(slot, os.path.join(gone, "slot"))
        fsync_directory(cell_dir)
    purge([gone])
    with store_lock(identity):
        _drop_empty(cell_dir)
    return True


def _drop_empty(directory: str) -> None:
    try:
        os.rmdir(directory)
    except OSError:
        pass


# What a rescue export writes of a rescued .git besides the pack: the
# files git needs to open it and the run's work in it, each through the
# sanitizer (§3.3); never hooks, config.worktree, FETCH_HEAD, modules/,
# worktrees/ or anything else the run left there.
_RESCUE_RAW = ("HEAD", "packed-refs", "index", "shallow")
_RESCUE_DIRS = ("refs/", "refs/heads/", "refs/tags/", "objects/", "objects/info/", "objects/pack/")
_RESCUE_CONFIG = (("repositoryformatversion", "0"), ("filemode", "false"), ("bare", "false"),
                  ("logallrefupdates", "true"))
_SHARED_INDEX_FILE = re.compile(r"sharedindex\.[0-9a-f]{40}")


def export_rescue(channel: str, cell_id: str, repo_path: str, out: IO[bytes], *,
                  secrets: Iterable[str] = ()) -> None:
    """Write the rescue of (cell, path) to ``out`` as a zip of a ``.git``
    that opens as a repository of its own: its HEAD, refs and packed-refs,
    index (and shared index), shallow, reflogs, progress and info files,
    reflogs with rule 2 and every text with rule 3 of the sanitizer
    (``secrets``: every Git account's secret forms); its config rebuilt as
    the four format keys and the allow-listed pairs through the config
    rules (never a command-running key or a URL's credentials); the
    ``refs``, ``objects/info`` and ``objects/pack`` directories; and one
    pack of all its refs, reflogs and index reach through its own objects
    and the store's (``pack-objects --all --reflog --indexed-objects
    --missing=allow-any``), beside the rescue's own object files. The pack is made in a host-written staging
    gitdir under ``.collimate/tmp/export-*`` whose only alternate is the
    store, the rescue's loose objects and ``pack-<hex>.pack`` files copied
    in as data and indexed there: the rescue's own alternates and links
    are never followed. Every file is read through directory fds without
    following a symlink. :class:`LookupError` when there is no such
    rescue."""
    gitdir = rescue_gitdir(channel, cell_id, repo_path)
    if gitdir is None:
        raise LookupError(f"no rescue at {repo_path or '.'} in cell {cell_id}")
    secrets = tuple(secrets)
    staging = tempfile.mkdtemp(prefix="export-", dir=git_host.tmp_root())
    try:
        files = _rescued_files(gitdir, staging, secrets)
        pack_dir, objects = _rescue_pack(channel, gitdir, files, staging)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for directory in _RESCUE_DIRS:
                archive.writestr(f".git/{directory}", b"")
            for rel in sorted(files):
                with open(files[rel], "rb") as source, archive.open(f".git/{rel}", "w", force_zip64=True) as member:
                    shutil.copyfileobj(source, member)
            for rel, source in sorted(objects.items()):
                with open(source, "rb") as fh, archive.open(f".git/objects/{rel}", "w", force_zip64=True) as member:
                    shutil.copyfileobj(fh, member)
            for name in sorted(os.listdir(pack_dir)):
                archive.write(os.path.join(pack_dir, name), f".git/objects/pack/{name}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _rescue_kind(rel: str) -> str | None:
    """How a rescue export writes the rescued file ``rel``: ``raw``,
    ``reflog`` (rules 2 and 3), ``text`` (rule 3), ``config`` (rebuilt);
    None to leave it out."""
    if rel in _RESCUE_RAW or rel.startswith("refs/") or _SHARED_INDEX_FILE.fullmatch(rel):
        return "raw"
    if rel == "config":
        return "config"
    if rel.startswith("logs/"):
        return "reflog"
    if progress_name(rel) is not None or rel in ("info/exclude", "info/attributes", "info/sparse-checkout"):
        return "text"
    return None


def _rescued_files(gitdir: str, staging: str, secrets: tuple[str, ...]) -> dict[str, str]:
    """The files a rescue export writes (:func:`_rescue_kind`) by their
    relative path, read through directory fds without following a
    symlink and copied into ``staging`` as data, so nothing in the rescue
    changes while it is zipped, then sanitized there; ``objects/`` is
    left to :func:`_rescue_pack`."""
    found = {}
    copies = os.path.join(staging, "files")
    errors: list[OSError] = []

    def prune(path: str, entry: os.DirEntry) -> bool:
        return path == "objects" or path.startswith("objects/")

    for rel, fd in fs.walk_regular(gitdir, prune=prune, onerror=errors.append):
        kind = _rescue_kind(rel)
        if kind is None:
            continue
        target = os.path.join(copies, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as out:
            while chunk := os.read(fd, _COPY_CHUNK):
                out.write(chunk)
        if kind == "config":
            _rescue_config(target, secrets)
        elif kind != "raw":
            with open(target, "rb") as fh:
                body = fh.read()
            with open(target, "wb") as fh:
                fh.write(scrub_bytes(body, secrets, passwords=kind == "reflog"))
        found[rel] = target
    if "config" not in found:
        target = os.path.join(copies, "config")
        os.makedirs(copies, exist_ok=True)
        with open(target, "wb"):
            pass
        _rescue_config(target, secrets)
        found["config"] = target
    if errors:
        _logger.warning("rescue export of %s: %d entries unreadable, left out", gitdir, len(errors))
    return found


def _rescue_config(path: str, secrets: tuple[str, ...]) -> None:
    """Rewrite the copy of a rescued config at ``path`` as the four format
    keys and the pairs the sanitizer keeps of it (read with git, includes
    not followed; none when git cannot read it)."""
    pairs: list[tuple[str, str]] = []
    try:
        done = git_host.run(["config", "--file", path, "--no-includes", "--list", "-z"])
        pairs = parse_config_list(done.stdout)[0]
    except git_host.GitFailed:
        _logger.info("rescue export: a config git cannot read is written as the format keys alone")
    kept, _lines = sanitize_config(pairs, secrets)
    with open(path, "wb") as fh:
        fh.write(b"[core]\n" + b"".join(f"\t{key} = {value}\n".encode("ascii") for key, value in _RESCUE_CONFIG))
    for key, value in kept:
        git_host.run(["config", "--file", path, "--add", key, value])


def _rescue_pack(channel: str, gitdir: str, files: Mapping[str, str], staging: str) -> tuple[str, dict[str, str]]:
    """A directory holding one pack of everything the rescue's refs,
    reflogs and index reach through its own objects (copied in as data)
    and the store's; and those object files (each loose object, each
    ``pack-<hex>.pack`` with the ``.idx`` made for it) by their path under
    ``objects/``."""
    work = os.path.join(staging, "git")
    os.makedirs(os.path.join(work, "objects", "info"))
    os.makedirs(os.path.join(work, "objects", "pack"))
    os.makedirs(os.path.join(work, "refs"))
    for name, body in BARE_FILES.items():
        with open(os.path.join(work, name), "wb") as fh:
            fh.write(body)
    with open(os.path.join(work, "objects", "info", "alternates"), "wb") as fh:
        fh.write(objects_path(channel).encode() + b"\n")
    try:
        fd = os.open(gitdir, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK)
    except OSError as exc:
        raise StoreFailed(f"the rescue is unreadable: {exc.strerror or exc}") from None
    try:
        copied = _copy_objects(fd, work, frozenset(), float("inf"), max_files=10 ** 9, max_bytes=2 ** 62)
    finally:
        os.close(fd)
    for rel in sorted(copied):
        if rel.startswith("pack/"):
            try:
                git_host.run(["index-pack", os.path.join(work, "objects", rel)], git_dir=work, timeout=STORE_TIMEOUT)
            except git_host.GitFailed as exc:
                raise _failed(f"pack {rel[len('pack/'):]} of the rescue unreadable", exc) from None
    for rel, source in files.items():
        if rel in ("packed-refs", "index", "shallow") or rel.startswith(("refs/", "logs/")) \
                or _SHARED_INDEX_FILE.fullmatch(rel):
            target = os.path.join(work, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(source, target)
    pack_dir = os.path.join(staging, "pack")
    os.mkdir(pack_dir)
    # HEAD (when detached) and the progress files name roots no ref does;
    # the staging HEAD is the host's, as a garbage one would hide the gitdir.
    named = set()
    for rel, source in files.items():
        if rel in ("index", "packed-refs", "config") or ("/" in rel and rel.split("/", 1)[0] not in OPERATION_DIRS):
            continue
        with open(source, "rb") as fh:
            body = fh.read(READ_LIMIT)
        if rel == "HEAD":
            named |= {token for token in body.decode("ascii", "replace").split() if _HEX40.fullmatch(token)}
        else:
            named |= progress_shas(rel, body)
    # Under the store lock: no row need root what a rescue borrows from the
    # store, so a collect must not repack or sweep it mid-read.
    try:
        with store_lock(channel):
            held = GitdirObjects(work).exists(named)
            git_host.run(["pack-objects", "--revs", "--all", "--reflog", "--indexed-objects", "--missing=allow-any",
                          "-q", os.path.join(pack_dir, "pack")], git_dir=work, input=_feed(sorted(held)),
                         timeout=STORE_TIMEOUT)
    except git_host.GitFailed as exc:
        raise _failed("the rescue's objects could not be packed", exc) from None
    for name in os.listdir(pack_dir):
        if not name.endswith((".pack", ".idx")):
            os.unlink(os.path.join(pack_dir, name))
    objects = {}
    for rel in copied:
        objects[rel] = os.path.join(work, "objects", rel)
        if rel.startswith("pack/"):
            idx = rel[:-len(".pack")] + ".idx"
            objects[idx] = os.path.join(work, "objects", idx)
    return pack_dir, objects


def _sweep_rescues(channel: str, now: float) -> int:
    """Remove the channel's rescues older than 30 days (by their record's
    ``at``, else the slot's mtime), under the store lock; how many."""
    area = rescue_path(channel)
    removed = 0
    for cell in sorted(rescued_cells(channel)):
        cell_dir = os.path.join(area, cell)
        try:
            slots = os.listdir(cell_dir)
        except OSError:
            continue
        for slot in slots:
            path = os.path.join(cell_dir, slot)
            if _rescue_age(path, now) <= RESCUE_MAX_AGE_SECONDS:
                continue
            try:
                readonly_trees.remove_tree(path)
                removed += 1
            except OSError as exc:
                _logger.warning("git store %s: could not sweep the rescue %s: %s", channel, path, exc)
        _drop_empty(cell_dir)
    return removed


def _rescue_age(slot: str, now: float) -> float:
    record = _read_record(slot)
    if record is not None and isinstance(record.get("at"), str):
        try:
            at = datetime.datetime.strptime(record["at"], "%Y-%m-%dT%H:%M:%SZ")
            return now - at.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            pass
    try:
        return now - os.lstat(slot).st_mtime
    except OSError:
        return 0.0


# --- export and copy (§7) -------------------------------------------------------------------------


@contextmanager
def _shallow_env(lines: Iterable[str]) -> Iterator[dict[str, str] | None]:
    """``GIT_SHALLOW_FILE`` naming a temporary file of ``lines``, or None
    when there are none."""
    kept = sorted(set(lines))
    if not kept:
        yield None
        return
    fd, path = tempfile.mkstemp(prefix="git-shallow-", dir=git_host.tmp_root())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(_feed(kept))
        yield {"GIT_SHALLOW_FILE": path}
    finally:
        os.unlink(path)


def pack_for_export(channel: str, state: RepositoryState, pack_dir: str) -> str | None:
    """A pack of everything ``state`` needs, written into ``pack_dir`` as
    ``pack-<hash>.pack`` beside its ``.idx`` by ``GIT_SHALLOW_FILE=<the
    state's shallow> git --git-dir=<store> pack-objects --revs`` over its
    roots; its path, or None when the state names no object (an unborn
    repository). No lock: the rows are committed, so their objects stay.
    :class:`StoreFailed` with git's words."""
    wanted = sorted(roots(state))
    if not wanted:
        return None
    store = store_path(channel)
    if not os.path.isdir(store):
        raise StoreFailed("repository history incomplete in the channel store: there is no store")
    with _shallow_env(shallow_of(state)) as env:
        try:
            done = git_host.run(["pack-objects", "--revs", "-q", os.path.join(pack_dir, "pack")], git_dir=store,
                                env=env, input=_feed(wanted), timeout=STORE_TIMEOUT)
        except git_host.GitFailed as exc:
            raise _failed("repository history not exported", exc) from None
    return os.path.join(pack_dir, f"pack-{done.stdout.decode('ascii').strip()}.pack")


def _grafts(store: str, shallow: Iterable[str]) -> tuple[list[str], set[str]]:
    """§8 step 3 over ``shallow`` in ``store``: the shas with a parent the
    store lacks (the grafts a walk stops at) and the parents it holds of
    those shallow commits, which become roots, since a walk under the
    grafts would not reach them through a graft. Never the raw union: a
    shallow commit whose parents are all here is a full repository's."""
    wanted = sorted(set(shallow))
    if not wanted:
        return [], set()
    done = git_host.run(["cat-file", "--batch"], git_dir=store, input=_feed(wanted), timeout=STORE_TIMEOUT)
    parents = _commit_parents(done.stdout)
    present = GitdirObjects(store).exists({p for ps in parents.values() for p in ps})
    grafts = sorted(sha for sha, ps in parents.items() if any(p not in present for p in ps))
    return grafts, {p for ps in parents.values() for p in ps if p in present}


def _commit_parents(batch: bytes) -> dict[str, list[str]]:
    """Each commit's parents in ``cat-file --batch`` output."""
    parents: dict[str, list[str]] = {}
    at = 0
    while at < len(batch):
        end = batch.index(b"\n", at)
        fields = batch[at:end].decode("ascii", "replace").split(" ")
        at = end + 1
        if len(fields) != 3:
            continue
        size = int(fields[2])
        body = batch[at:at + size]
        at += size + 1
        if fields[1] == "commit":
            header = body.split(b"\n\n", 1)[0].decode("utf-8", "replace").split("\n")
            parents[fields[0]] = [line[len("parent "):] for line in header if line.startswith("parent ")]
    return parents


def copy_between_stores(source_channel: str, new_channel: str, states: Iterable[RepositoryState]) -> None:
    """Copy every object ``states`` need from the source channel's store
    into the new channel's (New channel from channel, §7). No refs are
    written; any failure raises :class:`StoreFailed`, so the create
    unwinds and removes the new store.

    The states go in groups of one ``shallow`` part (none: no
    ``GIT_SHALLOW_FILE``), each walked under its own boundary as
    :func:`pack_for_export` walks one state, never under a cut made from
    other states (a shallow commit whose parent the source holds only for
    an uncopied, shallower clone would lead the walk to that clone's cut):

    1. ``GIT_SHALLOW_FILE=<the group's shallow> pack-objects --revs
       --stdout -q`` in the source over the group's roots into
       ``index-pack --stdin --fsck-objects`` in the new store, under its
       lock.
    2. ``rev-list --objects --missing=error --stdin`` in the new store (no
       alternates) under the same shallow file; then ``cat-file
       --batch-check`` over every root."""
    source_channel, new_channel = canonical_channel_id(source_channel), canonical_channel_id(new_channel)
    groups: dict[frozenset[str], set[str]] = {}
    for state in states:
        found = roots(state)
        if found:
            groups.setdefault(frozenset(shallow_of(state)), set()).update(found)
    if not groups:
        return
    source = store_path(source_channel)
    if not os.path.isdir(source):
        raise StoreFailed("repository history not copied: the source channel has no store")
    ensure(new_channel)
    for shallow, wanted in sorted(groups.items(), key=lambda group: sorted(group[0])):
        with _shallow_env(shallow) as env, \
                tempfile.TemporaryDirectory(prefix="git-copy-", dir=git_host.tmp_root()) as work:
            pack = os.path.join(work, "copy.pack")
            try:
                with open(pack, "wb") as out:
                    git_host.run(["pack-objects", "--revs", "--stdout", "-q"], git_dir=source, env=env,
                                 input=_feed(sorted(wanted)), output=out, timeout=STORE_TIMEOUT)
            except git_host.GitFailed as exc:
                raise _failed("repository history not copied", exc) from None
            with store_lock(new_channel):
                target = _existing_store(new_channel)
                try:
                    if _pack_count(pack):
                        with open(pack, "rb") as stream:
                            git_host.run(["index-pack", "--stdin", "--fsck-objects"], git_dir=target, input=stream,
                                         store_write=True, timeout=STORE_TIMEOUT)
                    git_host.run(["rev-list", "--objects", "--missing=error", "--quiet", "--stdin"],
                                 git_dir=target, env=env, input=_feed(sorted(wanted)), timeout=STORE_TIMEOUT)
                except git_host.GitFailed as exc:
                    raise _failed("repository history not copied", exc) from None
    with store_lock(new_channel):
        _check_store(_existing_store(new_channel), set().union(*groups.values()))


# --- ahead and behind (§9) ------------------------------------------------------------------------


_counts: OrderedDict[tuple[str, str, str], dict] = OrderedDict()
_counts_lock = threading.Lock()
_NO_COUNTS = {"ahead": None, "behind": None}


def _upstream_shas(state: RepositoryState) -> tuple[str, str] | None:
    """The shas of the branch HEAD names and of its upstream's tracking
    ref (``branch.<b>.merge`` through ``remote.<r>.fetch``, or itself for
    the remote ``.``), or None when either is unknown."""
    found = branch_config(state)
    if found is None or not found[0].startswith("refs/heads/"):
        return None
    branch_ref, remote, merge = found
    if not remote or not merge:
        return None
    if remote == ".":
        tracking = merge
    else:
        config = state.part("config")
        fetch = []
        for key, value in json.loads(config) if config is not None else ():
            section, subsection, variable = split_config_key(key)
            if section == "remote" and subsection == remote and variable == "fetch" and isinstance(value, str):
                fetch.append(value)
        tracking = _tracking_ref(fetch, merge)
    refs = parse_refs(state.part("refs") or b"")
    local = resolve_ref(refs, refs.get(branch_ref))
    upstream = resolve_ref(refs, refs.get(tracking)) if tracking else None
    return (local, upstream) if local and upstream else None


def _tracking_ref(refspecs: Iterable[str], merge: str) -> str | None:
    """Where the first refspec mapping ``merge`` puts it, as git's
    ``remote_find_tracking`` finds it: a negative (``^``) refspec or one
    without a destination maps nothing (2.31 and 2.53 alike)."""
    for spec in refspecs:
        source, colon, destination = spec.removeprefix("+").partition(":")
        if not colon or not destination or source.startswith("^"):
            continue
        if "*" in source:
            prefix, _, suffix = source.partition("*")
            if merge.startswith(prefix) and merge.endswith(suffix) and len(merge) >= len(prefix) + len(suffix):
                return destination.replace("*", merge[len(prefix):len(merge) - len(suffix)], 1)
        elif source == merge:
            return destination
    return None


def _cache(key: tuple[str, str, str], value: dict) -> None:
    with _counts_lock:
        _counts[key] = value
        _counts.move_to_end(key)
        while len(_counts) > AHEAD_BEHIND_CACHE:
            _counts.popitem(last=False)


def cached_ahead_behind(channel: str, repo_path: str, state_hash: str,
                        load: Callable[[], RepositoryState] | None = None) -> dict:
    """The cached counts of :func:`ahead_behind`, or nulls; never runs git."""
    with _counts_lock:
        found = _counts.get((str(channel), state_hash, repo_path))
    return dict(found) if found is not None else dict(_NO_COUNTS)


def ahead_behind(channel: str, repo_path: str, state_hash: str, load: Callable[[], RepositoryState]) -> dict:
    """``{"ahead", "behind"}`` of a repository's branch against its
    upstream, counted in the channel store by ``rev-list --left-right
    --count <branch>...<upstream>`` (with the state's ``shallow`` as
    ``GIT_SHALLOW_FILE``) in at most :data:`AHEAD_BEHIND_TIMEOUT` seconds,
    plus ``error: {reason, rescued: false}`` when git refused. Never
    raises: no upstream, no host git, no store or a timeout give nulls.
    Deterministic answers are cached in an LRU keyed by ``(channel,
    state_hash, repo_path)``; ``load`` reads the state on a miss. Call it
    off the event loop, for the channels a snapshot sends in full."""
    key = (str(channel), state_hash, repo_path)
    with _counts_lock:
        found = _counts.get(key)
        if found is not None:
            _counts.move_to_end(key)
            return dict(found)
    try:
        state = load()
        shas = _upstream_shas(state)
    except Exception as exc:
        # A corrupt or unreadable state, a DB error: nulls, not a failed snapshot.
        _logger.warning("ahead/behind of %s in %s: state unreadable: %s", repo_path or ".", channel, exc)
        return dict(_NO_COUNTS)
    if shas is None:
        _cache(key, dict(_NO_COUNTS))
        return dict(_NO_COUNTS)
    store = store_path(channel)
    if not git_host.capability().ok or not os.path.isdir(store):
        return dict(_NO_COUNTS)
    local, upstream = shas
    try:
        with _shallow_env(shallow_of(state)) as env:
            done = git_host.run(["rev-list", "--left-right", "--count", f"{local}...{upstream}"], git_dir=store,
                                env=env, timeout=AHEAD_BEHIND_TIMEOUT)
        ahead, behind = (int(n) for n in done.stdout.decode("ascii").split())
        result: dict = {"ahead": ahead, "behind": behind}
    except git_host.GitFailed as exc:
        if exc.returncode is None:
            return dict(_NO_COUNTS)
        result = {**_NO_COUNTS, "error": {"reason": _git_words(exc), "rescued": False}}
    except (OSError, ValueError) as exc:
        _logger.warning("ahead/behind of %s in %s: %s", repo_path or ".", channel, exc)
        return dict(_NO_COUNTS)
    _cache(key, result)
    return dict(result)


def forget_counts() -> None:
    """Empty the ahead/behind cache (tests)."""
    with _counts_lock:
        _counts.clear()


# --- GC (§8) --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Collected:
    """One channel's :func:`collect`: ``status`` is ``collected``,
    ``skipped`` (nothing to do, or an ingest in flight) or ``failed``
    (fail-closed: nothing swept); ``reason`` says why it was not
    collected; ``swept`` counts the unreachable loose objects and
    temporary packs removed, ``rescues`` the rescues past 30 days."""

    channel: str
    status: str
    reason: str = ""
    swept: int = 0
    rescues: int = 0


def collect_all() -> list[Collected]:
    """Collect every registered channel's store, one at a time, while no
    channel is being created or changed; run in a thread after the CAS GC
    (``cas_gc.run_periodic``, ``POST /api/storage/collect``). A parked store
    is never collected."""
    if creating_channels or changing_channels:
        _logger.warning("git GC skipped: a channel is being created, deleted or moved")
        return []
    from collimate_server.state import state, state_lock
    with state_lock:
        channels = [canonical_channel_id(channel.id) for channel in state.channels]
    done = []
    for channel in channels:
        try:
            if os.path.lexists(store_path(channel)) or os.path.lexists(rescue_path(channel)) or _has_rows(channel):
                done.append(collect(channel))
        except channel_paths.ChannelUnavailable as exc:
            _logger.warning("git GC skipped %s: %s", channel, exc.reason)
    return done


def _has_rows(channel: str) -> bool:
    from collimate_server.database import get_db_connection
    try:
        conn = get_db_connection(channel)
    except (FileNotFoundError, OSError):
        return False
    try:
        return conn.execute("SELECT 1 FROM cell_repositories LIMIT 1").fetchone() is not None
    except Exception:
        return True
    finally:
        conn.close()


class _Incomplete(Exception):
    """A fail-closed GC finding (E8): the channel is left as it is."""


def _state_roots(channel: str) -> tuple[set[str], set[str], int]:
    """The roots and shallow shas of every manifest any version of the
    channel names, live or removed, and how many rows there are. A part
    whose body does not hash to its key (``load_state``), or that roots
    would read past (:func:`_check_parts`), fails closed."""
    from collimate_server.database import get_db_connection
    try:
        conn = get_db_connection(channel)
    except Exception as exc:
        raise _Incomplete(f"unreadable channel database: {exc}") from None
    try:
        hashes = [row[0] for row in conn.execute("SELECT DISTINCT state_hash FROM cell_repositories")]
        count = conn.execute("SELECT COUNT(*) FROM cell_repositories").fetchone()[0]
        found: set[str] = set()
        shallow: set[str] = set()
        for state_hash in hashes:
            try:
                state = load_state(conn, state_hash)
                _check_parts(state)
                found |= roots(state)
                shallow |= shallow_of(state)
            except (LookupError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise _Incomplete(f"invalid repository state {state_hash}: {exc}") from None
        return found, shallow, count
    except _Incomplete:
        raise
    except Exception as exc:
        raise _Incomplete(f"unreadable channel database: {exc}") from None
    finally:
        conn.close()


def _check_parts(state: RepositoryState) -> None:
    """ValueError for a stored part whose lines :func:`roots` would skip:
    a HEAD, ``refs`` or ``shallow`` line that is not a value git wrote, a
    malformed or unterminated reflog line. Each would hide a root."""
    head = state.manifest()["head"]
    if not (_HEX40.fullmatch(head) or head.startswith("ref: ")):
        raise ValueError("the manifest's head is malformed")
    for name, body in state.named_parts().items():
        if name.startswith("reflogs/"):
            _, malformed, partial = parse_reflog(body)
            if malformed or partial:
                raise ValueError(f"part {name} has malformed lines")
        elif name in ("refs", "shallow"):
            lines = body.decode("utf-8").split("\n")
            if lines.pop() != "":
                raise ValueError(f"part {name} is unterminated")
            values = [line.rpartition(" ")[0] for line in lines] if name == "refs" else lines
            if not all(_HEX40.fullmatch(value) or (name == "refs" and value.startswith("ref: ")) for value in values):
                raise ValueError(f"part {name} has malformed lines")


def collect(channel: str, *, now: float | None = None) -> Collected:
    """One channel's git GC (§8), under the store lock, fail closed.

    1. The roots of every manifest any version names.
    2. Fail closed, with a line, on an invalid manifest or part, a refused
       ``update-ref``, rows without a store, a non-empty store without
       roots while rows exist, an unreadable DB, a non-zero repack (no
       sweep), a failed sweep unlink or fsync.
    3. Grafts from the ``shallow`` lists; their present parents become roots.
    4. One ``update-ref --stdin`` of ``create refs/gc-roots/<gc-id>/<n>``.
    5. ``GIT_SHALLOW_FILE=<grafts> git -c repack.writeBitmaps=false
       <store-write flags> repack -A -d -q``, after the sweep of the
       ``objects/pack`` temporaries; then the sweep of loose objects, each
       older than ``MIN_BLOB_AGE_SECONDS`` (no symlink followed), and the
       fsyncs. ``git prune`` never runs (P11).
    6. The roots' refs go in a ``finally``; rescues older than 30 days go.

    Skips the channel while an ingest is in flight, and, after the rescue
    sweep, when host git is not usable."""
    from collimate_server.cas_gc import MIN_BLOB_AGE_SECONDS
    identity = canonical_channel_id(channel)
    clock = time.time() if now is None else now
    if ingest_in_flight(identity):
        return Collected(identity, "skipped", "an ingest is in flight")
    with store_lock(identity):
        if ingest_in_flight(identity):
            return Collected(identity, "skipped", "an ingest is in flight")
        try:
            store = _writable(identity)
        except StoreRefused as exc:
            return Collected(identity, "skipped", exc.reason)
        rescues = _sweep_rescues(identity, clock)
        usable = git_host.capability()
        if not usable.ok:
            return Collected(identity, "skipped", f"git on this machine is {usable.describe()}", rescues=rescues)
        try:
            swept = _collect_store(identity, store, clock - MIN_BLOB_AGE_SECONDS)
        except _Incomplete as exc:
            _logger.warning("git GC of %s incomplete, nothing swept: %s", identity, exc)
            return Collected(identity, "failed", str(exc), rescues=rescues)
    if swept is None:
        return Collected(identity, "skipped", "no store", rescues=rescues)
    _logger.info("git GC of %s: swept %d, rescues swept %d", identity, swept, rescues)
    return Collected(identity, "collected", swept=swept, rescues=rescues)


def _collect_store(channel: str, store: str, cutoff: float) -> int | None:
    found, shallow, rows = _state_roots(channel)
    if not os.path.lexists(store):
        if rows:
            raise _Incomplete("rows without a store")
        return None
    if not _owned_directory(store):
        raise _Incomplete("the store is not a directory the server user owns")
    if rows and not found and _store_holds_objects(store):
        raise _Incomplete("a non-empty store without roots while rows exist")
    try:
        grafts, held = _grafts(store, shallow)
    except git_host.GitFailed as exc:
        raise _Incomplete(f"grafts unreadable: {_git_words(exc)}") from None
    found |= held
    gc_id = uuid_module.uuid4().hex[:12]
    names = [f"refs/gc-roots/{gc_id}/{n}" for n in range(len(found))]
    try:
        if found:
            try:
                git_host.run(["update-ref", "--stdin"], git_dir=store, timeout=STORE_TIMEOUT,
                             input=_feed(f"create {name} {sha}" for name, sha in zip(names, sorted(found))))
            except git_host.GitFailed as exc:
                raise _Incomplete(f"update-ref refused the roots: {_git_words(exc)}") from None
        swept = _sweep(store, cutoff, loose=False)
        with _shallow_env(grafts) as env:
            try:
                git_host.run(["-c", "repack.writeBitmaps=false", "repack", "-A", "-d", "-q"], git_dir=store,
                             env=env, store_write=True, timeout=STORE_TIMEOUT)
            except git_host.GitFailed as exc:
                raise _Incomplete(f"repack failed: {_git_words(exc)}") from None
        return swept + _sweep(store, cutoff, loose=True)
    finally:
        _drop_gc_roots(store, gc_id, names)


def _store_holds_objects(store: str) -> bool:
    objects = os.path.join(store, "objects")
    for entry in os.listdir(objects):
        path = os.path.join(objects, entry)
        if entry == "pack":
            if any(name.endswith(".pack") for name in os.listdir(path)):
                return True
        elif _LOOSE_DIR.fullmatch(entry) and os.path.isdir(path) and os.listdir(path):
            return True
    return False


def _sweep(store: str, cutoff: float, *, loose: bool) -> int:
    """Unlink the files last modified before ``cutoff`` (regular files, no
    symlink followed), then fsync what changed; how many. ``loose``: the
    loose objects, after the repack. Otherwise the ``objects/pack``
    temporaries, before it: ``tmp_*`` and the ``.tmp-<pid>-pack-*`` a killed
    repack leaves, which hold no object a row needs and which git would
    read as a pack (a torn ``.pack``/``.idx`` pair fails every later repack)."""
    objects = os.path.join(store, "objects")
    pack = os.path.join(objects, "pack")
    removed = 0
    touched = set()
    candidates = []
    if loose:
        for entry in os.listdir(objects):
            if _LOOSE_DIR.fullmatch(entry):
                directory = os.path.join(objects, entry)
                if os.path.isdir(directory) and not os.path.islink(directory):
                    candidates += [(directory, name) for name in os.listdir(directory) if _LOOSE_FILE.fullmatch(name)]
    else:
        candidates += [(pack, name) for name in os.listdir(pack) if name.startswith(_PACK_TEMPORARIES)]
    try:
        for directory, name in candidates:
            path = os.path.join(directory, name)
            found = os.lstat(path)
            if not stat.S_ISREG(found.st_mode) or found.st_mtime >= cutoff:
                continue
            os.unlink(path)
            removed += 1
            touched.add(directory)
        for directory in sorted(touched | ({objects, pack} if loose else set())):
            fsync_directory(directory)
    except OSError as exc:
        raise _Incomplete(f"sweep failed: {exc}") from None
    return removed


def _drop_gc_roots(store: str, gc_id: str, names: list[str]) -> None:
    if names:
        try:
            git_host.run(["update-ref", "--stdin"], git_dir=store, timeout=STORE_TIMEOUT,
                         input=_feed(f"delete {name}" for name in names))
        except git_host.GitFailed as exc:
            _logger.warning("git GC: could not delete refs/gc-roots/%s: %s", gc_id, exc)
    shutil.rmtree(os.path.join(store, "refs", "gc-roots", gc_id), ignore_errors=True)
    _drop_empty(os.path.join(store, "refs", "gc-roots"))


# --- startup hygiene (C9) -------------------------------------------------------------------------


def startup_hygiene(registered: Iterable[str]) -> list[str]:
    """Run once at startup, before anything can make a quarantine, a ref
    or a lock: remove ``.collimate/tmp/git-harvest-[0-9a-f]{8}_*``, export
    staging (``export-*``) and the other git scratch a killed process left
    there (:data:`_SCRATCH`), no symlink followed; in each registered
    store remove ``packed-refs.lock``, ``HEAD.lock``, ``config.lock`` and
    ``refs/**/*.lock``, then delete every ref, unless host git is not
    usable (:func:`git_host.capability` first). One line per removal."""
    usable = git_host.capability().ok
    outcomes = []
    tmp = git_host.tmp_root()
    for entry in sorted(os.listdir(tmp)):
        if _QUARANTINE.fullmatch(entry) or entry.startswith(_SCRATCH):
            try:
                readonly_trees.remove_tree(os.path.join(tmp, entry))
                outcomes.append(f"removed .collimate/tmp/{entry}")
            except OSError as exc:
                outcomes.append(f"could not remove .collimate/tmp/{entry}: {exc}")
    for channel in sorted({str(name) for name in registered}):
        store = store_path(channel)
        if not _owned_directory(store):
            continue
        with store_lock(channel):
            outcomes += _clear_locks(channel, store)
            if usable:
                outcomes += _delete_refs(channel, store)
    return outcomes


def _clear_locks(channel: str, store: str) -> list[str]:
    stale = [name for name in _STORE_LOCKS if os.path.isfile(os.path.join(store, name))]
    refs = os.path.join(store, "refs")
    if os.path.isdir(refs) and not os.path.islink(refs):
        stale += [f"refs/{rel}" for rel, _ in fs.walk_regular(refs) if rel.endswith(".lock")]
    removed = []
    for name in stale:
        try:
            os.unlink(os.path.join(store, name))
            removed.append(f"removed the stale lock {name} in the git store of {channel}")
        except OSError as exc:
            removed.append(f"could not remove {name} in the git store of {channel}: {exc}")
    return removed


def _delete_refs(channel: str, store: str) -> list[str]:
    try:
        listed = git_host.run(["for-each-ref", "--format=%(refname)"], git_dir=store)
        names = [name for name in listed.stdout.decode("utf-8", "replace").splitlines() if name]
        if names:
            git_host.run(["update-ref", "--stdin"], git_dir=store, input=_feed(f"delete {name}" for name in names))
    except git_host.GitFailed as exc:
        return [f"could not delete the refs in the git store of {channel}: {exc}"]
    gone = os.path.join(store, "refs", "gc-roots")
    if os.path.isdir(gone) and not os.path.islink(gone):
        shutil.rmtree(gone, ignore_errors=True)
    return [f"deleted {len(names)} refs in the git store of {channel}"] if names else []
