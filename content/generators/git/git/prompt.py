# SPDX-License-Identifier: MIT
"""The Git runners' settings: the Repository (the runner's prompt) read into
the repository's canonical address, and the Folder, Branch and Push to
branch checked. Every refusal here comes before any process starts, and no
message echoes the unvalidated input: only checked addresses, folders and
branches appear in them."""
import re
from dataclasses import dataclass
from typing import Callable, Literal
from urllib.parse import urlsplit

from collimate import fs, git_hosts
from collimate.git_hosts import GitAccount
from collimate.pointers import RUNS_PREFIX, STAGING_PREFIX, is_dotgit_segment

REMOTE_CAP = 512                          # code points

# ── the canonical address rule ────────────────────────────────────────

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
_BRANCH_BAD_CHARS = frozenset(" ~^:?*[\\")
# The segments a repository folder may not hold, casefolded: the private
# band, the run band and the workspace skip dirs (a repository path's rule).
_FOLDER_FORBIDDEN = frozenset({STAGING_PREFIX, RUNS_PREFIX.casefold()}
                              | {name.casefold() for name in fs.WORKSPACE_SKIP_DIRS})

# ── messages ──────────────────────────────────────────────────────────

I1 = ("Set the runner's Repository: its address (https://host/path, ssh://user@host/path or "
      "user@host:path), or owner/name with the runner's Git account set to a GitHub or GitLab entry")
I1b = ("owner/name needs this runner's Git account set to a GitHub or GitLab entry, which says which server it is "
       "on: choose it in the runner's Git account setting (Capabilities); a Key manager entry is not used until a "
       "runner selects it. Or name the repository by its address (https://host/path)")
I1c = ("The bound Git server account needs the repository's full address (ssh://user@host/path or user@host:path "
       "for an SSH key, https://host/path for a public repository)")
I2 = ("Repository addresses carry no user name, password, token, query or fragment; use https://host/path, "
      "ssh://git@host/path or git@host:path (credentials belong in Key manager)")
I2b = ("git over plain http, git:// or local paths is refused: the repository would arrive unauthenticated; use "
       "https or ssh")
I3 = ("A repository is owner/name (group/sub/name on GitLab), https://host/path, ssh://user@host[:port]/path or "
      "user@host:path")
I3g = "A GitHub repository is owner/name: exactly two path segments"
I4 = "Repository is one line naming one repository; it has {n} lines"
I5 = ("Folder is a path inside this cell: . for the cell root, or a folder such as app or libs/app (no .., no "
      "leading /)")
I5b = "That folder is reserved in a cell (.git, .collrun, _runs, node_modules, .venv, __pycache__); choose another"
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


def check_remote(v) -> bool:
    """Whether ``v`` is a canonical address: https, ``ssh://`` or scp, with
    no credential, query or fragment, a lowercase host, no default port, and
    only safe path segments."""
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


def _parts(remote: str) -> "tuple[str, str, str]":
    """``(host[:port], separator, path)`` of an address ``remote_problem``
    admits: ``/`` before an https or ssh path (the path keeps no leading
    ``/``), ``:`` before an scp path."""
    if remote.startswith(("https://", "ssh://")):
        rest = remote.split("://", 1)[1]
        rest = rest.split("@", 1)[-1] if remote.startswith("ssh://") else rest
        hostport, _, path = rest.partition("/")
        return hostport, "/", path
    rest = remote.split("@", 1)[-1]
    host, _, path = rest.partition(":")
    return host, ":", path


def shorten(text: str, width: int = 60) -> str:
    """``text``, cut to ``width`` characters with an ellipsis."""
    return text if len(text) <= width else text[:width - 1] + "…"


def display_of(remote: str) -> str:
    """``host[:port]/path`` (scp: ``host:path``) without scheme, user or one
    trailing ``.git``, cut to 120 characters."""
    hostport, sep, path = _parts(remote)
    return shorten(f"{hostport}{sep}{path}".removesuffix(".git"), 120)


# ── the Repository ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Typed:
    """A prompt address after normalisation."""
    form: Literal["H", "X", "C"]
    user: str
    host: str
    port: int | None
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


def _forge_address(base: str, project: str, service: str) -> str:
    """The forge's canonical address ``<base>/<project>.git`` (one ``.git``),
    with the github rule (two segments, I3g) or the gitlab segment rule."""
    project = project.removesuffix(".git")
    segments = project.split("/") if project else []
    if service == "github":
        if len(segments) != 2:
            raise ValueError(I3g)
    elif not (2 <= len(segments) <= 20 and all(_GITLAB_SEGMENT.fullmatch(s) for s in segments)
              and not segments[-1].endswith((".git", ".atom"))):
        raise ValueError(I3)
    address = f"{base}/{project}.git"
    if any(s in ("", ".", "..") for s in segments) or not check_remote(address):
        raise ValueError(I3)
    return address


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
    address = _forge_address(base, "/".join(kept), service)
    if kept != segments:
        log(f"Using {display_of(address)} (the rest of the address is a page, not the repository)")
    return address


def _ssh_project(typed: _Typed) -> str:
    """The project path of an ssh or scp address on a forge (I3 for ``~``)."""
    path = typed.path.removeprefix("/")
    if path.startswith("~"):
        raise ValueError(I3)
    return path


def _generic(typed: _Typed) -> str:
    address = typed.text()
    if not check_remote(address):
        raise ValueError(I3)
    return address


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
            return _forge_address(web, text.removesuffix("/"), account.service)
        host, _port, _prefix = _web_parts(web)
        if typed.form == "H":
            below = _under(typed, web)
            if below is None:
                raise ValueError(A1.format(base=_web_label(web), host=typed.hostport()))
            return _https_forge(typed, web, below, account.service, log)
        if typed.host != host or typed.port not in (None, 22):
            raise ValueError(A1.format(base=_web_label(web), host=typed.hostport()))
        return _forge_address(web, _ssh_project(typed), account.service)
    if account.auth == "basic":
        if typed is None:
            address = f"{account.web}/{text.removesuffix('/')}"
            if not check_remote(address):
                raise ValueError(I3)
            return address
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
    address = _generic(typed)
    name = git_hosts.host_key_name(typed.host, typed.port)
    if not any(name in known.hosts for known in account.known_hosts):
        raise ValueError(A4.format(hostport=name))
    return address


def parse_prompt(prompt: "str | None", account: "GitAccount | None", *,
                 log: Callable[[str], None]) -> str:
    """The canonical address of the runner's Repository (its prompt): a short
    ``owner/name`` under the bound forge or Git server, or an https, ssh or
    scp address. Every refusal (I1–I4, A1–A4) comes before any process."""
    lines = [line.strip() for line in str(prompt or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError(I1)
    if len(lines) > 1:
        raise ValueError(I4.format(n=len(lines)))
    return _parse_address(lines[0], account, log)


# ── Folder and branches ───────────────────────────────────────────────


def check_folder(value: str) -> str:
    """The Folder setting as the repository's cell-relative folder: ``.`` for
    the cell root, else a normalized path (``app``, ``libs/app``). A path that
    climbs (``..``) or is absolute refuses (I5), and so does one with a
    ``.git``-equivalent, ``.collrun``, ``_runs`` or skip-dir segment (I5b),
    casefolded as the host keys repositories."""
    if value.rstrip("/") == ".":
        return "."
    raw = value.split("/")
    if "\\" in value or "\0" in value or ".." in raw:
        raise ValueError(I5)
    try:
        parts = fs.relative_parts(value)
    except PermissionError:
        raise ValueError(I5) from None
    if any(part.casefold() in _FOLDER_FORBIDDEN or is_dotgit_segment(part) for part in parts):
        raise ValueError(I5b)
    return "/".join(parts)


def default_folder(remote: str) -> str:
    """The folder named after the address's last path segment (one ``.git`` removed)."""
    name = _parts(remote)[2].rsplit("/", 1)[-1].removesuffix(".git")
    try:
        folder = check_folder(name)
    except ValueError:
        raise ValueError(I5c) from None
    if folder in (".", "") or "/" in folder:
        raise ValueError(I5c)
    return folder


def check_branch(name: str, message: str = I6) -> str:
    """``name`` when git takes it as a branch name, else ``ValueError(message)``."""
    if not _branch_ok(name):
        raise ValueError(message)
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
