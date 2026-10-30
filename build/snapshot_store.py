#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The module snapshot's bodies, kept off main on the orphan branch ``module-snapshot``.

main commits the snapshot's lockfile, ``module-snapshot/index.json`` (a sha256
per URL), with ``LICENSES.json`` and ``builders.json``; the bodies the index
names, ``module-snapshot/modules/<sha256>``, are git-ignored there. The branch
``module-snapshot`` holds them: one tree, ``modules/<sha256>`` and nothing else,
a commit per publish. A published body is never deleted, so the branch's tip
holds every body any main commit names. This script writes the branch with git
plumbing and never checks it out; it is stdlib only.

    python3 build/snapshot_store.py publish   # after a freeze: commit the bodies the branch lacks
    python3 build/snapshot_store.py fetch     # write every body the index names into modules/
    python3 build/snapshot_store.py check     # offline: each of them is there, with its sha256

``publish`` adds every body under ``modules/`` the branch lacks (each must hash
to its name) in one commit on ``refs/heads/module-snapshot``, creating the
branch on ``origin/module-snapshot`` when only that exists, and as a root commit
when neither does; it prints the commit. Pushing the branch is the developer's.

``fetch`` reads each body the index names from the local branch, else
``origin/module-snapshot``, after ``git fetch origin module-snapshot`` when a
body is in neither; it verifies each against its sha256, rewrites a file that
differs, and removes a file under ``modules/`` that the index does not name once
the branch holds it (so the folder follows the index across checkouts). It is
idempotent, and refuses, naming it, a body the index names that exists nowhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "module-snapshot"
BRANCH = "refs/heads/module-snapshot"
REMOTE = "refs/remotes/origin/module-snapshot"
FETCH = "python3 build/snapshot_store.py fetch"
SHA256 = re.compile(r"[0-9a-f]{64}")
SHOWN = 10  # names printed before "and N more"


class StoreError(Exception):
    """A refusal, with the reason to print."""


def git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, check=False)
    if result.returncode:
        raise StoreError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def resolve(root: Path, ref: str) -> str | None:
    """The commit ``ref`` names, or None when it does not exist."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                            capture_output=True, check=False)
    return result.stdout.decode().strip() or None


def stored(root: Path, commit: str) -> dict[str, str]:
    """The bodies in ``commit``'s ``modules/``: name -> blob id."""
    bodies = {}
    for record in git(root, "ls-tree", "-z", commit, "modules/").split(b"\0"):
        if record:
            meta, _, path = record.decode().partition("\t")
            bodies[path.removeprefix("modules/")] = meta.split()[2]
    return bodies


def read_blobs(root: Path, blobs: list[str]) -> list[bytes]:
    """The bytes of each blob id, in order (one ``git cat-file --batch``)."""
    out = git(root, "cat-file", "--batch", stdin="".join(blob + "\n" for blob in blobs).encode())
    bodies, at = [], 0
    for blob in blobs:
        end = out.index(b"\n", at)
        header = out[at:end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise StoreError(f"git cat-file: {blob} is not a blob")
        size = int(header[2])
        bodies.append(out[end + 1:end + 1 + size])
        at = end + 1 + size + 1
    return bodies


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def indexed(root: Path) -> set[str]:
    """Every body name ``module-snapshot/index.json`` names."""
    path = root / SNAPSHOT / "index.json"
    try:
        entries = json.loads(path.read_bytes())["entries"]
        names = {entry["hash"] for entry in entries.values()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise StoreError(f"{path.relative_to(root)} is unreadable: {error}") from None
    if not all(isinstance(name, str) and SHA256.fullmatch(name) for name in names):
        raise StoreError(f"{path.relative_to(root)} names a body that is not a sha256")
    return names


def current(path: Path, name: str) -> bool:
    return path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == name


def listing(names) -> str:
    names = sorted(names)
    shown = ", ".join(f"modules/{name}" for name in names[:SHOWN])
    return shown + (f" and {len(names) - SHOWN} more" if len(names) > SHOWN else "")


def sources(root: Path) -> list[dict[str, str]]:
    """The bodies of the local branch, then of ``origin/module-snapshot``."""
    return [stored(root, commit) for commit in (resolve(root, BRANCH), resolve(root, REMOTE)) if commit]


def located(wanted: set[str], refs: list[dict[str, str]]) -> dict[str, str]:
    found = {}
    for bodies in refs:
        for name in wanted - found.keys():
            if name in bodies:
                found[name] = bodies[name]
    return found


def fetch(root: Path = ROOT) -> int:
    names = indexed(root)
    modules = root / SNAPSHOT / "modules"
    wanted = {name for name in names if not current(modules / name, name)}
    refs = sources(root)
    found = located(wanted, refs)
    fetched = ""
    if wanted - found.keys():
        try:
            git(root, "fetch", "--no-tags", "origin", f"+{BRANCH}:{REMOTE}")
        except StoreError as error:
            fetched = f"; {error}"
        refs = sources(root)
        found = located(wanted, refs)
    missing = wanted - found.keys()
    if missing:
        raise StoreError(f"{len(missing)} of the {len(names)} bodies {SNAPSHOT}/index.json names are on no "
                         f"module-snapshot branch, local or origin's: {listing(missing)}{fetched}. "
                         "Whoever froze them publishes them (python3 build/snapshot_store.py publish) "
                         "and pushes the branch")
    modules.mkdir(parents=True, exist_ok=True)
    order = sorted(found)
    for name, data in zip(order, read_blobs(root, [found[name] for name in order])):
        if digest(data) != name:
            raise StoreError(f"the module-snapshot branch's modules/{name} does not match its sha256")
        partial = modules / f".{name}.partial"
        partial.write_bytes(data)
        os.replace(partial, modules / name)
    held = set().union(*refs) if refs else set()
    removed = kept = 0
    for path in sorted(modules.iterdir()):
        if path.name in names:
            continue
        if (path.name in held or path.name.endswith(".partial")) and path.is_file() and not path.is_symlink():
            path.unlink()
            removed += 1
        else:
            kept += 1
            print(f"fetch: kept {path.relative_to(root)}, which the index does not name and the branch lacks")
    print(f"fetch: {len(names)} bodies in {SNAPSHOT}/modules/ ({len(found)} written, "
          f"{len(names) - len(found)} already there, {removed} unnamed removed)")
    return 0


def check(root: Path = ROOT) -> int:
    names = indexed(root)
    modules = root / SNAPSHOT / "modules"
    bad = [name for name in sorted(names) if not current(modules / name, name)]
    if bad:
        print(f"check: {len(bad)} of the {len(names)} bodies {SNAPSHOT}/index.json names are missing or "
              f"differ from their sha256: {listing(bad)}")
        print(f"check: run {FETCH}")
        return 1
    print(f"check: all {len(names)} bodies {SNAPSHOT}/index.json names are in {SNAPSHOT}/modules/")
    return 0


def publish(root: Path = ROOT) -> int:
    modules = root / SNAPSHOT / "modules"
    files = sorted(modules.iterdir()) if modules.is_dir() else []
    for path in files:
        if not SHA256.fullmatch(path.name) or not current(path, path.name):
            raise StoreError(f"{path.relative_to(root)} is not a body named by its sha256")
    local, remote = resolve(root, BRANCH), resolve(root, REMOTE)
    if local and remote and subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", remote, local],
                                           capture_output=True, check=False).returncode:
        raise StoreError("origin/module-snapshot has commits the local branch lacks: "
                         "git fetch origin module-snapshot:module-snapshot, then publish")
    parent = local or remote
    held = stored(root, parent) if parent else {}
    new = [path for path in files if path.name not in held]
    if not new:
        if not parent:
            raise StoreError(f"{SNAPSHOT}/modules/ holds no body to publish")
        if not local:
            git(root, "update-ref", "-m", "snapshot_store publish", BRANCH, parent, "")
        print(f"publish: the branch already holds all {len(files)} bodies")
        print(parent)
        return 0
    blobs = git(root, "hash-object", "-w", "--no-filters", "--stdin-paths",
                stdin="".join(f"{path}\n" for path in new).encode()).decode().split()
    entries = {**held, **{path.name: blob for path, blob in zip(new, blobs)}}
    subtree = git(root, "mktree", "-z", stdin="".join(
        f"100644 blob {blob}\t{name}\0" for name, blob in sorted(entries.items())).encode()).decode().strip()
    tree = git(root, "mktree", "-z", stdin=f"040000 tree {subtree}\tmodules\0".encode()).decode().strip()
    index = digest((root / SNAPSHOT / "index.json").read_bytes())
    message = (f"module-snapshot: {len(new)} new bodies, {len(entries)} in all\n\n"
               f"Published for {SNAPSHOT}/index.json sha256 {index}.\n")
    commit = git(root, "commit-tree", tree, *(("-p", parent) if parent else ()), "-m", message).decode().strip()
    git(root, "update-ref", "-m", "snapshot_store publish", BRANCH, commit, local or "")
    print(f"publish: {len(new)} new bodies, {len(entries)} on the branch")
    print(commit)
    return 0


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(prog="python3 build/snapshot_store.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("publish", "fetch", "check"))
    args = parser.parse_args(argv)
    try:
        return {"publish": publish, "fetch": fetch, "check": check}[args.command](root)
    except StoreError as error:
        print(f"snapshot_store {args.command}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
