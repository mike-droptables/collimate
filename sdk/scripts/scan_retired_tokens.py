#!/usr/bin/env python3
"""Fail on any retired name in the SDK side of the repo.

The retired names, the files each scan reads and the few places a name is
still allowed live in one data file, ``shared/retired-names.json``; every
scan skips that file by path and nothing else, so this script, its test and
every other file are scanned like any source: no name is written down twice,
and none is assembled from parts to hide it. A scan reads a file set (the
SDK's sources, or the whole SDK side of the repo) and matches its literal
``names``, its ``whole_names`` (not followed by an identifier character) and
its regular-expression ``patterns``, ignoring case where it says so. Each
scan's ``matches`` and ``spares`` are examples its test checks it hits and
spares.

Run from anywhere: ``python scripts/scan_retired_tokens.py``. It prints each
hit and exits 1 on any; ``tests/test_retired_token_scan.py`` runs it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Paths are relative to the workspace root: the SDK member is sdk/, the
# first-party trees sit beside it.
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "sdk" / "shared" / "retired-names.json"


def load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def scans(data: dict | None = None) -> dict[str, dict]:
    """The scans by id."""
    return {scan["id"]: scan for scan in (data or load())["scans"]}


def pattern(scan: dict) -> re.Pattern:
    """One expression for every name and pattern of ``scan``."""
    parts = [re.escape(name) for name in scan.get("names", [])]
    parts += [re.escape(name) + r"(?![A-Za-z0-9_])" for name in scan.get("whole_names", [])]
    parts += scan.get("patterns", [])
    return re.compile("|".join(parts), re.IGNORECASE if scan.get("ignore_case") else 0)


def files(set_name: str, root: Path = ROOT, data: dict | None = None):
    """Tracked and new files of a file set (ignored build output such as
    packages/*/dist or node_modules is not source), never the data file."""
    spec = (data or load())["file_sets"][set_name]
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *spec["roots"]],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    suffixes = set(spec.get("suffixes", []))
    excluded = tuple(spec.get("exclude", []))
    for name in sorted(set(listed)):
        path = root / name
        if path.resolve() == DATA or name.startswith(excluded) or not path.is_file():
            continue
        if suffixes and path.suffix not in suffixes:
            continue
        yield path


def allowed(scan: dict, name: str, rel: str) -> bool:
    """Whether ``name`` may stand at ``rel`` (a repo-relative path)."""
    rule = scan.get("allowed", {}).get(name)
    return bool(rule) and (rel.startswith(tuple(rule.get("under", []))) or rel.endswith(tuple(rule.get("ending", []))))


def scan(scan_id: str, root: Path = ROOT) -> list[str]:
    """Every hit of one scan, as ``path:line: match``."""
    data = load()
    spec = scans(data)[scan_id]
    expression = pattern(spec)
    hits = []
    for path in files(spec["files"], root, data):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            for match in expression.finditer(line):
                if not allowed(spec, match.group(0), rel):
                    hits.append(f"{rel}:{number}: {match.group(0)}")
    return hits


def main() -> int:
    hits = [hit for scan_id in scans() for hit in scan(scan_id)]
    for hit in hits:
        print(hit)
    if hits:
        print("retired names: hits above", file=sys.stderr)
        return 1
    print("retired token scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
