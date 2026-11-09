"""Harvest: a run's ``.git`` directories read back into its cell's rows
(``docs/plans/2026-10-31-git-in-cells/design.md`` §4.3, §4.6-§4.9).

A run that holds its cell's lease (:mod:`repository_leases`) gets a real
``.git`` per row at prepare (:func:`materialize`); whatever git it runs
there, the host reads each ``.git`` as data and lands it in the cell's rows
through :func:`harvest`, of four kinds:

* ``final``, once the run's processes are fenced, before its workspace goes;
* ``periodic``, every :data:`PERIODIC_SECONDS` while the run lives
  (:func:`periodic`), when a ``.git`` changed;
* ``now``, one bounded attempt before a copy of a cell a live run holds
  (:func:`now`, :func:`settle`);
* ``recovery``, at startup, for a run the previous server lost
  (:func:`recover`).

Per repository: discovery (``repository_state.discover``), the quarantine,
the reader, ``normalize``, the unchanged check, the band guard, then, inside
``git_store.in_flight``, ``ingest`` and one version (``Git state``) holding
every changed row. A ``final`` or ``recovery`` harvest that cannot keep a
``.git`` rescues it (``git_store.rescue``); the others log and leave it to
the final. All of it is synchronous, for worker threads: a run's harvests
are serialized by its :class:`Lane`, and nothing here holds the store lock
or a cell lock across an await. Nothing here is HTTP; the routes call
:func:`retry_rescue` and :func:`discard_rescue`.

Any other run of the cell gets a read-only ``.git`` per row instead
(``RunGit.readonly``): its reads work, its writes fail, no harvest of it
ever reads it, and it follows the cell, rewritten from each new state a
harvest or a rescue route lands there (:func:`follow`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import stat
import threading
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field

from collimate import fs

import collimate_server.cas as cas
import collimate_server.git_host as git_host
import collimate_server.git_materialize as git_materialize
import collimate_server.git_store as git_store
import collimate_server.readonly_trees as readonly_trees
import collimate_server.repository_leases as repository_leases
import collimate_server.repository_state as rs
from collimate_server.cell_commits import cell_commit_lock, commit_cell_change
from collimate_server.database import changing_channels, get_db_connection
from collimate_server.repository_rows import load_state, rows_of
from collimate_server.reserved_paths import is_reserved_path
from collimate_server.run_folders import is_run_path

_logger = logging.getLogger(__name__)

FINAL = "final"
PERIODIC = "periodic"
NOW = "now"
RECOVERY = "recovery"

# A live run's periodic harvest interval, and the bound of one ``now``.
PERIODIC_SECONDS = 15.0
NOW_SECONDS = 5.0

# How many reads a final harvest tries before an unstable ``.git`` is rescued.
FINAL_TRIES = 3

LABEL = "Git state"
SOURCE_TYPE = "git_harvest"
DISCARD_LABEL = "Discard git history"
DISCARD_SOURCE_TYPE = "git_discard"
RETRY_LABEL = "Retry git history"

# The lock files a periodic tick waits out (§4.9): a ref or packed-refs
# update, a gc, a pack being received.
_LOCKS = ("HEAD.lock", "packed-refs.lock", "gc.pid")


def _no_git(capability: git_host.Capability) -> str:
    return f"git on this machine is {capability.describe()}; install git 2.31 or later"


def no_git_line(capability: git_host.Capability) -> str:
    """Why a run on a cell with repositories gets no ``.git``, on a machine
    without usable git: its run log's line, its card's and its
    ``/runs/active`` row's ``git.reason``."""
    return f"repositories left out: {_no_git(capability)}"


# Why a ``docker: limited`` run on a cell with repositories gets no ``.git``
# (R10: no ``.git``, bind, lease or harvest), said the same three ways.
LIMITED_LINE = "repositories left out: a docker: limited run gets no .git"


def no_git_reason(capability: git_host.Capability) -> str:
    """Why a run's git history was not kept on a machine without usable git."""
    return f"git history from this run was not kept: {_no_git(capability)}"


def _cell_path(repo_path: str, path: str) -> str:
    return f"{repo_path}/{path}" if repo_path else path


# --- the run's git ------------------------------------------------------------------------------


@dataclass
class Lane:
    """One run's harvest lane: its attempts run one at a time under
    ``lock`` across read, ingest and commit; ``ledger`` names the run's
    object files its commits landed, which later quarantines skip;
    ``committed`` is the attempt number of the last commit, so an older
    attempt never lands after a newer one; ``closed`` once the final
    harvest ran, after which no attempt reads or commits."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    ledger: set[str] = field(default_factory=set)
    committed: int = 0
    closed: bool = False


@dataclass
class RunGit:
    """What the harvest knows of one run on one cell: who it is, the tip it
    froze (``frozen_tip``, ``frozen_rows``) and its lease (None while it
    holds none: a run another run's lease kept to a read-only ``.git``, or
    one on a cell without rows until its first harvest claims it).
    ``readonly`` maps each path whose read-only ``.git`` it has to the state
    hash that ``.git`` was last written from (:func:`materialize`,
    :func:`follow`): never harvested, rescued or claimed, for the run's
    whole life. ``holder`` says it claimed at start;
    ``failed`` maps the paths whose materialize failed to why; ``removed``
    names the paths whose row its own harvest removed (a deleted ``.git``),
    which it may write again while no row is there; ``refused``
    names the cell paths the host refused the run's writes of,
    ``skipped`` those its live-files mirror skipped (its final reconcile
    reports them), and ``flush_failed`` says its last cell write failed
    (§4.6). ``auto`` marks ``live_files_mode: auto``, which gets no
    periodic and no ``now``. A recovery harvest's ``recovery_rows`` are
    its base: the rows the run's own last harvest wrote, else (a holder)
    the frozen ones.
    Lines go to the run log through ``log`` (``warn`` for warning events),
    once per run and cause."""

    channel: str
    cell_id: str
    run_id: str
    label: str
    workspace: str
    generator_name: str
    frozen_tip: str
    frozen_rows: dict[str, str]
    lease: repository_leases.Lease | None = None
    holder: bool = False
    readonly: dict[str, str] = field(default_factory=dict)
    auto: bool = False
    secrets: tuple[str, ...] = ()
    failed: dict[str, str] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    flush_failed: bool = False
    recovery_rows: dict[str, str] = field(default_factory=dict)
    lane: Lane = field(default_factory=Lane)
    fingerprints: dict[str, tuple] = field(default_factory=dict)
    logged: set[str] = field(default_factory=set)
    pending: list[tuple[str, str]] = field(default_factory=list)
    log: Callable[[str], None] | None = None
    warn: Callable[[str], None] | None = None

    def say(self, line: str, *, warning: bool = False) -> None:
        """Log ``line`` once per run (a warning event with ``warning``);
        lines before the run log exists wait for :meth:`attach`."""
        if line in self.logged:
            return
        self.logged.add(line)
        sink = self.warn if warning else self.log
        if sink is None:
            self.pending.append(("warn" if warning else "log", line))
        else:
            sink(line)

    def attach(self, log: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Send lines to the run log from now on, the waiting ones first."""
        self.log, self.warn = log, warn
        pending, self.pending = self.pending, []
        for kind, line in pending:
            (warn if kind == "warn" else log)(line)

    @property
    def materialized(self) -> dict[str, str]:
        return self.lease.materialized if self.lease is not None else {}


def secret_forms(account_context: Mapping[str, Mapping]) -> tuple[str, ...]:
    """The secret forms of the run's bound Git accounts, which the
    sanitizer redacts from every state it stores."""
    from collimate.git_hosts import account_from_record
    forms: list[str] = []
    for account in account_context.values():
        if account.get("kind") != "git":
            continue
        try:
            forms += account_from_record(dict(account)).secret_forms()
        except ValueError:
            continue
    return tuple(dict.fromkeys(forms))


# --- materialize failures, for the snapshot ------------------------------------------------------

_failures: dict[tuple[str, str], dict[str, str]] = {}
_failures_lock = threading.Lock()


def materialize_errors(channel: str, cell_id: str) -> dict[str, dict]:
    """The cell's repositories whose last materialize failed, as the
    snapshot entry's ``error`` (``{reason, rescued: false}``) by path."""
    with _failures_lock:
        failed = dict(_failures.get((str(channel), str(cell_id)), {}))
    return {path: {"reason": reason, "rescued": False} for path, reason in failed.items()}


def _note_failures(channel: str, cell_id: str, failed: Mapping[str, str]) -> None:
    with _failures_lock:
        if failed:
            _failures[(str(channel), str(cell_id))] = dict(failed)
        else:
            _failures.pop((str(channel), str(cell_id)), None)


def _clear_failure(channel: str, cell_id: str, repo_path: str) -> None:
    with _failures_lock:
        held = _failures.get((str(channel), str(cell_id)))
        if held is not None:
            held.pop(repo_path, None)
            if not held:
                del _failures[(str(channel), str(cell_id))]


def forget_failures() -> None:
    """Drop every recorded materialize failure (tests)."""
    with _failures_lock:
        _failures.clear()


# --- materialize (§4.3) --------------------------------------------------------------------------


def materialize(run: RunGit, hydrated: Collection[str]) -> dict[str, tuple[git_materialize.Link, ...]]:
    """Write a ``.git`` for each of the frozen tip's rows, within the cap,
    into the run's workspace (call after ``hydrate_workspace`` and before
    ``file_revisions.json``; ``hydrated`` names the cell files it wrote).
    The holder's is writable: a success sets ``materialized[repo_path]``; a
    failure leaves the repository out with its rows kept and its error on
    the snapshot entry. A run without the lease (another run holds it) gets
    a read-only one: a success sets ``readonly[repo_path]``; a failure
    leaves it out, with its line in the run log alone. Returns the links to
    create per repository (:func:`create_links`)."""
    if not run.frozen_rows:
        return {}
    capability = git_host.capability()
    if not capability.ok:
        run.say(no_git_line(capability))
        return {}
    lease = run.lease
    readonly = lease is None
    kept, overflow = rs.cap_repositories(run.frozen_rows, run.frozen_rows)
    if overflow:
        run.say(f"{overflow} repositories over the cell's {rs.MAX_REPOSITORIES_PER_CELL} left out")
    objects = git_store.objects_path(run.channel)
    links: dict[str, tuple[git_materialize.Link, ...]] = {}
    failed: dict[str, str] = {}
    conn = get_db_connection(run.channel)
    try:
        states = {}
        for repo_path in kept:
            try:
                states[repo_path] = load_state(conn, run.frozen_rows[repo_path])
            except (LookupError, ValueError) as exc:
                failed[repo_path] = f"stored state unreadable: {exc}"
                run.say(f"git {rs.shown(repo_path)}: materialize failed: {failed[repo_path]}")
    finally:
        conn.close()
    for repo_path, state in states.items():
        work_tree = os.path.join(run.workspace, *fs.relative_parts(repo_path)) if repo_path else run.workspace
        try:
            os.makedirs(work_tree, exist_ok=True)
            done = git_materialize.write_gitdir(work_tree, state, objects, repo_path=repo_path, hydrated=hydrated,
                                                readonly=readonly)
        except git_materialize.MaterializeFailed as exc:
            failed[repo_path] = exc.entry()["reason"]
            for line in exc.lines:
                run.say(f"git {line}" if line.startswith(rs.shown(repo_path)) else f"git {rs.shown(repo_path)}: {line}")
            continue
        except OSError as exc:
            failed[repo_path] = f"work tree unwritable: {exc.strerror or exc}"
            run.say(f"git {rs.shown(repo_path)}: materialize failed: {failed[repo_path]}")
            continue
        if readonly:
            run.readonly[repo_path] = done.state_hash
        else:
            lease.materialized[repo_path] = done.state_hash
        links[repo_path] = done.links
        for line in done.lines:
            run.say(f"git {rs.shown(repo_path)}: {line}")
        for line in done.warnings:
            run.say(f"git {rs.shown(repo_path)}: {line}", warning=True)
    if not readonly:
        # The cell's materialize failures are the holder's to report.
        run.failed = failed
        _note_failures(run.channel, run.cell_id, failed)
    return links


def create_links(run: RunGit, planned: Mapping[str, Iterable[git_materialize.Link]]) -> None:
    """Create the links HEAD records for each materialized repository, after
    the run's last host write (``file_revisions.json``). Each is checked
    again across the cell's repositories (``link_ok_in_cell``): a link
    whose target passes through another repository's link is not created.
    Then any created link that resolves outside the workspace is removed."""
    planned = {repo_path: tuple(links) for repo_path, links in planned.items()}
    every = {_cell_path(repo_path, link.path).casefold() for repo_path, links in planned.items() for link in links}
    made: list[str] = []
    for repo_path, links in planned.items():
        kept, refused = [], []
        for link in links:
            cell = _cell_path(repo_path, link.path)
            (kept if git_materialize.link_ok_in_cell(cell, link.target, every) else refused).append(link)
        if refused:
            run.say(f"git {rs.shown(repo_path)}: links not created (through another repository's link): "
                    f"{', '.join(link.path for link in refused)}")
        work_tree = os.path.join(run.workspace, *fs.relative_parts(repo_path)) if repo_path else run.workspace
        for line in git_materialize.create_links(work_tree, kept, repo_path=repo_path):
            run.say(f"git {rs.shown(repo_path)}: {line}")
        made += [_cell_path(repo_path, link.path) for link in kept]
    root = os.path.realpath(run.workspace)
    for cell in made:
        path = os.path.join(run.workspace, *cell.split("/"))
        if not os.path.islink(path):
            continue
        resolved = os.path.realpath(path)
        if resolved != root and not resolved.startswith(root + os.sep):
            os.unlink(path)
            run.say(f"git: link {cell} removed: it resolves outside the workspace")


# --- fingerprints (§4.9) -------------------------------------------------------------------------


def fingerprint(workspace: str, repo_path: str) -> tuple[tuple, bool] | None:
    """What a ``.git`` looks like now, cheaply: ``(size, mtime_ns)`` of the
    files the reader reads plus the mtimes of every directory, ``objects/``,
    ``objects/pack`` and the fan-out directories included (their object
    files are not opened); and whether a ref, packed-refs or gc lock or a
    pack being received is there. None when the ``.git`` cannot be opened."""
    try:
        parts = fs.relative_parts(repo_path) if repo_path else []
        parent = fs.open_parent(workspace, [*parts, ".git"], create=False)
        try:
            gitdir = os.open(".git", os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    except (OSError, PermissionError):
        return None
    seen: list[tuple] = []
    locked = False

    def prune(path: str, entry: os.DirEntry) -> bool:
        nonlocal locked
        name = entry.name
        if path in _LOCKS or (path.startswith("refs/") and name.endswith(".lock")):
            locked = True
        under_objects = path.startswith("objects/")
        if path.startswith("objects/pack/"):
            if name.startswith("tmp_pack_"):
                locked = True
            return True
        try:
            found = entry.stat(follow_symlinks=False)
        except OSError:
            return True
        if stat.S_ISDIR(found.st_mode):
            seen.append((path, "d", found.st_mtime_ns))
            return under_objects and path != "objects/pack"
        return under_objects

    try:
        for path, fd in fs.walk_regular(".", dir_fd=gitdir, prune=prune, max_depth=rs.MAX_DEPTH,
                                        max_entries=rs.GITDIR_MAX_ENTRIES):
            found = os.fstat(fd)
            seen.append((path, "f", found.st_size, found.st_mtime_ns))
        seen.append((".", "d", os.fstat(gitdir).st_mtime_ns))
    finally:
        os.close(gitdir)
    return tuple(sorted(seen)), locked


# --- harvest (§4.7, §4.8) ------------------------------------------------------------------------


@dataclass
class _Candidate:
    repo_path: str
    quarantine: git_store.Quarantine
    state: rs.RepositoryState
    base: rs.RepositoryState | None


@dataclass
class _Refused:
    reason: str
    git_stderr: str = ""


@dataclass
class Harvested:
    """What one harvest did: the version it wrote (None: nothing changed or
    nothing could land), the paths it rescued, and those it could neither
    land nor rescue (``unkept``: their ``.git`` must stay in the workspace
    for a later recovery)."""

    version_id: str | None = None
    rescued: tuple[str, ...] = ()
    unkept: tuple[str, ...] = ()


def _base_kept_out(base: rs.RepositoryState | None) -> list[str]:
    if base is None:
        return []
    return [line for line in (base.part("kept_out") or b"").decode("utf-8").split("\n") if line]


def _kept_out(repo_path: str, read: rs.GitdirRead, objects: rs.GitdirObjects, base: rs.RepositoryState | None,
              tip_files: set[str], *, add: Callable[[str], bool] | None = None) -> tuple[list[str], int]:
    """A candidate's ``kept_out`` (§4.6): the base's entries still tracked
    in its HEAD and absent from the cell, plus, with ``add``, the HEAD
    regular files absent from the cell that ``add`` says the cell could not
    take; and how many other tracked files (gitlinks aside) the cell
    lacks."""
    carried = _base_kept_out(base)
    head = rs.resolve_ref(read.refs, read.head) if read.head is not None else None
    if head is None:
        return [], 0
    try:
        tree = objects.tree(head)
    except git_host.GitFailed:
        return carried, 0
    files = {}
    for mode, _sha, raw in tree:
        try:
            files[raw.decode("utf-8")] = mode
        except UnicodeDecodeError:
            continue
    absent = {path for path in files if _cell_path(repo_path, path) not in tip_files}
    kept = {path for path in carried if path in absent}
    if add is not None:
        kept |= {path for path in absent - kept if files[path] in ("100644", "100755") and add(path)}
    return sorted(kept), len([path for path in absent - kept if files[path] != "160000"])


def _discover(run: RunGit) -> rs.Discovery:
    """The run's repositories as its harvests see them: every one but those
    whose read-only ``.git`` it has, which nothing harvests, rescues or
    claims, whatever the run did there."""
    found = rs.discover(run.workspace)
    if not run.readonly:
        return found
    return rs.Discovery(tuple(path for path in found.repositories if path not in run.readonly),
                        {path: line for path, line in found.unusable.items() if path not in run.readonly},
                        found.errors)


def _blocked_reason(run: RunGit, otherwise: str) -> str:
    """Why a run may not write a ``.git``: another run's lease, else
    ``otherwise``."""
    held = repository_leases.holder(run.channel, run.cell_id)
    if held is not None and held is not run.lease:
        return f"repository lease held by run {held.run_id[:8]}"
    return otherwise


class _Harvest:
    """One harvest of one run, under its lane."""

    def __init__(self, run: RunGit, kind: str, capability: git_host.Capability):
        self.run = run
        self.kind = kind
        self.capability = capability
        self.rescued: list[str] = []
        self.unkept: list[str] = []
        self.final = kind in (FINAL, RECOVERY)

    # -- helpers --------------------------------------------------------

    def say(self, line: str, *, warning: bool = False) -> None:
        self.run.say(line, warning=warning)

    def gitdir(self, repo_path: str) -> str:
        parts = fs.relative_parts(repo_path) if repo_path else []
        return os.path.join(self.run.workspace, *parts, ".git")

    def rescue(self, repo_path: str, reason: str, git_stderr: str = "", *, cell_id: str | None = None,
               base_state_hash: str | None = None) -> None:
        """Keep a ``.git`` this harvest cannot (final and recovery only),
        under the cell lock: a Retry or Discard of the cell's rescue, which
        holds it from its read to its removal, never removes this newer one."""
        run = self.run
        if base_state_hash is None:
            base_state_hash = run.materialized.get(repo_path) or run.frozen_rows.get(repo_path)
        try:
            with cell_commit_lock(cell_id or run.cell_id):
                git_store.rescue(run.channel, cell_id or run.cell_id, repo_path, self.gitdir(repo_path),
                                 run_id=run.run_id, base_state_hash=base_state_hash, reason=reason,
                                 git_stderr=git_stderr, secrets=run.secrets)
        except (OSError, git_store.StoreFailed, ValueError) as exc:
            self.say(f"git {rs.shown(repo_path)}: not kept ({reason}), and could not be rescued: {exc}",
                     warning=True)
            # A channel being deleted takes its runs' history with it; any
            # other failure keeps the .git in the workspace for recovery.
            if not (isinstance(exc, git_store.StoreRefused) and run.channel in changing_channels):
                self.unkept.append(repo_path)
            return
        self.rescued.append(repo_path)
        self.say(f"git {rs.shown(repo_path)}: history not saved: {reason}; kept as a rescue (Retry, Export or "
                 "Discard it on the cell)", warning=True)

    def result(self, version_id: str | None) -> Harvested:
        return Harvested(version_id, tuple(self.rescued), tuple(self.unkept))

    def block(self, repo_path: str, reason: str) -> None:
        """A ``.git`` the run may not write: rescued by a final harvest."""
        if self.final:
            self.rescue(repo_path, reason)
        else:
            self.say(f"git {rs.shown(repo_path)}: not harvested: {reason}")

    def refuse(self, repo_path: str, refusal: _Refused) -> None:
        if self.final:
            self.rescue(repo_path, refusal.reason, refusal.git_stderr)
        else:
            self.say(f"git {rs.shown(repo_path)}: not harvested: {refusal.reason}")

    # -- the steps ------------------------------------------------------

    def tip(self) -> tuple[str, dict[str, str], set[str]]:
        conn = get_db_connection(self.run.channel)
        try:
            from collimate_server.versions import find_tip, tip_file_map
            tip = find_tip(conn, self.run.cell_id)
            return tip, rows_of(conn, tip), set(tip_file_map(conn, tip))
        finally:
            conn.close()

    def base_of(self, repo_path: str) -> tuple[rs.RepositoryState | None, str | None]:
        """The state a repository is harvested against: what the run
        materialized or last harvested there (recovery: its
        ``recovery_rows``), else none."""
        run = self.run
        if self.kind == RECOVERY:
            base_hash = run.recovery_rows.get(repo_path)
        else:
            base_hash = run.materialized.get(repo_path)
        if base_hash is None:
            return None, None
        conn = get_db_connection(run.channel)
        try:
            return load_state(conn, base_hash), base_hash
        finally:
            conn.close()

    def writable(self, repo_path: str, tip_rows: Mapping[str, str]) -> bool:
        """§4.8's writable paths: the run's materialized ones; as holder,
        those whose materialize failed; those whose row the run's own harvest
        removed while no row is there (a ``.git`` deleted and made again);
        and paths without a row at freeze whose current row, if any, is the
        run's own (recovery: those of its ``recovery_rows``, and paths
        without a row then or now)."""
        run = self.run
        if self.kind == RECOVERY:
            if repo_path in run.recovery_rows:
                return True
        elif (repo_path in run.materialized or (run.holder and repo_path in run.failed)
              or (repo_path in run.removed and tip_rows.get(repo_path) is None)):
            return True
        if repo_path in run.frozen_rows:
            return False
        current = tip_rows.get(repo_path)
        return current is None or current == run.materialized.get(repo_path)

    def claim(self) -> bool:
        """The mid-run claim (§4.1): a run without the lease takes it when no
        run holds it and the tip's rows still equal those it froze; a final
        harvest takes it releasing, so starts and copies wait on it."""
        return _claim(self.run, repository_leases.RELEASING if self.final else repository_leases.LIVE)

    def kept_out(self, repo_path: str, read: rs.GitdirRead, quarantine: git_store.Quarantine,
                 base: rs.RepositoryState | None, tip_files: set[str]) -> list[str]:
        """The repository's ``kept_out`` (§4.6): ``periodic`` and ``now``
        copy the base's; the others carry forward the base's entries still
        tracked in the candidate HEAD and absent from the cell, and the
        final harvest after a flush that landed adds the HEAD files the cell
        could not take."""
        if self.kind in (PERIODIC, NOW):
            return _base_kept_out(base)
        adds = self.kind == FINAL and not self.run.flush_failed
        kept, lacking = _kept_out(repo_path, read, quarantine.objects, base, tip_files,
                                  add=(lambda path: self.withheld(repo_path, path)) if adds else None)
        if lacking and not adds:
            self.say(f"git {rs.shown(repo_path)}: cell lacks {lacking} tracked files")
        return kept

    def withheld(self, repo_path: str, path: str) -> bool:
        """Whether a HEAD file regular in the work tree is one the cell could
        not take: the run's live-files mirror skipped it (its final
        reconcile said so, or it lies under a skip directory, which the
        mirror never reads), or the host refused or dropped the run's write
        of it (a refusal, a reserved or run-folder path, a body over the
        blob cap)."""
        cell = _cell_path(repo_path, path)
        try:
            found = os.lstat(os.path.join(self.run.workspace, *cell.split("/")))
        except OSError:
            return False
        if not stat.S_ISREG(found.st_mode):
            return False
        segments = cell.split("/")
        return (cell in self.run.skipped or cell in self.run.refused or is_reserved_path(cell)
                or is_run_path(cell) or any(segment in fs.WORKSPACE_SKIP_DIRS for segment in segments[:-1])
                or found.st_size > cas.MAX_BLOB_BYTES)

    def read(self, repo_path: str, tip_files: set[str], stack: ExitStack) -> _Candidate | _Refused | None:
        """Steps 1-5 for one repository: the candidate state with its open
        quarantine, a refusal, or None when nothing changed (or, for a
        periodic tick, when it should wait)."""
        run = self.run
        try:
            base, base_hash = self.base_of(repo_path)
        except (LookupError, ValueError) as exc:
            return _Refused(f"base state unreadable: {exc}")
        tries = FINAL_TRIES if self.final else 1
        for _attempt in range(tries):
            before = fingerprint(run.workspace, repo_path)
            if before is None:
                return _Refused("gitdir unreadable")
            if self.kind == PERIODIC:
                if before[1] or run.fingerprints.get(repo_path) == before[0]:
                    return None
            try:
                quarantine = git_store.quarantine(run.channel, run.workspace, repo_path, run_id=run.run_id,
                                                  ledger=run.lane.ledger)
            except git_store.StoreFailed as exc:
                return _Refused(exc.reason, exc.git_stderr)
            try:
                read = rs.read_repository(run.workspace, repo_path, alternates=git_store.objects_path(run.channel))
                if self.kind == RECOVERY and read.refusal is None and read.head is None:
                    quarantine.close()
                    return _Refused("HEAD unreadable", _head_words(self.gitdir(repo_path)))
                kept_out = self.kept_out(repo_path, read, quarantine, base, tip_files) \
                    if read.refusal is None else []
                normalized = rs.normalize(read, quarantine.objects, base=base, secrets=run.secrets,
                                          kept_out=kept_out)
                after = fingerprint(run.workspace, repo_path)
            except BaseException:
                quarantine.close()
                raise
            if after is None or after[0] != before[0]:
                quarantine.close()
                continue
            for line in normalized.dropped:
                self.say(f"git {rs.shown(repo_path)}: {line}")
            for line in normalized.warnings:
                self.say(f"git {rs.shown(repo_path)}: {line}", warning=True)
            if normalized.refusal is not None or normalized.state is None:
                quarantine.close()
                return _Refused(normalized.refusal or "not readable")
            if self.kind == PERIODIC:
                run.fingerprints[repo_path] = before[0]
            state = normalized.state
            if state.manifest_hash == base_hash and not quarantine.copied:
                quarantine.close()
                return None
            stack.callback(quarantine.close)
            try:
                guarded = git_store.band_guard(quarantine, state, base)
            except git_store.StoreFailed as exc:
                return _Refused(exc.reason, exc.git_stderr)
            for line in guarded.dropped:
                self.say(f"git {rs.shown(repo_path)}: {line}")
            for line in guarded.warnings:
                self.say(f"git {rs.shown(repo_path)}: {line}", warning=True)
            if guarded.refusal is not None or guarded.state is None:
                return _Refused(guarded.refusal or "history holds .collrun/")
            return _Candidate(repo_path, quarantine, guarded.state, base)
        return _Refused(f"the .git kept changing while it was read ({tries} tries)") if self.final else None

    def run_harvest(self) -> Harvested:
        run = self.run
        found = _discover(run)
        for repo_path, line in sorted(found.unusable.items()):
            self.say(f"git {line}")
        for line in found.errors:
            self.say(f"git: workspace not fully read: {line}")
        if self.final and not self.capability.ok:
            for repo_path in found.repositories:
                self.rescue(repo_path, no_git_reason(self.capability))
            return self.result(None)
        _tip, tip_rows, tip_files = self.tip()
        paths, overflow = rs.cap_repositories(found.repositories, tip_rows)
        if overflow:
            self.say(f"git: {overflow} repositories over the cell's {rs.MAX_REPOSITORIES_PER_CELL} not kept")
        writable = [path for path in paths if self.writable(path, tip_rows)]
        for repo_path in paths:
            if repo_path not in writable:
                self.block(repo_path, _blocked_reason(run, f"cell already has a repository at {rs.shown(repo_path)}"))
        if writable and run.lease is None and not self.claim():
            for repo_path in writable:
                self.block(repo_path, _blocked_reason(run, f"cell changed since run {run.run_id[:8]}"))
            writable = []
        if run.lease is None:
            return self.result(None)
        seq = run.lease.next_seq()
        removed: list[str] = []
        if self.kind != RECOVERY and not found.errors:
            present = set(found.repositories) | set(found.unusable)
            for repo_path in sorted(set(run.materialized) - present):
                try:
                    os.lstat(self.gitdir(repo_path))
                except FileNotFoundError:
                    removed.append(repo_path)
                except OSError:
                    continue
        with ExitStack() as stack:
            candidates: list[_Candidate] = []
            for repo_path in writable:
                got = self.read(repo_path, tip_files, stack)
                if isinstance(got, _Refused):
                    self.refuse(repo_path, got)
                elif got is not None:
                    candidates.append(got)
            if not candidates and not removed:
                return self.result(None)
            with git_store.in_flight(run.channel):
                landed: list[_Candidate] = []
                for candidate in candidates:
                    try:
                        git_store.ingest(candidate.quarantine, candidate.state, candidate.base)
                    except git_store.StoreFailed as exc:
                        self.refuse(candidate.repo_path, _Refused(exc.reason, exc.git_stderr))
                        continue
                    landed.append(candidate)
                committed, version_id = self.commit(seq, landed, removed)
            if committed:
                for candidate in landed:
                    run.lane.ledger |= candidate.quarantine.landed()
        return self.result(version_id)

    def commit(self, seq: int, landed: list[_Candidate], removed: list[str]) -> tuple[bool, str | None]:
        """Step 9: one version replacing the changed rows (``None`` for a
        removed ``.git``), under the cell lock. Returns whether it landed
        (not a stale attempt or a DB failure) and the version, None when
        every row was unchanged."""
        run = self.run
        lease = run.lease
        assert lease is not None
        patch: dict[str, rs.RepositoryState | None] = {c.repo_path: c.state for c in landed}
        patch.update({repo_path: None for repo_path in removed})
        try:
            with cell_commit_lock(run.cell_id):
                if seq <= run.lane.committed:
                    return False, None
                _tip, tip_rows, _files = self.tip()
                if self.kind == RECOVERY:
                    for candidate in list(landed):
                        if tip_rows.get(candidate.repo_path) != run.recovery_rows.get(candidate.repo_path):
                            del patch[candidate.repo_path]
                            landed.remove(candidate)
                            self.rescue(candidate.repo_path, f"cell changed since run {run.run_id[:8]}")
                changed = sorted(path for path, state in patch.items()
                                 if (tip_rows.get(path) != state.manifest_hash if state is not None
                                     else path in tip_rows))
                version_id = commit_cell_change(
                    run.channel, run.cell_id, run.run_id, repositories=patch, label=LABEL,
                    source_type=SOURCE_TYPE, generator_name=run.generator_name,
                    in_transaction=self.mark_harvested_in if self.final else None)
                run.lane.committed = seq
        except Exception as exc:
            if not self.final:
                self.say(f"git: harvest not saved: {exc}")
                return False, None
            for candidate in landed:
                self.rescue(candidate.repo_path, f"the cell's git state could not be saved: {exc}")
            return False, None
        for repo_path, state in patch.items():
            if state is None:
                lease.materialized.pop(repo_path, None)
                run.removed.add(repo_path)
            else:
                lease.materialized[repo_path] = state.manifest_hash
                run.removed.discard(repo_path)
            run.failed.pop(repo_path, None)
            _clear_failure(run.channel, run.cell_id, repo_path)
        if version_id is not None:
            self.say(f"git: saved {', '.join(rs.shown(path) for path in changed)}")
            follow(run.channel, run.cell_id, by=run.run_id)
        return True, version_id

    def mark_harvested_in(self, conn, _before, _after) -> None:
        if self.unkept:
            return
        conn.execute("UPDATE runs SET git_harvested_at=COALESCE(git_harvested_at, CURRENT_TIMESTAMP) WHERE id=?",
                     (self.run.run_id,))

    def mark_harvested(self) -> None:
        """A final or recovery harvest that wrote no version still records
        that it ran, so no later recovery harvests the run again; not while
        a ``.git`` it could neither land nor rescue waits in the workspace."""
        if not self.final or self.unkept:
            return
        conn = get_db_connection(self.run.channel)
        try:
            self.mark_harvested_in(conn, None, None)
            conn.commit()
        finally:
            conn.close()


def _head_words(gitdir: str) -> str:
    """What git says of an unreadable HEAD (P15)."""
    try:
        git_host.run(["rev-parse", "--verify", "HEAD"], git_dir=gitdir)
    except git_host.GitFailed as exc:
        return exc.stderr
    return ""


def harvest(run: RunGit, kind: str) -> Harvested:
    """Harvest the run's repositories (``kind``: :data:`FINAL`,
    :data:`PERIODIC`, :data:`NOW` or :data:`RECOVERY`) under its lane.
    Without usable host git ``periodic`` and ``now`` do nothing and
    ``final`` and ``recovery`` rescue every ``.git``. A ``periodic`` or
    ``now`` attempt that gets the lane once the run's final harvest ran, or
    once its lease is gone, does nothing: the run no longer writes the
    cell's git state (R1)."""
    capability = git_host.capability()
    if kind in (PERIODIC, NOW) and not capability.ok:
        return Harvested()
    with run.lane.lock:
        if kind in (PERIODIC, NOW) and _over(run):
            return Harvested()
        work = _Harvest(run, kind, capability)
        try:
            return work.run_harvest()
        except Exception as exc:
            _logger.exception("git harvest (%s) of run %s failed", kind, run.run_id)
            if not work.final:
                run.say(f"git: harvest failed: {exc}")
                return work.result(None)
            for repo_path in _discover(run).repositories:
                work.rescue(repo_path, f"harvest failed: {exc}")
            return work.result(None)
        finally:
            work.mark_harvested()
            if kind == FINAL:
                run.lane.closed = True


def _claim(run: RunGit, state) -> bool:
    """Take the cell's lease for ``run`` in ``state`` when no run holds it
    and the tip's rows still equal those the run froze."""
    with cell_commit_lock(run.cell_id):
        conn = get_db_connection(run.channel)
        try:
            from collimate_server.versions import find_tip
            rows = rows_of(conn, find_tip(conn, run.cell_id))
        finally:
            conn.close()
        if rows != run.frozen_rows:
            return False
        try:
            run.lease = repository_leases.claim(run.channel, run.cell_id, run.run_id, run.label, state=state)
        except repository_leases.RepositoryBusy:
            return False
    return True


def begin_release(run: RunGit) -> None:
    """At the run's exit, before its final harvest (§4.1 Waiting): its lease
    turns releasing (a stopped run's already is, since its stop request:
    ``ProcessManager.request_stop``); a run that holds none but left a
    ``.git`` takes the lease releasing now (the mid-run claim of its final
    harvest), so a run, terminal or copy started while that harvest lands
    waits for it (a first Clone into a cell without rows); a read-only
    ``.git`` is not one. Blocking: call it from a worker thread."""
    if run.lease is None and git_host.capability().ok and _discover(run).repositories:
        _claim(run, repository_leases.RELEASING)
    repository_leases.begin_release(run.channel, run.cell_id, run.run_id)


def _over(run: RunGit) -> bool:
    """Whether the run's harvests are over: its final harvest ran, or the
    lease it held was released or is another's."""
    if run.lane.closed:
        return True
    lease = run.lease
    return lease is not None and (lease.released.is_set()
                                  or repository_leases.holder(run.channel, run.cell_id) is not lease)


# --- when (§4.9) ---------------------------------------------------------------------------------


async def periodic(run: RunGit, stop: asyncio.Event) -> None:
    """A live run's periodic harvest: every :data:`PERIODIC_SECONDS` until
    ``stop`` is set, one attempt at a time (an overrun skips a tick)."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PERIODIC_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(harvest, run, PERIODIC)
        except Exception:
            _logger.exception("periodic git harvest of run %s failed", run.run_id)


def _live_run(channel: str, cell_id: str) -> RunGit | None:
    lease = repository_leases.holder(channel, cell_id)
    if lease is None or lease.state != repository_leases.LIVE:
        return None
    from collimate_server.run_registry import active_managers
    manager = active_managers.get(lease.run_id)
    run = getattr(manager, "git", None)
    if not isinstance(run, RunGit) or run.auto or run.lease is not lease:
        return None
    return run


def now(channel: str, cell_id: str, *, timeout: float = NOW_SECONDS) -> bool:
    """One harvest attempt of at most ``timeout`` seconds for the live,
    non-``auto`` run holding the cell's lease, before a copy reads its rows.
    True when it finished in time (or there was nothing to do); on a timeout
    the caller goes on with the last rows, logged. Blocking: call it from a
    worker thread."""
    run = _live_run(channel, cell_id)
    if run is None:
        return True
    done = threading.Event()

    def attempt() -> None:
        try:
            harvest(run, NOW)
        finally:
            done.set()

    threading.Thread(target=attempt, name=f"git-now:{run.run_id[:8]}", daemon=True).start()
    if done.wait(timeout):
        return True
    _logger.warning("git state of cell %s not harvested from run %s within %.0f s; using the last rows",
                    cell_id, run.run_id[:8], timeout)
    return False


def settle(channel: str, cell_ids: Iterable[str]) -> None:
    """Before a copy of ``cell_ids`` (Duplicate, Run in new cell, Fork from
    the tip, export): wait up to a minute on a releasing lease (a run's
    final harvest, a recovery), then harvest a live holder :func:`now`.
    Blocking; call it outside any cell lock, from a worker thread."""
    for cell_id in dict.fromkeys(str(c) for c in cell_ids):
        held = repository_leases.holder(channel, cell_id)
        if held is not None and held.state == repository_leases.RELEASING:
            _logger.info("Saving git state of cell %s (run %s) before the copy", cell_id, held.run_id[:8])
            repository_leases.wait_released_sync(channel, cell_id)
        now(channel, cell_id)


# --- the read-only .git follows the cell ---------------------------------------------------------


def follow(channel: str, cell_id: str, *, by: str | None = None) -> None:
    """After a version changed the cell's rows: every live run of the cell
    but ``by`` that has a read-only ``.git`` gets each brought to the cell's
    tip (:func:`follow_run`). Never blocks or raises."""
    from collimate_server.run_registry import active_managers
    for manager in active_managers.values():
        run = getattr(manager, "git", None)
        if (isinstance(run, RunGit) and run.readonly and run.run_id != by
                and (run.channel, run.cell_id) == (str(channel), str(cell_id))):
            follow_run(run)


def follow_run(run: RunGit) -> None:
    """Bring the run's read-only ``.git`` directories to its cell's tip, off
    the caller's thread (:func:`_follow`)."""
    threading.Thread(target=_follow, args=(run,), name=f"git-follow:{run.run_id[:8]}", daemon=True).start()


def _follow(run: RunGit) -> None:
    """Under the run's lane, so never beside its own harvest: a read-only
    ``.git`` whose tip row is no longer the state it was written from is
    written again from the tip's, and one whose row is gone is removed.
    Reading the tip here, not the state that set it off, keeps the newest
    state whichever of two threads gets the lane last. Nothing once the
    run's final harvest ran. A failure leaves the old tree, with one line
    in the run log."""
    try:
        with run.lane.lock:
            if run.lane.closed or not run.readonly or not git_host.capability().ok:
                return
            from collimate_server.versions import find_tip
            states: dict[str, rs.RepositoryState] = {}
            conn = get_db_connection(run.channel)
            try:
                tip_rows = rows_of(conn, find_tip(conn, run.cell_id))
                for repo_path, built in sorted(run.readonly.items()):
                    current = tip_rows.get(repo_path)
                    if current is None or current == built:
                        continue
                    try:
                        states[repo_path] = load_state(conn, current)
                    except (LookupError, ValueError) as exc:
                        run.say(f"git {rs.shown(repo_path)}: read-only .git not updated: stored state unreadable: {exc}")
            finally:
                conn.close()
            for repo_path in sorted(path for path in run.readonly if path not in tip_rows):
                _drop_readonly(run, repo_path)
            for repo_path, state in states.items():
                _rewrite_readonly(run, repo_path, state)
    except Exception:
        _logger.exception("the read-only .git of run %s could not follow its cell", run.run_id)


def _work_tree(run: RunGit, repo_path: str) -> str:
    return os.path.join(run.workspace, *fs.relative_parts(repo_path)) if repo_path else run.workspace


def _present(run: RunGit, repo_path: str) -> set[str]:
    """The regular files the run's workspace holds under ``repo_path`` now,
    as cell paths: a rewritten read-only ``.git``'s ``hydrated``."""
    def prune(path: str, _entry: os.DirEntry) -> bool:
        cell = _cell_path(repo_path, path)
        return is_reserved_path(cell) or is_run_path(cell)

    return {_cell_path(repo_path, path) for path, _fd in fs.walk_regular(_work_tree(run, repo_path), prune=prune)}


def _rewrite_readonly(run: RunGit, repo_path: str, state: rs.RepositoryState) -> None:
    """Write the read-only ``.git`` at ``repo_path`` again from ``state``,
    built in the run band (``.collrun``, never mirrored into the cell) and
    put in place entry by entry (``write_gitdir(readonly=True)``), through
    the repository's folder as reached from the workspace through no link
    (``within``). Nothing where the run removed that folder; one log line
    where it put a link (or a file) in its place, or in ``.collrun``'s: a
    live run's own links never turn the host to a ``.git`` outside its
    workspace."""
    work_tree = _work_tree(run, repo_path)
    scratch = os.path.join(run.workspace, ".collrun")
    try:
        os.close(_reach(run, repo_path))
    except FileNotFoundError:
        return
    except OSError:
        run.say(f"git {rs.shown(repo_path)}: read-only .git not updated: {_NOT_A_FOLDER}")
        return
    try:
        os.close(fs.open_parent(run.workspace, [".collrun", ".git.materialize"], create=True))
    except OSError:
        run.say(f"git {rs.shown(repo_path)}: read-only .git not updated: .collrun is a link or not a folder")
        return
    try:
        done = git_materialize.write_gitdir(work_tree, state, git_store.objects_path(run.channel),
                                            repo_path=repo_path, hydrated=_present(run, repo_path), readonly=True,
                                            scratch=scratch, within=run.workspace)
    except git_materialize.MaterializeFailed as exc:
        run.say(f"git {rs.shown(repo_path)}: read-only .git not updated: {exc.reason}")
        return
    except OSError as exc:
        run.say(f"git {rs.shown(repo_path)}: read-only .git not updated: {exc.strerror or exc}")
        return
    run.readonly[repo_path] = done.state_hash


def _drop_readonly(run: RunGit, repo_path: str) -> None:
    """The cell no longer has a repository at ``repo_path``: its read-only
    ``.git`` goes, removed through the repository's folder as reached from
    the workspace through no link, and the path leaves ``readonly``."""
    try:
        work_tree = _reach(run, repo_path)
    except FileNotFoundError:
        work_tree = None
    except OSError:
        run.say(f"git {rs.shown(repo_path)}: read-only .git not removed: {_NOT_A_FOLDER}")
        return
    if work_tree is not None:
        try:
            readonly_trees.remove_tree(".git", dir_fd=work_tree)
        except FileNotFoundError:
            pass
        except OSError as exc:
            run.say(f"git {rs.shown(repo_path)}: read-only .git not removed: {exc.strerror or exc}")
            return
        finally:
            os.close(work_tree)
    run.say(f"git {rs.shown(repo_path)}: read-only .git removed: the cell has no repository there now")
    del run.readonly[repo_path]


_NOT_A_FOLDER = "its folder is a link or not a folder"


def _reach(run: RunGit, repo_path: str) -> int:
    """An fd on the repository's folder in the run's workspace, reached
    through no link (``fs.open_parent``); the caller closes it. Raises
    ``FileNotFoundError`` where the folder is gone, another ``OSError``
    where a link or a file is on the way."""
    parts = fs.relative_parts(repo_path) if repo_path else []
    return fs.open_parent(run.workspace, [*parts, ".git"], create=False)


# --- recovery (§4.9, §8) -------------------------------------------------------------------------


# How many Merges and Splits recovery follows from a run's cell to the cell
# that holds its files now.
_MOVE_HOPS = 16


def _resolve_cell(channel: str, conn, cell_id: str) -> tuple[str, str]:
    """Where a recovered run's cell is now (``target_cell_id`` via
    ``cell_moves``): ``(cell_id, "")`` on the canvas; for a cell a Merge or
    Split consumed, the canvas cell that took its files, with ``"moved"``;
    else ``cell_id`` with why it gets no ingest ("cell was removed", "cell
    could not be resolved")."""
    import collimate_server.removed_cells as removed_cells
    from collimate_server.cell_placement import find_cell
    from collimate_server.state import get_channel_by_id, state_lock

    def on_canvas(cell: str) -> bool:
        with state_lock:
            channel_obj = get_channel_by_id(channel)
            return channel_obj is not None and find_cell(channel_obj, cell)[1] is not None

    if on_canvas(cell_id):
        return cell_id, ""
    current, seen = cell_id, {cell_id}
    for _hop in range(_MOVE_HOPS):
        record = removed_cells.get(conn, current)
        if record is None or record.reason not in ("merged", "split"):
            if current == cell_id and record is not None and record.reason == "removed":
                return cell_id, "cell was removed"
            break
        op = "merge" if record.reason == "merged" else "split"
        newest = conn.execute("SELECT op_id FROM cell_moves WHERE from_cell=? AND op=? ORDER BY id DESC LIMIT 1",
                              (current, op)).fetchone()
        if newest is None:
            break
        taken = [row[0] for row in conn.execute(
            "SELECT to_cell FROM cell_moves WHERE from_cell=? AND op=? AND op_id=? ORDER BY ord, id",
            (current, op, newest[0]))]
        live = next((cell for cell in taken if on_canvas(cell)), None)
        if live is not None:
            return live, "moved"
        current = next((cell for cell in taken if cell not in seen), "")
        if not current:
            break
        seen.add(current)
    return cell_id, "cell could not be resolved"


def _harvested_rows(conn, frozen_tip: str, run_id: str) -> dict[str, str] | None:
    """The rows of the newest version after ``frozen_tip`` on its cell's
    chain that the run's own harvest wrote; None when none did."""
    from collimate_server.cell_files import walk_versions
    own = {row[0] for row in conn.execute("SELECT id FROM cells WHERE run_id=? AND source_type=?",
                                          (run_id, SOURCE_TYPE))}
    if not own:
        return None
    for version in reversed(walk_versions(conn, frozen_tip)):
        if version in own:
            return rows_of(conn, version)
    return None


def recover(channel: str, cell_id: str, run_id: str, workspace: str) -> Harvested:
    """The recovery harvest of an ended, unharvested run's workspace
    (R11): §4.8 for every ``.git`` (for a run that did not hold the lease
    at start, but those at its frozen rows, its read-only ones) against the
    rows the run's own last harvest wrote, else (a holder) its frozen tip's,
    never a removal; a removed, merged, split or unresolvable cell, a cell
    changed since then, an unreadable HEAD and no usable git rescue. Marks
    the run harvested. The lines go to the run's log, which is baked
    again."""
    conn = get_db_connection(channel)
    try:
        row = conn.execute("SELECT generator_name, git_input_version_id, git_holder, git_harvested_at, target_cell_id "
                           "FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row[1] is None or row[3] is not None:
            return Harvested()
        frozen_rows = rows_of(conn, row[1])
        own_rows = _harvested_rows(conn, row[1], run_id)
        resolved, why = _resolve_cell(channel, conn, cell_id)
    finally:
        conn.close()
    if why == "moved":
        why = f"cell changed since run {run_id[:8]}"
    lines: list[str] = []
    lease = repository_leases.holder(channel, cell_id)
    # A run that did not hold the lease at start had a read-only .git at
    # each frozen row (or none, where it failed or git was unusable): never
    # its to keep, whatever is there now.
    run = RunGit(channel=channel, cell_id=resolved, run_id=run_id, label=repository_leases.RECOVERY,
                 workspace=workspace, generator_name=row[0] or RECOVERY, frozen_tip=row[1],
                 frozen_rows=frozen_rows, holder=bool(row[2]), readonly={} if row[2] else dict(frozen_rows),
                 recovery_rows=own_rows if own_rows is not None else (dict(frozen_rows) if row[2] else {}),
                 lease=lease if lease is not None and lease.label == repository_leases.RECOVERY else None,
                 log=lines.append, warn=lambda line: lines.append(f"[WARNING] {line}"))
    try:
        if not why and run.lease is not None:
            return harvest(run, RECOVERY)
        work = _Harvest(run, RECOVERY, git_host.capability())
        with run.lane.lock:
            for repo_path in _discover(run).repositories:
                work.rescue(repo_path, why or _blocked_reason(run, f"cell changed since run {run_id[:8]}"))
            work.mark_harvested()
        return work.result(None)
    finally:
        _log_recovery(channel, run_id, lines)


def _log_recovery(channel: str, run_id: str, lines: list[str]) -> None:
    if not lines:
        return
    from collimate_server.process_manager.finalization import commit_run_log
    conn = get_db_connection(channel)
    try:
        conn.executemany("INSERT INTO run_events (run_id, event_type, payload) VALUES (?, ?, ?)",
                         [(run_id, "stderr", line) if line.startswith("[WARNING] ") else (run_id, "log", line)
                          for line in lines])
        conn.commit()
    finally:
        conn.close()
    commit_run_log(channel, run_id)


# --- rescue routes (§4.9) ------------------------------------------------------------------------


class GitUnavailable(RuntimeError):
    """Host git is not usable, so a rescue cannot be retried."""


class CellRemoved(RuntimeError):
    """The cell is on the Removed shelf (or was merged or split away), so its
    rescue cannot be retried until it is restored."""


def _require_unleased(channel: str, cell_id: str) -> None:
    lease = repository_leases.holder(channel, cell_id)
    if lease is not None:
        raise repository_leases.RepositoryBusy(lease.label, lease.run_id)


def account_secret_forms() -> tuple[str, ...]:
    """The secret forms of every Git account in the Key manager, for a state
    stored outside any run (a retried rescue, an import) or written outside
    one (an export): no run's bound accounts apply, so the sanitizer redacts
    all of them; an account whose secrets cannot be read is passed over."""
    import collimate_server.accounts as accounts
    try:
        listed = accounts.list_accounts()["accounts"]
    except (accounts.AccountError, OSError):
        return ()
    context = {}
    for record in listed:
        if record.get("kind") != "git":
            continue
        try:
            context[record["id"]] = accounts.private_account(record["id"])
        except (accounts.AccountError, OSError):
            continue
    return secret_forms(context)


def _gitdir_state(gitdir: str, repo_path: str, quarantine: git_store.Quarantine, *,
                  base: rs.RepositoryState | None, tip_files: set[str], alternates: str | None,
                  secrets: tuple[str, ...], kept_by_index: bool = False,
                  say: Callable[[str], None] | None = None) -> rs.RepositoryState:
    """§4.8 steps 2-3 over a ``.git`` held as data outside any run (a rescue,
    an uploaded import): the reader (``alternates``: the one line its gate
    admits), then ``normalize`` against ``base`` with ``secrets`` (every
    Git account's secret forms, :func:`account_secret_forms`), ``kept_out``
    carried forward from the base and, with ``kept_by_index``, the HEAD
    regular files the cell lacks that the ``.git``'s own index marks
    skip-worktree (an uploaded export's ``kept_out``, so its absence is no
    deletion). ValueError with the refusal; the dropped and warning lines
    go to ``say``."""
    read = rs.read_gitdir(gitdir, repo_path=repo_path, alternates=alternates)
    kept_out: list[str] = []
    if read.refusal is None:
        skipped = _index_skipped(read, quarantine.objects) if kept_by_index else None
        kept_out = _kept_out(repo_path, read, quarantine.objects, base, tip_files,
                             add=skipped.__contains__ if skipped is not None else None)[0]
    normalized = rs.normalize(read, quarantine.objects, base=base, secrets=secrets, kept_out=kept_out)
    if say is not None:
        for line in (*normalized.dropped, *normalized.warnings):
            say(f"git {rs.shown(repo_path)}: {line}")
    if normalized.refusal is not None or normalized.state is None:
        raise ValueError(normalized.refusal or "not readable")
    return normalized.state


def _index_skipped(read: rs.GitdirRead, objects: rs.GitdirObjects) -> set[str]:
    """The paths the read ``.git``'s index marks skip-worktree (none when it
    has no readable index)."""
    if read.index is None:
        return set()
    try:
        entries = objects.index(read.index, read.shared_index)
    except (git_host.GitFailed, ValueError):
        return set()
    return {entry.path.decode("utf-8", "replace") for entry in entries
            if entry.stage == 0 and entry.flags & rs.SKIP_WORKTREE_FLAG}


def retry_rescue(channel: str, cell_id: str, repo_path: str) -> str:
    """Retry: §4.8 over the rescued ``.git`` of (cell, path) against the
    cell's tip row there, under the cell lock, carrying the row's
    ``kept_out`` forward; on success the rescue goes. Returns the version
    written (the tip when nothing changed). LookupError without such a
    rescue, ``RepositoryBusy`` while a run holds the cell's lease,
    :class:`CellRemoved` for a cell off the canvas, :class:`GitUnavailable`
    without usable git, and ``git_store.StoreFailed`` (or ``ValueError``
    with the refusal) when it still cannot be kept."""
    import collimate_server.removed_cells as removed_cells
    from collimate_server.persistence import repository_run, write_repository_version
    from collimate_server.versions import find_tip, tip_file_map
    with cell_commit_lock(cell_id):
        gitdir = git_store.rescue_gitdir(channel, cell_id, repo_path)
        if gitdir is None:
            raise LookupError(f"no rescue at {rs.shown(repo_path)}")
        _require_unleased(channel, cell_id)
        capability = git_host.capability()
        slot = os.path.dirname(gitdir)
        conn = get_db_connection(channel)
        try:
            if removed_cells.get(conn, cell_id) is not None:
                raise CellRemoved("restore the cell to retry its git history")
            if not capability.ok:
                raise GitUnavailable(_no_git(capability))
            tip = find_tip(conn, cell_id)
            base_hash = rows_of(conn, tip).get(repo_path)
            base = load_state(conn, base_hash) if base_hash is not None else None
            tip_files = set(tip_file_map(conn, tip))
        finally:
            conn.close()
        secrets = account_secret_forms()
        with git_store.quarantine(channel, slot, "", run_id=_RETRY_RUN) as quarantine:
            normalized = _gitdir_state(gitdir, repo_path, quarantine, base=base, tip_files=tip_files,
                                       alternates=git_store.objects_path(channel), secrets=secrets)
            guarded = git_store.band_guard(quarantine, normalized, base)
            if guarded.refusal is not None or guarded.state is None:
                raise ValueError(guarded.refusal or "history holds .collrun/")
            state = guarded.state
            with git_store.in_flight(channel):
                git_store.ingest(quarantine, state, base)
                if state.manifest_hash == base_hash:
                    version_id = tip
                else:
                    conn = get_db_connection(channel)
                    try:
                        run_id = repository_run(conn, channel, cell_id, RETRY_LABEL)
                        version_id = write_repository_version(conn, cell_id, run_id, {repo_path: state},
                                                              label=LABEL, source_type=SOURCE_TYPE)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                    finally:
                        conn.close()
        git_store.discard(channel, cell_id, repo_path)
    from collimate_server.cell_commits import broadcast_cell_commit
    broadcast_cell_commit(channel, cell_id, None, SOURCE_TYPE, files=[])
    follow(channel, cell_id)
    return version_id


# Retry quarantines carry no run's id.
_RETRY_RUN = "00000000"


def discard_rescue(channel: str, cell_id: str, repo_path: str) -> str | None:
    """Discard: under the cell lock, then the store lock, remove the rescue
    of (cell, path); with a tip row at the path also write the version
    that drops it (``Discard git history``), which is how a root
    repository is removed (R7). Returns that version's id, or None.
    LookupError without such a rescue; ``RepositoryBusy`` while leased."""
    from collimate_server.persistence import repository_run, write_repository_version
    from collimate_server.versions import find_tip
    with cell_commit_lock(cell_id):
        _require_unleased(channel, cell_id)
        if repo_path not in git_store.rescues_of(channel, cell_id):
            raise LookupError(f"no rescue at {rs.shown(repo_path)}")
        version_id = None
        conn = get_db_connection(channel)
        try:
            if repo_path in rows_of(conn, find_tip(conn, cell_id)):
                run_id = repository_run(conn, channel, cell_id, DISCARD_LABEL)
                version_id = write_repository_version(conn, cell_id, run_id, {repo_path: None},
                                                      label=DISCARD_LABEL, source_type=DISCARD_SOURCE_TYPE)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        git_store.discard(channel, cell_id, repo_path)
    from collimate_server.cell_commits import broadcast_cell_commit
    broadcast_cell_commit(channel, cell_id, None, DISCARD_SOURCE_TYPE, files=[])
    follow(channel, cell_id)
    return version_id


# --- import (§7) ---------------------------------------------------------------------------------


class NotAdopted(ValueError):
    """An uploaded ``.git`` an import cannot keep; its message is the cause,
    with git's words after it when git gave some."""


def _adopt_reason(exc: git_store.StoreFailed) -> str:
    """A store failure as ``<cause>; git: <words>``."""
    cause, said, words = exc.reason.partition(": git ")
    return f"{cause}; git: {words}" if said else exc.reason


@dataclass
class Adopted:
    """An uploaded ``.git`` read as the state of a new cell's repository at
    ``repo_path``, its objects copied into ``quarantine``: :meth:`land`
    carries them into the store; close the quarantine (or use ``with``)
    once the rows have committed or the import gave up."""

    repo_path: str
    state: rs.RepositoryState
    quarantine: git_store.Quarantine

    def land(self) -> None:
        """§4.8 steps 6-8 with no haves (an import has no base), inside
        ``git_store.in_flight`` for the channel held until the rows commit.
        :class:`NotAdopted` with git's words when the store refuses."""
        try:
            git_store.ingest(self.quarantine, self.state, None)
        except git_store.StoreFailed as exc:
            raise NotAdopted(_adopt_reason(exc)) from None

    def close(self) -> None:
        self.quarantine.close()

    def __enter__(self) -> Adopted:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def adopt(channel: str, holder: str, repo_path: str, *, cell_files: Collection[str],
          secrets: tuple[str, ...]) -> Adopted:
    """Read the uploaded ``<holder>/.git`` as the state of the repository at
    ``repo_path`` of a new cell holding ``cell_files`` (§7 Import): its
    objects into a quarantine, the reader with no ``alternates`` (so the
    gate refuses a repository that borrows objects), ``normalize`` with no
    base and ``secrets`` redacted, ``kept_out`` the HEAD files the cell
    lacks that the upload's index marks skip-worktree, and a refusal when
    HEAD's tree holds a top-level ``.collrun/``. The caller lands it
    (:meth:`Adopted.land`). :class:`NotAdopted` with the cause when it cannot be kept; host git
    must be usable (the caller checks :func:`git_host.capability`)."""
    try:
        quarantine = git_store.quarantine(channel, holder, "", run_id=os.path.basename(holder))
    except git_store.StoreFailed as exc:
        raise NotAdopted(_adopt_reason(exc)) from None
    try:
        state = _gitdir_state(os.path.join(holder, ".git"), repo_path, quarantine, base=None,
                              tip_files=set(cell_files), alternates=None, secrets=secrets, kept_by_index=True,
                              say=_logger.info)
        commit = rs.resolve_ref(rs.parse_refs(state.part("refs") or b""), state.manifest()["head"])
        if commit is not None and git_store.tree_holds_band(quarantine.path, commit):
            raise NotAdopted(f"HEAD holds {git_store.BAND}/ at its top")
    except NotAdopted:
        quarantine.close()
        raise
    except ValueError as exc:
        quarantine.close()
        raise NotAdopted(str(exc)) from None
    except git_host.GitFailed as exc:
        quarantine.close()
        raise NotAdopted(f"HEAD's tree unreadable; git: {exc.stderr.strip() or exc}") from None
    except BaseException:
        quarantine.close()
        raise
    return Adopted(repo_path, state, quarantine)


__all__ = [
    "Adopted", "CellRemoved", "DISCARD_LABEL", "DISCARD_SOURCE_TYPE", "FINAL", "FINAL_TRIES", "GitUnavailable",
    "Harvested", "LABEL", "Lane", "NOW", "NOW_SECONDS", "NotAdopted", "PERIODIC", "PERIODIC_SECONDS", "RECOVERY",
    "RETRY_LABEL", "RunGit", "SOURCE_TYPE", "account_secret_forms", "adopt", "begin_release", "create_links", "discard_rescue",
    "fingerprint", "forget_failures", "harvest", "materialize", "materialize_errors", "no_git_reason", "now",
    "periodic", "recover", "retry_rescue", "secret_forms", "settle",
]
