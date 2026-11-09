"""Materialize: a real ``.git`` written from a stored state, into a run's
work tree (``mode="run"``) or an export's (``mode="export"``)
(``docs/plans/2026-10-31-git-in-cells/design.md`` §4.3, §4.6, §7).

:func:`write_gitdir` builds ``<work_tree>/.git.materialize-<n>`` and renames
it to ``.git`` only after every step succeeded (a read-only one then loses
its write bits, or replaces an older read-only ``.git`` entry by entry): the files of the state
(HEAD, headerless packed-refs, symbolic refs, reflogs, info, shallow), the
host's own config, a check that the channel store holds every object the
state names, an index built from HEAD with plumbing (``read-tree``, the
operation's ``--index-info`` replay, skip-worktree, the filter guard,
``--refresh``), the stored config pairs, and the operation's progress files
last, with no git command after them. Nothing resets, checks out, merges,
stashes or rebases (``reset -q`` deletes ``MERGE_HEAD``; P7): the work
tree's files are the cell's, and git is told about them, never the reverse.

Every git command runs through :mod:`git_host` with an explicit
``--git-dir``/``--work-tree``. A failure is the repository's alone
(:class:`MaterializeFailed`): its partial directory is removed and the
caller leaves it out of the run, keeping its rows. The links HEAD records
are planned here and created by :func:`create_links` after the run's last
host write. Nothing here knows about runs, leases or the DB.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal

from collimate import fs

import collimate_server.cas as cas
import collimate_server.git_host as git_host
import collimate_server.readonly_trees as readonly_trees
import collimate_server.repository_state as rs
from collimate_server.reserved_paths import RESERVED_PREFIX, is_dotgit_segment, is_reserved_path
from collimate_server.run_folders import RUNS_PREFIX

# What ``git init`` writes into ``description``.
DESCRIPTION = b"Unnamed repository; edit this file 'description' to name the repository.\n"

_GITLINK = "160000"
_SYMLINK = "120000"
_REGULAR = ("100644", "100755")

# The host's bands at a workspace root: ``.collrun``, ``_runs``, ``_agent``,
# ``.pydantic-ai-harness`` (their exclude lines, unanchored).
_BANDS = frozenset(line.strip("/") for line in rs.HOST_EXCLUDES_ROOT)

# A HEAD path with one of these segments is never a cell file (§4.3 step 8).
_SKIP_SEGMENTS = frozenset(fs.WORKSPACE_SKIP_DIRS | {RESERVED_PREFIX, RUNS_PREFIX})

# A link is created only when neither side has one of these segments
# (casefolded) or a ``.git``-equivalent one (§4.3 step 11).
_LINK_FORBIDDEN = frozenset(name.casefold() for name in fs.WORKSPACE_SKIP_DIRS | _BANDS)

# The stored pairs written with the host's config, before the index steps,
# because ``sparse-checkout reapply`` reads them (§4.3 step 2).
_SPARSE_KEYS = frozenset({"core.sparsecheckout", "core.sparsecheckoutcone"})

# How many paths a skip-worktree line names before it counts the rest.
_NAMED = 5

_MIB = 1024 * 1024

Mode = Literal["run", "export"]


class MaterializeFailed(Exception):
    """One repository could not be materialized: ``step`` is where
    (``files``, ``config``, ``objects``, ``pack``, ``read-tree``,
    ``replay``, ``skip-worktree``, ``filter``, ``refresh``, ``finish``),
    ``reason`` git's words or the step's exit code. Its partial directory
    is gone; ``lines`` are what to log, this failure's own line last."""

    def __init__(self, repo_path: str, step: str, reason: str, lines: Iterable[str] = ()):
        self.repo_path = repo_path
        self.step = step
        self.reason = reason
        super().__init__(f"{rs.shown(repo_path)}: materialize failed at {step}: {reason}")
        self.lines = (*lines, str(self))

    def entry(self) -> dict:
        """The snapshot entry's ``error``."""
        return {"reason": self.reason}


@dataclass(frozen=True)
class Link:
    """A link HEAD records that :func:`create_links` makes: ``path``
    relative to the work tree, ``target`` the blob's bytes."""

    path: str
    target: bytes


@dataclass(frozen=True)
class Materialized:
    """A written ``.git``: ``state_hash`` is the state it holds, the run's
    ``materialized[repo_path]`` and its harvest's base; ``links`` the links
    to create once the run's last host write is done (run mode); ``lines``
    to log, ``warnings`` to log and raise as warning events."""

    state_hash: str
    links: tuple[Link, ...] = ()
    lines: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _cell_path(repo_path: str, path: str) -> str:
    return f"{repo_path}/{path}" if repo_path else path


def _anchored(path: str) -> str:
    """An exclude line matching exactly the file ``path`` (relative to the
    repository): anchored, its pattern characters and a trailing space
    escaped."""
    escaped = re.sub(r"([\\*?\[])", r"\\\1", path)
    return f"/{escaped[:-1]}\\ " if escaped.endswith(" ") else f"/{escaped}"


def _value_ok(value: str) -> bool:
    """Whether a HEAD or ref value is 40-hex or ``ref: <refs/...>``."""
    return bool(rs.HEX40.fullmatch(value) or rs.symref(value))


def _ref_name_ok(name: str) -> bool:
    return rs.ref_name_ok(name) and not rs.ignored_ref(name)


def _key_path_ok(name: str) -> bool:
    """Whether a path derived from a state key is a plain relative path."""
    try:
        return "/".join(fs.relative_parts(name)) == name
    except PermissionError:
        return False


def _progress_name_ok(name: str) -> bool:
    """Whether ``name`` is one of the operation files a state keeps (§3.3,
    Progress), as a plain relative path."""
    return _key_path_ok(name) and rs.progress_name(name) == name


def _named(paths: Collection[str]) -> str:
    shown = sorted(paths)
    text = ", ".join(shown[:_NAMED])
    return text + (f" and {len(shown) - _NAMED} more" if len(shown) > _NAMED else "")


def _size(limit: int) -> str:
    return f"{limit // _MIB} MiB" if limit % _MIB == 0 else f"{limit} bytes"


def _blob_sha(fd: int) -> str:
    """The git blob id of an open regular file's bytes, unfiltered (what
    ``git hash-object --no-filters`` gives)."""
    size = os.fstat(fd).st_size
    digest = hashlib.sha1(b"blob %d\0" % size)
    read = 0
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
        read += len(chunk)
    if read != size:
        raise OSError(f"file changed while it was hashed ({read} of {size} bytes)")
    return digest.hexdigest()


def _filemode_trusted(path: str) -> bool:
    """``git init``'s probe of ``core.filemode`` on the new ``config``: the
    filesystem keeps a flipped owner-exec bit (and the file did not start
    executable)."""
    try:
        before = os.lstat(path)
        os.chmod(path, before.st_mode ^ stat.S_IXUSR)
        after = os.lstat(path)
        os.chmod(path, before.st_mode)
    except OSError:
        return False
    return before.st_mode != after.st_mode and not before.st_mode & stat.S_IXUSR


def _config_text(sections: Iterable[tuple[str, Iterable[tuple[str, str]]]]) -> bytes:
    out = []
    for section, pairs in sections:
        out.append(f"[{section}]\n")
        out += [f"\t{key} = {value}\n" for key, value in pairs]
    return "".join(out).encode("ascii")


def export_state(state: rs.RepositoryState, *, omit_history: bool) -> rs.RepositoryState:
    """The state an export writes (§7, Local history): the state itself, or
    with "Omit local git history" its refs without ``refs/stash``, no
    reflogs and no ``ORIG_HEAD``, the operation's own files and ``index``
    kept. Its :func:`repository_state.roots` are what the export packs (the
    sanitizer :func:`write_gitdir` runs on it removes no root); a
    ``refs/stash`` in the original's refs is the stash the export's note
    names."""
    if not omit_history:
        return state
    parts = {name: body for name, body in state.named_parts().items()
             if not name.startswith("reflogs/") and name != "progress/ORIG_HEAD"}
    refs = rs.parse_refs(parts.get("refs", b""))
    refs.pop("refs/stash", None)
    parts["refs"] = rs.format_refs(refs)
    return rs.make_state(state.manifest()["head"], parts)


def write_gitdir(
    work_tree: str,
    state: rs.RepositoryState,
    objects: str,
    *,
    repo_path: str = "",
    hydrated: Collection[str] = (),
    mode: Mode = "run",
    excluded: Collection[str] = (),
    renamed: Collection[str] = (),
    pack: str | None = None,
    staging: str | None = None,
    secrets: Iterable[str] = (),
    readonly: bool = False,
    scratch: str | None = None,
    within: str | None = None,
) -> Materialized:
    """Write ``state`` as ``<work_tree>/.git`` (§4.3 Building, §7 Export).

    ``work_tree`` is the repository's folder: ``<ws>/<repo_path>`` in a
    run, the folder an export stages its files in. ``objects`` is the
    channel store's absolute ``objects`` directory. ``hydrated`` names the
    cell files present in the work tree, as cell paths (``repo_path``
    prefixed); a HEAD path the cell does not hold shows as deleted unless
    it is skip-worktree (``kept_out``, links, gitlinks, the paths a cell
    cannot hold).

    ``mode="run"``: ``objects/info/alternates`` names the store, the host's
    config keeps gc and fsmonitor off, exec bits follow HEAD, the index is
    refreshed, and the links to create are returned. The state is written
    exactly as stored (R5).
    ``mode="export"``: the sanitizer runs once more with ``secrets`` (the
    bound accounts' secret forms; §3.3, last on export); no alternates;
    ``pack`` (a pack file the store made over the state's roots, None when
    there are none) is indexed into ``objects/pack``; the config is the
    stored pairs with the four format keys; the index is built in
    ``staging`` (a directory to create, e.g.
    ``.collimate/tmp/export-<id>/<n>``, removed after) with alternates to
    the store, every link and gitlink skip-worktree, as are ``excluded``
    (the HEAD paths the export drops or renames, as cell paths), and its
    stat stays zeroed; ``renamed`` (the names, relative to the repository,
    the export wrote those files under) go into ``info/exclude``, so
    status is clean.

    ``readonly`` (run mode): the ``.git`` of a run that does not hold the
    cell's lease. Every directory and file of it loses its write bits
    (:func:`readonly_trees.make_readonly`), so git reads it and every write
    fails, and a read-only ``.git`` already there (an older state of the
    cell's) has its entries replaced in place
    (:func:`readonly_trees.replace_entries`), keeping the directory a
    container binds. ``scratch`` is the directory it is built in instead of
    the work tree: a build beside a live run's files would mirror into its
    cell. ``within`` (the run's workspace, for a ``.git`` written while the
    run is live) is the directory the work tree must be reached from
    through no link: the work tree is opened once that way, and ``.git`` is
    put in place, replaced or removed only through that fd, so a link the
    run puts in place of its folder never turns the host to a ``.git``
    outside the workspace. The work tree must exist then.

    Raises :class:`MaterializeFailed` (the directory removed) for this
    repository alone, whatever went wrong in it."""
    secrets = tuple(secrets)
    if not os.path.isabs(objects):
        raise ValueError(f"the store's objects path must be absolute: {objects!r}")
    if mode == "export" and staging is None:
        raise ValueError("an export builds its index in a staging directory")
    if mode == "run" and (pack is not None or staging is not None or excluded or renamed or secrets):
        raise ValueError("pack, staging, excluded, renamed and secrets are an export's")
    if mode == "export" and readonly:
        raise ValueError("only a run's .git is read-only")
    if scratch is not None and not readonly:
        raise ValueError("only a read-only .git is built in a scratch directory")
    return _Build(work_tree, state, objects, repo_path=repo_path, hydrated=hydrated, mode=mode,
                  excluded=excluded, renamed=renamed, pack=pack, staging=staging, secrets=secrets,
                  readonly=readonly, scratch=scratch, within=within).run()


class _Build:
    """One :func:`write_gitdir`: the state's parts, the directories, the
    lines, and the steps in their order."""

    def __init__(self, work_tree: str, state: rs.RepositoryState, objects: str, *, repo_path: str,
                 hydrated: Collection[str], mode: Mode, excluded: Collection[str], renamed: Collection[str],
                 pack: str | None, staging: str | None, secrets: tuple[str, ...], readonly: bool,
                 scratch: str | None, within: str | None):
        self.work_tree = os.path.abspath(work_tree)
        self.state = state
        self.objects = objects
        self.repo_path = repo_path
        self.hydrated = frozenset(hydrated)
        self.mode = mode
        self.excluded = frozenset(excluded)
        self.renamed = sorted(renamed)
        self.pack = pack
        self.staging = None if staging is None else os.path.abspath(staging)
        self.secrets = secrets
        self.readonly = readonly
        self.scratch = None if scratch is None else os.path.abspath(scratch)
        self.within = None if within is None else os.path.abspath(within)
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.gitdir = ""
        self.step = "files"

    def _parse(self) -> None:
        """The state's parts, as the steps write them."""
        if self.mode == "export":
            self.state, removed = rs.sanitize_state(self.state, self.secrets)
            self.lines += [f"{rs.shown(self.repo_path)}: {line}" for line in removed]
        parts = self.state.named_parts()
        self.head = self.state.manifest()["head"]
        self.refs = rs.parse_refs(parts.get("refs", b""))
        stored = [(key, value) for key, value in json.loads(parts.get("config", b"[]"))]
        # The allow-list again: a stored part holding anything else (a
        # ``filter.<x>.clean``, a hook path) has that pair left out.
        self.config = [(key, value) for key, value in stored if rs.config_allowed(key, value)]
        dropped = sorted({key for key, value in stored if not rs.config_allowed(key, value)})
        if dropped:
            self.lines.append(f"{rs.shown(self.repo_path)}: config not written (not allowed): {_named(dropped)}")
        self.reflogs = {name[len("reflogs/"):]: body for name, body in parts.items() if name.startswith("reflogs/")}
        self.progress = {name[len("progress/"):]: body for name, body in parts.items()
                         if name.startswith("progress/")}
        self.index = parts.get("index")
        self.info = {name: parts[name] for name in rs.INFO_PARTS if name in parts}
        self.shallow = parts.get("shallow")
        self.kept_out = frozenset(line for line in parts.get("kept_out", b"").decode("utf-8").split("\n") if line)

    # --- the frame ---------------------------------------------------------------------------------

    def run(self) -> Materialized:
        try:
            try:
                self._parse()
                self._recheck()
                if self._occupied(_found(os.path.join(self.work_tree, ".git"))):
                    raise MaterializeFailed(self.repo_path, self.step, f"{rs.shown(self.repo_path)}/.git already exists",
                                            self.lines)
                if self.within is None:
                    os.makedirs(self.work_tree, exist_ok=True)
                self.gitdir = self._make_dir(".git.materialize")
                links = self._steps()
            except MaterializeFailed:
                raise
            except git_host.GitFailed as exc:
                raise MaterializeFailed(self.repo_path, self.step, str(exc), self.lines) from None
            except OSError as exc:
                raise MaterializeFailed(self.repo_path, self.step, f"{exc.strerror or exc}", self.lines) from None
            except Exception as exc:  # noqa: BLE001 — a malformed part fails its repository alone (B8)
                raise MaterializeFailed(self.repo_path, self.step, f"{type(exc).__name__}: {exc}",
                                        self.lines) from exc
        finally:
            # Set until the rename: whatever stopped the build, its partial
            # directory goes (a stray one would mirror as cell files).
            if self.gitdir:
                readonly_trees.remove_tree(self.gitdir, ignore_errors=True)
            if self.staging is not None:
                shutil.rmtree(self.staging, ignore_errors=True)
        return Materialized(self.state.manifest_hash, links, tuple(self.lines), tuple(self.warnings))

    def _occupied(self, found: os.stat_result | None) -> bool:
        """Whether ``found`` (what is at ``.git``) is something this build
        may not take the place of: anything, except a directory a read-only
        build replaces in place."""
        return found is not None and not (self.readonly and stat.S_ISDIR(found.st_mode))

    def _make_dir(self, prefix: str) -> str:
        """A new directory ``<prefix>-<n>`` in the scratch directory, else
        beside ``.git`` in the work tree."""
        parent = self.scratch or self.work_tree
        n = 0
        while True:
            path = os.path.join(parent, f"{prefix}-{n}")
            try:
                os.mkdir(path, 0o755)
                return path
            except FileExistsError:
                n += 1

    def _steps(self) -> tuple[Link, ...]:
        self._files()
        self.step = "config"
        self._host_config()
        if self.mode == "export":
            self.step = "pack"
            self._index_pack()
        self.step = "objects"
        self._object_check()
        commit = rs.resolve_ref(self.refs, self.head)
        index_dir = self.gitdir if self.mode == "run" else self._staging(commit)
        tree = self._tree(index_dir, commit)
        self.step = "read-tree"
        self._git(index_dir, ["read-tree", commit or "--empty"])
        index_paths: set[bytes] = set()
        if self.index:
            self.step = "replay"
            self._git(index_dir, ["update-index", "-z", "--index-info"], input=self.index)
            index_paths = {path for _, _, _, path in rs.index_lines(self.index)}
        self._sparse(index_dir)
        if self.mode == "run":
            self._modes(tree)
        self.step = "skip-worktree"
        links = self._skip_worktree(index_dir, tree, index_paths)
        if self.mode == "run":
            self.step = "refresh"
            # Exit 1 is expected (unmerged entries, a deleted file; P6);
            # anything else (128: the index could not be written) fails it.
            self._git(index_dir, ["update-index", "-q", "--refresh"], ok=(0, 1))
        else:
            shutil.copyfile(os.path.join(index_dir, "index"), os.path.join(self.gitdir, "index"))
        self.step = "finish"
        self._stored_config()
        self._write_progress()
        work_tree = self._open_work_tree()
        try:
            found = _found(".git", work_tree)
            if self._occupied(found):
                raise MaterializeFailed(self.repo_path, self.step, f"{rs.shown(self.repo_path)}/.git already exists",
                                        self.lines)
            if found is not None:
                readonly_trees.replace_entries(".git", self.gitdir, self._make_dir(".git.stale"), dir_fd=work_tree)
                readonly_trees.remove_tree(self.gitdir, ignore_errors=True)
            else:
                os.rename(self.gitdir, ".git", dst_dir_fd=work_tree)
                self.gitdir = ""
                if self.readonly:
                    try:
                        readonly_trees.make_readonly(".git", dir_fd=work_tree)
                    except OSError:
                        # A read-only .git that is not read-only is no .git.
                        readonly_trees.remove_tree(".git", ignore_errors=True, dir_fd=work_tree)
                        raise
        finally:
            os.close(work_tree)
        self.gitdir = ""
        return links

    def _open_work_tree(self) -> int:
        """An fd on the work tree: through no link from ``within`` when set
        (``fs.open_parent``), else as named."""
        if self.within is None:
            return os.open(self.work_tree, os.O_RDONLY | fs.O_DIRECTORY)
        relative = os.path.relpath(self.work_tree, self.within)
        parts = [] if relative == "." else relative.split(os.sep)
        return fs.open_parent(self.within, [*parts, ".git"], create=False)

    def _git(self, git_dir: str, args: list[str], *, input: bytes | None = None, ok=(0,),
             work_tree: str | None = None) -> git_host.GitResult:
        return git_host.run(args, git_dir=git_dir, work_tree=work_tree or self.work_tree, input=input,
                            ok_returncodes=ok)

    # --- before anything is written ----------------------------------------------------------------

    def _recheck(self) -> None:
        """The progress set again (A9): one ``progress_problem`` refuses
        leaves out the whole operation and its index, as the reader would."""
        problem = rs.progress_problem(self.progress) if self.progress else None
        if problem is not None:
            self.warnings.append(f"{rs.shown(self.repo_path)}: {problem}")
            self.progress = {}
            self.index = None
        for name in sorted(self.progress):
            if name in self.progress and not _progress_name_ok(name):
                group = rs.progress_set(name)
                for other in [other for other in self.progress if rs.progress_set(other) == group]:
                    del self.progress[other]
                self.lines.append(f"{rs.shown(self.repo_path)}: progress {group}: a bad name; the operation not kept")

    # --- step 1: files -----------------------------------------------------------------------------

    def _files(self) -> None:
        if not _value_ok(self.head):
            raise MaterializeFailed(self.repo_path, "files", f"HEAD is not a ref or an object id: {self.head!r}",
                                    self.lines)
        for directory in ("refs/heads", "refs/tags", "objects/info", "objects/pack", "hooks", "info", "logs"):
            os.makedirs(os.path.join(self.gitdir, directory), 0o755, exist_ok=True)
        put = self._put
        put("HEAD", f"{self.head}\n".encode("ascii"))
        put("description", DESCRIPTION)
        packed = []
        for name in sorted(self.refs, key=lambda n: n.encode("utf-8")):
            value = self.refs[name]
            if not _ref_name_ok(name):
                self.lines.append(f"{rs.shown(self.repo_path)}: a ref with a bad name not written")
            elif value.startswith("ref: "):
                if _value_ok(value):
                    put(name, f"{value}\n".encode("utf-8"))
                else:
                    self.lines.append(f"{rs.shown(self.repo_path)}: ref {name} has a bad value; not written")
            else:
                packed.append(f"{value} {name}\n")
        if packed:
            put("packed-refs", "".join(packed).encode("utf-8"))
        for name, body in sorted(self.reflogs.items()):
            if name == "HEAD" or _ref_name_ok(name):
                put(f"logs/{name}", body)
            else:
                self.lines.append(f"{rs.shown(self.repo_path)}: a reflog with a bad name not written")
        if self.shallow:
            put("shallow", self.shallow)
        host = [*rs.HOST_EXCLUDES, *(rs.HOST_EXCLUDES_ROOT if not self.repo_path else ())]
        if self.mode == "export" and not self.repo_path:
            host.append(rs.PROVENANCE_EXCLUDE)
        if self.renamed:
            host += [rs.RENAMED_BEGIN, *(_anchored(path) for path in self.renamed), rs.RENAMED_END]
        # The host's lines go first: the reader strips them line by line,
        # so the stored part comes back byte for byte, a last line without
        # LF included.
        put("info/exclude", "".join(f"{line}\n" for line in host).encode("utf-8") + self.info.get("exclude", b""))
        for name in ("attributes", "sparse-checkout"):
            if name in self.info:
                put(f"info/{name}", self.info[name])
        if self.mode == "run":
            put("objects/info/alternates", f"{self.objects}\n".encode("utf-8"))

    def _put(self, name: str, body: bytes) -> None:
        """Write the gitdir file ``name`` through :func:`fs.write_file`
        (no component followed through a link), its name checked first."""
        if not _key_path_ok(name):
            raise MaterializeFailed(self.repo_path, self.step, f"not a gitdir path: {name!r}", self.lines)
        fs.write_file(self.gitdir, name, body)

    # --- step 2: the host's config -----------------------------------------------------------------

    def _host_config(self) -> None:
        """The config the host writes and never persists. A run's: the
        format keys, gc, maintenance and fsmonitor off, ``core.filemode``
        and ``core.ignoreCase`` probed as ``git init`` probes them,
        ``core.precomposeUnicode`` on darwin, and the stored sparse pairs.
        An export's: the four format keys, ``core.filemode=false``."""
        path = os.path.join(self.gitdir, "config")
        if self.mode == "export":
            fs.write_file(self.gitdir, "config", _config_text([("core", [
                ("repositoryformatversion", "0"), ("filemode", "false"), ("bare", "false"),
                ("logallrefupdates", "true")])]))
            return
        core = [("repositoryformatversion", "0"), ("bare", "false"), ("logallrefupdates", "true"),
                ("fsmonitor", "false")]
        fs.write_file(self.gitdir, "config", _config_text([("core", core)]))
        filemode = _filemode_trusted(path)
        core.insert(1, ("filemode", "true" if filemode else "false"))
        if os.path.exists(os.path.join(self.gitdir, "CoNfIg")):
            core.append(("ignorecase", "true"))
        if sys.platform == "darwin":
            core.append(("precomposeunicode", "true"))
        fs.write_file(self.gitdir, "config", _config_text([("core", core), ("gc", [("auto", "0")]),
                                                            ("maintenance", [("auto", "false")])]))
        for key, value in self.config:
            if key.lower() in _SPARSE_KEYS:
                git_host.run(["config", "--file", path, "--add", key, value])

    def _stored_config(self) -> None:
        """Each stored pair with ``git config --add``, in stored order,
        after the index steps: ``--refresh`` would otherwise run what a
        stored key names (§3.3, Config)."""
        path = os.path.join(self.gitdir, "config")
        for key, value in self.config:
            if self.mode == "export" or key.lower() not in _SPARSE_KEYS:
                git_host.run(["config", "--file", path, "--add", key, value])

    # --- export: the pack ----------------------------------------------------------------------------

    def _index_pack(self) -> None:
        """Index the store's pack for this export into ``objects/pack``."""
        if self.pack is None:
            return
        pack_dir = os.path.join(self.gitdir, "objects", "pack")
        incoming = os.path.join(pack_dir, "incoming.pack")
        shutil.copyfile(self.pack, incoming)
        done = git_host.run(["index-pack", incoming], git_dir=self.gitdir)
        name = done.stdout.decode("ascii", "replace").strip().split()[-1]
        for suffix in ("pack", "idx", "rev"):
            source = os.path.join(pack_dir, f"incoming.{suffix}")
            if os.path.exists(source):
                os.rename(source, os.path.join(pack_dir, f"pack-{name}.{suffix}"))

    # --- step 3: the object check ------------------------------------------------------------------

    def _object_check(self) -> None:
        """Every sha HEAD, the refs, the progress files and the ``index``
        name (gitlinks aside: another repository's commits; P16) must be in
        the store (an export: in its own pack)."""
        named = {"refs": self.state.part("refs") or b"",
                 **{f"progress/{name}": body for name, body in self.progress.items()}}
        if self.index:
            named["index"] = self.index
        wanted = sorted(rs.roots(rs.make_state(self.head, named)))
        if not wanted:
            return
        done = git_host.run(["cat-file", "--batch-check"], git_dir=self.gitdir,
                            input="".join(f"{sha}\n" for sha in wanted).encode("ascii"))
        if any(line.endswith(" missing") for line in done.stdout.decode("ascii", "replace").splitlines()):
            raise MaterializeFailed(self.repo_path, self.step, "repository history incomplete in the channel store: "
                                    f"{rs.shown(self.repo_path)}", self.lines)

    # --- export: the staging gitdir ------------------------------------------------------------------

    def _staging(self, commit: str | None) -> str:
        """The gitdir an export's index is built in: HEAD's commit (or an
        unborn branch), alternates to the store, the state's attributes for
        the filter guard and its sparse patterns and pairs for the reapply."""
        assert self.staging is not None
        staging = self.staging
        for directory in ("refs", "objects/info", "info"):
            os.makedirs(os.path.join(staging, directory), 0o755, exist_ok=True)
        fs.write_file(staging, "HEAD", f"{commit or 'ref: refs/heads/none'}\n".encode("ascii"))
        fs.write_file(staging, "config", _config_text([("core", [
            ("repositoryformatversion", "0"), ("filemode", "false"), ("bare", "false")])]))
        fs.write_file(staging, "objects/info/alternates", f"{self.objects}\n".encode("utf-8"))
        for name in ("attributes", "sparse-checkout"):
            if name in self.info:
                fs.write_file(staging, f"info/{name}", self.info[name])
        for key, value in self.config:
            if key.lower() in _SPARSE_KEYS:
                git_host.run(["config", "--file", os.path.join(staging, "config"), "--add", key, value])
        return staging

    def _tree(self, git_dir: str, commit: str | None) -> list[tuple[str, str, str, int | None, bytes]]:
        """HEAD's entries as ``(mode, type, sha, size, path)``; a size is
        None for a gitlink."""
        if commit is None:
            return []
        self.step = "read-tree"
        done = git_host.run(["ls-tree", "-r", "-l", "-z", "--full-tree", commit], git_dir=git_dir)
        entries = []
        for record in done.stdout.split(b"\0"):
            if not record:
                continue
            head, _, path = record.partition(b"\t")
            mode, kind, sha, size = head.decode("ascii").split()
            entries.append((mode, kind, sha, None if size == "-" else int(size), path))
        return entries

    # --- steps 6 and 7: sparse and modes -----------------------------------------------------------

    def _sparse(self, git_dir: str) -> None:
        """``sparse-checkout reapply`` when the stored config turns it on;
        a failure is only logged. An export reapplies against an empty work
        tree in its staging gitdir: the index gets the out-of-pattern bits,
        and no file of the export's is removed."""
        if not any(key.lower() == "core.sparsecheckout" and rs.git_bool(value) for key, value in self.config):
            return
        work_tree = None
        if self.mode == "export":
            work_tree = os.path.join(git_dir, "worktree")
            os.makedirs(work_tree, exist_ok=True)
        try:
            self._git(git_dir, ["sparse-checkout", "reapply"], work_tree=work_tree)
        except git_host.GitFailed as exc:
            self.lines.append(f"{rs.shown(self.repo_path)}: sparse-checkout reapply failed: {exc}")

    def _modes(self, tree) -> None:
        """Hydrated regular stage-0 entries get the index's exec bits both
        ways: 100755 gains x where it has r, 100644 loses every x; through
        the parent's fd, never a link (cells carry no modes)."""
        modes = {path: mode for mode, _, _, _, path in tree if mode in _REGULAR}
        if self.index:
            for mode, _, stage, path in rs.index_lines(self.index):
                if stage == 0 and mode != "0":
                    modes[path] = mode
                else:
                    modes.pop(path, None)
        failed = []
        for raw, mode in sorted(modes.items()):
            if mode not in _REGULAR:
                continue
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if _cell_path(self.repo_path, path) not in self.hydrated:
                continue
            try:
                _chmod(self.work_tree, path, mode == "100755")
            except OSError as exc:
                failed.append(f"{path} ({exc.strerror or exc})")
        if failed:
            self.lines.append(f"{rs.shown(self.repo_path)}: exec bits not applied: {_named(failed)}")

    # --- steps 8 and 9: skip-worktree and the filter guard -----------------------------------------

    def _skip_worktree(self, git_dir: str, tree, index_paths: set[bytes]) -> tuple[Link, ...]:
        """Mark skip-worktree every HEAD path the work tree does not hold
        as git would expect it, never an ``index`` path; return the links to
        create (run mode)."""
        classes: dict[str, set[bytes]] = {}
        marked: set[bytes] = set()

        def mark(kind: str, path: bytes) -> None:
            if path not in marked:
                marked.add(path)
                classes.setdefault(kind, set()).add(path)

        links: list[tuple[str, bytes]] = []
        regular: list[tuple[str, bytes]] = []
        limit = cas.MAX_BLOB_BYTES
        for mode, _, sha, size, raw in tree:
            if raw in index_paths:
                continue
            if mode == _GITLINK:
                mark("gitlinks", raw)
                continue
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                mark("paths a cell cannot hold", raw)
                continue
            cell = _cell_path(self.repo_path, path)
            if any(segment in _SKIP_SEGMENTS for segment in path.split("/")):
                mark("paths in skipped folders", raw)
            elif not _admitted(cell):
                mark("paths a cell cannot hold", raw)
            elif cell in self.excluded:
                mark("paths the export renames or leaves out", raw)
            elif path in self.kept_out and cell not in self.hydrated:
                mark("files kept out of the cell", raw)
            elif mode == _SYMLINK:
                if self.mode == "export":
                    mark("links", raw)
                else:
                    links.append((sha, raw))
            elif size is not None and size > limit:
                mark(f"files over {_size(limit)}", raw)
            else:
                regular.append((sha, raw))
        created = self._plan_links(git_dir, tree, links, mark) if links else ()
        filtered = self._filter_guard(git_dir, regular)
        for raw in filtered:
            mark("filtered files", raw)
        for kind, paths in classes.items():
            if kind == "filtered files":
                continue
            names = [raw.decode("utf-8", "backslashreplace") for raw in paths]
            self.lines.append(f"{rs.shown(self.repo_path)}: skip-worktree, {len(paths)} {kind}: {_named(names)}")
        if filtered:
            self.lines.append(f"{rs.shown(self.repo_path)}: skip-worktree, {len(filtered)} filtered files frozen "
                              "at HEAD (their cell file is missing or differs)")
        if marked:
            self._git(git_dir, ["update-index", "-z", "--skip-worktree", "--stdin"],
                      input=b"".join(path + b"\0" for path in sorted(marked)))
        return created

    def _plan_links(self, git_dir: str, tree, links: list[tuple[str, bytes]], mark) -> tuple[Link, ...]:
        """The links of HEAD to create; the rest are marked (§4.3 step 11)."""
        targets = _blobs(git_dir, sorted({sha for sha, _ in links}))
        occupied = _occupied(self.hydrated)
        heads_links = frozenset(raw.decode("utf-8", "replace").casefold() for mode, _, _, _, raw in tree
                                if mode == _SYMLINK)
        created = []
        for sha, raw in links:
            path = raw.decode("utf-8")
            target = targets.get(sha)
            if target is None or not _link_ok(path, target, self.repo_path, occupied, heads_links):
                mark("links not created", raw)
            else:
                created.append(Link(path, target))
        return tuple(created)

    def _filter_guard(self, git_dir: str, candidates: list[tuple[str, bytes]]) -> list[bytes]:
        """The regular entries a ``filter`` attribute names (P9) whose cell
        file is missing or whose bytes are not HEAD's blob: frozen at HEAD
        (skip-worktree), since the work tree holds the cell's bytes, never
        a filter's output (§4.3 step 9, F17).

        The cell file is hashed here (:func:`_file_sha`), not by ``git
        hash-object --no-filters --stdin-paths``: the id is the same for a
        regular file, but hash-object opens the path with a plain
        ``open()``, which follows a link planted at it and blocks on a
        FIFO. Anything but a regular file is frozen."""
        if not candidates:
            return []
        self.step = "filter"
        done = self._git(git_dir, ["check-attr", "--cached", "-z", "--stdin", "filter"],
                         input=b"".join(path + b"\0" for _, path in candidates))
        fields = done.stdout.split(b"\0")
        named = {fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] not in (b"unspecified", b"unset")}
        frozen = []
        for sha, raw in candidates:
            if raw not in named:
                continue
            path = raw.decode("utf-8")
            if _cell_path(self.repo_path, path) not in self.hydrated or _file_sha(self.work_tree, path) != sha:
                frozen.append(raw)
        self.step = "skip-worktree"
        return frozen

    # --- step 11: the progress files, last -----------------------------------------------------------

    def _write_progress(self) -> None:
        """The operation's files, the last write: no git command runs after
        them, so none can act on (or remove) them before the run does."""
        for name in sorted(self.progress):
            self._put(name, self.progress[name])


def _admitted(cell_path: str) -> bool:
    """Whether a cell can hold ``cell_path`` (§4.3 step 8, admission)."""
    if "\\" in cell_path or is_reserved_path(cell_path):
        return False
    try:
        return "/".join(fs.relative_parts(cell_path)) == cell_path
    except PermissionError:
        return False


def _found(path: str, dir_fd: int | None = None) -> os.stat_result | None:
    """What is at ``path`` (named in ``dir_fd``), not following a link; None
    for nothing."""
    try:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _occupied(hydrated: Iterable[str]) -> set[str]:
    """Every cell path a cell file lies at or under: the files and their folders."""
    found = set()
    for path in hydrated:
        while path and path not in found:
            found.add(path)
            path = path.rpartition("/")[0]
    return found


def _link_ok(path: str, target: bytes, repo_path: str, occupied: Collection[str], links: Collection[str]) -> bool:
    """Whether the link ``path`` -> ``target`` is created: a relative
    target inside the work tree, no ``.git``-equivalent, band or skip-dir
    segment on either side, and no cell file at or under the link
    (``occupied``: :func:`_occupied` of the cell's files).

    The target is walked a component at a time from the link's folder: a
    ``..`` above the work tree refuses it, and so does passing through
    another of HEAD's links (``links``, casefolded): the kernel resolves
    what follows through that link's target, which the walk cannot see
    (``x -> y/..`` with ``y -> .`` leaves the work tree)."""
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text or "\0" in text or text.startswith("/") or "\\" in text:
        return False
    stack = [part for part in posixpath.dirname(path).split("/") if part]
    names = text.split("/")
    for n, name in enumerate(names):
        if name in ("", "."):
            continue
        if name == "..":
            if not stack:
                return False
            stack.pop()
        else:
            stack.append(name)
        if n < len(names) - 1 and "/".join(stack).casefold() in links:
            return False
    resolved = "/".join(stack)
    for segment in [*path.split("/"), *text.split("/"), *resolved.split("/")]:
        if segment in ("", ".", ".."):
            continue
        if is_dotgit_segment(segment) or segment.casefold() in _LINK_FORBIDDEN:
            return False
    return _cell_path(repo_path, path) not in occupied


def link_ok_in_cell(cell_path: str, target: bytes, links: Collection[str]) -> bool:
    """:func:`_link_ok` across a cell's repositories: the link at
    ``cell_path`` (cell-relative) stays in the cell's work tree and passes
    through none of ``links``, every link the cell's repositories create
    (cell-relative, casefolded). Each repository checks only its own HEAD's
    links, so a root link through a nested repository's ``..`` link
    passes there."""
    return _link_ok(cell_path, target, "", (), links)


def _blobs(git_dir: str, shas: list[str]) -> dict[str, bytes]:
    """The bytes of each blob ``shas`` names, by ``cat-file --batch``."""
    done = git_host.run(["cat-file", "--batch"], git_dir=git_dir,
                        input="".join(f"{sha}\n" for sha in shas).encode("ascii"))
    out, at, found = done.stdout, 0, {}
    while at < len(out):
        end = out.index(b"\n", at)
        fields = out[at:end].decode("ascii", "replace").split(" ")
        at = end + 1
        if len(fields) != 3 or fields[1] == "missing":
            continue
        size = int(fields[2])
        if fields[1] == "blob":
            found[fields[0]] = out[at:at + size]
        at += size + 1
    return found


def _file_sha(work_tree: str, path: str) -> str | None:
    """The blob id of the regular file ``path`` in the work tree, or None
    when it is not one (missing, a link, unreadable)."""
    try:
        fd = fs.open_file(work_tree, path, write=False)
    except OSError:
        return None
    try:
        return _blob_sha(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def _chmod(work_tree: str, path: str, executable: bool) -> None:
    """Give the regular file ``path`` HEAD's exec bits, through its
    parent's fd, never following a link."""
    parts = fs.relative_parts(path)
    parent = fs.open_parent(work_tree, parts, create=False)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"not a regular file: {path}")
        mode = stat.S_IMODE(st.st_mode)
        wanted = mode | ((mode & 0o444) >> 2) if executable else mode & ~0o111
        if wanted != mode:
            os.fchmod(fd, wanted)
    finally:
        os.close(fd)


def create_links(work_tree: str, links: Iterable[Link], *, repo_path: str = "") -> list[str]:
    """Create the links :func:`write_gitdir` planned, after the run's last
    host write: each with ``os.symlink`` through its parent's fd, the
    parents made without following a link (an ancestor that is one refuses
    it), nothing already at its path. The lines to log; a link not created
    shows as deleted in ``git status``."""
    lines, failed = [], []
    for link in links:
        try:
            parts = fs.relative_parts(link.path)
            parent = fs.open_parent(work_tree, parts, create=True)
        except OSError as exc:
            failed.append(f"{link.path} ({exc.strerror or exc})")
            continue
        try:
            os.symlink(link.target, parts[-1], dir_fd=parent)
        except OSError as exc:
            failed.append(f"{link.path} ({exc.strerror or exc})")
        finally:
            os.close(parent)
    if failed:
        lines.append(f"{rs.shown(repo_path)}: links not created: {_named(failed)}")
    return lines

