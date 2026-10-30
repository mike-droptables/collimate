#!/usr/bin/env python3
"""Publish this repo to the public mirror as one orphan commit.

    python3 build/push_public.py                    # a dry run, the default: every step but the push
    python3 build/push_public.py --push --message-file PATH [--tag vX.Y.Z]

The private repo reaches the public one (``PUBLIC_URL``) only through this
tool, run by the owner with their own git credentials. In order:

1. Preflight: the ref (default ``HEAD``) is contained in ``origin/main``, so
   it was pushed to the private origin, and the working tree is clean
   (``--allow-dirty`` overrides; the stage reads the commit either way).
2. Stage ``git archive <ref>`` into a scratch directory, never the working
   tree, so an ignored file (``.env``, ``node_modules``, ``api/static``)
   cannot leak.
3. Remove every entry of the ref's ``.github/sync-exclude.txt`` (a literal
   path anchored at the root) and assert each is absent; assert every nested
   ``docs/`` folder outside those entries survived, ``sdk/shared/docs/``
   among them, and that no symlink dangles or leaves the tree.
4. Scan the staged tree for secrets: the key shapes in ``SHAPES``, ``.env``
   files and ``node_modules`` content, and gitleaks's rules too when it is on
   PATH. Any hit fails.
5. Build the orphan commit (no parent, authored as the repo's git user) in a
   scratch repository holding a fetch of the public main, and assert its tree
   is the ref's tree minus the excluded paths, blob for blob. When the tree
   carries ``build/snapshot_store.py``, whose bodies CI fetches from the
   ``module-snapshot`` branch, ``origin/module-snapshot`` must hold every body
   the staged index names; it is scanned too and goes along with the commit.
6. Fresh-clone build: clone that commit and run the checks its own CI runs
   first (``checks``), against the scratch repository as its origin.
7. Print the commit, its tree and the diff stat against the public main. Only
   with ``--push``: force-push the commit to the public main, leased on the
   main fetched in step 5, atomically with the snapshot branch (a
   fast-forward) and the ``--tag``.

A release starts from that tag (``.github/workflows/release.yml``), so
``--tag`` is checked by the same ``build/release_policy.py guard`` the
release runs. On success the scratch keeps ``public.git``, whose ``publish``
branch is the commit, for inspection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
PUBLIC_URL = "git@github.com:mike-droptables/collimate.git"
EXCLUDE_FILE = ".github/sync-exclude.txt"
# The canonical authoring guides (AGENTS.md rule 4) ship, whatever the list says.
REQUIRED_DOCS = ("sdk/shared/docs",)
# The release fetches the module snapshot bodies from this branch when the tree
# carries the store that reads it.
SNAPSHOT_BRANCH = "module-snapshot"
SNAPSHOT_STORE = "build/snapshot_store.py"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# The key shapes this repo's providers issue, each found by its prefix. A
# prefixed shape must start a token (the byte before it is not [A-Za-z0-9_-]),
# so `task-...` is no `sk-` key, and its body must be key length, longer than
# the placeholders the tests use. A private key's armour header counts only
# with a body after it: the next line (past any PEM `Proc-Type:`/`DEK-Info:`
# headers and their blank line) holds at least 16 base64 characters, so code,
# help text or a placeholder that names the header is no hit, while a key with
# LF or CRLF line ends is.
SHAPES = {
    "sk-": re.compile(rb"sk-[A-Za-z0-9_-]{32,}"),            # OpenAI, Anthropic, OpenRouter
    "csk-": re.compile(rb"csk-[A-Za-z0-9_-]{32,}"),          # Cerebras
    "AIza": re.compile(rb"AIza[0-9A-Za-z_-]{35}"),            # Google
    "ghp_": re.compile(rb"ghp_[A-Za-z0-9]{36}"),              # GitHub classic token
    "gho_": re.compile(rb"gho_[A-Za-z0-9]{36}"),
    "ghu_": re.compile(rb"ghu_[A-Za-z0-9]{36}"),
    "ghs_": re.compile(rb"ghs_[A-Za-z0-9]{36}"),
    "ghr_": re.compile(rb"ghr_[A-Za-z0-9]{36}"),
    "github_pat_": re.compile(rb"github_pat_[A-Za-z0-9_]{60,}"),  # GitHub fine-grained token
    "-----BEGIN": re.compile(rb"-----BEGIN[ A-Z0-9]*PRIVATE KEY(?: BLOCK)?-----\r?\n"
                             rb"(?:(?:[A-Za-z-]+: [^\r\n]*\r?\n)+\r?\n)?[A-Za-z0-9+/=]{16}"),
}
_TOKEN_BYTE = re.compile(rb"[A-Za-z0-9_-]")
# Values third-party code publishes, by the sha256 of the value; each may stand
# only in a content-addressed module body (a file named by its own sha256).
ALLOWED = {
    # excalidraw's public Firebase web config, inside its esm.sh bundles.
    "831eae7f580f7721f62d7f654e8c071879bcafd6452d5613f8db051f15082b85": "excalidraw's Firebase web key",
}
_MODULE_BODY = re.compile(r"[0-9a-f]{64}")


class Refused(Exception):
    """A step's condition does not hold; nothing was pushed."""


def say(message: str) -> None:
    print(f"push_public: {message}", flush=True)


def git(*args: str | Path, cwd: Path | None = None, env: dict | None = None, input: str | None = None) -> str:
    result = subprocess.run(["git", *map(str, args)], check=False, cwd=cwd, env={**os.environ, **(env or {})},
                            input=input, capture_output=True, text=True)
    if result.returncode:
        raise Refused(f"git {' '.join(map(str, args))} failed: {result.stderr.strip()}")
    return result.stdout.strip()


# -- 1. preflight -----------------------------------------------------------------
def preflight(repo: Path, ref: str, allow_dirty: bool) -> str:
    try:
        sha = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=repo)
    except Refused:
        raise Refused(f"{ref} names no commit in {repo}") from None
    contained = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "refs/remotes/origin/main"],
                               check=False, cwd=repo, capture_output=True, text=True)
    if contained.returncode:
        raise Refused(f"{ref} ({sha[:12]}) is not contained in origin/main: push it to the private "
                      "origin first (or `git fetch origin` if it is there)")
    if not allow_dirty and git("status", "--porcelain", cwd=repo):
        raise Refused("the working tree has uncommitted changes; commit them, or pass --allow-dirty "
                      f"(the stage reads {sha[:12]} either way)")
    return sha


# -- 2 and 3. stage and filter ----------------------------------------------------
def read_excludes(text: str) -> list[str]:
    """The exclude list's entries, as root-relative paths without slashes.
    Every entry is a literal path anchored with a leading '/'."""
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path = line[1:].rstrip("/")
        if (not line.startswith("/") or not path or any(c in line for c in "*?[]\\")
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise Refused(f"{EXCLUDE_FILE}:{number}: {line!r} is not one literal path anchored with '/'")
        entries.append(path)
    return entries


def excluded(path: str, entries: list[str]) -> bool:
    return any(path == entry or path.startswith(entry + "/") for entry in entries)


def stage(repo: Path, sha: str, dest: Path) -> None:
    """Extract ``git archive <sha>`` into the empty ``dest``; a member that
    would land outside it (an absolute or escaping link) is refused."""
    dest.mkdir(parents=True)
    if git("rev-parse", f"{sha}^{{tree}}", cwd=repo) == EMPTY_TREE:
        return
    problem = ""
    with subprocess.Popen(["git", "archive", "--format=tar", sha], cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as archive:
        assert archive.stdout is not None and archive.stderr is not None
        try:
            with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
                tar.extractall(dest, filter="data")
        except tarfile.TarError as error:
            problem = f"{type(error).__name__}: {error}"
        archive.stdout.read()  # the rest of the stream, so git never writes into a closed pipe
        stderr = archive.stderr.read().decode().strip()
    if archive.returncode or problem:
        raise Refused(f"staging git archive {sha[:12]} failed: {stderr or problem}")


def apply_excludes(tree: Path, entries: list[str]) -> None:
    for entry in entries:
        path = tree / entry
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()
    assert_absent(tree, entries)


def assert_absent(tree: Path, entries: list[str]) -> None:
    present = [entry for entry in entries if os.path.lexists(tree / entry)]
    if present:
        raise Refused(f"excluded paths are still in the staged tree: {', '.join(present)}")


def nested_docs(tracked: list[str], entries: list[str]) -> list[str]:
    """Every ``docs`` folder below the root that the exclude list keeps."""
    found = set()
    for path in tracked:
        parts = path.split("/")
        for i in range(1, len(parts) - 1):
            if parts[i] == "docs":
                folder = "/".join(parts[:i + 1])
                if not excluded(folder, entries):
                    found.add(folder)
    return sorted(found)


def assert_present(tree: Path, folders: list[str]) -> None:
    missing = [folder for folder in sorted({*folders, *REQUIRED_DOCS})
               if not (tree / folder).is_dir() or (tree / folder).is_symlink()]
    if missing:
        raise Refused(f"nested docs folders the public tree must keep are missing: {', '.join(missing)}")


def assert_links_inside(tree: Path) -> None:
    root = tree.resolve()
    bad = []
    for path in sorted(tree.rglob("*")):
        if path.is_symlink():
            target = path.resolve()
            if not target.is_relative_to(root) or not target.exists():
                bad.append(f"{path.relative_to(tree).as_posix()} -> {os.readlink(path)}")
    if bad:
        raise Refused("symlinks that dangle or leave the staged tree (an excluded target?): " + ", ".join(bad))


# -- 4. secret scan ---------------------------------------------------------------
def _allowed(value: bytes, rel: str) -> bool:
    return (hashlib.sha256(value).hexdigest() in ALLOWED
            and _MODULE_BODY.fullmatch(PurePosixPath(rel).name) is not None)


def _redact(value: bytes) -> str:
    return value[:8].decode(errors="replace") + "…"


def scan(tree: Path) -> tuple[int, list[str]]:
    """The number of files read and every hit, as ``path[:line]: what``."""
    hits, count = [], 0
    for folder, dirs, files in os.walk(tree):
        here = Path(folder)
        for name in dirs:
            if name == "node_modules":
                hits.append(f"{(here / name).relative_to(tree).as_posix()}: node_modules content")
        dirs[:] = [name for name in dirs if name != "node_modules"]
        for name in files:
            path = here / name
            rel = path.relative_to(tree).as_posix()
            if name == ".env" or name.startswith(".env."):
                hits.append(f"{rel}: a .env file")
            if path.is_symlink():
                continue
            count += 1
            data = path.read_bytes()
            for prefix, shape in SHAPES.items():
                needle = prefix.encode()
                at = data.find(needle)
                while at != -1:
                    match = shape.match(data, at)
                    token_start = prefix != "-----BEGIN" and at and _TOKEN_BYTE.fullmatch(data[at - 1:at])
                    if match and not token_start and not _allowed(match.group(0), rel):
                        line = data.count(b"\n", 0, at) + 1
                        hits.append(f"{rel}:{line}: {prefix} shape {_redact(match.group(0))}")
                    at = data.find(needle, at + 1)
    return count, hits + gitleaks(tree)


def gitleaks(tree: Path) -> list[str]:
    """gitleaks's findings on the staged tree, when it is on PATH, less the
    ``ALLOWED`` values. Its report holds the values, so it stays beside the tree."""
    exe = shutil.which("gitleaks")
    if not exe:
        return []
    report = tree.parent / "gitleaks.json"
    run = subprocess.run([exe, "dir", ".", "--no-banner", "--exit-code", "0",
                          "--report-format", "json", "--report-path", str(report)],
                         check=False, cwd=tree, capture_output=True, text=True)
    if run.returncode:
        raise Refused(f"gitleaks failed: {run.stderr.strip()}")
    hits = []
    for finding in json.loads(report.read_text() or "[]"):
        rel = PurePosixPath(os.path.normpath(finding["File"])).as_posix()
        value = str(finding.get("Secret", "")).encode()
        if not _allowed(value, rel):
            hits.append(f"{rel}:{finding.get('StartLine')}: gitleaks {finding.get('RuleID')} {_redact(value)}")
    return hits


@dataclass
class Staged:
    tree: Path
    sha: str
    entries: list[str]
    docs: list[str]
    files: int


def prepare(repo: Path, sha: str, dest: Path) -> Staged:
    """Steps 2 to 4: the filtered, checked tree of ``sha`` at ``dest``."""
    stage(repo, sha, dest)
    exclude_file = dest / EXCLUDE_FILE
    if not exclude_file.is_file():
        raise Refused(f"{sha[:12]} has no {EXCLUDE_FILE}")
    entries = read_excludes(exclude_file.read_text())
    apply_excludes(dest, entries)
    docs = nested_docs(tracked(repo, sha), entries)
    assert_present(dest, docs)
    assert_links_inside(dest)
    files, hits = scan(dest)
    if hits:
        raise Refused("the secret scan found:\n  " + "\n  ".join(hits))
    return Staged(dest, sha, entries, docs, files)


# -- 5. the orphan commit -----------------------------------------------------------
def listing(*git_dir: str | Path, rev: str, cwd: Path | None = None) -> dict[str, str]:
    """``path -> "mode oid"`` for every blob and link of ``rev``'s tree."""
    out = subprocess.run(["git", *map(str, git_dir), "ls-tree", "-r", "-z", "--full-tree", rev],
                         cwd=cwd, capture_output=True, check=True).stdout.decode()
    entries = {}
    for record in filter(None, out.split("\0")):
        meta, path = record.split("\t", 1)
        mode, _, oid = meta.split(" ")
        entries[path] = f"{mode} {oid}"
    return entries


def tracked(repo: Path, sha: str) -> list[str]:
    return list(listing(rev=sha, cwd=repo))


def remote_ref(url: str, ref: str) -> str | None:
    out = git("ls-remote", url, ref)
    return out.split()[0] if out else None


@dataclass
class Publication:
    repo: Path
    commit: str
    tree: str
    base: str | None


def build_commit(staged: Staged, private: Path, scratch: Path, url: str, message: str) -> Publication:
    repo = scratch / "public.git"
    git("init", "--quiet", "--bare", repo)
    base = remote_ref(url, "refs/heads/main")
    if base:
        git("--git-dir", repo, "fetch", "--quiet", "--depth=1", url, "+refs/heads/main:refs/public/main")
    try:
        name, email = git("config", "user.name", cwd=private), git("config", "user.email", cwd=private)
    except Refused:
        raise Refused("the private checkout has no git user.name and user.email; the public commit "
                      "is authored as them") from None
    identity = {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER")
                for key, value in (("NAME", name), ("EMAIL", email))}
    index = {"GIT_INDEX_FILE": str(scratch / "index")}
    git("--git-dir", repo, "--work-tree", staged.tree, "add", "--all", "--force", ".", env=index)
    tree = git("--git-dir", repo, "write-tree", env=index)
    commit = git("--git-dir", repo, "commit-tree", tree, "-F", "-", env=identity, input=message)
    git("--git-dir", repo, "update-ref", "refs/heads/publish", commit)
    expected = {path: entry for path, entry in listing(rev=staged.sha, cwd=private).items()
                if not excluded(path, staged.entries)}
    actual = listing("--git-dir", repo, rev=commit)
    if actual != expected:
        differ = sorted(set(actual) ^ set(expected) | {p for p in actual if p in expected and actual[p] != expected[p]})
        raise Refused(f"the commit's tree is not {staged.sha[:12]}'s minus the excluded paths: "
                      + ", ".join(differ[:10]))
    return Publication(repo, commit, tree, base)


def snapshot_branch(staged: Staged, private: Path, publication: Publication, scratch: Path) -> str | None:
    """The private origin's module snapshot branch, when the staged tree fetches
    its bodies from it: it must hold every body the staged index names, it is
    scanned like the tree, and it is fetched into the scratch repository. The
    branch only grows, so the push fast-forwards it with its history."""
    if not (staged.tree / SNAPSHOT_STORE).is_file():
        return None
    ref = f"refs/remotes/origin/{SNAPSHOT_BRANCH}"
    sha = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], check=False, cwd=private,
                         capture_output=True, text=True).stdout.strip()
    if not sha:
        raise Refused(f"the tree carries {SNAPSHOT_STORE}, whose bodies the release fetches from the "
                      f"{SNAPSHOT_BRANCH} branch, and origin has no such branch (git fetch origin?)")
    bodies = scratch / "snapshot"
    stage(private, sha, bodies)
    index = json.loads((staged.tree / "module-snapshot/index.json").read_text())
    named = {entry["hash"] for entry in index["entries"].values()}
    held = {path.name for path in (bodies / "modules").iterdir()} if (bodies / "modules").is_dir() else set()
    if named - held:
        raise Refused(f"origin/{SNAPSHOT_BRANCH} ({sha[:12]}) lacks {len(named - held)} of the bodies the "
                      f"staged module-snapshot/index.json names: publish them (python3 {SNAPSHOT_STORE} "
                      "publish) and push the branch to origin first")
    _, hits = scan(bodies)
    if hits:
        raise Refused(f"the secret scan of {SNAPSHOT_BRANCH} found:\n  " + "\n  ".join(hits))
    shutil.rmtree(bodies)
    git("--git-dir", publication.repo, "fetch", "--quiet", private, f"+{ref}:refs/heads/{SNAPSHOT_BRANCH}")
    return sha


# -- 6. the fresh-clone build --------------------------------------------------------
def checks(tree: Path) -> list[str]:
    """What the clone runs, in the order its CI does (setup-toolchain, the
    snapshot fetch, the generated-files check), then the SDK wheel build."""
    commands = ["bash build/prepare_sdk_checkout.sh"]
    if (tree / SNAPSHOT_STORE).is_file():
        commands.append(f"git fetch --depth=1 origin {SNAPSHOT_BRANCH} && python3 {SNAPSHOT_STORE} fetch")
    return commands + ["python3 scripts/generate.py --check", "uv build --package collimate-sdk --wheel"]


def fresh_clone_build(publication: Publication, clone: Path) -> list[str]:
    git("clone", "--quiet", "--branch", "publish", publication.repo, clone)
    # The clone's own tools, not the caller's virtualenv, path or git overrides.
    env = {key: value for key, value in os.environ.items()
           if key not in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME")
           and not key.startswith("GIT_")}
    commands = checks(clone)
    for command in commands:
        say(f"[clone] $ {command}")
        if subprocess.run(["bash", "-c", command], check=False, cwd=clone, env=env).returncode:
            raise Refused(f"the fresh clone's check failed: {command} (in {clone})")
    if "uv build" in commands[-1] and len(list((clone / "dist").glob("collimate_sdk-*.whl"))) != 1:
        raise Refused(f"uv build left no single collimate_sdk wheel in {clone / 'dist'}")
    return commands


# -- 7. report and push ----------------------------------------------------------------
def check_tag(staged: Staged, tag: str, url: str) -> None:
    guard = subprocess.run([sys.executable, "build/release_policy.py", "guard"], check=False, cwd=staged.tree,
                           env={**os.environ, "EVENT_NAME": "push", "GITHUB_REF_NAME": tag},
                           capture_output=True, text=True)
    if guard.returncode:
        raise Refused(f"--tag {tag}: {guard.stderr.strip() or guard.stdout.strip()}")
    if remote_ref(url, f"refs/tags/{tag}"):
        raise Refused(f"--tag {tag}: the public repo already has that tag")


def diff_stat(publication: Publication, scratch: Path) -> str:
    base = "refs/public/main" if publication.base else EMPTY_TREE
    full = git("--git-dir", publication.repo, "diff", "--stat=120", base, publication.commit)
    (scratch / "diffstat.txt").write_text(full + "\n")
    return git("--git-dir", publication.repo, "diff", "--stat=120", "--stat-count=40", base, publication.commit)


def push(publication: Publication, url: str, snapshot: str | None, tag: str | None) -> None:
    refspecs = [f"{publication.commit}:refs/heads/main"]
    if snapshot:
        refspecs.append(f"{snapshot}:refs/heads/{SNAPSHOT_BRANCH}")
    if tag:
        refspecs.append(f"{publication.commit}:refs/tags/{tag}")
    lease = f"--force-with-lease=refs/heads/main:{publication.base or ''}"
    say(f"$ git push --atomic {lease} {url} {' '.join(refspecs)}")
    git("--git-dir", publication.repo, "push", "--atomic", lease, url, *refspecs)


def message_for(staged: Staged, path: Path | None) -> str:
    if path is None:
        version = tomllib.loads((staged.tree / "api/pyproject.toml").read_text())["project"]["version"]
        return f"Collimate {version}\n"
    text = path.read_text().strip()
    if not text:
        raise Refused(f"--message-file {path} is empty")
    return text + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="every step but the push (the default)")
    mode.add_argument("--push", action="store_true", help="force-push the orphan commit to the public main")
    parser.add_argument("--ref", default="HEAD", help="the commit to publish (default HEAD)")
    parser.add_argument("--allow-dirty", action="store_true", help="publish the ref from a dirty working tree")
    parser.add_argument("--message-file", type=Path, help="the public commit's message (required with --push)")
    parser.add_argument("--tag", help="also push this release tag (v<version>) at the commit")
    parser.add_argument("--public-url", default=PUBLIC_URL, help=f"the public repo (default {PUBLIC_URL})")
    parser.add_argument("--repo", type=Path, default=REPO, help="the private checkout (default: this one)")
    parser.add_argument("--scratch", type=Path, help="an empty working directory, not under /tmp (default "
                        "~/.cache/collimate-push/<UTC time>): hatchling drops .gitignore under /tmp")
    args = parser.parse_args(argv)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    scratch = (args.scratch or cache / "collimate-push" / stamp).resolve()
    try:
        if args.push and args.message_file is None:
            raise Refused("--push needs --message-file: the public commit's message is reviewed, not generated")
        if scratch.exists() and any(scratch.iterdir()):
            raise Refused(f"--scratch {scratch} is not empty")
        repo = args.repo.resolve()
        sha = preflight(repo, args.ref, args.allow_dirty)
        say(f"staging {args.ref} = {sha} from {repo} into {scratch}")
        staged = prepare(repo, sha, scratch / "stage")
        say("excluded and asserted absent: " + ", ".join(f"/{entry}" for entry in staged.entries))
        say("nested docs asserted present: " + ", ".join(sorted({*staged.docs, *REQUIRED_DOCS})))
        engines = "built-in shapes, .env files, node_modules" + (", gitleaks" if shutil.which("gitleaks")
                                                                  else "; gitleaks is not on PATH")
        say(f"secret scan: {staged.files} files, no hits ({engines})")
        if args.tag:
            check_tag(staged, args.tag, args.public_url)
            say(f"tag {args.tag} names the staged version and is new on the public repo")
        publication = build_commit(staged, repo, scratch, args.public_url, message_for(staged, args.message_file))
        say(f"orphan commit {publication.commit} (tree {publication.tree}, no parent) equals "
            f"{sha[:12]}'s tree minus the excluded paths")
        snapshot = snapshot_branch(staged, repo, publication, scratch)
        if snapshot:
            say(f"{SNAPSHOT_BRANCH} {snapshot}: scanned, no hits")
        commands = fresh_clone_build(publication, scratch / "clone")
        say(f"the fresh clone passed: {'; '.join(commands)}")
        stat = diff_stat(publication, scratch)
        say(f"diff against the public main {publication.base or '(none: an empty repo)'}:\n{stat}")
        for leftover in ("stage", "clone", "index", "gitleaks.json"):
            path = scratch / leftover
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        refs = "main" + (f", {SNAPSHOT_BRANCH}" if snapshot else "") + (f", tag {args.tag}" if args.tag else "")
        if not args.push:
            say(f"dry run: nothing pushed. --push would force-push {publication.commit} to "
                f"{args.public_url} ({refs}), leased on main = {publication.base or 'absent'}. "
                f"Inspect: git --git-dir {publication.repo} show --stat publish")
            return 0
        push(publication, args.public_url, snapshot, args.tag)
        say(f"pushed {publication.commit} to {args.public_url} ({refs})")
        return 0
    except Refused as refused:
        print(f"push_public: refused: {refused}", file=sys.stderr)
        if scratch.exists():
            print(f"push_public: the scratch is kept at {scratch}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
