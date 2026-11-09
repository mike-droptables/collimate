# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml"]
# ///
"""Git: three thin runners over plain git on this machine, one per action.
Clone brings a repository into a folder of this cell (or its root), Update
brings the remote's new commits into a repository's branch, Push sends its
commits. The cell keeps the repository's own ``.git``, which the host keeps
between runs; every command names it (``--git-dir``/``--work-tree``) and
never discovers one. The address, credential and commit author come from the
run's Git account (_env_shared/git_account.py, _env_shared/git_run.py), the
Repository, Folder and branches are checked first (prompt.py), and the
manifest's auto mirror carries what git writes into the cell. What git says
reaches the run's log in git's own words."""
import asyncio
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))
import gen  # noqa: E402
from collimate import fs  # noqa: E402
from collimate.git_hosts import RemoteRefused, remote_problem  # noqa: E402

import prompt  # noqa: E402
from _env_shared import git_account, git_run  # noqa: E402
from _env_shared.git_run import Git, GitFailure, Remote  # noqa: E402

MAIN = "settings.set_yaml"
ACTIONS = ("clone", "update", "push")
# Update's on_divergence: how git pull joins the remote's commits.
_MODES = {"merge": "--no-rebase", "rebase": "--rebase", "ff-only": "--ff-only"}
# What a cell root holds beside a repository cloned into it: the host's
# bands and the folder marker, which the root repository's info/exclude
# keeps out of git.
_BANDS = (".collrun", "_runs", "_agent", ".pydantic-ai-harness")
_ROOT_KEEPS = frozenset({*_BANDS, ".collkeep"})
_EXCLUDES = ("/.collrun/", "/_runs/", "/_agent/", "/.pydantic-ai-harness/", ".collkeep")
_FOLDER_MARKER = ".collkeep"
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_REBASING = ("rebase-merge", "rebase-apply")
# Where git merge --autostash keeps the changes it set aside until the merge is committed.
_SET_ASIDE = "MERGE_AUTOSTASH"
_MARKER = "leftover conflict marker"
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_BASE = re.compile(r"[0-9a-fA-F]{4,64}")
# A git push --porcelain status line: flag, TAB, from:to, TAB, summary.
_PUSHED = re.compile(r"([ +\-*!=])\t[^\t]*\t.*")

# ── messages ──────────────────────────────────────────────────────────

ACTION_ERROR = "Action must be Clone, Update or Push"
DIVERGENCE_ERROR = "On divergence must be merge, rebase or ff-only"
BASE_ERROR = "Base commit is a commit id: 4 to 64 hexadecimal digits"
E1 = "Git needs internet access; set Internet to full in this runner's permissions. Nothing changed"
E2 = "git is not installed on this machine (not on the run's PATH); install git 2.31 or later and run again"
E3 = ("git on this machine does not run (git --version failed); on macOS install the Command Line Tools "
      "(xcode-select --install)")
E4 = "git {v} is too old; Git runs need git 2.31 or later"
A5 = ("SSH remotes need Internet set to full in this runner's permissions: SSH cannot pass the run's web proxy. "
      "Nothing changed")
UNEXPECTED = "Git run failed unexpectedly ({type}); see the log"

F1 = "{f} already holds a repository; run Git Update or Git Push on it, or set Folder to another folder"
F2 = "{f} is a file or a link in this cell, not a folder; set Folder to another folder"
F3 = ("{f} holds files but no repository. To keep them as a checkout of {r}, set Base commit to the commit they "
      "are based on (a JSON record left by the retired checkout handler names it as its commit), or attach them "
      "in a terminal: git init -b {b} && git remote add origin {u} && git fetch --tags origin && git reset <commit> "
      "&& git branch --set-upstream-to=origin/{b}")
F4 = "no repository at {f}"
F5 = "this cell has no repository; run Git Clone first, or set Folder"
F6 = "this cell has several repositories ({list}); set Folder to the one to {verb}"
C1 = "Remote branch {b} not found in upstream origin"
C2 = "{r} {b} has a top-level {segment}, which the cell keeps for itself; clone it into a folder instead"
C3 = "Base commit {c} is not in {r}'s history"
C4 = "{r} has no default branch to attach to; set Branch"
C5 = "Base commit is only used to attach files already in the folder; cloning {r} instead"
L1 = "You appear to have cloned an empty repository."
L2 = "warning: remote HEAD refers to nonexistent ref, unable to checkout"
U1 = "finish the merge first"
U2 = "finish the merge first: staged resolutions must be re-added"
U3 = "HEAD is detached in {f}; check out a branch to update it"
U4 = "branch {b} has no upstream, and {r} has no branch {b}; set one with git branch --set-upstream-to"
U5 = ("Update stopped on conflicts in {list}: the markers are in the files; resolve them and run Git Update "
      "again, or Push with a Commit message")
U5_SET_ASIDE = "; your uncommitted changes come back when the merge is committed"
U6 = ("Applying autostash resulted in conflicts in {list}: your uncommitted changes are also kept in the stash "
      "(git stash list)")
U7 = ("Update stopped rebasing on conflicts in {list}: resolve them, then git add them and git rebase --continue "
      "in a terminal")
U8 = "a rebase is in progress in {f}; finish it (git rebase --continue, or --abort) in a terminal"
U9 = ("a stopped merge left uncommitted changes set aside in {f} (.git/MERGE_AUTOSTASH); git merge --quit in a "
      "terminal puts them in the stash list, then git stash pop brings them back")
R1 = "{f} has no remote; add one (git remote add origin <url>)"
R2 = "{key} names {r}, which is not a remote of {f}"
R3 = "remote {r} of {f}: {problem}"
R4 = "a remote's addresses use different transports (https and ssh); give it addresses on one"
R5 = ("remote {r} of {f} pushes to another address than it fetches from, which needs a Git account bound to "
      "this runner")
P1 = "{f} has uncommitted changes; set Commit message to commit them before pushing"
P2 = "{f} still has conflict markers ({list}); resolve them, then push"
P3 = "HEAD is detached in {f}; set Push to branch"
P4 = "Everything up-to-date, but {f} still has changes git did not commit"


class GitRunError(RuntimeError):
    """A refusal or failure whose ``str`` is the run's one-line report."""


# ── settings ──────────────────────────────────────────────────────────


def _text(s: dict, key: str, label: str) -> str:
    """A one-line text setting, stripped; a YAML 1.1 boolean (an unquoted
    on/off/yes/no) or None is blank, and a line break refuses (never
    echoing the value)."""
    value = s.get(key)
    if value is None or isinstance(value, bool):
        return ""
    value = str(value).strip()
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be one line")
    return value


def _choice(value, allowed: Sequence[str], default: str, error: str) -> str:
    """A dropdown value: a YAML 1.1 boolean or None is the default; anything outside ``allowed`` refuses."""
    if value is None or isinstance(value, bool):
        return default
    choice = str(value).strip().lower()
    if choice not in allowed:
        raise ValueError(error)
    return choice


def _where(folder: str) -> str:
    return "the cell root" if folder == "." else f"{folder}/"


def _list(paths: Sequence[str]) -> str:
    paths = list(paths)
    shown = ", ".join(prompt.shorten(p) for p in paths[:3])
    return shown + (f", +{len(paths) - 3} more" if len(paths) > 3 else "")


def _parts_of(folder: str) -> list[str]:
    return [] if folder == "." else folder.split("/")


# ── the cell's folders ────────────────────────────────────────────────


def _folder_state(workspace: Path, folder: str) -> str:
    """What ``folder`` holds, reached without following a link: ``absent``,
    ``empty`` (nothing, or only the folder marker), ``files`` or
    ``repository`` (a ``.git`` entry). A link or a file on the way refuses (F2)."""
    parts = _parts_of(folder)
    try:
        fd = fs.open_parent(str(workspace), [*parts, ".git"], create=False)
    except FileNotFoundError:
        return "absent"
    except OSError:
        raise GitRunError(F2.format(f=_where(folder))) from None
    try:
        names = os.listdir(fd)
    finally:
        os.close(fd)
    if ".git" in names:
        return "repository"
    return "files" if set(names) - {_FOLDER_MARKER} else "empty"


def _has_gitdir(workspace: Path, folder: str) -> bool:
    """Whether ``folder`` holds a ``.git`` directory, every part of the way a
    real directory (a gitfile or a link is no repository here)."""
    try:
        fd = fs.open_parent(str(workspace), [*_parts_of(folder), ".git"], create=False)
    except OSError:
        return False
    try:
        info = os.stat(".git", dir_fd=fd, follow_symlinks=False)
    except OSError:
        return False
    finally:
        os.close(fd)
    return stat.S_ISDIR(info.st_mode)


# ── one run's git ─────────────────────────────────────────────────────


@dataclass
class _Session:
    """One run's git: the account, a temporary HOME and the local ``Git``
    (no transport at all: it never reaches a remote)."""
    workspace: Path
    account: "git_account.GitAccount | None"
    home: Path
    log: Callable[[str], None]
    secrets: tuple

    def __post_init__(self) -> None:
        self.local = Git(Remote("", ()), None, self.home, log=self.log, secrets=self.secrets)

    def network(self, urls: Sequence[str], *, config: Sequence[str] = ()) -> Git:
        """The ``Git`` for a remote at ``urls`` (production addresses, the
        first fetched from): one transport, the account's credential for
        every address, refused before any process when it cannot serve one.
        The address git really talks to (``resolve_remote``, through the
        ``git_run.transport`` seam) stands in for each address git reads
        through ``url.<to>.insteadOf``, so ``.git/config`` keeps the
        repository's own: a forge SSH account reaches an https origin over
        SSH without rewriting it."""
        remotes = [git_run.resolve_remote(url, self.account) for url in urls]
        if len({remote.protocols for remote in remotes}) > 1:
            raise GitRunError(R4)
        if remotes[0].protocols == ("ssh",) and gen.grant().internet != "full":
            raise GitRunError(A5)
        auths = [self._credential(remote) for remote in remotes]
        wires = [git_run.transport(remote, url) for remote, url in zip(remotes, urls)]
        rewrites = [f"url.{wire.url}.insteadOf={url}" for url, wire in zip(urls, wires) if wire.url != url]
        return Git(wires[0], auths[0], self.home, config=(*rewrites, *config), log=self.log,
                   secrets=self.secrets)

    def _credential(self, remote: Remote):
        r = prompt.display_of(remote.url)
        if self.account is None:
            if remote.protocols == ("ssh",):
                raise GitRunError(prompt.A2.format(r=r))
            return None
        try:
            return self.account.auth_for(remote)
        except RemoteRefused as exc:
            f = exc.fields
            if exc.code == "scope":
                message = prompt.A1.format(base=f.get("base", ""), host=f.get("host", ""))
            elif exc.code == "transport":
                message = prompt.A3.format(method=f["method"], scheme=f["scheme"], other=f["other"])
            else:
                message = prompt.A4.format(hostport=f.get("hostport", ""))
            raise GitRunError(message) from None

    async def git(self, top: Path, *args: str, ok: tuple[int, ...] = (0,), echo: bool = False,
                  input: "bytes | None" = None) -> git_run.Completed:
        """A local command in the repository whose work tree is ``top``."""
        return await self.local.run(args, top, ok=ok, echo=echo, input=input, repository=top)

    async def text(self, top: Path, *args: str, ok: tuple[int, ...] = (0, 1)) -> str:
        """A local command's stdout, stripped ("" for an exit in ``ok`` other than 0)."""
        done = await self.git(top, *args, ok=ok)
        return done.stdout.decode("utf-8", "surrogateescape").strip() if done.code == 0 else ""


async def _preflight(log: Callable[[str], None]) -> None:
    try:
        version = await git_run.git_version()
    except GitFailure as exc:
        if exc.kind == "missing_git":
            raise GitRunError(E2) from None
        if exc.kind == "old_git":
            raise GitRunError(E4.format(v=exc.version)) from None
        raise GitRunError(E3) from None
    log(f"git {version}")


def _records(out: bytes) -> list[str]:
    return [r.decode("utf-8", "surrogateescape") for r in out.split(b"\0") if r]


def _literal(paths: Sequence[str]) -> list[str]:
    return [f":(literal){p}" for p in paths]


async def _config(session: _Session, top: Path) -> dict[str, list[str]]:
    """The repository's own config (``--local``), by key as git spells it
    (section and variable lowercase, subsection as written)."""
    out = (await session.git(top, "config", "--local", "-z", "--list", ok=(0, 1))).stdout
    config: dict[str, list[str]] = {}
    for record in _records(out):
        key, _, value = record.partition("\n")
        config.setdefault(key, []).append(value)
    return config


def _last(config: dict[str, list[str]], key: str) -> str:
    values = config.get(key)
    return values[-1] if values else ""


async def _head(session: _Session, top: Path) -> tuple[str, str]:
    """``(branch, sha)`` of HEAD; ``""`` for a detached HEAD's branch or an unborn HEAD's sha."""
    branch = await session.text(top, "symbolic-ref", "-q", "--short", "HEAD")
    sha = await session.text(top, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    return branch, sha if _SHA.fullmatch(sha) else ""


# ── Clone ─────────────────────────────────────────────────────────────


async def _clone(session: _Session, net: Git, url: str, address: str, folder: str, branch: str, base: str) -> str:
    r = prompt.display_of(address)
    workspace = session.workspace
    if folder == ".":
        if os.path.lexists(workspace / ".git"):
            raise GitRunError(F1.format(f=_where(folder)))
        state = "files" if set(os.listdir(workspace)) - _ROOT_KEEPS else "empty"
    else:
        state = await asyncio.to_thread(_folder_state, workspace, folder)
    if state == "repository":
        raise GitRunError(F1.format(f=_where(folder)))
    if state == "files":
        if not base:
            raise GitRunError(F3.format(f=_where(folder), r=r, b=branch or "main", u=url))
        return await _attach(session, net, url, r, folder, branch, base)
    if base:
        session.log(C5.format(r=r))
    if folder == ".":
        return await _clone_root(session, net, url, r, branch)
    marker = workspace / folder / _FOLDER_MARKER
    if state == "empty" and os.path.lexists(marker):
        os.unlink(marker)                 # git clones only into an empty folder
    session.log(f"Cloning {r} into {_where(folder)}")
    try:
        await net.run(["clone", *(["--branch", branch] if branch else []), "--", url, folder], workspace,
                      network=True, echo=True)
    except BaseException:
        if state == "empty" and (workspace / folder).is_dir() and not os.path.lexists(marker):
            marker.write_bytes(b"")
        raise
    head_branch, sha = await _head(session, workspace / folder)
    if not sha:
        return f"Cloned {r} into {_where(folder)}: it has no commits yet"
    return f"Cloned {r} {head_branch or branch} into {_where(folder)} at {sha[:7]}"


async def _start(session: _Session, net: Git, top: Path, url: str, branch: str) -> None:
    """``git init`` at ``top`` with origin at ``url``, fetched with its tags."""
    await session.local.run(["init", "-q", "-b", branch or "main", str(top)], top)
    await session.git(top, "remote", "add", "origin", "--", url)
    await net.run(["fetch", "--tags", "origin"], top, network=True, echo=True, repository=top)


async def _remote_head(session: _Session, net: Git, top: Path) -> str:
    """The remote's default branch: ``origin/HEAD`` after ``set-head -a``
    (whose exit 1, "Cannot determine remote HEAD", is an empty remote's), or ""."""
    await net.run(["remote", "set-head", "origin", "-a"], top, network=True, ok=(0, 1), repository=top)
    head = await session.text(top, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    return head.removeprefix("origin/") if head.startswith("origin/") else ""


async def _exists(session: _Session, top: Path, ref: str) -> bool:
    return (await session.git(top, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}", ok=(0, 1))).code == 0


def _exclude_bands(top: Path) -> None:
    """Append to a root repository's info/exclude the host's bands and the
    folder marker it does not list yet."""
    info = top / ".git" / "info"
    info.mkdir(exist_ok=True)
    path = info / "exclude"
    current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    missing = [line for line in _EXCLUDES if line not in current.splitlines()]
    if not missing:
        return
    lead = "" if not current or current.endswith("\n") else "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(lead + "".join(f"{line}\n" for line in missing))


async def _clone_root(session: _Session, net: Git, url: str, r: str, branch: str) -> str:
    """Clone into the cell root, which already holds the host's bands: init,
    fetch and check out, never ``git clone`` into ``.``."""
    top = session.workspace
    session.log(f"Cloning {r} into the cell root")
    try:
        await _start(session, net, top, url, branch)
        default = await _remote_head(session, net, top)
        target, tag = branch or default, False
        if branch and not await _exists(session, top, f"refs/remotes/origin/{branch}"):
            if not await _exists(session, top, f"refs/tags/{branch}"):
                raise GitRunError(C1.format(b=branch))
            tag = True
        if not target:
            remotes = await session.text(top, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/")
            session.log(L1 if not remotes else L2)
            _exclude_bands(top)
            return f"Cloned {r} into the cell root: it has no commits yet"
        rev = f"refs/tags/{target}" if tag else f"refs/remotes/origin/{target}"
        names = _records((await session.git(top, "ls-tree", "--name-only", "-z", rev)).stdout)
        for name in names:
            if name.casefold() in {band.casefold() for band in _BANDS}:
                raise GitRunError(C2.format(r=r, b=target, segment=name))
        if tag:
            await session.git(top, "checkout", "--detach", rev, echo=True)
        else:
            await session.git(top, "checkout", "-B", target, "--track", f"origin/{target}", echo=True)
        _exclude_bands(top)
    except BaseException:
        shutil.rmtree(top / ".git", ignore_errors=True)
        raise
    _head_branch, sha = await _head(session, top)
    return f"Cloned {r} {target} into the cell root at {sha[:7]}"


async def _attach(session: _Session, net: Git, url: str, r: str, folder: str, branch: str, base: str) -> str:
    """Make the files already in ``folder`` a checkout of ``url`` at ``base``:
    the branch points at it, its index is its tree, the files stay as they
    are, so ``git status`` shows the edits made since."""
    top = session.workspace if folder == "." else session.workspace / folder
    session.log(f"Attaching {_where(folder)} to {r} at {base}")
    try:
        await _start(session, net, top, url, branch)
        if not branch:
            branch = await _remote_head(session, net, top)
            if not branch:
                raise GitRunError(C4.format(r=r))
            await session.git(top, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        if not await _exists(session, top, base):
            raise GitRunError(C3.format(c=base, r=r))
        await session.git(top, "reset", "-q", base, "--")
        await session.git(top, "branch", f"--set-upstream-to=origin/{branch}")
        if folder == ".":
            _exclude_bands(top)
    except BaseException:
        shutil.rmtree(top / ".git", ignore_errors=True)
        raise
    await session.git(top, "status", "--short", "--branch", echo=True)
    _branch, sha = await _head(session, top)
    return f"Attached {_where(folder)} to {r} {branch} at {sha[:7]}"


# ── Update and Push ───────────────────────────────────────────────────


async def _repository(session: _Session, folder: str, verb: str) -> str:
    """The repository the run works on: the Folder setting, or with it blank
    the cell's only repository."""
    if folder:
        if not await asyncio.to_thread(_has_gitdir, session.workspace, folder):
            raise GitRunError(F4.format(f=_where(folder)))
        return folder
    found = await asyncio.to_thread(git_run.repositories, session.workspace)
    if not found:
        raise GitRunError(F5)
    if len(found) > 1:
        raise GitRunError(F6.format(list=_list(found), verb=verb))
    return found[0]


async def _remote(session: _Session, top: Path, folder: str, config: dict[str, list[str]],
                  keys: Sequence[str]) -> str:
    """The first remote ``keys`` name, else the only remote, else origin; a
    key naming no remote refuses."""
    remotes = (await session.text(top, "remote", ok=(0,))).splitlines()
    for key in keys:
        name = _last(config, key)
        if name:
            if name not in remotes:
                raise GitRunError(R2.format(key=key, r=name, f=_where(folder)))
            return name
    if len(remotes) == 1:
        return remotes[0]
    if "origin" in remotes:
        return "origin"
    raise GitRunError(R1.format(f=_where(folder)))


async def _urls(session: _Session, top: Path, folder: str, r: str, *, push: bool = False) -> list[str]:
    """The addresses of remote ``r`` (``--push``: those it pushes to), each one a run may use."""
    out = await session.text(top, "remote", "get-url", *(["--push"] if push else []), "--all", r, ok=(0,))
    urls = out.splitlines()
    for url in urls:
        problem = remote_problem(url)
        if problem:
            raise GitRunError(R3.format(r=r, f=_where(folder), problem=problem))
    if not urls:
        raise GitRunError(R1.format(f=_where(folder)))
    return urls


async def _unmerged(session: _Session, top: Path) -> list[str]:
    out = (await session.git(top, "ls-files", "-u", "-z")).stdout
    return sorted({record.split("\t", 1)[1] for record in _records(out) if "\t" in record})


async def _markers(session: _Session, top: Path, *args: str) -> list[str]:
    """The paths in which ``git diff <args>`` (a ``--check``) finds a
    conflict marker, each such line logged (whitespace errors exit 2 as
    well, so the text decides)."""
    done = await session.git(top, "diff", *args, ok=(0, 1, 2))
    paths: set[str] = set()
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        where, found, _ = line.partition(f": {_MARKER}")
        if found:
            session.log(line)
            paths.add(where.rpartition(":")[0])
    return sorted(paths)


async def _finish_merge(session: _Session, top: Path) -> None:
    """A merge a run left stopped: commit it once its conflicts are resolved
    in the files. Only the formerly unmerged paths are added; every other
    change stays for the autostash."""
    paths = await _unmerged(session, top)
    if paths and await _markers(session, top, "HEAD", "--check", "--", *_literal(paths)):
        raise GitRunError(U1)
    if await _markers(session, top, "--cached", "--check"):
        raise GitRunError(U1)
    if not paths and (await session.git(top, "diff", "--cached", "--quiet", "HEAD", ok=(0, 1))).code == 0:
        raise GitRunError(U2)
    if paths:
        await session.git(top, "add", "-A", "--", *_literal(paths))
    await session.git(top, "commit", "--no-edit", echo=True)


async def _makes_commits(session: _Session, top: Path, branch: str, mode: str,
                        config: dict[str, list[str]]) -> bool:
    """Whether ``git pull`` in ``mode`` records commits of its own: a merge
    or a rebase of a branch and an upstream neither of which contains the
    other, or a merge that never fast-forwards (``pull.ff``/``merge.ff``
    false) bringing new commits."""
    upstream = f"{branch}@{{upstream}}"
    if mode == "ff-only" or not await _exists(session, top, upstream):
        return False

    async def contains(newer: str, older: str) -> bool:
        return (await session.git(top, "merge-base", "--is-ancestor", older, newer, ok=(0, 1))).code == 0

    if await contains("HEAD", upstream):
        return False
    never_ff = mode == "merge" and any(_last(config, key).lower() in ("false", "no", "off", "0")
                                       for key in ("pull.ff", "merge.ff"))
    return never_ff or not await contains(upstream, "HEAD")


def _stopped(top: Path, conflicts: Sequence[str]) -> str:
    """U5, saying where a merge's autostash keeps the uncommitted changes."""
    message = U5.format(list=_list(conflicts))
    return message + U5_SET_ASIDE if os.path.lexists(top / ".git" / _SET_ASIDE) else message


async def _update(session: _Session, folder: str, mode: str) -> str:
    folder = await _repository(session, folder, "update")
    top = session.workspace if folder == "." else session.workspace / folder
    if any(os.path.lexists(top / ".git" / name) for name in _REBASING):
        raise GitRunError(U8.format(f=_where(folder)))
    merging = os.path.lexists(top / ".git" / "MERGE_HEAD")
    if not merging and os.path.lexists(top / ".git" / _SET_ASIDE):
        raise GitRunError(U9.format(f=_where(folder)))
    if merging:
        await _finish_merge(session, top)
    branch, before = await _head(session, top)
    if not branch:
        raise GitRunError(U3.format(f=_where(folder)))
    config = await _config(session, top)
    tracked = bool(_last(config, f"branch.{branch}.remote") and _last(config, f"branch.{branch}.merge"))
    r = await _remote(session, top, folder, config, [f"branch.{branch}.remote"] if tracked else [])
    net = session.network(await _urls(session, top, folder, r))
    await net.run(["fetch", r], top, network=True, echo=True, repository=top)
    if not tracked:
        if not await _exists(session, top, f"refs/remotes/{r}/{branch}"):
            raise GitRunError(U4.format(b=branch, r=r))
        await session.git(top, "branch", f"--set-upstream-to={r}/{branch}", echo=True)
    pull = ["pull", _MODES[mode]]
    if mode == "merge" and _last(config, "pull.ff").lower() == "only":
        pull.append("--ff")               # on git 2.31 pull.ff=only stops even an explicit merge
    if before and await _makes_commits(session, top, branch, mode, config):
        # Those commits need an author: git's own "Please tell me who you
        # are" now, before the autostash takes a dirty tree's changes aside.
        for ident in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            await session.git(top, "var", ident)
    stash = await session.text(top, "rev-parse", "-q", "--verify", "refs/stash")
    try:
        await net.run([*pull, "--autostash", r], top, network=True, echo=True, repository=top)
    except GitFailure as exc:
        conflicts = await _unmerged(session, top)
        if exc.code == 1 and conflicts:
            if any(os.path.lexists(top / ".git" / name) for name in _REBASING):
                raise GitRunError(U7.format(list=_list(conflicts))) from None
            raise GitRunError(_stopped(top, conflicts)) from None
        raise
    conflicts = await _unmerged(session, top)
    if conflicts:
        if await session.text(top, "rev-parse", "-q", "--verify", "refs/stash") != stash:
            raise GitRunError(U6.format(list=_list(conflicts)))
        raise GitRunError(_stopped(top, conflicts))
    _branch, after = await _head(session, top)
    if after == before:
        note = f"{_where(folder)} is up to date with {r}/{branch} at {after[:7]}"
        return f"Committed the merge; {note}" if merging else note
    verb = "Committed the merge and updated" if merging else "Updated"
    return f"{verb} {_where(folder)} {branch} from {r}: {before[:7] or 'unborn'}→{after[:7]}"


async def _dirty(session: _Session, top: Path) -> bool:
    out = (await session.git(top, "status", "--porcelain", "-z", "--untracked-files=all")).stdout
    return bool(out.strip(b"\0"))


async def _push(session: _Session, folder: str, message: str, push_branch: str) -> str:
    folder = await _repository(session, folder, "push")
    top = session.workspace if folder == "." else session.workspace / folder
    if any(os.path.lexists(top / ".git" / name) for name in _REBASING):
        raise GitRunError(U8.format(f=_where(folder)))
    branch, head = await _head(session, top)
    if not branch and not push_branch:
        raise GitRunError(P3.format(f=_where(folder)))
    if folder == ".":
        _exclude_bands(top)
    operation = any(os.path.lexists(top / ".git" / name) for name in _IN_PROGRESS)
    if operation or await _dirty(session, top):
        # Markers in the files of unmerged paths, and while a merge,
        # cherry-pick or revert stands, markers already added: only those
        # (a plain edit's "=======" underline is no conflict).
        unmerged = await _unmerged(session, top)
        marked = (await _markers(session, top, "HEAD", "--check", "--", *_literal(unmerged))
                  if head and unmerged else [])
        if operation:
            marked += await _markers(session, top, "--cached", "--check")
        if marked:
            raise GitRunError(P2.format(f=_where(folder), list=_list(sorted(set(marked)))))
        if not message:
            raise GitRunError(P1.format(f=_where(folder)))
        await session.git(top, "add", "-A")
        await session.git(top, "commit", "-m", message, echo=True)
    config = await _config(session, top)
    upstream = _last(config, f"branch.{branch}.remote") if branch else ""
    merge = _last(config, f"branch.{branch}.merge") if branch else ""
    keys = ([f"branch.{branch}.pushremote", "remote.pushdefault", f"branch.{branch}.remote"] if branch
            else ["remote.pushdefault"])
    r = await _remote(session, top, folder, config, keys)
    urls = await _urls(session, top, folder, r, push=True)
    if session.account is None and set(urls) != set(await _urls(session, top, folder, r)):
        raise GitRunError(R5.format(r=r, f=_where(folder)))
    if push_branch:
        dest = push_branch
    elif r == upstream and merge.startswith("refs/heads/"):
        dest = merge.removeprefix("refs/heads/")
    else:
        dest = branch
    net = session.network(urls, config=(f"remote.{r}.mirror=false",))
    set_upstream = not (upstream and merge) and not push_branch
    done = await net.run(["push", "--porcelain", *(["--set-upstream"] if set_upstream else []), r,
                          f"HEAD:refs/heads/{dest}"], top, network=True, echo=True, repository=top)
    statuses = [line[0] for line in done.stdout.decode("utf-8", "replace").splitlines() if _PUSHED.fullmatch(line)]
    _branch, sha = await _head(session, top)
    if statuses and all(flag == "=" for flag in statuses):
        if await _dirty(session, top):
            raise GitRunError(P4.format(f=_where(folder)))
        return f"Everything up-to-date: {r} {dest} is at {sha[:7]}"
    return f"Pushed {_where(folder)} {branch or 'HEAD'} to {r} {dest} at {sha[:7]}"


# ── the run ───────────────────────────────────────────────────────────


async def run():
    secrets: tuple = ()

    def log(line: str) -> None:
        gen.log(git_run.scrub(str(line), secrets))

    try:
        if gen.grant().internet == "none":
            raise GitRunError(E1)
        s = gen.config(MAIN) or {}
        action = _choice(s.get("action"), ACTIONS, "clone", ACTION_ERROR)
        account = git_account.git_account()
        secrets = account.secret_forms() if account else ()
        folder_setting = _text(s, "folder", "Folder")
        folder = prompt.check_folder(folder_setting) if folder_setting else ""
        workspace = Path(gen.workspace_path())
        if action == "clone":
            address = prompt.parse_prompt(gen.prompt, account, log=log)
            branch = _text(s, "branch", "Branch")
            if branch:
                prompt.check_branch(branch)
            base = _text(s, "base_commit", "Base commit")
            if base and not _BASE.fullmatch(base):
                raise ValueError(BASE_ERROR)
            folder = folder or prompt.default_folder(address)
        elif action == "update":
            mode = _choice(s.get("on_divergence"), tuple(_MODES), "merge", DIVERGENCE_ERROR)
        else:
            message = _text(s, "commit_message", "Commit message")
            push_branch = _text(s, "push_branch", "Push to branch")
            if push_branch:
                prompt.check_branch(push_branch, prompt.I7)
        git_run.sweep_stale_sessions()
        with tempfile.TemporaryDirectory(prefix=f"collimate-git-{os.getpid()}-") as tmp:
            session = _Session(workspace, account, Path(tmp), log, secrets)
            if action == "clone":
                # Every refusal of the address and its credential comes before any process.
                url = git_run.resolve_remote(address, account).url
                net = session.network([url])
            await _preflight(log)
            if action == "clone":
                note = await _clone(session, net, url, address, folder, branch, base.lower())
            elif action == "update":
                note = await _update(session, folder, mode)
            else:
                note = await _push(session, folder, message, push_branch)
        log(note)
        gen.finish(note=git_run.scrub(note, secrets), outcome="pass")
    except (GitRunError, ValueError, PermissionError) as exc:
        message = git_run.scrub(str(exc), secrets)
        log(message)
        gen.finish(note=message, outcome="fail")
        raise RuntimeError(message) from None
    except GitFailure as exc:
        # git_run logged what failed and git's own words already.
        message = git_run.failure_line(exc)
        gen.finish(note=message, outcome="fail")
        raise RuntimeError(message) from None
    except (gen.Cancelled, asyncio.CancelledError):
        raise
    except Exception as exc:
        gen.finish(note=UNEXPECTED.format(type=type(exc).__name__), outcome="fail")
        raise


if __name__ == "__main__":
    gen.main(run)
