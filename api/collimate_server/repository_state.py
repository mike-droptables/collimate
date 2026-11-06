"""A cell's git state as data: the normalized form every writer stores and
every run is materialized from (``docs/plans/2026-10-31-git-in-cells/design.md``
§3.3, §4.7). A state is a manifest over content-addressed parts; this module
holds the shape, the discovery of a workspace's repositories, the reader of a
``.git`` directory as data, the format gate, the allow-list, the sanitizer,
expiry, caps, the canonical parts and their hash, and the roots parser.

Nothing here writes a gitdir or the DB. The reader opens no file through a
symlink and reads only regular files under caps; the git commands it needs
(config listing, the index, ``ls-tree``, ``cat-file``, ``rev-list``) run
through :mod:`git_host` against a copy or against the gitdir an
:class:`Objects` names (a quarantine, a store), never the run's live config.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from collimate import fs

import collimate_server.git_host as git_host
from collimate_server.provenance import EXPORT_FILE

# How many repositories one cell version may hold (§3.3, Repository cap).
MAX_REPOSITORIES_PER_CELL = 256

# The cap of one part, and of one reflog (§3.3, Caps).
PART_CAP = 1024 * 1024
REFLOG_CAP = 4 * 1024 * 1024

# How much of a reflog or an index file the reader reads at most; a reflog
# keeps its newest lines when longer.
READ_LIMIT = 64 * 1024 * 1024

# The reader's walk of one gitdir: at most this many entries, this deep.
GITDIR_MAX_ENTRIES = 50_000
MAX_DEPTH = 64

# Reflog expiry by git's defaults (gc.reflogExpire, gc.reflogExpireUnreachable).
EXPIRE_SECONDS = 90 * 86400
EXPIRE_UNREACHABLE_SECONDS = 30 * 86400

NULL_OID = "0" * 40

# A loose ref, HEAD and ``objects/info/alternates`` are small; anything
# larger is not one.
_LOOSE_CAP = 4096
_ALTERNATES_CAP = 64 * 1024

# The exclude lines the host writes into ``info/exclude`` (§4.2, A16):
# stripped on read, written again at materialize.
HOST_EXCLUDES_ROOT: tuple[str, ...] = ("/.collrun/", "/_runs/", "/_agent/", "/.pydantic-ai-harness/")
HOST_EXCLUDES: tuple[str, ...] = (".collkeep",)
# The line an export adds for its provenance file at a root repository's top
# (§7): stripped on read like the host's, so an imported export stores none.
PROVENANCE_EXCLUDE = f"/{EXPORT_FILE}"
# The block an export writes after the host's lines, one anchored line per
# file it wrote under a name HEAD does not know (``safe_name``, ``(n)``;
# §7 Index): stripped on read whole, markers included.
RENAMED_BEGIN = "# collimate export: files written under another name"
RENAMED_END = "# collimate export: end"

# Refs never kept: GC's transient roots, fetch's prefetch refs, replace refs
# (git itself ignores them, as fetch does).
IGNORED_REF_PREFIXES: tuple[str, ...] = ("refs/gc-roots/", "refs/prefetch/", "refs/replace/")

# The operation state kept (§3.3, Progress): these files at the top of the
# gitdir, every ``BISECT_*`` one, and every regular file under the operation
# directories. Not FETCH_HEAD or rr-cache/.
PROGRESS_FILES: frozenset[str] = frozenset({
    "MERGE_HEAD", "MERGE_MSG", "MERGE_MODE", "MERGE_RR", "MERGE_AUTOSTASH", "AUTO_MERGE", "ORIG_HEAD",
    "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "SQUASH_MSG", "NOTES_MERGE_PARTIAL", "NOTES_MERGE_REF",
})
OPERATION_DIRS: tuple[str, ...] = ("NOTES_MERGE_WORKTREE", "rebase-merge", "rebase-apply", "sequencer")

# What marks an operation under way, so the index is kept (§3.3, Operation index).
_OPERATION_HEADS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD")

# The progress files whose full oids are roots (§3.3, Roots).
_PROGRESS_ROOTS: frozenset[str] = frozenset({
    "MERGE_HEAD", "ORIG_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "AUTO_MERGE", "MERGE_AUTOSTASH",
    "BISECT_HEAD", "rebase-merge/onto", "rebase-merge/orig-head", "rebase-merge/stopped-sha",
    "rebase-merge/autostash", "rebase-merge/amend", "rebase-apply/onto", "rebase-apply/orig-head",
    "rebase-apply/abort-safety", "rebase-apply/autostash", "sequencer/head", "sequencer/abort-safety",
})

# The merge strategies an operation may resume with (A9).
_STRATEGIES = frozenset({"ort", "recursive", "resolve", "octopus", "ours", "subtree"})

# The info parts (A16), by their file under ``info/``.
INFO_PARTS: tuple[str, ...] = ("exclude", "attributes", "sparse-checkout")

# The config a state keeps, one row per section: whether a subsection is
# "none" (forbidden), "named" (required) or "any", and the variables kept
# (None: every one). Sections and variables match case-insensitively,
# subsections byte-exact; everything else is run-local (hooks, hooksPath,
# fsmonitor, sshCommand, credential, include(If), alias, filters and
# drivers, textconv, proxies, extraHeader, insteadOf, uploadpack, receive,
# gc, maintenance, protocol, safe, signing).
ALLOWED_CONFIG: Mapping[str, tuple[str, frozenset[str] | None]] = {
    "remote": ("named", frozenset({"url", "pushurl", "fetch", "push", "tagopt", "prune", "prunetags",
                                   "skipfetchall"})),
    "branch": ("named", frozenset({"remote", "pushremote", "merge", "rebase", "description"})),
    "user": ("none", frozenset({"name", "email", "signingkey"})),
    "core": ("none", frozenset({"autocrlf", "eol", "safecrlf", "whitespace", "commentchar", "ignorestat",
                                "quotepath", "sparsecheckout", "sparsecheckoutcone"})),
    "pull": ("none", frozenset({"rebase", "ff"})),
    "push": ("none", frozenset({"default", "autosetupremote", "followtags", "useforceifincludes"})),
    "fetch": ("none", frozenset({"prune", "prunetags", "parallel"})),
    "merge": ("none", frozenset({"ff", "conflictstyle", "log", "autostash"})),
    "rebase": ("none", frozenset({"autostash", "autosquash", "updaterefs", "missingcommitscheck"})),
    "init": ("none", frozenset({"defaultbranch"})),
    "commit": ("none", frozenset({"cleanup", "verbose", "status"})),
    "tag": ("none", frozenset({"sort"})),
    "log": ("none", frozenset({"date", "decorate", "follow"})),
    "status": ("none", frozenset({"showuntrackedfiles", "branch", "short", "relativepaths"})),
    "diff": ("none", frozenset({"algorithm", "renames", "colormoved", "mnemonicprefix", "noprefix"})),
    "rerere": ("any", None),
    "column": ("any", None),
    "color": ("any", None),
    "help": ("none", frozenset({"autocorrect"})),
}

# The config keys whose values are remote addresses (sanitizer rule 1).
_URL_KEYS = {("remote", "url"), ("remote", "pushurl"), ("branch", "remote"), ("branch", "pushremote")}

# The format gate's messages (§3.3, Format gate).
REFTABLE = "reftable ref storage is not kept; run `git refs migrate --ref-format=files`"
SHA256 = "sha256 objects are not kept"
PARTIAL = "partial clone: re-clone without --filter"
BORROWS = ("borrows objects (--shared/--reference): run `git repack -a -d` and remove "
           "objects/info/alternates")
_ALLOWED_EXTENSIONS = frozenset({"worktreeconfig", "preciousobjects", "noop", "refstorage", "objectformat"})

# What dropping the operation index means for a run (R4).
STAGED_RESOLUTIONS = "staged resolutions must be re-added"

# The files of a host-written bare gitdir (a store, a quarantine, a
# staging gitdir; never ``git init``, P1) beside its ``objects/`` and ``refs/``.
BARE_FILES = {
    "HEAD": b"ref: refs/heads/none\n",
    "config": b"[core]\n\trepositoryformatversion = 0\n\tbare = true\n",
}

HEX40 = re.compile(r"[0-9a-f]{40}")
_SHARED_INDEX = re.compile(r"sharedindex\.[0-9a-f]{40}")
_REFLOG_LINE = re.compile(rb"([0-9a-f]{40}) ([0-9a-f]{40}) ([^\t\n]*>) ([0-9]+) ([+-][0-9]{4})(\t[^\n]*)?")
_SCHEME_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)", re.S)
_TEXT_PASSWORD = re.compile(rb"([A-Za-z][A-Za-z0-9+.-]*://)([^\s/@:]*):[^\s/@]*@")
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_ITA_FLAG = 0x20000000
# ``ls-files --debug``'s flag of a skip-worktree entry (CE_SKIP_WORKTREE).
SKIP_WORKTREE_FLAG = 0x40000000


@dataclass(frozen=True)
class RepositoryState:
    """One repository's normalized state: the manifest's hash, which is the
    row's ``state_hash``, and every part body keyed by its hash, the
    manifest's own body included. Writers store the parts and the row;
    readers rebuild a ``.git`` from them."""

    manifest_hash: str
    parts: Mapping[str, bytes]

    def manifest(self) -> dict:
        """The parsed manifest: ``{"head": ..., "parts": {name: hash}}``."""
        return json.loads(self.parts[self.manifest_hash])

    def part(self, name: str) -> bytes | None:
        """The body of the part the manifest names ``name``, or None when absent."""
        part_hash = self.manifest()["parts"].get(name)
        return None if part_hash is None else self.parts[part_hash]

    def named_parts(self) -> dict[str, bytes]:
        """Every part body by its manifest name."""
        return {name: self.parts[part_hash] for name, part_hash in self.manifest()["parts"].items()}


def part_hash(body: bytes) -> str:
    """The lowercase hex sha256 a part is stored under."""
    return hashlib.sha256(body).hexdigest()


def canonical_json(value) -> bytes:
    """Sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def make_state(head: str, parts: Mapping[str, bytes]) -> RepositoryState:
    """The state of ``head`` (``ref: <name>`` or 40-hex) over ``parts`` by
    name. A part with an empty body is omitted, except a ``progress/`` file,
    whose presence is itself state (``rebase-merge/interactive``)."""
    kept = {name: body for name, body in parts.items() if body or name.startswith("progress/")}
    manifest = canonical_json({"head": head, "parts": {name: part_hash(body) for name, body in kept.items()}})
    manifest_hash = part_hash(manifest)
    bodies = {part_hash(body): body for body in kept.values()}
    bodies[manifest_hash] = manifest
    return RepositoryState(manifest_hash, bodies)


# --- names and formats -----------------------------------------------------------------------------


def check_ref_format(name: str) -> bool:
    """Whether ``name`` is a ref name ``git check-ref-format`` accepts with
    at least two components (``HEAD`` is not one), valid UTF-8."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if (not name or name == "@" or "/" not in name or name.startswith("/") or name.endswith(("/", "."))
            or "//" in name or ".." in name or "@{" in name or _REF_FORBIDDEN.search(name)):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


def ignored_ref(name: str) -> bool:
    return name.startswith(IGNORED_REF_PREFIXES)


def ref_name_ok(name: str) -> bool:
    """Whether ``name`` is a ``refs/**`` name a state may hold (ignored or not)."""
    return name.startswith("refs/") and check_ref_format(name)


def symref(value: str) -> str | None:
    """The target of a ``ref: <name>`` value, or None."""
    if value.startswith("ref: "):
        target = value[5:]
        if target.startswith("refs/") and check_ref_format(target):
            return target
    return None


def _ref_value(body: bytes) -> str | None:
    """A loose ref's (or HEAD's) value: 40-hex or ``ref: <name>``, with at
    most one trailing newline; None when it is neither."""
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        return None
    if text.endswith("\n"):
        text = text[:-1]
    if HEX40.fullmatch(text):
        return text
    return text if symref(text) else None


def git_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "yes", "on", "1")


def split_config_key(key: str) -> tuple[str, str | None, str]:
    """A config key as (section, subsection, variable): split at the first
    and last dot, section and variable lowercased."""
    first, last = key.find("."), key.rfind(".")
    if first < 0:
        return key.lower(), None, ""
    section, variable = key[:first].lower(), key[last + 1:].lower()
    return section, (key[first + 1:last] if last > first else None), variable


def config_allowed(key: str, value: str) -> bool:
    """Whether a state keeps the pair (:data:`ALLOWED_CONFIG`)."""
    section, subsection, variable = split_config_key(key)
    row = ALLOWED_CONFIG.get(section)
    if row is None or not variable:
        return False
    rule, variables = row
    if (rule == "none" and subsection is not None) or (rule == "named" and not subsection):
        return False
    if variables is not None and variable not in variables:
        return False
    return not (section == "remote" and variable == "push" and value.startswith("+"))


def parse_config_list(output: bytes) -> tuple[list[tuple[str, str]], int]:
    """The pairs of ``git config --list -z``: each entry is split at its
    first newline, and a key without one is a value-less boolean, ``true``
    (P8); and how many entries were left out for not being UTF-8."""
    pairs, skipped = [], 0
    for entry in output.split(b"\0"):
        if not entry:
            continue
        key, newline, value = entry.partition(b"\n")
        try:
            pairs.append((key.decode("utf-8"), value.decode("utf-8") if newline else "true"))
        except UnicodeDecodeError:
            skipped += 1
    return pairs, skipped


# --- the sanitizer (A7, R14) ----------------------------------------------------------------------


def strip_url_userinfo(url: str) -> str:
    """Rule 1: a ``scheme://`` address but ``ssh://`` loses its whole
    userinfo; ``ssh://`` and scp addresses (``user@host:path``) lose only a
    ``:password``; a scheme address loses its query and fragment. Local
    paths are kept as they are."""
    match = _SCHEME_URL.match(url)
    if match:
        scheme, authority, path = match.groups()
        user, at, host = authority.rpartition("@")
        if at:
            if scheme.lower() == "ssh":
                user = user.split(":", 1)[0]
                authority = f"{user}@{host}" if user else host
            else:
                authority = host
        return f"{scheme}://{authority}{path}"
    head = url.split("/", 1)[0]
    userinfo, at, host = head.rpartition("@")
    if at and ":" in host and ":" in userinfo:
        return userinfo.split(":", 1)[0] + url[len(userinfo):]
    return url


def _strip_text_passwords(text: bytes) -> bytes:
    """Rule 2: ``scheme://user:secret@`` in free text becomes ``scheme://user@``."""
    return _TEXT_PASSWORD.sub(rb"\1\2@", text)


def _redact(text: bytes, secrets: Sequence[bytes]) -> bytes:
    """Rule 3: every secret form becomes ``[redacted]``."""
    for secret in secrets:
        text = text.replace(secret, b"[redacted]")
    return text


def _secret_forms(secrets: Iterable[str]) -> list[bytes]:
    return sorted({s.encode("utf-8") for s in secrets if s}, key=len, reverse=True)


def scrub_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Free text the host keeps beside a state (git's stderr in a rescue
    record) under rules 2 and 3: a URL's password goes, and every secret
    form becomes ``[redacted]``."""
    clean = _redact(_strip_text_passwords(text.encode("utf-8", "replace")), _secret_forms(secrets))
    return clean.decode("utf-8", "replace")


def scrub_bytes(body: bytes, secrets: Iterable[str] = (), *, passwords: bool = True) -> bytes:
    """Bytes the host hands out beside a state (a rescued ``.git``'s
    reflogs, progress and info files): rule 3, every secret form becomes
    ``[redacted]``, and with ``passwords`` rule 2, a URL's password goes."""
    return _redact(_strip_text_passwords(body) if passwords else body, _secret_forms(secrets))


def _transport_helper(url: str) -> bool:
    """Rule 4: an ``ext::`` or ``fd::`` address, which runs a command or
    reads a descriptor."""
    return url.strip().lower().startswith(("ext::", "fd::"))


def sanitize_config(pairs: Iterable[tuple[str, str]], secrets: Iterable[str] = ()
                    ) -> tuple[list[tuple[str, str]], list[str]]:
    """The allow-listed pairs with the sanitizer's config rules applied (1,
    URL userinfo; 3, secret forms; 4, ``ext::``/``fd::`` remotes), and a
    line per removal naming the key, never the value."""
    forms = _secret_forms(secrets)
    pairs = [(key, value) for key, value in pairs if config_allowed(key, value)]
    lines: list[str] = []
    helpers = set()
    for key, value in pairs:
        section, subsection, variable = split_config_key(key)
        if (section, variable) in {("remote", "url"), ("remote", "pushurl")} and _transport_helper(value):
            helpers.add(subsection)
    kept = []
    for key, value in pairs:
        section, subsection, variable = split_config_key(key)
        if section == "remote" and subsection in helpers:
            lines.append(f"config {key}: dropped with its ext::/fd:: remote")
            continue
        clean = value
        if (section, variable) in _URL_KEYS:
            if _transport_helper(value):
                lines.append(f"config {key}: ext::/fd:: address dropped")
                continue
            clean = strip_url_userinfo(value)
        clean = _redact(clean.encode("utf-8"), forms).decode("utf-8")
        if clean != value:
            lines.append(f"config {key}: credentials removed")
        kept.append((key, clean))
    return kept, lines


def _redact_part(name: str, body: bytes, forms: Sequence[bytes], lines: list[str]) -> bytes:
    """Rule 3 over a part kept as bytes (a progress file, an info part).
    Rule 2 is for reflog messages alone: a patch or todo a resumed
    operation applies must keep every other byte it had."""
    clean = _redact(body, forms)
    if clean != body:
        lines.append(f"{name}: credentials removed")
    return clean


# --- the operation index (A1, R5) -----------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """One record of ``git ls-files -s --debug -z``."""

    mode: str
    sha: str
    stage: int
    path: bytes
    flags: int


def parse_ls_files_debug(output: bytes) -> list[IndexEntry]:
    """The records of ``git ls-files -s --debug -z``: ``<mode> <sha>
    <stage>\\t<path>`` NUL, then five debug lines, the last ``  size: N\\tflags: <hex>``."""
    entries = []
    at = 0
    while at < len(output):
        end = output.index(b"\0", at)
        head, _, path = output[at:end].partition(b"\t")
        mode, sha, stage = head.decode("ascii").split(" ")
        at = end + 1
        flags = 0
        for _ in range(5):
            line_end = output.index(b"\n", at)
            line = output[at:line_end]
            at = line_end + 1
            if b"flags: " in line:
                flags = int(line.rpartition(b"flags: ")[2], 16)
        entries.append(IndexEntry(mode, sha, int(stage), path, flags))
    return entries


def _removal(path: bytes) -> bytes:
    return f"0 {NULL_OID}\t".encode("ascii") + path


def _entry_line(entry: IndexEntry) -> bytes:
    return f"{entry.mode} {entry.sha} {entry.stage}\t".encode("ascii") + entry.path


def operation_index(entries: Sequence[IndexEntry], tree: Sequence[tuple[str, str, bytes]]) -> bytes:
    """The ``index`` part: NUL-terminated ``update-index --index-info``
    lines that turn ``read-tree HEAD`` (``tree``: HEAD's ``(mode, sha,
    path)`` entries) into the index ``entries`` describe. Per unmerged path,
    a removal line then stages 1-3; per stage-0 entry differing from HEAD,
    a removal line then the entry; per HEAD path the index lacks, a removal
    line. An intent-to-add entry is left out: ``--index-info`` cannot carry
    it, and replay would stage an empty file (P20)."""
    head = {path: (mode, sha) for mode, sha, path in tree}
    staged: dict[bytes, list[IndexEntry]] = {}
    for entry in entries:
        staged.setdefault(entry.path, []).append(entry)
    lines = []
    for path in sorted(set(staged) | set(head)):
        records = staged.get(path, [])
        unmerged = sorted((e for e in records if e.stage), key=lambda e: e.stage)
        if unmerged:
            lines += [_removal(path), *map(_entry_line, unmerged)]
            continue
        zero = next((e for e in records if e.stage == 0), None)
        if zero is None:
            lines.append(_removal(path))
        elif not zero.flags & _ITA_FLAG and head.get(path) != (zero.mode, zero.sha):
            lines += [_removal(path), _entry_line(zero)]
    return b"".join(line + b"\0" for line in lines)


def index_lines(body: bytes) -> list[tuple[str, str, int, bytes]]:
    """The ``index`` part's replay lines as ``(mode, sha, stage, path)``; a
    removal is mode ``0``."""
    lines = []
    for record in body.split(b"\0"):
        head, tab, path = record.partition(b"\t")
        fields = head.decode("ascii", "replace").split(" ")
        if tab and len(fields) >= 2:
            stage = int(fields[2]) if len(fields) > 2 and fields[2].isdigit() else 0
            lines.append((fields[0], fields[1], stage, path))
    return lines


def _index_roots(body: bytes) -> set[str]:
    return {sha for mode, sha, _, _ in index_lines(body) if mode not in ("0", "160000") and sha != NULL_OID}


# --- object lookups -------------------------------------------------------------------------------


class Objects(Protocol):
    """What the normalizer asks of the objects a state's shas live in."""

    def exists(self, shas: Iterable[str]) -> dict[str, str]:
        """The present ones among ``shas``, each with its object type."""
        ...

    def unreachable(self, candidates: Iterable[str], tips: Iterable[str]) -> set[str]:
        """The commits among ``candidates`` no ``tips`` reach."""
        ...

    def tree(self, commit: str) -> list[tuple[str, str, bytes]]:
        """``commit``'s files as ``(mode, sha, path)``, recursively."""
        ...

    def index(self, body: bytes, shared: Mapping[str, bytes]) -> list[IndexEntry]:
        """The records of an index file's bytes; ``shared``, a split index's
        ``sharedindex.<sha>`` files by name."""
        ...


class GitdirObjects:
    """:class:`Objects` over a gitdir through host git: a harvest's
    quarantine (alternates to the store), or a store. ``shallow_file``
    becomes ``GIT_SHALLOW_FILE`` for walks over shallow history in a gitdir
    that has no ``shallow`` of its own."""

    def __init__(self, git_dir: str, *, shallow_file: str | None = None):
        self.git_dir = git_dir
        self.env = {"GIT_SHALLOW_FILE": shallow_file} if shallow_file else None

    def exists(self, shas: Iterable[str]) -> dict[str, str]:
        wanted = sorted(set(shas))
        if not wanted:
            return {}
        done = git_host.run(["cat-file", "--batch-check"], git_dir=self.git_dir, env=self.env,
                            input="".join(f"{sha}\n" for sha in wanted).encode("ascii"))
        found = {}
        for line in done.stdout.decode("ascii", "replace").splitlines():
            fields = line.split(" ")
            if len(fields) == 3 and fields[1] != "missing":
                found[fields[0]] = fields[1]
        return found

    def unreachable(self, candidates: Iterable[str], tips: Iterable[str]) -> set[str]:
        wanted = set(candidates)
        if not wanted:
            return set()
        feed = [*sorted(wanted), *(f"^{tip}" for tip in sorted(set(tips)))]
        done = git_host.run(["rev-list", "--stdin"], git_dir=self.git_dir, env=self.env,
                            input="".join(f"{line}\n" for line in feed).encode("ascii"))
        return wanted & set(done.stdout.decode("ascii", "replace").split())

    def tree(self, commit: str) -> list[tuple[str, str, bytes]]:
        done = git_host.run(["ls-tree", "-r", "-z", "--full-tree", commit], git_dir=self.git_dir, env=self.env)
        entries = []
        for record in done.stdout.split(b"\0"):
            if record:
                head, _, path = record.partition(b"\t")
                mode, _, sha = head.decode("ascii").split(" ")
                entries.append((mode, sha, path))
        return entries

    def index(self, body: bytes, shared: Mapping[str, bytes]) -> list[IndexEntry]:
        # The copy is listed in a gitdir of its own, borrowing this one's
        # objects (a sparse index expands from trees): git looks for a split
        # index's shared files in the gitdir (2.31) or beside the index.
        with tempfile.TemporaryDirectory(prefix="git-index-", dir=git_host.tmp_root()) as scratch:
            os.makedirs(os.path.join(scratch, "objects", "info"))
            os.mkdir(os.path.join(scratch, "refs"))
            files = {**BARE_FILES,
                     "objects/info/alternates": os.path.join(os.path.abspath(self.git_dir), "objects").encode() + b"\n",
                     "index": body, **shared}
            for name, content in files.items():
                with open(os.path.join(scratch, name), "wb") as fh:
                    fh.write(content)
            try:
                done = git_host.run(["ls-files", "-s", "--debug", "-z"], git_dir=scratch)
            except git_host.GitFailed as exc:
                raise git_host.GitFailed(exc.command, exc.returncode, exc.stderr.replace(scratch + os.sep, ""),
                                         timed_out=exc.timed_out) from None
        return parse_ls_files_debug(done.stdout)


# --- discovery (§4.7) -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """What a workspace holds: ``repositories``, the paths whose ``.git`` is
    a readable directory ("" the root), shallowest first; ``unusable``, the
    paths whose ``.git`` is a symlink, a gitfile or unreadable, each with
    its line (their rows are kept); ``errors``, what else the walk could not
    read (any one cancels every removal a harvest would record)."""

    repositories: tuple[str, ...]
    unusable: Mapping[str, str]
    errors: tuple[str, ...]


def shown(repo_path: str) -> str:
    """A repository path as lines name it: ``.`` for the root."""
    return repo_path or "."


def discover(workspace: str) -> Discovery:
    """Every repository under ``workspace``: each directory named exactly
    ``.git``. The walk follows no symlink, opens no file, looks no further
    into a ``.git`` than whether it can be listed, never enters a
    ``.collrun``, ``_runs`` or other ``WORKSPACE_SKIP_DIRS`` directory, and
    stops :data:`MAX_DEPTH` levels down."""
    skip = (set(fs.WORKSPACE_SKIP_DIRS) - {".git"}) | {".collrun", "_runs"}
    repositories: list[str] = []
    unusable: dict[str, str] = {}
    errors: list[str] = []

    def prune(path: str, entry: os.DirEntry) -> bool:
        parent = path.rpartition("/")[0]
        if parent == ".git" or parent.endswith("/.git"):
            return True
        if entry.name == ".git":
            if entry.is_symlink():
                unusable[parent] = f"not kept: {shown(parent)}/.git is a symlink"
            elif entry.is_dir(follow_symlinks=False):
                repositories.append(parent)
                return False
            elif entry.is_file(follow_symlinks=False):
                unusable[parent] = f"not kept: submodule {shown(parent)} (gitfile)"
            else:
                unusable[parent] = f"not kept: {shown(parent)}/.git is not a directory"
            return True
        return not entry.is_dir(follow_symlinks=False) or entry.name in skip

    def failed(exc: OSError) -> None:
        path = exc.filename or ""
        if path == ".git" or path.endswith("/.git"):
            repo_path = path[:-len(".git")].rstrip("/")
            if repo_path in repositories:
                repositories.remove(repo_path)
                unusable[repo_path] = f"not kept: {shown(repo_path)}/.git is unreadable ({exc.strerror or exc})"
                return
        errors.append(f"{path or '.'}: {exc.strerror or exc}")

    for _ in fs.walk_regular(workspace, prune=prune, max_depth=MAX_DEPTH, onerror=failed):
        pass
    order = sorted(repositories, key=lambda p: (p.count("/") + bool(p), p))
    return Discovery(tuple(order), unusable, tuple(errors))


def cap_repositories(paths: Iterable[str], existing: Iterable[str]) -> tuple[list[str], int]:
    """The repository paths a cell keeps under
    :data:`MAX_REPOSITORIES_PER_CELL` (paths with rows first, then new
    paths shallowest first, then in path order) and how many overflow."""
    rows = set(existing)
    ordered = sorted(set(paths), key=lambda p: (p not in rows, p.count("/") + bool(p), p))
    return ordered[:MAX_REPOSITORIES_PER_CELL], max(0, len(ordered) - MAX_REPOSITORIES_PER_CELL)


# --- the reader (§4.7) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ReflogEntry:
    """One valid reflog line (``line`` without its LF, kept byte for byte)."""

    old: str
    new: str
    timestamp: int
    line: bytes


def parse_reflog(body: bytes) -> tuple[list[ReflogEntry], int, bool]:
    """A reflog's valid entries, how many malformed lines were dropped, and
    whether a last line without LF (a write under way) was dropped. A line
    is ``<old> SP <new> SP <ident> SP <epoch> SP <tz> [TAB <message>] LF``;
    the ident ends at the last ``>`` before the timestamp, and a line
    without a TAB is valid (``update-ref`` without ``-m``; P19)."""
    lines = body.split(b"\n")
    partial = lines.pop() != b""
    entries, malformed = [], 0
    for line in lines:
        match = _REFLOG_LINE.fullmatch(line)
        if match is None:
            malformed += 1
            continue
        entries.append(ReflogEntry(match.group(1).decode("ascii"), match.group(2).decode("ascii"),
                                   int(match.group(4)), line))
    return entries, malformed, partial


@dataclass
class GitdirRead:
    """What the reader took from one gitdir, as data, for :func:`normalize`.
    ``refusal`` set means nothing else was kept: the format gate, an
    unreadable gitdir, a refs or config cap. ``head`` None means HEAD was
    unreadable (its base value is kept); ``bad_refs`` are refs whose value
    is not one (their base values are kept). ``oversized`` names the parts
    whose file was over its cap (``index``: over :data:`READ_LIMIT`).
    ``shared_index`` holds a split index's ``sharedindex.<sha>`` files by
    name. ``lines`` are logged, ``warnings`` also become warning events."""

    repo_path: str
    refusal: str | None = None
    head: str | None = None
    refs: dict[str, str] = field(default_factory=dict)
    bad_refs: set[str] = field(default_factory=set)
    reflogs: dict[str, list[ReflogEntry]] = field(default_factory=dict)
    config: list[tuple[str, str]] = field(default_factory=list)
    info: dict[str, bytes] = field(default_factory=dict)
    shallow: bytes | None = None
    progress: dict[str, bytes] = field(default_factory=dict)
    oversized: set[str] = field(default_factory=set)
    index: bytes | None = None
    shared_index: dict[str, bytes] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_capped(fd: int, cap: int) -> bytes | None:
    """At most ``cap`` bytes of ``fd``; None when it holds more."""
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(1 << 20, cap + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            return None


def _read_tail(fd: int, limit: int) -> tuple[bytes, bool]:
    """The whole of ``fd`` when it is at most ``limit`` bytes, else its last
    ``limit`` bytes from a line start; and whether it was cut."""
    size = os.fstat(fd).st_size
    if size <= limit:
        body = _read_capped(fd, limit)
        if body is not None:
            return body, False
        size = os.fstat(fd).st_size
    tail = os.pread(fd, limit, max(0, size - limit))
    return tail[tail.find(b"\n") + 1:], True


def progress_name(path: str) -> str | None:
    """The progress name of a gitdir path, or None when it is not one."""
    top, slash, _ = path.partition("/")
    if slash:
        return path if top in OPERATION_DIRS else None
    return path if path in PROGRESS_FILES or path.startswith("BISECT_") else None


def progress_set(name: str) -> str:
    """The set a progress file belongs to: its operation directory, or the
    file itself at the top. Git cannot resume half a set."""
    top, slash, _ = name.partition("/")
    return top if slash else name


_OPTS_HEADER = "[options]"
_OPTS_PAIR = re.compile(r"\t([a-z][a-z0-9-]*) = (.*)")
_OPTS_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _opts_value(raw: str) -> str | None:
    """A ``sequencer/opts`` value as git's config writer quotes it (whole in
    double quotes when it holds ``;`` or ``#`` or starts or ends with a
    space; ``\\n``, ``\\t``, ``\\"``, ``\\\\`` escaped), unquoted; None for any
    other spelling."""
    quoted = len(raw) >= 2 and raw[0] == raw[-1] == '"'
    inner = raw[1:-1] if quoted else raw
    if not quoted and (inner != inner.strip(" ") or ";" in inner or "#" in inner):
        return None
    value, at = [], 0
    while at < len(inner):
        char = inner[at]
        if char == '"' or ord(char) < 0x20 or char == "\x7f":
            return None
        if char == "\\":
            escaped = _OPTS_ESCAPES.get(inner[at + 1:at + 2])
            if escaped is None:
                return None
            value.append(escaped)
            at += 2
            continue
        value.append(char)
        at += 1
    return "".join(value)


def sequencer_options(body: bytes) -> list[tuple[str, str, str]] | None:
    """The options of ``sequencer/opts`` as ``(variable, value, line)``, in
    file order, or None when the file is not in the one form git writes
    it: an ``[options]`` header and a ``\\t<variable> = <value>`` line per
    option. Only that form means to the reader exactly what it means to git."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text and not text.endswith("\n"):
        return None
    options, headed = [], False
    for line in text.split("\n")[:-1]:
        if line == _OPTS_HEADER:
            headed = True
            continue
        if not line:
            continue
        match = _OPTS_PAIR.fullmatch(line)
        value = None if match is None else _opts_value(match.group(2))
        if not headed or match is None or value is None:
            return None
        options.append((match.group(1), value, line))
    return options


def progress_problem(progress: Mapping[str, bytes]) -> str | None:
    """Why an operation's state must not be kept (A9), or None: a todo
    line whose first word is ``exec`` or ``x``; a merge strategy git does
    not ship, in ``rebase-merge/strategy`` or ``sequencer/opts``; a
    ``sequencer/opts`` not in the form git writes; a signing option, which
    is never stored. Checked by the reader and again by ``write_gitdir``.

    Blank and comment lines never start with ``exec``, so no
    ``core.commentChar`` is consulted: a comment character of ``e`` or
    ``x`` makes git skip a line this refuses, never the reverse, whatever
    config the resumed operation reads."""
    for name in ("rebase-merge/git-rebase-todo", "sequencer/todo"):
        for raw in progress.get(name, b"").decode("utf-8", "replace").splitlines():
            words = raw.split(None, 1)
            if words and words[0] in ("exec", "x"):
                return f"{name} runs commands (exec); the operation is not kept"
    strategy = progress.get("rebase-merge/strategy")
    if strategy is not None and strategy.decode("utf-8", "replace").removesuffix("\n") not in _STRATEGIES:
        return "rebase-merge/strategy names an unknown merge strategy; the operation is not kept"
    opts = progress.get("sequencer/opts")
    if opts is not None:
        options = sequencer_options(opts)
        if options is None:
            return "sequencer/opts is not in the form git writes; the operation is not kept"
        if any(variable == "strategy" and value not in _STRATEGIES for variable, value, _ in options):
            return "sequencer/opts names an unknown merge strategy; the operation is not kept"
        if any(variable == "gpg-sign" for variable, _, _ in options):
            return "sequencer/opts holds options.gpg-sign, which is never stored"
    if any(os.path.basename(name) == "gpg_sign_opt" for name in progress):
        return "a gpg_sign_opt file is never stored"
    return None


class _Walk:
    """One walk of a gitdir: the regular files the reader reads, by path,
    each read under its cap (None when over it), and the layout facts the
    gate needs."""

    def __init__(self) -> None:
        self.files: dict[str, bytes | None] = {}
        self.cut: set[str] = set()
        self.errors: list[str] = []
        self.hooks = False
        self.worktrees = False
        self.reftable = False
        self.promisor = False
        self.layout = False

    def prune(self, path: str, entry: os.DirEntry) -> bool:
        name = entry.name
        top, slash, rest = path.partition("/")
        # Git's temporary files: a lock anywhere; tmp_*/*.tmp outside refs/
        # and logs/, where they are ordinary ref names (``tmp_fix``).
        if name.endswith(".lock") or (top not in ("refs", "logs") and (name.endswith(".tmp")
                                                                       or name.startswith("tmp_"))):
            return True
        is_dir = entry.is_dir(follow_symlinks=False)
        if path in ("refs/heads", "refs/tags") and not is_dir:
            self.layout = True
            return True
        if not slash:
            if top in ("refs", "logs", "info", "objects", "hooks", "worktrees", *OPERATION_DIRS):
                return not is_dir
            if top == "reftable":
                self.reftable = True
                return True
            return is_dir or not (top in ("HEAD", "config", "config.worktree", "packed-refs", "shallow", "index")
                                  or _SHARED_INDEX.fullmatch(top) or progress_name(top))
        if top == "hooks":
            self.hooks = self.hooks or not name.endswith(".sample")
            return True
        if top == "worktrees":
            self.worktrees = True
            return True
        if top == "objects":
            if rest in ("info", "pack"):
                return not is_dir
            if rest.startswith("pack/"):
                self.promisor = self.promisor or name.endswith(".promisor")
                return True
            return rest != "info/alternates"
        if top == "info":
            return is_dir or rest not in INFO_PARTS
        return False

    def read(self, path: str, fd: int) -> None:
        if path.startswith("logs/"):
            self.files[path], cut = _read_tail(fd, READ_LIMIT)
            if cut:
                self.cut.add(path)
        elif path in ("index", "packed-refs") or _SHARED_INDEX.fullmatch(path):
            self.files[path] = _read_capped(fd, READ_LIMIT)
        elif path == "HEAD" or path.startswith("refs/"):
            self.files[path] = _read_capped(fd, _LOOSE_CAP)
        elif path == "objects/info/alternates":
            self.files[path] = _read_capped(fd, _ALTERNATES_CAP)
        else:
            self.files[path] = _read_capped(fd, PART_CAP)

    def failed(self, exc: OSError) -> None:
        self.errors.append(f"{exc.filename or '.'}: {exc.strerror or exc}")


def read_repository(workspace: str, repo_path: str, *, alternates: str | None = None) -> GitdirRead:
    """Read ``<workspace>/<repo_path>/.git`` (no component followed through
    a symlink): see :func:`read_gitdir`."""
    parts = fs.relative_parts(repo_path) if repo_path else []
    try:
        parent = fs.open_parent(workspace, [*parts, ".git"], create=False)
        try:
            gitdir = os.open(".git", os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError as exc:
        return GitdirRead(repo_path, refusal=f"gitdir unreadable: {exc.strerror or exc}")
    try:
        return _read(gitdir, repo_path, alternates)
    finally:
        os.close(gitdir)


def read_gitdir(gitdir: str, *, repo_path: str = "", alternates: str | None = None) -> GitdirRead:
    """Read a gitdir as data, in order: the format gate; HEAD; loose refs,
    then ``packed-refs`` (loose wins); reflogs; config; ``info/*``;
    ``shallow``; progress; the index file. ``repo_path`` says whether the
    root's host exclude lines apply; ``alternates`` is the one
    ``objects/info/alternates`` line a materialized gitdir may hold (the
    store's absolute ``objects``). Objects are left to the harvest."""
    try:
        fd = os.open(gitdir, os.O_RDONLY | fs.O_DIRECTORY | fs.O_NOFOLLOW | fs.O_NONBLOCK)
    except OSError as exc:
        return GitdirRead(repo_path, refusal=f"gitdir unreadable: {exc.strerror or exc}")
    try:
        return _read(fd, repo_path, alternates)
    finally:
        os.close(fd)


def _read(gitdir_fd: int, repo_path: str, alternates: str | None) -> GitdirRead:
    walk = _Walk()
    for path, fd in fs.walk_regular(".", dir_fd=gitdir_fd, prune=walk.prune, max_depth=MAX_DEPTH,
                                    max_entries=GITDIR_MAX_ENTRIES, onerror=walk.failed):
        walk.read(path, fd)
    read = GitdirRead(repo_path)
    if walk.errors:
        read.refusal = f"gitdir unreadable: {walk.errors[0]}"
        return read
    config = _config_pairs(walk, read, "config")
    if read.refusal is None:
        read.refusal = _gate(walk, config, alternates)
    if read.refusal is not None:
        return read
    _read_head(walk, read)
    _read_refs(walk, read)
    if read.refusal is not None:
        return read
    _read_reflogs(walk, read)
    worktree = any(key.lower() == "extensions.worktreeconfig" and git_bool(value) for key, value in config)
    if worktree and "config.worktree" in walk.files:
        config = config + _config_pairs(walk, read, "config.worktree")
    read.config = config
    if read.refusal is not None:
        return read
    if any(key.lower() == "core.hookspath" for key, _ in config):
        read.lines.append("not kept: core.hooksPath")
    if walk.hooks:
        read.lines.append("not kept: hooks")
    if walk.worktrees:
        read.lines.append("not kept: worktrees")
    _read_info(walk, read)
    _read_shallow(walk, read)
    _read_progress(walk, read)
    if "index" in walk.files:
        read.index = walk.files["index"]
        if read.index is None:
            read.oversized.add("index")
        read.shared_index = {path: body for path, body in walk.files.items()
                             if body is not None and _SHARED_INDEX.fullmatch(path)}
    return read


def _config_pairs(walk: _Walk, read: GitdirRead, name: str) -> list[tuple[str, str]]:
    """The pairs of the gitdir's ``name`` (``config``, ``config.worktree``),
    read with git from a copy, includes not followed; a refusal when it is
    over its cap or git cannot read it."""
    body = walk.files.get(name)
    if name not in walk.files:
        return []
    if body is None:
        read.refusal = f"{name} over 1 MiB"
        return []
    with tempfile.TemporaryDirectory(prefix="git-read-", dir=git_host.tmp_root()) as scratch:
        copy = os.path.join(scratch, "config")
        with open(copy, "wb") as fh:
            fh.write(body)
        try:
            done = git_host.run(["config", "--file", copy, "--no-includes", "--list", "-z"])
        except git_host.GitFailed as exc:
            read.refusal = f"{name} unreadable: {exc.stderr.replace(copy, name).strip() or exc}"
            return []
    pairs, skipped = parse_config_list(done.stdout)
    if skipped:
        read.lines.append(f"{name}: {skipped} entries not UTF-8 dropped")
    return pairs


def _gate(walk: _Walk, config: Sequence[tuple[str, str]], alternates: str | None) -> str | None:
    """Why the format gate refuses the gitdir (C6, A10), or None."""
    head = walk.files.get("HEAD")
    if walk.reftable or walk.layout or (head is not None and head.strip() == b"ref: refs/heads/.invalid"):
        return REFTABLE
    for key, value in config:
        section, subsection, variable = split_config_key(key)
        if section == "core" and subsection is None and variable == "repositoryformatversion":
            try:
                version = int(value)
            except ValueError:
                version = -1
            if not 0 <= version <= 1:
                return f"repository format version {value} is not kept"
        elif section == "extensions" and subsection is None:
            if variable == "refstorage" and value.strip().lower() != "files":
                return REFTABLE
            if variable == "objectformat" and value.strip().lower() != "sha1":
                return SHA256
            if variable == "partialclone":
                return PARTIAL
            if variable not in _ALLOWED_EXTENSIONS:
                return f"extensions.{variable} is not kept"
        elif section == "remote" and subsection is not None and variable in ("promisor", "partialclonefilter"):
            return PARTIAL
    if walk.promisor:
        return PARTIAL
    if "objects/info/alternates" in walk.files:
        body = walk.files["objects/info/alternates"]
        if alternates is None or body is None or body.decode("utf-8", "replace").strip("\n") != alternates:
            return BORROWS
    return None


def _read_head(walk: _Walk, read: GitdirRead) -> None:
    body = walk.files.get("HEAD")
    read.head = None if body is None else _ref_value(body)
    if read.head is None:
        read.lines.append("HEAD unreadable; its base value is kept")


def _keep_ref(read: GitdirRead, name: str) -> bool:
    if ignored_ref(name):
        read.lines.append(f"ref {name} ignored")
        return False
    if not ref_name_ok(name):
        read.lines.append("a ref with a bad name dropped")
        return False
    return True


def _read_refs(walk: _Walk, read: GitdirRead) -> None:
    packed = walk.files.get("packed-refs", b"")
    if packed is None:
        # Over READ_LIMIT: refused like a refs part over its cap (rescue).
        read.refusal = "refs over 1 MiB"
        return
    for raw in packed.split(b"\n"):
        if not raw or raw.startswith((b"#", b"^")):
            continue
        sha, space, name_bytes = raw.partition(b" ")
        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError:
            read.lines.append("a packed ref with a bad name dropped")
            continue
        if not space or not HEX40.fullmatch(sha.decode("ascii", "replace")):
            read.lines.append("a malformed packed-refs line dropped")
            continue
        if _keep_ref(read, name):
            read.refs[name] = sha.decode("ascii")
    for path, body in walk.files.items():
        if not path.startswith("refs/") or not _keep_ref(read, path):
            continue
        value = None if body is None else _ref_value(body)
        if value is None:
            read.refs.pop(path, None)
            read.bad_refs.add(path)
            read.lines.append(f"ref {path} has a bad value; its base value is kept")
        else:
            read.refs[path] = value
            read.bad_refs.discard(path)


def _read_reflogs(walk: _Walk, read: GitdirRead) -> None:
    for path, body in walk.files.items():
        if not path.startswith("logs/"):
            continue
        name = path[len("logs/"):]
        if name != "HEAD" and (ignored_ref(name) or not _keep_ref(read, name)):
            continue                                        # an ignored ref is logged once, with its ref
        entries, malformed, partial = parse_reflog(body or b"")
        if malformed:
            read.lines.append(f"reflog {name}: {malformed} malformed lines dropped")
        if partial:
            read.lines.append(f"reflog {name}: a partial last line dropped")
        if path in walk.cut:
            read.lines.append(f"reflog {name}: entries beyond its newest 64 MiB not read")
        read.reflogs[name] = entries


def strip_host_excludes(body: bytes, repo_path: str) -> bytes:
    """``info/exclude`` without the lines the host (and an export) writes there."""
    host = {*HOST_EXCLUDES, *((*HOST_EXCLUDES_ROOT, PROVENANCE_EXCLUDE) if not repo_path else ())}
    kept, renamed = [], False
    for line in body.split(b"\n"):
        text = line.decode("utf-8", "replace")
        if renamed:
            renamed = text != RENAMED_END
        elif text == RENAMED_BEGIN:
            renamed = True
        elif text not in host:
            kept.append(line)
    return b"\n".join(kept)


def _read_info(walk: _Walk, read: GitdirRead) -> None:
    for name in INFO_PARTS:
        path = f"info/{name}"
        if path not in walk.files:
            continue
        body = walk.files[path]
        if body is None:
            read.oversized.add(name)
            continue
        read.info[name] = strip_host_excludes(body, read.repo_path) if name == "exclude" else body


def _read_shallow(walk: _Walk, read: GitdirRead) -> None:
    if "shallow" in walk.files:
        body = walk.files["shallow"]
        if body is None:
            read.oversized.add("shallow")
        else:
            read.shallow = body


def _read_progress(walk: _Walk, read: GitdirRead) -> None:
    for path, body in walk.files.items():
        name = progress_name(path)
        if name is None:
            continue
        if os.path.basename(name) == "gpg_sign_opt":
            read.lines.append(f"progress {name}: not kept (signing)")
            continue
        if body is None:
            read.oversized.add(f"progress/{name}")
            continue
        read.progress[name] = body
    options = sequencer_options(read.progress.get("sequencer/opts", b""))
    if options and any(variable == "gpg-sign" for variable, _, _ in options):
        kept = [line for variable, _, line in options if variable != "gpg-sign"]
        read.progress["sequencer/opts"] = "".join(f"{line}\n" for line in [_OPTS_HEADER, *kept]).encode("utf-8")
        read.lines.append("progress sequencer/opts: options.gpg-sign not kept (signing)")
    problem = progress_problem(read.progress)
    if problem is not None:
        read.warnings.append(problem)
        read.progress = {}
        read.oversized = {name for name in read.oversized if not name.startswith("progress/")}
        walk.files.pop("index", None)


# --- the normalizer (§3.3) ------------------------------------------------------------------------


@dataclass(frozen=True)
class Normalized:
    """What :func:`normalize` made of a read: the ``state``, or the
    ``refusal`` that keeps the repository out (the gate, an unreadable HEAD
    with no base, a missing tip, a refs or config cap: a final harvest
    rescues it); ``warnings``, the lines that are also warning events;
    ``dropped``, every other line to log. No line holds a value the sanitizer removed."""

    state: RepositoryState | None
    refusal: str | None = None
    warnings: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()


def _base_refs(base: RepositoryState | None) -> dict[str, str]:
    body = None if base is None else base.part("refs")
    return parse_refs(body or b"")


def parse_refs(body: bytes) -> dict[str, str]:
    """The ``refs`` part as ``{name: value}``; a value is 40-hex or ``ref: <name>``."""
    refs = {}
    for line in body.decode("utf-8").split("\n"):
        if line:
            value, _, name = line.rpartition(" ")
            refs[name] = value
    return refs


def format_refs(refs: Mapping[str, str]) -> bytes:
    """The ``refs`` part of ``{name: value}``: ``<value> <name>`` lines,
    byte-sorted by name."""
    return "".join(f"{refs[name]} {name}\n" for name in sorted(refs, key=lambda n: n.encode("utf-8"))).encode("utf-8")


def resolve_ref(refs: Mapping[str, str], value: str | None) -> str | None:
    """The sha a ref value comes to through symbolic refs, or None."""
    for _ in range(5):
        if value is None:
            return None
        target = symref(value)
        if target is None:
            return value if HEX40.fullmatch(value) else None
        value = refs.get(target)
    return None


def _in_operation(progress: Mapping[str, bytes], entries: Sequence[IndexEntry]) -> bool:
    return (any(name in progress for name in _OPERATION_HEADS)
            or any(progress_set(name) in OPERATION_DIRS for name in progress)
            or any(entry.stage for entry in entries))


def progress_shas(name: str, body: bytes) -> set[str]:
    """The full oids a progress file names that are roots (§3.3, Roots):
    none for a file that names no commit (a message, a todo, a patch)."""
    if name not in _PROGRESS_ROOTS:
        return set()
    return {token for token in body.decode("ascii", "replace").split() if HEX40.fullmatch(token)}


def _drop_set(progress: dict[str, bytes], name: str) -> list[str]:
    group = progress_set(name)
    gone = [other for other in progress if progress_set(other) == group]
    for other in gone:
        del progress[other]
    return gone


def normalize(
    read: GitdirRead,
    objects: Objects,
    *,
    base: RepositoryState | None = None,
    secrets: Iterable[str] = (),
    kept_out: Iterable[str] = (),
    now: float | None = None,
) -> Normalized:
    """Turn a read into the normalized state: base values for a bad HEAD
    and bad refs; the operation index; the root filter (a missing tip
    refuses, a missing index sha drops ``index`` alone, any other missing
    root goes with its reflog line, progress set or shallow line); reflog
    expiry; the sanitizer (``secrets``: the bound accounts' secret forms);
    the caps; the canonical parts and their hash. ``objects`` holds every
    sha the read names (the quarantine, with the store behind it);
    ``kept_out`` is the harvest's own part (§4.6)."""
    if read.refusal is not None:
        return Normalized(None, read.refusal, tuple(read.warnings), tuple(read.lines))
    lines = list(read.lines)
    warnings = list(read.warnings)
    base_head = None if base is None else base.manifest()["head"]
    head = read.head if read.head is not None else base_head
    if head is None:
        return Normalized(None, "HEAD unreadable", tuple(warnings), tuple(lines))
    refs = dict(read.refs)
    base_refs = _base_refs(base)
    for name in sorted(read.bad_refs):
        if name in base_refs:
            refs[name] = base_refs[name]
        else:
            lines.append(f"ref {name} dropped (no base value)")
    progress = dict(read.progress)
    reflogs = {name: list(entries) for name, entries in read.reflogs.items()}
    shallow_lines = (read.shallow or b"").decode("ascii", "replace").split("\n")
    shallow = sorted({line for line in shallow_lines if HEX40.fullmatch(line)})
    if any(line and not HEX40.fullmatch(line) for line in shallow_lines):
        lines.append("shallow: malformed lines dropped")

    index = _index_part(read, objects, head, refs, progress, warnings, lines)

    # The root filter (§4.8 step 2): every sha the state names must exist.
    detached = head if HEX40.fullmatch(head) else None
    tips = {name: value for name, value in refs.items() if HEX40.fullmatch(value)}
    wanted = set(tips.values()) | ({detached} if detached else set()) | set(shallow)
    wanted |= {sha for entries in reflogs.values() for e in entries for sha in (e.old, e.new) if sha != NULL_OID}
    wanted |= {sha for name, body in progress.items() for sha in progress_shas(name, body)}
    wanted |= _index_roots(index or b"")
    try:
        present = objects.exists(wanted)
    except git_host.GitFailed as exc:
        return Normalized(None, f"objects unreadable: {exc}", tuple(warnings), tuple(lines))
    for name, sha in sorted(tips.items()):
        if sha not in present:
            return Normalized(None, f"history incomplete: {name} names a missing object",
                              tuple(warnings), tuple(lines))
    if detached and detached not in present:
        return Normalized(None, "history incomplete: HEAD names a missing object", tuple(warnings), tuple(lines))
    if index and not _index_roots(index) <= set(present):
        index = None
        warnings.append(f"index: objects missing; {STAGED_RESOLUTIONS}")
    for name in sorted(reflogs):
        kept = [e for e in reflogs[name] if {e.old, e.new} - {NULL_OID} <= set(present)]
        if len(kept) != len(reflogs[name]):
            lines.append(f"reflog {name}: {len(reflogs[name]) - len(kept)} entries naming missing objects dropped")
            reflogs[name] = kept
    for name in sorted(progress):
        if name in progress and not progress_shas(name, progress[name]) <= set(present):
            gone = _drop_set(progress, name)
            lines.append(f"progress {progress_set(name)}: names a missing object; {len(gone)} files dropped")
    if not set(shallow) <= set(present):
        lines.append("shallow: lines naming missing commits dropped")
        shallow = [sha for sha in shallow if sha in present]

    clock = time.time() if now is None else now
    for name in sorted(reflogs):
        reflogs[name] = _expire(name, reflogs[name], refs, head, objects, clock, lines)

    forms = _secret_forms(secrets)
    config, config_lines = sanitize_config(read.config, secrets)
    lines += config_lines
    config.sort()
    reflog_bodies = {name: _sanitize_reflog(name, entries, forms, lines) for name, entries in reflogs.items()}
    progress = {name: _redact_part(f"progress {name}", body, forms, lines) for name, body in progress.items()}

    parts: dict[str, bytes] = {
        "refs": format_refs(refs),
        "config": canonical_json([list(pair) for pair in config]) if config else b"",
    }
    for name in ("refs", "config"):
        if len(parts[name]) > PART_CAP:
            return Normalized(None, f"{name} over 1 MiB", tuple(warnings), tuple(lines))
    if index is not None and len(index) > PART_CAP:
        index = None
        warnings.append(f"index over 1 MiB; {STAGED_RESOLUTIONS}")
    if index:
        parts["index"] = index
    for name, body in reflog_bodies.items():
        parts[f"reflogs/{name}"] = _trim_reflog(name, body, warnings, lines)
    oversized = {name[len("progress/"):] for name in read.oversized if name.startswith("progress/")}
    oversized |= {name for name, body in progress.items() if len(body) > PART_CAP}
    for name in sorted(oversized):
        _drop_set(progress, name)
        warnings.append(f"progress/{name} over 1 MiB; {progress_set(name)} not kept")
    for name in sorted(read.oversized - {"index", *(f"progress/{name}" for name in oversized)}):
        warnings.append(f"{name} over 1 MiB; not kept")
    parts.update({f"progress/{name}": body for name, body in progress.items()})
    extras = {name: _redact_part(name, body, forms, lines) for name, body in read.info.items()}
    extras["shallow"] = "".join(f"{sha}\n" for sha in shallow).encode("ascii")
    extras["kept_out"] = "".join(f"{path}\n" for path in sorted(set(kept_out))).encode("utf-8")
    for name, body in extras.items():
        if len(body) > PART_CAP:
            warnings.append(f"{name} over 1 MiB; not kept")
        elif body:
            parts[name] = body
    return Normalized(make_state(head, parts), None, tuple(warnings), tuple(lines))


def _index_part(read: GitdirRead, objects: Objects, head: str, refs: Mapping[str, str],
                progress: Mapping[str, bytes], warnings: list[str], lines: list[str]) -> bytes | None:
    """The operation index of the read's index file, or None when no
    operation is under way or it cannot be read: logged, and a warning when
    an operation the index belongs to is kept without it (R4)."""
    if read.index is None:
        if "index" in read.oversized and _in_operation(progress, []):
            warnings.append(f"index over 64 MiB not read; {STAGED_RESOLUTIONS}")
        elif "index" in read.oversized:
            lines.append("index over 64 MiB not read")
        return None
    try:
        entries = objects.index(read.index, read.shared_index)
    except (git_host.GitFailed, ValueError) as exc:
        if _in_operation(progress, []):
            warnings.append(f"index unreadable ({exc}); {STAGED_RESOLUTIONS}")
        else:
            lines.append(f"index unreadable, not kept: {exc}")
        return None
    if not _in_operation(progress, entries):
        return None
    commit = resolve_ref(refs, head)
    try:
        tree = objects.tree(commit) if commit else []
    except (git_host.GitFailed, ValueError) as exc:
        lines.append(f"index not kept, HEAD's tree unreadable: {exc}")
        return None
    return operation_index(entries, tree) or None


def _expire(name: str, entries: list[ReflogEntry], refs: Mapping[str, str], head: str, objects: Objects,
            now: float, lines: list[str]) -> list[ReflogEntry]:
    """Reflog expiry as data, by git's defaults: an entry older than 90
    days goes, and one older than 30 days whose old or new sha (not the
    null oid) its ref's tip does not reach (for HEAD, no ref does); an
    unconfigured ``refs/stash`` never expires. One ``rev-list`` per reflog
    (``git reflog expire`` differs between 2.31 and 2.53; P10)."""
    if name == "refs/stash":
        return entries
    total, unreachable_before = now - EXPIRE_SECONDS, now - EXPIRE_UNREACHABLE_SECONDS
    kept = [e for e in entries if e.timestamp >= total]
    candidates = {sha for e in kept if e.timestamp < unreachable_before for sha in (e.old, e.new) if sha != NULL_OID}
    if candidates:
        if name == "HEAD":
            tips = {sha for value in [*refs.values(), head] if (sha := resolve_ref(refs, value))}
        else:
            tips = {sha} if (sha := resolve_ref(refs, refs.get(name))) else set()
        try:
            unreachable = objects.unreachable(candidates, tips)
        except git_host.GitFailed:
            lines.append(f"reflog {name}: reachability unreadable; unreachable entries kept")
            unreachable = set()
        kept = [e for e in kept if not (e.timestamp < unreachable_before and {e.old, e.new} & unreachable)]
    if len(kept) != len(entries):
        lines.append(f"reflog {name}: {len(entries) - len(kept)} expired entries dropped")
    return kept


def _sanitize_reflog(name: str, entries: Sequence[ReflogEntry], forms: Sequence[bytes], lines: list[str]) -> bytes:
    """The reflog part: each entry's line, its message (after the TAB) sanitized."""
    out, changed = [], False
    for entry in entries:
        line = entry.line
        prefix, tab, message = line.partition(b"\t")
        if tab:
            clean = _redact(_strip_text_passwords(message), forms)
            if clean != message:
                changed = True
                line = prefix + tab + clean
        out.append(line + b"\n")
    if changed:
        lines.append(f"reflog {name}: credentials removed from messages")
    return b"".join(out)


def _trim_reflog(name: str, body: bytes, warnings: list[str], lines: list[str]) -> bytes:
    """A reflog over :data:`REFLOG_CAP` without its oldest lines, keeping its
    newest; each lost stash entry logged by name."""
    if len(body) <= REFLOG_CAP:
        return body
    entries = body.split(b"\n")[:-1]
    size, keep = 0, len(entries)
    while keep and size + len(entries[keep - 1]) + 1 <= REFLOG_CAP:
        size += len(entries[keep - 1]) + 1
        keep -= 1
    lost = keep
    warnings.append(f"reflog {name} over 4 MiB; its {lost} oldest entries dropped")
    if name == "refs/stash":
        lines += [f"refs/stash: stash@{{{len(entries) - 1 - n}}} dropped (reflog over 4 MiB)" for n in range(lost)]
    return b"".join(line + b"\n" for line in entries[lost:])


def sanitize_state(state: RepositoryState, secrets: Iterable[str] = ()) -> tuple[RepositoryState, list[str]]:
    """The sanitizer again over a stored state, last on export (§3.3), with
    the secrets bound now: the config rules (1, 3, 4), the reflog message
    rules (2, 3), rule 3 over the progress and info parts. The lines name
    each removal's part and key, never its value."""
    forms = _secret_forms(secrets)
    lines: list[str] = []
    parts = {}
    for name, body in state.named_parts().items():
        if name == "config":
            config, config_lines = sanitize_config([(key, value) for key, value in json.loads(body)], secrets)
            lines += config_lines
            body = canonical_json([list(pair) for pair in sorted(config)]) if config else b""
        elif name.startswith("reflogs/"):
            entries, _, _ = parse_reflog(body)
            body = _sanitize_reflog(name[len("reflogs/"):], entries, forms, lines)
        elif name.startswith("progress/"):
            body = _redact_part(f"progress {name[len('progress/'):]}", body, forms, lines)
        elif name in INFO_PARTS:
            body = _redact_part(name, body, forms, lines)
        parts[name] = body
    return make_state(state.manifest()["head"], parts), lines


# --- roots (C7) -----------------------------------------------------------------------------------


def shallow_of(state: RepositoryState) -> set[str]:
    """The commits the state's ``shallow`` part names."""
    body = state.part("shallow") or b""
    return {line for line in body.decode("ascii", "replace").split("\n") if HEX40.fullmatch(line)}


def roots(state: RepositoryState) -> set[str]:
    """Every sha the state needs kept, the one parser of ingest, GC, export
    and copy: ref values and a detached HEAD; reflog old and new values but
    the null oid; the full oids of the progress files that name commits;
    ``shallow`` shas; the shas of ``index`` lines but gitlinks (160000),
    which name commits of another repository (P16). Messages, todo lines,
    patches, ``MERGE_RR`` and ``BISECT_LOG`` are not roots."""
    found: set[str] = set()
    head = state.manifest()["head"]
    if HEX40.fullmatch(head):
        found.add(head)
    for name, body in state.named_parts().items():
        if name == "refs":
            found |= {value for value in parse_refs(body).values() if HEX40.fullmatch(value)}
        elif name.startswith("reflogs/"):
            entries, _, _ = parse_reflog(body)
            found |= {sha for e in entries for sha in (e.old, e.new) if sha != NULL_OID}
        elif name.startswith("progress/"):
            found |= progress_shas(name[len("progress/"):], body)
        elif name == "shallow":
            found |= shallow_of(state)
        elif name == "index":
            found |= _index_roots(body)
    return found
