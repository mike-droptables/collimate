# SPDX-License-Identifier: MIT
"""Clone, update and push Git repositories held in folders of a cell.

A cell never holds ``.git``. Beside each repository folder ``<folder>/`` the
cell root holds ``<folder>.git-checkout.json``, recording ``{about, remote,
branch, commit}``: the remote's address (never a credential) and the commit
the folder's files are based on. It is the built-in ``git-checkout``
handler's type: the app checks it, and the handler's skill tells agents how
to run git against that commit without ``.git``. It sits beside the folder,
not in it, so the handler's verifier reads the root's files instead of the
whole repository. Every git step runs in a temporary clone in the system
temp folder (``git_run``) with the run's Git account (``git_account``), and
the cell is changed only after every network and git step succeeded:

* **sync** clones a folder the first time, and later merges the remote's new
  commits into the folder's edits; a conflict changes nothing.
* **push** sends the folder's edits as one commit on the recorded commit:
  straight on, after merging the remote's new commits, or onto a new branch.
  It never force-pushes.

The engine commits the cell itself, in two phases: the files first, then,
only when every file of a folder landed, its checkout record. A record therefore
always records a commit whose files the cell holds (plus local edits), so a
lost or partial save converges on the next run instead of letting a push
delete refused files on the remote.

Symlinks, submodules, files over 20 MB and anything under
``WORKSPACE_SKIP_DIRS`` are kept in Git only: never placed in the cell, never
deleted by the overlay, never pushed as deletions.

No message echoes unvalidated input: only checked remotes, folders and
branches, and cell or repository paths appear in them."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import urlsplit

import gen
from collimate import fs, git_hosts
from collimate.pointers import RUNS_PREFIX

from _env_shared import git_run
from _env_shared.git_account import GitAccount, RemoteRefused
from _env_shared.git_run import Git, GitFailure, Identity, Remote

MARKER_SUFFIX = ".git-checkout.json"      # the built-in git-checkout handler's type
FILE_CAP = 20 * 1024 * 1024               # tests may monkeypatch it smaller
MARKER_CAP = 4096                         # bytes, whole file
ABOUT_CAP = 500                           # code points
REMOTE_CAP = 512                          # code points
ABOUT = ("Git checkout record: the folder of the same name beside this file holds this repository's files at "
         "this commit plus the cell's edits, without .git. Before any git operation (diff, log, blame, compare, "
         "sync or push) read the git-checkout handler's skill (handler_skills tool, id git-checkout).")
_VERIFY_RACED = "cell changed during verification; retry"   # api/run_verification.RunVerifier.raced
_RECORD_ATTEMPTS = 3

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FOLDER = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,99}")
_RESERVED_FOLDERS = frozenset({".git", ".collrun", "_agent", RUNS_PREFIX.casefold(), "node_modules",
                               ".venv", "__pycache__"})
_BRANCH_BAD_CHARS = frozenset(" ~^:?*[\\")
_PLACEABLE_MODES = ("100644", "100755")
_SKIP = fs.WORKSPACE_SKIP_DIRS
_KEPT_SUFFIX = " · {k} paths kept in Git only (symlinks, submodules, files over 20 MB, node_modules/.venv/__pycache__)"
_NOTE_CAP = 300

# ── the record's remote rule (verify.ts states the same rule) ─────────

LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = rf"{LABEL}(?:\.{LABEL})*"                         # lowercase; single label allowed; no IPv6; never "-" first
SEG = r"[A-Za-z0-9_.+%][A-Za-z0-9_.+%~-]{0,99}"          # never starts "-" or "~"
USER = r"[A-Za-z_][A-Za-z0-9_.-]{0,63}"                  # never starts "-"; uppercase allowed (CodeCommit key ids)
PORT = r"(?::([1-9][0-9]{0,4}))?"
_HTTPS = re.compile(rf"https://({HOST}){PORT}((?:/{SEG}){{1,32}})")
_SSH = re.compile(rf"ssh://{USER}@({HOST}){PORT}(/~(?:{USER})?)?((?:/{SEG}){{1,32}})")
_SCP = re.compile(rf"{USER}@({HOST}):(/|~(?:{USER})?/)?({SEG}(?:/{SEG}){{0,31}})")   # user@host:path, no port
_BAD_ESCAPE = re.compile(r"%(?![0-9A-F]{2})|%(?:2E|2F|5C|[01][0-9A-F]|7F)")
_HOST_RE = re.compile(HOST)
_USER_RE = re.compile(USER)
_GITLAB_SEGMENT = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*")
_FORGE_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}

# ── messages ──────────────────────────────────────────────────────────

I1 = ("Name the repository in the runner's prompt: its address (https://host/path, ssh://user@host/path or "
      "user@host:path), or owner/name with a GitHub or GitLab account bound")
I1b = ("owner/name needs a bound GitHub or GitLab account to say which server it is on; name the repository by "
       "its address (https://host/path) instead")
I1c = ("The bound Git server account needs the repository's full address (ssh://user@host/path or user@host:path "
       "for an SSH key, https://host/path for a public repository)")
I2 = ("Repository addresses carry no user name, password, token, query or fragment; use https://host/path, "
      "ssh://git@host/path or git@host:path (credentials belong in Key manager)")
I2b = ("git over plain http, git:// or local paths is refused: the repository would arrive unauthenticated; use "
       "https or ssh")
I3 = ("A repository is owner/name (group/sub/name on GitLab), https://host/path, ssh://user@host[:port]/path or "
      "user@host:path")
I3g = "A GitHub repository is owner/name: exactly two path segments"
I4 = "The prompt names one repository; it has {n} lines"
I5 = "Folder must be one folder name of letters, digits, '.', '_' or '-' (at most 100 characters)"
I5b = "That folder name is reserved in a cell; choose another"
I5c = "The repository's name cannot be a cell folder; set Folder"
I6 = "Branch is not a valid Git branch name"
I7 = "Push to branch is not a valid Git branch name"

A1 = ("The bound Git account is for {base}, not {host}; bind an account for {host}, or unbind it to clone a public "
      "repository anonymously. Nothing changed")
A2 = "{r} is an SSH address, and SSH needs a Git account with an SSH key bound to this runner. Nothing changed"
A3 = ("The bound Git account signs in with {method}, which cannot reach {scheme} addresses; use the repository's "
      "{other} address or another account. Nothing changed")
A4 = ("No host key is known for {hostport}: paste its known_hosts line into the account's Known hosts in Key "
      "manager, checked against the fingerprints the server publishes. Nothing was sent. Nothing changed")
A5 = ("SSH remotes need Internet set to full in this runner's permissions: SSH cannot pass the run's web proxy. "
      "Nothing changed")
A6 = ("The bound Git account is public (no credentials), and pushing needs a credential: bind a Git account with a "
      "token or an SSH key that can write to {r}. Nothing changed")

E1 = "Git needs internet access; set Internet to full in this runner's permissions. Nothing changed"
E2 = "git is not installed on this machine (not on the run's PATH); install git 2.31 or later and run again"
E3 = ("git on this machine does not run (git --version failed); on macOS install the Command Line Tools "
      "(xcode-select --install)")
E4 = "git {v} is too old; Git runs need git 2.31 or later"
E5 = ("ssh is not installed on this machine (not on the run's PATH); install the OpenSSH client, or use a Git "
      "account with a token. Nothing changed")

F1 = "{f} is a file in this cell, not a repository folder; set Folder to another name. Nothing changed"
F2 = ("Folder {f}/ already exists and was not cloned by Git Clone / Update (the cell has no {f}.git-checkout.json); "
      "set Folder to another name. Nothing changed")
F3 = ("{f}.git-checkout.json is not a valid checkout record ({reason}); restore it, or rename {f}/ and clone "
      "again. Nothing changed")
F4 = "Folder {f}/ holds {other}, not {r}; set Folder to another name. Nothing changed"
F5 = "Folder {f}/ follows branch {mb}, not {b}; clear the Branch setting or use another folder. Nothing changed"
F6 = ("Folder {existing}/ already exists and differs from {f} only in case; set Folder to {existing} or "
      "another name. Nothing changed")
F7 = "Folder {f}/ has no clone of {r}; run Git Clone / Update first. Nothing pushed"
F9 = ("Folder {f}/ holds none of {r}'s files at the recorded commit {B7}, so it is not the checkout "
      "{f}.git-checkout.json records (a folder renamed or made anew?). Nothing changed; rename the record "
      "together with its folder, or rename {f}/ and clone again")
F8 = "Several folders hold {r} ({list}); set Folder to choose one. Nothing changed"

N1 = "Could not reach {host} while {step} {r}; check this machine's network. Nothing changed"
N2 = ("{r} was not found anonymously: check the address, or bind a Git account if it is private. "
      "Nothing changed")
N3 = ("{r} was not found with the selected Git account: check the address and that its {cred} can read "
      "it{hint3}. Nothing changed")
N4 = "{host} rejected the selected Git account's {cred}; replace it in Key manager. Nothing changed"
N4s = ("{host} refused the Git account's SSH key ({fp}); add its public key (Verify in Key manager shows it) to "
       "your account or the repository's deploy keys. Nothing changed")
N5 = "{host} refused access to {r} (HTTP 403): the {cred} may lack access{hint5}. Nothing changed"
N6 = "git failed while {step} {r} (exit {code}). Nothing changed"
N6v = "git reported the push to {r} {dst} but {host} still shows {A7}. Nothing changed; run Git Push again"
N7 = "{host} redirected {r}: it was probably renamed or moved; use its new address. Nothing changed"
N8 = ("The SSH host key {hostport} presented is not one the Git account trusts, so nothing was sent; check the "
      "server's published fingerprints and fix Known hosts in Key manager (for github.com or gitlab.com, update "
      "Collimate). Nothing changed")
NK = "ssh could not use the Git account's SSH key; replace it in Key manager. Nothing changed"
NT = "git timed out while {step} {r}. Nothing changed"

R2 = "{r} has paths that differ only in case ({a}, {b}); a cell cannot hold both. Nothing changed"
R3 = "{r} has no commits yet; there is nothing to clone"
R4 = "{r} has no branch {b}. Nothing changed"
R5 = "Branch {b} no longer exists in {r}. Nothing changed; rename {f}/ and clone again to start over"
R6 = ("{b} in {r} no longer contains the recorded commit {B7} (its history was rewritten). Nothing changed; "
      "to continue, rename {f}/, clone again and copy your edits across")
R7 = ("{f}/{p} cannot be synced: the repository keeps a {kind} at {k} that the cell does not hold (kept in "
      "Git only); rename your file. Nothing changed")
R8 = "{f}/{p} is not a regular file of at most 20 MB, so it cannot be synced. Nothing changed"

M1 = "Did not update {r}: {n} files conflict between your edits and the remote's {b} ({list}). Nothing changed"
M3 = ("Did not push {r}: the remote's {b} moved and {n} files conflict with your edits ({list}). Nothing was "
      "pushed or changed; push to a new branch instead (Push to branch)")

P2 = "Did not push {r}: {f}/ has no files, so the commit would delete every file. Nothing changed"
P3 = "Branch {x} already exists in {r}; pushing never overwrites a branch, so choose a new name. Nothing changed"
P4 = "Did not push {r} {dst}: the branch moved on the remote while pushing. Nothing changed; run Git Push again"
P5 = ("{host} refused the push to {r} {dst}: a protected branch, push rule or server hook. Nothing changed; push "
      "to a new branch (Push to branch)")
P5b = ("{host} refused the push to {r} {dst}. Nothing changed; push to a new branch, or check the account's write "
       "access")
P6 = "The selected Git account cannot push to {r}; give its {cred} write access{hint6}. Nothing changed"
P7a = ("Pushed {r} {dst} {P7}, but the cell refused {list} ({reason}), so {f}.git-checkout.json keeps the "
       "previous commit; run Git Clone / Update to re-sync {f}/. Your edits are on {host}")
P7b = ("Pushed {r} {dst} {P7}, but the cell could not record it ({reason}); run Git Clone / Update to "
       "re-sync {f}/. Your edits are on {host}")
P7c = ("Pushed {r} {dst} {P7}, then the run was stopped before the cell recorded it; run Git Clone / "
       "Update to re-sync {f}/. Your edits are on {host}")
P8 = ("Could not confirm whether the push of {P7} to {r} {dst} reached {host}; the cell is unchanged. Run "
      "Git Clone / Update: it brings the commit in if it landed")
C1 = "The cell changed while the Git run ran, so nothing was saved; run it again"
C2 = "Could not save {f}/ to the cell ({reason}); nothing was saved. Run it again"
C3 = ("Updated only part of {f}/: the cell refused {list} ({reason}). {f}.git-checkout.json keeps the "
      "previous commit, so the next Git Clone / Update retries them; your edits are kept")
C4 = ("Cloned only part of {r} into {f}/: the cell refused {list} ({reason}), and {f}/ has no checkout record "
      "yet; delete {f}/ before cloning again")
C5 = ("Updated {f}/ but could not record the new commit in {f}.git-checkout.json ({reason}); the next Git "
      "Clone / Update finishes it")
C6 = "Cloned {r} into {f}/ but could not write {f}.git-checkout.json ({reason}); delete {f}/ and clone again"


# ── errors ────────────────────────────────────────────────────────────


class RepositoryError(RuntimeError):
    """A refusal or failure whose ``str`` is the one-line report;
    ``details`` are extra lines for the run's log."""

    def __init__(self, message: str, *, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.details = tuple(details)


class RepositoryConflict(RepositoryError):
    """Merging the remote's commits conflicts with the folder's edits;
    ``details`` name each conflicting path ("Conflict: <remote> path")."""


class PartialSave(RepositoryError):
    """The cell refused some of a folder's files; its checkout record was not written."""

    def __init__(self, message: str, *, refused: Sequence[str] = (), details: Sequence[str] = ()) -> None:
        super().__init__(message, details=details)
        self.refused = tuple(refused)


class PushedNotRecorded(RepositoryError):
    """The push landed on the remote but the cell did not record it."""

    def __init__(self, message: str, *, pushed: str, details: Sequence[str] = ()) -> None:
        super().__init__(message, details=details)
        self.pushed = pushed


@dataclass(frozen=True)
class Target:
    remote: str                        # canonical record remote (check_remote)
    folder: str                        # validated top-level folder


@dataclass(frozen=True)
class Marker:
    """A checkout record's engine fields; ``about`` is always ABOUT."""
    remote: str
    branch: str
    commit: str

    def data(self) -> dict:
        return {"about": ABOUT, "remote": self.remote, "branch": self.branch, "commit": self.commit}

    def text(self) -> str:
        return json.dumps(self.data(), indent=2) + "\n"


def marker_path(folder: str) -> str:
    """The cell-root path of ``folder``'s checkout record."""
    return folder + MARKER_SUFFIX


# ── remotes ───────────────────────────────────────────────────────────


def check_remote(v) -> bool:
    """Whether ``v`` is a record remote: https, ``ssh://`` or scp, with no
    credential, query or fragment, a lowercase host, no default port, and
    only safe path segments (the rule ``derived/verify.ts`` states too)."""
    if not isinstance(v, str) or len(v) > REMOTE_CAP:
        return False
    m, scheme, tilde = _HTTPS.fullmatch(v), "https", None
    if m:
        host, port, path = m.groups()
    elif (m := _SSH.fullmatch(v)):
        scheme = "ssh"
        host, port, tilde, path = m.groups()
    else:
        m = _SCP.fullmatch(v)
        if not m:
            return False
        scheme, port = "scp", None
        host, lead, rest = m.groups()
        tilde = lead if lead and lead.startswith("~") else None
        path = "/" + rest
    if len(host) > 253:
        return False
    if port is not None and (int(port) > 65535 or int(port) == (443 if scheme == "https" else 22)):
        return False
    segments = path[1:].split("/")
    if len(segments) + (tilde is not None) > 32:
        return False
    if any(s in (".", "..") for s in segments):
        return False
    return not _BAD_ESCAPE.search(path)


def remote_key(remote: str) -> str:
    """What two remotes are compared by (F4, F8, resolve_target): casefolded,
    without one trailing ``.git``."""
    return remote.casefold().removesuffix(".git")


def _parts(remote: str) -> "tuple[str, str, str]":
    """``(host[:port], separator, path)`` of a record remote: ``/`` before an
    https or ssh path (the path keeps no leading ``/``), ``:`` before an scp path."""
    if remote.startswith(("https://", "ssh://")):
        rest = remote.split("://", 1)[1]
        rest = rest.split("@", 1)[1] if remote.startswith("ssh://") else rest
        hostport, _, path = rest.partition("/")
        return hostport, "/", path
    rest = remote.split("@", 1)[1]
    host, _, path = rest.partition(":")
    return host, ":", path


def display_of(remote: str) -> str:
    """``host[:port]/path`` (scp: ``host:path``) without scheme, user or one
    trailing ``.git``, cut to 120 characters."""
    hostport, sep, path = _parts(remote)
    return _cut(f"{hostport}{sep}{path}".removesuffix(".git"), 120)


def host_of(remote: str) -> str:
    """``host[:port]`` of a record remote (scp: ``host``)."""
    return _parts(remote)[0]


# ── validation ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Typed:
    """A prompt address after normalisation."""
    form: Literal["H", "X", "C"]
    user: str
    host: str
    port: "int | None"
    path: str                          # https/ssh: starts "/"; scp: as typed after ":"

    def text(self) -> str:
        if self.form == "C":
            return f"{self.user}@{self.host}:{self.path}"
        hostport = self.host if self.port is None else f"{self.host}:{self.port}"
        if self.form == "H":
            return f"https://{hostport}{self.path}"
        return f"ssh://{self.user}@{hostport}{self.path}"

    def hostport(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


def _typed(text: str) -> "_Typed | None":
    """The address in ``text`` normalised (lowercase scheme and host, no
    default port, one trailing ``/`` removed, a missing ssh user is ``git``);
    None for a short path. Refuses I2, I2b or I3 without echoing ``text``."""
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        if scheme not in ("https", "ssh"):
            raise ValueError(I2b)
        if "?" in text or "#" in text:
            raise ValueError(I2)
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            raise ValueError(I3) from None
        if scheme == "https" and "@" in parts.netloc:
            raise ValueError(I2)
        if parts.password is not None:
            raise ValueError(I2)
        user = parts.username if parts.username is not None else "git"
        if parts.username == "":
            raise ValueError(I3)
        host = (parts.hostname or "").lower()
        if port in ((443,) if scheme == "https" else (22,)):
            port = None
        path = parts.path.removesuffix("/")
        typed = _Typed("H" if scheme == "https" else "X", user, host, port, path)
    elif "::" in text:
        raise ValueError(I2b)
    elif ":" in text or "@" in text:
        at, colon = text.find("@"), text.find(":")
        if colon < 0:
            raise ValueError(I3)
        if 0 <= at and colon < at and "/" not in text[:at]:
            raise ValueError(I2)          # user:password@host:path
        if 0 <= at < colon:
            user, host, path = text[:at], text[at + 1:colon], text[colon + 1:]
        else:
            user, host, path = "git", text[:colon], text[colon + 1:]
        if "?" in path or "#" in path:
            raise ValueError(I2)
        path = path.removesuffix("/")
        typed = _Typed("C", user, host.lower(), None, path)
    else:
        if text.startswith(("/", "./", "../", "~")):
            raise ValueError(I2b)
        return None
    if (not _HOST_RE.fullmatch(typed.host) or len(typed.host) > 253 or (typed.port is not None and not
            1 <= typed.port <= 65535) or (typed.form != "H" and not _USER_RE.fullmatch(typed.user))):
        raise ValueError(I3)
    return typed


def _segments(path: str) -> list[str]:
    return path.strip("/").split("/") if path.strip("/") else []


def _forge_record(base: str, project: str, service: str) -> str:
    """The forge canonical record ``<base>/<project>.git`` (one ``.git``),
    with the github rule (two segments, I3g) or the gitlab segment rule."""
    project = project.removesuffix(".git")
    segments = project.split("/") if project else []
    if service == "github":
        if len(segments) != 2:
            raise ValueError(I3g)
    elif not (2 <= len(segments) <= 20 and all(_GITLAB_SEGMENT.fullmatch(s) for s in segments)
              and not segments[-1].endswith((".git", ".atom"))):
        raise ValueError(I3)
    record = f"{base}/{project}.git"
    if any(s in ("", ".", "..") for s in segments) or not check_remote(record):
        raise ValueError(I3)
    return record


def _trim(service: str, segments: list[str]) -> list[str]:
    """A forge page address cut to its repository: a github page keeps two
    segments, a gitlab page drops everything from the ``-`` segment on."""
    if service == "github":
        return segments[:2]
    return segments[:segments.index("-")] if "-" in segments else segments


def _web_parts(web: str) -> "tuple[str, int | None, str]":
    parts = urlsplit(web)
    return (parts.hostname or "").lower(), parts.port, parts.path.rstrip("/")


def _web_label(web: str) -> str:
    host, port, path = _web_parts(web)
    return (host if port is None else f"{host}:{port}") + path


def _under(typed: _Typed, web: str) -> "str | None":
    """The path of an https address below ``web`` (no leading ``/``), or None."""
    host, port, prefix = _web_parts(web)
    if typed.form != "H" or typed.host != host or typed.port != port:
        return None
    if not typed.path.startswith(prefix + "/"):
        return None
    return typed.path[len(prefix) + 1:]


def _https_forge(typed: _Typed, base: str, project_path: str, service: str,
                 log: Callable[[str], None]) -> str:
    segments = _segments(project_path)
    kept = _trim(service, segments)
    record = _forge_record(base, "/".join(kept), service)
    if kept != segments:
        log(f"Using {display_of(record)} (the rest of the address is a page, not the repository)")
    return record


def _ssh_project(typed: _Typed) -> str:
    """The project path of an ssh or scp address on a forge (I3 for ``~``)."""
    path = typed.path.removeprefix("/")
    if path.startswith("~"):
        raise ValueError(I3)
    return path


def _generic(typed: _Typed) -> str:
    record = typed.text()
    if not check_remote(record):
        raise ValueError(I3)
    return record


def _anonymous_https(typed: _Typed, log: Callable[[str], None]) -> str:
    """An https address with no credential: forge canonical on github.com and
    gitlab.com (page trim included), else generic."""
    service = _FORGE_HOSTS.get(typed.host) if typed.port is None else None
    if service is None:
        return _generic(typed)
    return _https_forge(typed, f"https://{typed.host}", typed.path.removeprefix("/"), service, log)


def _parse_address(text: str, account: "GitAccount | None", log: Callable[[str], None]) -> str:
    typed = _typed(text)
    if account is None:
        if typed is None:
            raise ValueError(I1b)
        if typed.form == "H":
            return _anonymous_https(typed, log)
        raise ValueError(A2.format(r=display_of(_generic(typed))))
    if account.forge:
        web = account.web
        if typed is None:
            return _forge_record(web, text.removesuffix("/"), account.service)
        host, _port, _prefix = _web_parts(web)
        if typed.form == "H":
            below = _under(typed, web)
            if below is None:
                raise ValueError(A1.format(base=_web_label(web), host=typed.hostport()))
            return _https_forge(typed, web, below, account.service, log)
        if typed.host != host or typed.port not in (None, 22):
            raise ValueError(A1.format(base=_web_label(web), host=typed.hostport()))
        return _forge_record(web, _ssh_project(typed), account.service)
    if account.auth == "basic":
        if typed is None:
            record = f"{account.web}/{text.removesuffix('/')}"
            if not check_remote(record):
                raise ValueError(I3)
            return record
        if typed.form != "H":
            raise ValueError(A3.format(method="a username and token", scheme="SSH", other="https"))
        if _under(typed, account.web) is None:
            raise ValueError(A1.format(base=_web_label(account.web), host=typed.hostport()))
        return _generic(typed)
    if typed is None:
        raise ValueError(I1c)
    if account.auth == "none":
        if typed.form != "H":
            raise ValueError(A3.format(method="no credentials", scheme="SSH", other="https"))
        return _anonymous_https(typed, log)
    # git + ssh_key
    if typed.form == "H":
        raise ValueError(A3.format(method="an SSH key", scheme="https", other="SSH"))
    record = _generic(typed)
    name = git_hosts.host_key_name(typed.host, typed.port)
    if not any(name in known.hosts for known in account.known_hosts):
        raise ValueError(A4.format(hostport=name))
    return record


def parse_prompt(prompt: "str | None", account: "GitAccount | None", *,
                 log: Callable[[str], None] = gen.log) -> str:
    """The canonical record remote of the runner's prompt (§1.7): a short
    ``owner/name`` under the bound forge or Git server, or an https, ssh or
    scp address. Every refusal (I1–I4, A1–A4) comes before any process."""
    lines = [line.strip() for line in str(prompt or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError(I1)
    if len(lines) > 1:
        raise ValueError(I4.format(n=len(lines)))
    return _parse_address(lines[0], account, log)


def check_folder(folder: str) -> str:
    if not isinstance(folder, str) or not _FOLDER.fullmatch(folder) or folder in (".", ".."):
        raise ValueError(I5)
    if folder.casefold() in _RESERVED_FOLDERS or folder.casefold().endswith(MARKER_SUFFIX):
        raise ValueError(I5b)
    return folder


def default_folder(remote: str) -> str:
    """The folder named after the remote's last path segment (one ``.git`` removed)."""
    try:
        return check_folder(_parts(remote)[2].rsplit("/", 1)[-1].removesuffix(".git"))
    except ValueError:
        raise ValueError(I5c) from None


def check_branch(name: str) -> str:
    if not _branch_ok(name):
        raise ValueError(I6)
    return name


def _utf8_size(name: str) -> int:
    try:
        return len(name.encode("utf-8"))
    except UnicodeEncodeError:          # a lone surrogate has no UTF-8 form
        return 256


def _branch_ok(name) -> bool:
    if not isinstance(name, str) or not name or _utf8_size(name) > 255:
        return False
    if any(ord(c) < 32 or ord(c) == 127 or c in _BRANCH_BAD_CHARS for c in name):
        return False
    if name.startswith(("-", "/")) or name.endswith(("/", ".")) or name in ("@", "HEAD"):
        return False
    if "//" in name or ".." in name or "@{" in name:
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


# ── workspace reading ─────────────────────────────────────────────────


def _invalid(folder: str, reason: str) -> RepositoryError:
    return RepositoryError(F3.format(f=folder, reason=reason))


def _no_constant(name: str):
    raise ValueError(name)              # NaN and Infinity are not JSON


def read_marker(workspace: Path, folder: str) -> "Marker | None":
    """The folder's checkout record (``marker_path``); ``None`` when there is
    none. An invalid record raises :class:`RepositoryError` (F3)."""
    try:
        fd = fs.open_file(str(workspace), marker_path(folder), write=False)
    except FileNotFoundError:
        return None
    except (OSError, PermissionError):
        raise _invalid(folder, "not a regular file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(folder, "not a regular file")
        if info.st_size > MARKER_CAP:
            raise _invalid(folder, "larger than 4 KB")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MARKER_CAP + 1)
    finally:
        os.close(fd)
    if len(raw) > MARKER_CAP:
        raise _invalid(folder, "larger than 4 KB")
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except ValueError:
        raise _invalid(folder, "not UTF-8 JSON") from None
    if not isinstance(data, dict) or set(data) != {"about", "remote", "branch", "commit"}:
        raise _invalid(folder, "not an object with exactly about, remote, branch and commit")
    about = data["about"]
    if not isinstance(about, str) or not 1 <= len(about) <= ABOUT_CAP:
        raise _invalid(folder, "bad about")
    remote, branch, commit = data["remote"], data["branch"], data["commit"]
    if not check_remote(remote):
        raise _invalid(folder, "bad remote")
    if not _branch_ok(branch):
        raise _invalid(folder, "bad branch")
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise _invalid(folder, "bad commit")
    return Marker(remote, branch, commit)


def _cut(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _list(paths: Sequence[str]) -> str:
    paths = list(paths)
    shown = ", ".join(_cut(p) for p in paths[:3])
    return shown + (f", +{len(paths) - 3} more" if len(paths) > 3 else "")


def resolve_target(workspace: Path, remote: str, folder: str = "",
                   *, log: Callable[[str], None] = gen.log) -> Target:
    """The folder that holds ``remote``: the Folder setting when set,
    else the one top-level folder whose checkout record records it, else one
    named after it. A record whose folder is gone is ignored."""
    if folder:
        return Target(remote, check_folder(folder))
    matches: list[str] = []
    for entry in sorted(os.scandir(workspace), key=lambda e: e.name):
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            check_folder(entry.name)
        except (OSError, ValueError):
            continue
        try:
            marker = read_marker(workspace, entry.name)
        except RepositoryError:
            log(f"Ignored folder {entry.name}/: {entry.name}{MARKER_SUFFIX} is not a valid checkout record")
            continue
        if marker is not None and remote_key(marker.remote) == remote_key(remote):
            matches.append(entry.name)
    if len(matches) > 1:
        raise RepositoryError(F8.format(r=display_of(remote), list=_list([f"{m}/" for m in matches])))
    if matches:
        return Target(remote, matches[0])
    return Target(remote, default_folder(remote))


def _folder_state(workspace: Path, target: Target, branch: str) -> "Marker | None":
    """``None`` when the folder does not exist yet (clone), else its valid
    checkout record (update or push); every other state refuses (F1–F6)."""
    exact = folded = None
    with os.scandir(workspace) as entries:
        for entry in entries:
            if entry.name == target.folder:
                exact = entry
            elif entry.name.casefold() == target.folder.casefold():
                folded = entry
    f = target.folder
    if exact is None:
        if folded is not None:
            raise RepositoryError(F6.format(existing=folded.name, f=f))
        try:
            info = os.lstat(workspace / marker_path(f))
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _invalid(f, "not a regular file")
        return None             # a left-over record: the clone deletes it with the files (phase 1)
    if not exact.is_dir(follow_symlinks=False):
        raise RepositoryError(F1.format(f=f))
    marker = read_marker(workspace, f)
    if marker is None:
        raise RepositoryError(F2.format(f=f))
    if remote_key(marker.remote) != remote_key(target.remote):
        raise RepositoryError(F4.format(f=f, other=display_of(marker.remote), r=display_of(target.remote)))
    if branch and branch != marker.branch:
        raise RepositoryError(F5.format(f=f, mb=marker.branch, b=branch))
    return marker


# ── reports ───────────────────────────────────────────────────────────


def _kept(k: int) -> str:
    return _KEPT_SUFFIX.format(k=k) if k > 0 else ""


@dataclass(frozen=True)
class SyncReport:
    kind: Literal["cloned", "updated", "current"]
    target: Target
    branch: str
    before: "str | None"
    after: str
    commits: int
    written: int
    deleted: int
    local_edits: int
    kept_in_git: int

    def note(self) -> str:
        r, f, b = display_of(self.target.remote), self.target.folder, self.branch
        if self.kind == "cloned":
            return f"Cloned {r} {b}@{self.after[:7]} into {f}/: {self.written} files" + _kept(self.kept_in_git)
        if self.kind == "current":
            return f"Checked {r} {b}: {f}/ is up to date at {self.after[:7]}"
        edits = (f" · kept your edits in {self.local_edits} files" if self.local_edits else " · no local edits")
        return (f"Updated {f}/ from {r} {b} {(self.before or '')[:7]}→{self.after[:7]} ({self.commits} new "
                f"commits): {self.written} files written, {self.deleted} deleted" + edits
                + _kept(self.kept_in_git))


@dataclass(frozen=True)
class PushReport:
    kind: Literal["pushed", "merged", "new_branch", "nothing", "absorbed"]
    target: Target
    branch: str
    pushed_to: "str | None"
    base: str
    remote_head: str
    recorded: str
    pushed: "str | None"
    upstream_commits: int
    local_edits: int
    written: int
    deleted: int
    compare_url: "str | None"

    def note(self) -> str:
        r, f, b = display_of(self.target.remote), self.target.folder, self.branch
        p7 = (self.pushed or "")[:7]
        if self.kind == "pushed":
            return f"Pushed {f}/ to {r} {self.pushed_to} {self.base[:7]}→{p7}: {self.local_edits} files changed"
        if self.kind == "merged":
            return (f"Pushed {f}/ to {r} {self.pushed_to} at {p7} after merging {self.upstream_commits} new "
                    f"commits from the remote: {self.local_edits} files of yours, {self.written + self.deleted} "
                    f"files from the remote written to {f}/")
        if self.kind == "new_branch":
            x = self.pushed_to
            note = f"Pushed {f}/ to new branch {x} of {r} at {p7}: {self.local_edits} files changed; {f}/ now follows {x}"
            if not self.compare_url:
                return note
            linked = f"{note} · open a merge or pull request: {self.compare_url}"
            return linked if len(linked) <= _NOTE_CAP else note
        if self.kind == "nothing":
            note = f"Nothing to push: {f}/ matches {r} {b} at {self.base[:7]}"
            if self.upstream_commits:
                note += (f" · the remote's {b} has {self.upstream_commits} newer commits; run Git Clone / Update "
                         "to bring them in")
            return note
        return (f"Nothing new to push: the remote's {b} at {self.remote_head[:7]} already has your edits; {f}/ "
                f"now records it ({self.written} files from the remote written)")


async def _commit() -> None:
    """The commit seam: the engine's only flush."""
    await gen.commit()


# ── git helpers ───────────────────────────────────────────────────────

_git_version: "str | None" = None


async def _preflight(log: Callable[[str], None]) -> None:
    global _git_version
    if _git_version is None:
        try:
            version = await git_run.git_version()
        except GitFailure as exc:
            if exc.kind == "missing_git":
                raise RepositoryError(E2) from None
            if exc.kind == "old_git":
                raise RepositoryError(E4.format(v=exc.version)) from None
            raise RepositoryError(E3) from None
        _git_version = version
    log(f"git {_git_version}")


@dataclass(frozen=True)
class _Credential:
    cred: str
    hint3: str = ""
    hint5: str = ""
    hint6: str = ""


def _credential(account: "GitAccount | None") -> "_Credential | None":
    """How messages name the account's credential; None when nothing is sent."""
    if account is None or account.auth == "none":
        return None
    if account.auth == "ssh_key":
        return _Credential("SSH key", " (a deploy key belongs to one repository)", "",
                           " (a deploy key needs write access)")
    if account.auth == "basic":
        return _Credential("username and token")
    if account.service == "github":
        return _Credential("token", " (a fine-grained token must list the repository)",
                           ", or the organisation requires SSO authorisation for it", " to the repository's contents")
    return _Credential("token", " (read_repository, or write_repository to push)", "",
                       " (write_repository, and the Developer role or above)")


def _fingerprint(account: GitAccount) -> str:
    try:
        return git_hosts.inspect_private_key(account.secret).fingerprint
    except ValueError:
        return "unreadable key"


def _explain(failure: GitFailure, target: Target, *, account: "GitAccount | None", step: str,
             remote: "Remote | None" = None) -> RepositoryError:
    """The one-line report of a git failure. Nothing is retried without the
    credential: a rejected account is reported, never downgraded."""
    r, host, kind = display_of(target.remote), host_of(target.remote), failure.kind
    cred = _credential(account)
    if kind == "network":
        message = N1.format(host=host, step=step, r=r)
    elif kind == "timeout":
        message = NT.format(step=step, r=r)
    elif kind == "redirect":
        message = N7.format(host=host, r=r)
    elif kind == "missing_git":
        message = E2
    elif kind == "missing_ssh":
        message = E5
    elif kind == "host_key":
        message = N8.format(hostport=remote.known_host if remote and remote.known_host else host)
    elif kind == "key":
        message = NK
    elif cred is None and kind in ("forbidden", "auth", "not_found"):
        message = N2.format(r=r)
    elif kind == "forbidden":
        message = (P6.format(r=r, cred=cred.cred, hint6=cred.hint6) if step == "pushing"
                   else N5.format(host=host, r=r, cred=cred.cred, hint5=cred.hint5))
    elif kind == "auth":
        message = (N4s.format(host=host, fp=_fingerprint(account)) if account.auth == "ssh_key"
                   else N4.format(host=host, cred=cred.cred))
    elif kind == "not_found":
        message = N3.format(r=r, cred=cred.cred, hint3=cred.hint3)
    else:
        message = N6.format(step=step, r=r, code=failure.code)
    return RepositoryError(message)


def _resolve(target: Target, account: "GitAccount | None") -> "tuple[Remote, git_run.HttpsAuth | git_run.SshAuth | None]":
    """The record's production address and the account's credential for it;
    A1–A4 (RepositoryError) before any process when the account cannot serve it."""
    remote = git_run.resolve_remote(target.remote, account)
    if account is None:
        if remote.protocols == ("ssh",):
            raise RepositoryError(A2.format(r=display_of(target.remote)))
        return remote, None
    try:
        return remote, account.auth_for(remote)
    except RemoteRefused as exc:
        f = exc.fields
        if exc.code == "scope":
            message = A1.format(base=f.get("base", ""), host=f.get("host", ""))
        elif exc.code == "transport":
            message = A3.format(method=f["method"], scheme=f["scheme"], other=f["other"])
        else:
            message = A4.format(hostport=f.get("hostport", ""))
        raise RepositoryError(message) from None


def _check_transport(target: Target, account: "GitAccount | None") -> None:
    """A5, then A1–A4: every address refusal before any git process (push adds A6)."""
    if (git_run.resolve_remote(target.remote, account).protocols == ("ssh",)
            and gen.grant().internet != "full"):
        raise RepositoryError(A5)
    _resolve(target, account)


class _Session:
    """One target's git work: its ``Git``, temporary checkout and messages."""

    def __init__(self, target: Target, account: "GitAccount | None", home: Path,
                 identity: "Identity | None" = None) -> None:
        self.target = target
        self.account = account
        self.home = home
        self.dir = home / "checkout"
        remote, auth = _resolve(target, account)
        self.remote = remote                     # the production address, for messages
        self.git = Git(git_run.transport(remote, target.remote), auth, home, identity)

    def error(self, failure: GitFailure, step: str) -> RepositoryError:
        return _explain(failure, self.target, account=self.account, step=step, remote=self.remote)

    def broken(self, step: str, code: int = 0) -> RepositoryError:
        return RepositoryError(N6.format(step=step, r=display_of(self.target.remote), code=code))

    async def net(self, args: Sequence[str], step: str, *, cwd: "Path | None" = None,
                  ok: tuple[int, ...] = (0,)) -> git_run.Completed:
        try:
            return await self.git.run(args, cwd or self.dir, network=True, ok=ok)
        except GitFailure as exc:
            raise self.error(exc, step) from None

    async def local(self, args: Sequence[str], step: str, *, ok: tuple[int, ...] = (0,),
                    input: "bytes | None" = None, no_lazy_fetch: bool = False) -> git_run.Completed:
        try:
            return await self.git.run(args, self.dir, ok=ok, input=input, no_lazy_fetch=no_lazy_fetch)
        except GitFailure as exc:
            if exc.kind in ("missing_git", "timeout"):
                raise self.error(exc, step) from None
            raise self.broken(step, exc.code) from None

    async def sha(self, rev: str, step: str) -> str:
        out = (await self.local(["rev-parse", "--verify", "--end-of-options", rev], step)).stdout
        value = out.decode("ascii", "replace").strip()
        if not _SHA.fullmatch(value):
            raise self.broken(step)
        return value

    async def count(self, base: str, head: str) -> int:
        if base == head:
            return 0
        out = (await self.local(["rev-list", "--count", f"{base}..{head}"], "reading")).stdout
        try:
            return int(out.decode("ascii").strip())
        except ValueError:
            raise self.broken("reading") from None

    def paths(self, out: bytes, step: str = "reading") -> list[str]:
        """``-z`` path output, each checked as a relative POSIX path."""
        return [self.check_path(p, step) for p in _records(out)]

    def check_path(self, raw: bytes, step: str = "reading") -> str:
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            path = raw.decode("utf-8", "surrogateescape")
        parts = path.split("/")
        if (not path or path.startswith("/") or "\0" in path
                or any(part in ("", ".", "..") for part in parts)):
            raise self.broken(step)
        return path

    # -- remote --------------------------------------------------------

    async def ls_remote_raw(self, ref: str) -> "str | None":
        """The sha of ``refs/heads/<ref>`` on the remote, or None; GitFailure
        propagates unexplained."""
        out = (await self.git.run(["ls-remote", self.git.remote.url, f"refs/heads/{ref}"], self.home,
                                  network=True)).stdout
        for line in out.decode("utf-8", "replace").splitlines():
            sha, _, name = line.partition("\t")
            if name == f"refs/heads/{ref}":
                if not _SHA.fullmatch(sha):
                    raise self.broken("checking")
                return sha
        return None

    async def ls_remote_branch(self, ref: str) -> "str | None":
        try:
            return await self.ls_remote_raw(ref)
        except GitFailure as exc:
            raise self.error(exc, "checking") from None

    async def ls_remote_default(self) -> tuple[str, str]:
        out = (await self.net(["ls-remote", "--symref", self.git.remote.url, "HEAD"], "checking",
                              cwd=self.home)).stdout
        branch = sha = None
        for line in out.decode("utf-8", "replace").splitlines():
            left, _, name = line.partition("\t")
            if name != "HEAD":
                continue
            if left.startswith("ref: refs/heads/"):
                branch = left[len("ref: refs/heads/"):]
            elif _SHA.fullmatch(left):
                sha = left
        if sha is None:
            raise RepositoryError(R3.format(r=display_of(self.target.remote)))
        if branch is None or not _branch_ok(branch):
            raise self.broken("checking")
        return branch, sha

    async def clone(self, branch: str) -> str:
        await self.net(["clone", "--quiet", "--filter=blob:none", "--single-branch", "--branch", branch,
                        "--no-tags", "--no-checkout", "--no-recurse-submodules", "--",
                        self.git.remote.url, str(self.dir)], "cloning", cwd=self.home)
        return await self.sha(f"refs/remotes/origin/{branch}^{{commit}}", "cloning")

    async def checkout(self, rev: str) -> None:
        await self.net(["checkout", "--quiet", "--detach", rev, "--"], "checking out")

    async def contains(self, base: str, head: str) -> bool:
        result = await self.local(["merge-base", "--is-ancestor", base, head], "checking",
                                  ok=(0, 1, 128), no_lazy_fetch=True)
        return result.code == 0

    # -- trees ---------------------------------------------------------

    async def index_modes(self) -> dict[str, str]:
        out = (await self.local(["ls-files", "-s", "-z"], "reading")).stdout
        modes: dict[str, str] = {}
        for record in _records(out):
            meta, _, raw = record.partition(b"\t")
            modes[self.check_path(raw)] = meta.split(b" ", 1)[0].decode("ascii", "replace")
        return modes

    async def tree_modes(self, rev: str) -> dict[str, str]:
        out = (await self.local(["ls-tree", "-r", "-z", rev], "reading")).stdout
        modes: dict[str, str] = {}
        for record in _records(out):
            meta, _, raw = record.partition(b"\t")
            modes[self.check_path(raw)] = meta.split(b" ", 1)[0].decode("ascii", "replace")
        return modes

    async def checked_out(self) -> "_Entries":
        """The checkout's index split into placeable paths and paths kept in Git."""
        modes = await self.index_modes()
        return await asyncio.to_thread(_split, self.dir, modes)


def _records(out: bytes) -> list[bytes]:
    return [r for r in out.split(b"\0") if r]


@dataclass
class _Entries:
    placeable: set[str]
    kept: dict[str, str]          # path -> kind


def _split(checkout: Path, modes: dict[str, str]) -> _Entries:
    placeable: set[str] = set()
    kept: dict[str, str] = {}
    for path, mode in modes.items():
        if set(path.split("/")) & _SKIP:
            kept[path] = "folder that cells never keep"
        elif mode == "120000":
            kept[path] = "symlink"
        elif mode == "160000":
            kept[path] = "submodule"
        elif mode in _PLACEABLE_MODES:
            try:
                info = os.lstat(checkout / path)
            except OSError:
                kept[path] = "file over 20 MB"
                continue
            if stat.S_ISLNK(info.st_mode):
                kept[path] = "symlink"
            elif not stat.S_ISREG(info.st_mode) or info.st_size > FILE_CAP:
                kept[path] = "file over 20 MB"
            else:
                placeable.add(path)
        else:
            kept[path] = "file over 20 MB"
    return _Entries(placeable, kept)


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def _case_clash(*groups: "set[str] | Sequence[str]") -> "tuple[str, str] | None":
    """Two paths (or folders on their way) of the joined groups that differ only in case."""
    seen: dict[str, str] = {}
    for group in groups:
        for path in sorted(group):
            for item in [*_ancestors(path), path]:
                key = item.casefold()
                other = seen.setdefault(key, item)
                if other != item:
                    return other, item
    return None


def _refuse_case(target: Target, *groups) -> None:
    clash = _case_clash(*groups)
    if clash:
        raise RepositoryError(R2.format(r=display_of(target.remote), a=_cut(clash[0]), b=_cut(clash[1])))


# ── the cell side ─────────────────────────────────────────────────────


def _cell_files(workspace: Path, target: Target, log: Callable[[str], None]) -> set[str]:
    """Every file of the folder (relative to it), skipped folders aside; a symlink, special file or file over the cap refuses (R8)."""
    root = workspace / target.folder
    files: set[str] = set()
    skipped = 0
    stack = [""]
    while stack:
        rel = stack.pop()
        with os.scandir(root / rel if rel else root) as entries:
            for entry in entries:
                path = f"{rel}/{entry.name}" if rel else entry.name
                if entry.name in _SKIP or entry.name.casefold() == ".git":
                    # git never tracks these names; a cell never syncs them
                    skipped += _count_files(Path(entry.path)) if entry.is_dir(follow_symlinks=False) else 1
                    continue
                if entry.is_symlink():
                    raise RepositoryError(R8.format(f=target.folder, p=_cut(path)))
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_size > FILE_CAP:
                        raise RepositoryError(R8.format(f=target.folder, p=_cut(path)))
                    files.add(path)
                else:
                    raise RepositoryError(R8.format(f=target.folder, p=_cut(path)))
    if skipped:
        log(f"{skipped} files under node_modules/.venv/__pycache__ in {target.folder}/ are never synced")
    return files


def _count_files(root: Path) -> int:
    return sum(len(names) for _d, _s, names in os.walk(root, followlinks=False))


def _refuse_kept_clash(target: Target, cell: set[str], kept: dict[str, str]) -> None:
    """R7: a cell file at, under or over a path the repository keeps in Git only."""
    under: dict[str, str] = {}
    for path in kept:
        for ancestor in _ancestors(path):
            under.setdefault(ancestor, path)
    for path in sorted(cell):
        hit = path if path in kept else next((a for a in _ancestors(path) if a in kept), None)
        if hit is None and path in under:
            hit = under[path]
        if hit is not None:
            raise RepositoryError(R7.format(f=target.folder, p=_cut(path), kind=kept[hit], k=_cut(hit)))


@dataclass
class _Overlay:
    local: str                     # L: B plus the folder's edits
    edits: int
    ignored: list[str]             # untracked, ignored cell files (stay in the cell, never pushed)
    cell: set[str]
    before: _Entries               # the entries of B


def _write_cell_into_checkout(workspace: Path, target: Target, checkout: Path,
                              before: _Entries, cell: set[str]) -> None:
    """Steps 5 and 6 of the overlay: the checkout's placeable files become
    exactly the cell's files (kept paths untouched), never following a link."""
    removed = sorted(before.placeable - cell)
    for path in removed:
        fs.unlink(str(checkout), path)
    for directory in sorted({a for p in removed for a in _ancestors(p)}, key=lambda d: -d.count("/")):
        try:
            os.rmdir(checkout / directory)
        except OSError:
            pass
    for path in sorted(cell):
        occupied = checkout / path
        try:
            if stat.S_ISDIR(os.lstat(occupied).st_mode):
                os.rmdir(occupied)
        except FileNotFoundError:
            pass
        except OSError:
            raise RepositoryError(R7.format(f=target.folder, p=_cut(path), kind="folder", k=_cut(path))) from None
        try:
            fd = fs.open_file(str(workspace), f"{target.folder}/{path}", write=False)
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(FILE_CAP + 1)
        except OSError:            # replaced by a link or special file, or gone, since the walk
            raise RepositoryError(R8.format(f=target.folder, p=_cut(path))) from None
        if len(data) > FILE_CAP:
            raise RepositoryError(R8.format(f=target.folder, p=_cut(path)))
        try:
            fs.write_file(str(checkout), path, data)
        except OSError:
            hit = next((a for a in _ancestors(path) if os.path.islink(checkout / a)), path)
            raise RepositoryError(R7.format(f=target.folder, p=_cut(path), kind="symlink", k=_cut(hit))) from None


def _message_for(paths: list[str], folder: str) -> str:
    if len(paths) == 1:
        subject = f"Update {paths[0]}"
    elif len(paths) <= 3:
        subject = "Update " + ", ".join(paths[:-1]) + " and " + paths[-1]
    else:
        subject = ""
    if not subject or len(subject) > 72:
        subject = f"Update {len(paths)} files in {folder}"
    return subject


async def _overlay(workspace: Path, session: _Session, base: str, *, message: "str | None",
                   log: Callable[[str], None]) -> _Overlay:
    """Commit the folder's edits on B in the checkout: L."""
    target = session.target
    await session.checkout(base)
    before = await session.checked_out()
    cell = await asyncio.to_thread(_cell_files, workspace, target, log)
    if cell and before.placeable and not cell & before.placeable:
        # The record stays at the root when its folder is renamed or deleted;
        # a new folder of that name is not the checkout it records.
        raise RepositoryError(F9.format(f=target.folder, r=display_of(target.remote), B7=base[:7]))
    _refuse_kept_clash(target, cell, before.kept)
    _refuse_case(target, cell)
    _refuse_case(target, before.placeable | set(before.kept))
    _refuse_case(target, cell | set(before.kept))
    await asyncio.to_thread(_write_cell_into_checkout, workspace, target, session.dir, before, cell)
    await session.local(["add", "-A", "--", "."], "committing")
    staged = await session.local(["diff", "--cached", "--quiet"], "committing", ok=(0, 1))
    local = base
    if staged.code == 1:
        if message is None:
            names = session.paths((await session.local(["diff", "--cached", "--name-only", "-z"],
                                                       "committing")).stdout)
            message = _message_for(names, target.folder)
        await session.local(["commit", "--quiet", "--no-verify", "-F", "-"], "committing",
                            input=message.encode("utf-8"))
        local = await session.sha("HEAD", "committing")
    edits = 0
    if local != base:
        edits = len(session.paths((await session.local(["diff", "--name-only", "-z", base, local],
                                                       "reading")).stdout))
    ignored = session.paths((await session.local(["ls-files", "-o", "-i", "--exclude-standard", "-z"],
                                                 "reading")).stdout)
    # Ignored files are never committed; out of the checkout, a merge never trips on them.
    for path in ignored:
        fs.unlink(str(session.dir), path)
    return _Overlay(local, edits, ignored, cell, before)


async def _incoming_clashes(session: _Session, incoming: str, overlay: _Overlay) -> list[str]:
    """Conflicts between the cell's ignored files and what ``incoming``
    tracks; R7 for a cell file under a path ``incoming`` keeps in Git only."""
    modes = await session.tree_modes(incoming)
    paths = {p for p in modes if not set(p.split("/")) & _SKIP}
    folders = {a for p in paths for a in _ancestors(p)}
    conflicts = []
    for q in overlay.ignored:
        if q in paths or q in folders or any(a in paths for a in _ancestors(q)):
            conflicts.append(f"{q} (an ignored file in the cell; the remote now tracks it)")
    kept = {p: ("symlink" if m == "120000" else "submodule") for p, m in modes.items()
            if m in ("120000", "160000")}
    for path in sorted(overlay.cell):
        hit = next((a for a in _ancestors(path) if a in kept), None)
        if hit is not None:
            raise RepositoryError(R7.format(f=session.target.folder, p=_cut(path), kind=kept[hit], k=_cut(hit)))
    return conflicts


async def _merge(session: _Session, base: str, overlay: _Overlay, remote: str,
                 branch: str) -> "tuple[str | None, list[str]]":
    clashes = await _incoming_clashes(session, remote, overlay)
    conflicts: list[str] = []
    if overlay.local == base:
        await session.checkout(remote)
        merged: "str | None" = remote
    else:
        result = await session.net(["merge", "--no-ff", "--no-edit", "--no-verify", "-m",
                                    f"Merge {branch} of {display_of(session.target.remote)}", remote],
                                   "merging", ok=(0, 1))
        if result.code == 0:
            merged = await session.sha("HEAD", "merging")
        else:
            unmerged = session.paths((await session.local(["diff", "--name-only", "--diff-filter=U", "-z"],
                                                          "merging")).stdout)
            if not unmerged:
                raise session.broken("merging", result.code)
            conflicts = sorted(set(unmerged))
            merged = None
    conflicts += clashes
    return (None if conflicts else merged), conflicts


@dataclass
class _Plan:
    writes: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    kept: int = 0


async def _writeback(session: _Session, local: str, merged: str, remote: str, cell: set[str],
                     log: Callable[[str], None]) -> _Plan:
    """What the cell must change to go from L to M. A path M cannot place
    leaves the cell only when M holds the remote's own version of it (R's); one
    the merge made unplaceable (an edit pushed it over the cap) refuses (R8),
    since the cell would lose the edit and the recorded commit could not
    account for the missing file."""
    target = session.target
    out = (await session.local(["diff", "--name-status", "-z", "--no-renames", local, merged], "reading")).stdout
    records = _records(out)
    after = await session.checked_out()
    _refuse_case(target, after.placeable | set(after.kept))
    plan = _Plan(kept=len(after.kept))
    unplaceable: list[str] = []
    for i in range(0, len(records) - 1, 2):
        status = records[i].decode("ascii", "replace")
        path = session.check_path(records[i + 1])
        if path in after.placeable:
            plan.writes.append(path)
        elif status.startswith("D"):
            if path in cell:
                plan.deletes.append(path)
        else:
            unplaceable.append(path)
    if unplaceable:
        merged_away = set() if merged == remote else set(session.paths((await session.local(
            ["diff", "--name-only", "-z", "--no-renames", remote, merged], "reading")).stdout))
        for path in unplaceable:
            if path in merged_away:
                raise RepositoryError(R8.format(f=target.folder, p=_cut(path)))
            if path in cell and not set(path.split("/")) & _SKIP:
                plan.deletes.append(path)
                log(f"{target.folder}/{path} became a {after.kept.get(path, 'file over 20 MB')} in the repository "
                    "and is now kept in Git only")
    for path in plan.writes:
        info = os.lstat(session.dir / path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_CAP:
            raise session.broken("reading")
    return plan


# ── placement and the two-phase commit ────────────────────────────────


@dataclass
class _Change:
    """One folder's cell change: a clone, or a write-back plan; then its checkout record."""
    kind: Literal["clone", "update"]
    target: Target
    checkout: Path
    marker: Marker
    placeable: list[str] = field(default_factory=list)
    plan: _Plan = field(default_factory=_Plan)

    def staged(self) -> bool:
        return bool(self.placeable or self.plan.writes or self.plan.deletes)


@dataclass(frozen=True)
class _Pushed:
    remote: str
    branch: str
    sha: str


def _apply(workspace: Path, changes: Sequence[_Change]) -> None:
    """Place every clone and apply every write-back (one worker thread)."""
    for change in changes:
        folder = change.target.folder
        if change.kind == "clone":
            try:
                os.mkdir(workspace / folder)
            except FileExistsError:
                raise RepositoryError(F2.format(f=folder)) from None
            if os.path.lexists(workspace / marker_path(folder)):
                # A left-over record leaves in the files' flush: a partial,
                # stopped or unrecorded clone then leaves a folder without a
                # record (F2), never one beside the old record's commit.
                gen.delete_file(marker_path(folder))
            for path in sorted(change.placeable):
                destination = workspace / folder / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(change.checkout / path), str(destination))
                gen.track_file(f"{folder}/{path}", max_bytes=FILE_CAP)
            continue
        for path in change.plan.deletes:
            gen.delete_file(f"{folder}/{path}")
        for directory in sorted({a for p in change.plan.deletes for a in _ancestors(p)},
                                key=lambda d: -d.count("/")):
            try:
                os.rmdir(workspace / folder / directory)
            except OSError:
                pass
        for path in change.plan.writes:
            fd = fs.open_file(str(change.checkout), path, write=False)
            with os.fdopen(fd, "rb") as fh:
                info = os.fstat(fh.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_CAP:
                    raise OSError(f"not a regular file of at most 20 MB: {path}")
                data = fh.read()
            gen.update_file(f"{folder}/{path}", data)


async def _join(task: "asyncio.Future") -> None:
    """Await ``task`` to its end even through cancellation; re-raise a cancel after it."""
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        if not task.cancelled():
            task.exception()      # retrieved: the cancel wins
        raise cancelled
    task.result()


def _folders(changes: Sequence[_Change]) -> str:
    return "/, ".join(c.target.folder for c in changes)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, gen.PartialRefusal):
        first = next(iter(exc.errors.values()), "")
        return _cut(first, 80) if first else "refused by the cell"
    return type(exc).__name__


def _stopped(pushed: "_Pushed | None", target: Target) -> None:
    if pushed is not None:
        gen.finish(note=P7c.format(r=display_of(pushed.remote), dst=pushed.branch, P7=pushed.sha[:7],
                                   f=target.folder, host=host_of(pushed.remote)), outcome="fail")


def _markers_landed(stop: BaseException, changes: Sequence[_Change]) -> bool:
    """Whether the record flush a Stop interrupted committed every record:
    ``gen.commit`` waits for a flush already on its way and marks the cancel
    with whether it landed, and with the partial refusal it met."""
    if not getattr(stop, "_flush_landed", False):
        return False
    refusal = getattr(stop, "_flush_refusal", None)
    if refusal is None:
        return True
    return bool(refusal.paths) and not any(marker_path(c.target.folder) in refusal.paths for c in changes)


def _not_recorded(pushed: _Pushed, target: Target, reason: str) -> PushedNotRecorded:
    return PushedNotRecorded(P7b.format(r=display_of(pushed.remote), dst=pushed.branch, P7=pushed.sha[:7],
                                        reason=reason, f=target.folder, host=host_of(pushed.remote)),
                             pushed=pushed.sha)


def _owns(change: _Change, path: str) -> bool:
    return path.startswith(change.target.folder + "/")


async def _save(workspace: Path, changes: Sequence[_Change], *, log: Callable[[str], None],
                pushed: "_Pushed | None" = None) -> None:
    """Apply ``changes`` off the loop, commit the files (phase 1), then the
    checkout records of the folders whose files all landed (phase 2)."""
    if not changes:
        return
    first = changes[0].target
    task = asyncio.ensure_future(asyncio.to_thread(_apply, workspace, list(changes)))
    try:
        await _join(task)
    except asyncio.CancelledError:
        _stopped(pushed, first)
        raise
    except RepositoryError:
        if pushed is not None:
            raise _not_recorded(pushed, first, "the folder changed while pushing") from None
        raise
    except Exception as exc:
        if pushed is not None:
            raise _not_recorded(pushed, first, type(exc).__name__) from None
        raise RepositoryError(C2.format(f=_folders(changes), reason=type(exc).__name__)) from None

    remaining = list(changes)
    partial: "RepositoryError | None" = None
    if any(c.staged() for c in changes):
        try:
            await _commit()
        except gen.PartialRefusal as exc:
            refused = sorted(exc.paths)
            if refused:
                ours = [p for p in refused if any(_owns(c, p) for c in changes)]
                affected = [c for c in changes if any(_owns(c, p) for p in ours)]
                if not ours:
                    log(f"The cell refused files outside the repository folders: {_list(refused)}")
            else:
                ours, affected = [], list(changes)
            if affected:
                remaining = [c for c in changes if c not in affected]
                partial = _partial(affected, ours, _reason(exc), pushed)
        except gen.FileConflictError:
            if pushed is not None:
                raise _not_recorded(pushed, first, "the cell changed while pushing") from None
            raise RepositoryError(C1) from None
        except (gen.Cancelled, asyncio.CancelledError):
            _stopped(pushed, first)
            raise
        except Exception as exc:
            if pushed is not None:
                raise _not_recorded(pushed, first, type(exc).__name__) from None
            raise RepositoryError(C2.format(f=_folders(changes), reason=type(exc).__name__)) from None
    try:
        await _record(remaining, pushed=pushed)
    except RepositoryError as exc:
        if partial is None:
            raise
        partial = PartialSave(f"{partial}; {exc}", refused=getattr(partial, "refused", ()))
    if partial is not None:
        raise partial


def _partial(affected: Sequence[_Change], ours: Sequence[str], reason: str,
             pushed: "_Pushed | None") -> RepositoryError:
    messages = []
    refused: list[str] = []
    for change in affected:
        f = change.target.folder
        mine = [p for p in ours if _owns(change, p)]
        refused += mine
        shown = _list(mine) if mine else f"{f}/"
        if pushed is not None:
            return PushedNotRecorded(P7a.format(r=display_of(pushed.remote), dst=pushed.branch, P7=pushed.sha[:7],
                                                list=shown, reason=reason, f=f, host=host_of(pushed.remote)),
                                     pushed=pushed.sha)
        template = C4 if change.kind == "clone" else C3
        messages.append(template.format(r=display_of(change.target.remote), f=f, list=shown, reason=reason))
    return PartialSave("; ".join(messages), refused=refused)


def _raced_only(exc: "gen.PartialRefusal", paths: set[str]) -> bool:
    """Whether every refusal is a record the verifier met mid-change of the cell."""
    return bool(exc.paths) and exc.paths <= paths and all(
        exc.errors.get(p) == _VERIFY_RACED for p in exc.paths)


async def _record(changes: Sequence[_Change], *, pushed: "_Pushed | None") -> None:
    """Phase 2: stage and commit the checkout records. A verification race
    (the cell changed while the record was checked) is retried up to
    _RECORD_ATTEMPTS commits in all."""
    pending = list(changes)
    failure: "BaseException | None" = None
    for attempt in range(_RECORD_ATTEMPTS):
        if not pending:
            return
        for change in pending:
            gen.update_file(marker_path(change.target.folder), change.marker.text())
        try:
            await _commit()
            return
        except (gen.Cancelled, asyncio.CancelledError) as exc:
            if not _markers_landed(exc, pending):
                _stopped(pushed, pending[0].target)
            raise
        except gen.PartialRefusal as exc:
            failure = exc
            if attempt + 1 < _RECORD_ATTEMPTS and _raced_only(
                    exc, {marker_path(c.target.folder) for c in pending}):
                pending = [c for c in pending if marker_path(c.target.folder) in exc.paths]
                continue
        except Exception as exc:
            failure = exc
        break
    failed = list(pending)
    if isinstance(failure, gen.PartialRefusal) and failure.paths:
        failed = [c for c in pending if marker_path(c.target.folder) in failure.paths] or failed
    reason = _reason(failure)
    if pushed is not None:
        raise _not_recorded(pushed, failed[0].target, reason) from None
    raise RepositoryError("; ".join(
        (C6 if c.kind == "clone" else C5).format(r=display_of(c.target.remote), f=c.target.folder, reason=reason)
        for c in failed)) from None


# ── sync ──────────────────────────────────────────────────────────────


@dataclass
class _Prepared:
    kind: Literal["cloned", "updated", "current"]
    branch: str
    before: "str | None"
    after: str
    change: "_Change | None" = None
    commits: int = 0
    local_edits: int = 0
    kept: int = 0


async def sync(target: Target, *, account: "GitAccount | None", branch: str = "",
               log: Callable[[str], None] = gen.log) -> SyncReport:
    """Clone the target's folder, or merge the remote's new commits into it.
    Every refusal and conflict comes before the cell changes. A merge commit
    made here is by the account's commit author when one is bound."""
    workspace = Path(gen.workspace_path())
    marker = await asyncio.to_thread(_folder_state, workspace, target, branch)
    if gen.grant().internet == "none":
        raise RepositoryError(E1)
    _check_transport(target, account)
    await _preflight(log)
    git_run.sweep_stale_sessions()
    with tempfile.TemporaryDirectory(prefix=f"collimate-git-{os.getpid()}-") as tmp:
        session = _Session(target, account, Path(tmp), identity=account.identity if account else None)
        if marker is None:
            prepared = await _prepare_clone(session, branch, log)
        else:
            result = await _prepare_update(workspace, session, marker, log)
            if isinstance(result, list):
                paths = sorted(result)
                r = display_of(target.remote)
                raise RepositoryConflict(
                    M1.format(r=r, n=len(paths), b=marker.branch, list=_list(paths)),
                    details=[f"Conflict: {r} {p}" for p in paths])
            prepared = result
        if prepared.change is not None:
            await _save(workspace, [prepared.change], log=log)
    change = prepared.change
    report = SyncReport(prepared.kind, target, prepared.branch, prepared.before, prepared.after,
                        prepared.commits,
                        len(change.placeable) + len(change.plan.writes) if change else 0,
                        len(change.plan.deletes) if change else 0, prepared.local_edits, prepared.kept)
    log(report.note())
    return report


async def _prepare_clone(session: _Session, branch: str, log: Callable[[str], None]) -> _Prepared:
    target = session.target
    r = display_of(target.remote)
    log(f"Checking {r}")
    if branch:
        if await session.ls_remote_branch(branch) is None:
            raise RepositoryError(R4.format(r=r, b=branch))
        name = branch
    else:
        name, _sha = await session.ls_remote_default()
    log(f"Cloning {r} {name} into {target.folder}/")
    head = await session.clone(name)
    await session.checkout(head)
    entries = await session.checked_out()
    _refuse_case(target, entries.placeable | set(entries.kept))
    change = _Change("clone", target, session.dir, Marker(target.remote, name, head),
                     placeable=sorted(entries.placeable))
    return _Prepared("cloned", name, None, head, change, kept=len(entries.kept))


async def _prepare_update(workspace: Path, session: _Session, marker: Marker,
                          log: Callable[[str], None]) -> "_Prepared | list[str]":
    target = session.target
    branch, base = marker.branch, marker.commit
    r = display_of(target.remote)
    log(f"Checking {r} {branch} (recorded {base[:7]})")
    remote = await session.ls_remote_branch(branch)
    if remote is None:
        raise RepositoryError(R5.format(b=branch, r=r, f=target.folder))
    if remote == base:
        return _Prepared("current", branch, base, base)
    remote = await session.clone(branch)
    if remote == base:
        return _Prepared("current", branch, base, base)
    if not await session.contains(base, remote):
        raise RepositoryError(R6.format(b=branch, r=r, B7=base[:7], f=target.folder))
    overlay = await _overlay(workspace, session, base, message="Edits in the cell", log=log)
    log(f"Merging {r} {branch} {base[:7]}→{remote[:7]} with your edits in {overlay.edits} files")
    merged, conflicts = await _merge(session, base, overlay, remote, branch)
    if conflicts:
        return conflicts
    plan = await _writeback(session, overlay.local, merged, remote, overlay.cell, log)
    commits = await session.count(base, remote)
    change = _Change("update", target, session.dir, Marker(target.remote, branch, remote), plan=plan)
    return _Prepared("updated", branch, base, remote, change, commits=commits,
                     local_edits=overlay.edits, kept=plan.kept)


# ── push ──────────────────────────────────────────────────────────────


def _porcelain_status(out: bytes, destination: str) -> "tuple[str, str] | None":
    """``(flag, summary)`` of the porcelain line pushing to ``destination``."""
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].rpartition(":")[2] == destination:
            return parts[0], parts[2]
    return None


async def push(target: Target, *, account: GitAccount, message: "str | None",
               branch: str = "", push_branch: str = "",
               log: Callable[[str], None] = gen.log) -> PushReport:
    """Push the folder's edits to the remote as one commit on the recorded
    commit, by the account's commit author; never force-pushes. The cell
    records what was pushed."""
    if gen.grant().internet == "none":
        raise RepositoryError(E1)
    _check_transport(target, account)
    if account.auth == "none":
        raise RepositoryError(A6.format(r=display_of(target.remote)))
    author = account.identity
    workspace = Path(gen.workspace_path())
    marker = await asyncio.to_thread(_folder_state, workspace, target, branch)
    r, f = display_of(target.remote), target.folder
    if marker is None:
        raise RepositoryError(F7.format(f=f, r=r))
    b, base = marker.branch, marker.commit
    new_branch = None
    if push_branch and push_branch != b:
        if not _branch_ok(push_branch):
            raise ValueError(I7)
        new_branch = push_branch
    await _preflight(log)
    git_run.sweep_stale_sessions()
    with tempfile.TemporaryDirectory(prefix=f"collimate-git-{os.getpid()}-") as tmp:
        session = _Session(target, account, Path(tmp), identity=author)
        if await session.ls_remote_branch(b) is None:
            raise RepositoryError(R5.format(b=b, r=r, f=f))
        if new_branch and await session.ls_remote_branch(new_branch) is not None:
            raise RepositoryError(P3.format(x=new_branch, r=r))
        remote = await session.clone(b)
        if remote != base and not await session.contains(base, remote):
            raise RepositoryError(R6.format(b=b, r=r, B7=base[:7], f=f))
        overlay = await _overlay(workspace, session, base, message=message, log=log)
        upstream = await session.count(base, remote)
        local = overlay.local
        if overlay.edits == 0:
            return PushReport("nothing", target, b, None, base, remote, base, None, upstream, 0, 0, 0, None)
        if overlay.before.placeable and not (overlay.cell - set(overlay.ignored)):
            raise RepositoryError(P2.format(r=r, f=f))
        plan: "_Plan | None" = None
        if new_branch:
            kind, pushing, destination, expected = "new_branch", local, new_branch, None
            after_marker = Marker(target.remote, new_branch, local)
        elif remote == base:
            kind, pushing, destination, expected = "pushed", local, b, remote
            after_marker = Marker(target.remote, b, local)
        else:
            merged, conflicts = await _merge(session, base, overlay, remote, b)
            if conflicts:
                conflicts = sorted(conflicts)
                raise RepositoryConflict(M3.format(r=r, b=b, n=len(conflicts), list=_list(conflicts)),
                                         details=[f"Conflict: {r} {p}" for p in conflicts])
            plan = await _writeback(session, local, merged, remote, overlay.cell, log)
            same = (await session.sha(f"{merged}^{{tree}}", "reading")
                    == await session.sha(f"{remote}^{{tree}}", "reading"))
            if same:
                change = _Change("update", target, session.dir, Marker(target.remote, b, remote), plan=plan)
                await _save(workspace, [change], log=log)
                return PushReport("absorbed", target, b, None, base, remote, remote, None, upstream,
                                  overlay.edits, len(plan.writes), len(plan.deletes), None)
            kind, pushing, destination, expected = "merged", merged, b, remote
            after_marker = Marker(target.remote, b, merged)
        await _push_and_confirm(session, pushing, destination, expected, author, log)
        landed = _Pushed(target.remote, destination, pushing)
        change = _Change("update", target, session.dir, after_marker, plan=plan or _Plan())
        await _save(workspace, [change], log=log, pushed=landed)
    compare = account.compare_url(target.remote, b, new_branch) if new_branch else None
    return PushReport(kind, target, after_marker.branch, destination, base, remote, after_marker.commit, pushing,
                      upstream, overlay.edits, len(plan.writes) if plan else 0,
                      len(plan.deletes) if plan else 0, compare)


async def _push_and_confirm(session: _Session, pushing: str, destination: str, expected: "str | None",
                            author: Identity, log: Callable[[str], None]) -> None:
    """Push and decide from the remote's ref afterwards (the decision table, §4.10)."""
    r, host = display_of(session.target.remote), host_of(session.target.remote)
    log(f"Pushing {pushing[:7]} to {r} {destination} as {author.name} <{author.email}>")
    unknown = P8.format(P7=pushing[:7], r=r, dst=destination, host=host)
    try:
        result = await session.git.run(["push", "--porcelain", "--no-verify", "origin",
                                        f"{pushing}:refs/heads/{destination}"],
                                       session.dir, network=True, ok=(0, 1, 128))
    except GitFailure as exc:
        if exc.kind == "missing_git":
            raise session.error(exc, "pushing") from None
        result = git_run.Completed(exc.code, b"", exc.kind)
    except (gen.Cancelled, asyncio.CancelledError):
        gen.finish(note=unknown, outcome="fail")
        raise
    try:
        after = await session.ls_remote_raw(destination)
    except GitFailure:
        raise RepositoryError(unknown) from None
    except RepositoryError:
        raise RepositoryError(unknown) from None
    except (gen.Cancelled, asyncio.CancelledError):
        gen.finish(note=unknown, outcome="fail")
        raise
    if after == pushing:
        return
    if after != expected:
        raise RepositoryError(P4.format(r=r, dst=destination))
    status = _porcelain_status(result.stdout, f"refs/heads/{destination}")
    if status is not None and status[0].strip() == "!":
        summary = status[1].lower()
        if summary.startswith("[remote rejected]"):
            reason = summary[len("[remote rejected]"):]
            if any(word in reason for word in ("protected", "gh006", "gh013", "rule", "pre-receive hook declined")):
                raise RepositoryError(P5.format(host=host, r=r, dst=destination))
            if any(word in reason for word in ("permission", "denied", "not allowed")):
                cred = _credential(session.account) or _Credential("credential")
                raise RepositoryError(P6.format(r=r, cred=cred.cred, hint6=cred.hint6))
            raise RepositoryError(P5b.format(host=host, r=r, dst=destination))
        raise RepositoryError(P4.format(r=r, dst=destination))
    if result.code == 0:
        raise RepositoryError(N6v.format(r=r, dst=destination, host=host,
                                         A7=after[:7] if after else "no such branch"))
    failure = GitFailure(result.kind or "other", "push", result.code)
    raise session.error(failure, "pushing")
