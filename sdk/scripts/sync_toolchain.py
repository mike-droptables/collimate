#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Rewrite the npm manifest and workflow fields that restate a toolchain contract.

Two contracts hold the facts: ``shared/contracts/toolchain.json`` (the Node
range, the renderer runtime's React and its type packages) and
``src/collimate/renderer_bundle_contract.json`` (the renderer compiler's
esbuild; ``toolchain.json`` names it by reference). npm manifests and workflow
YAML cannot read JSON, so this script writes the contracts' values into them:

    python3 sdk/scripts/sync_toolchain.py          # rewrite, then refresh the lockfiles that disagree
    python3 sdk/scripts/sync_toolchain.py --check  # verify only; on drift, exit 1 and print the command

A rewrite replaces only the value's text, so every file keeps its formatting.
A lockfile whose root or installed entry disagrees with the contract is
refreshed with ``npm install --package-lock-only`` under the contract's Node;
without that Node the sync refuses and prints the exact commands.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
ROOT = SDK.parent
TOOLCHAIN = Path("sdk/shared/contracts/toolchain.json")
COMMAND = "python3 sdk/scripts/sync_toolchain.py"

# (manifest, key path, contract fact). "esbuild" is the bundle contract's
# esbuild_version; the other facts are toolchain.json's keys.
MANIFEST_FIELDS = (
    ("sdk/scripts/package.json", ("dependencies", "esbuild"), "esbuild"),
    ("sdk/scripts/package.json", ("devDependencies", "esbuild-wasm"), "esbuild"),
    ("sdk/packages/renderer-hooks/package.json", ("devDependencies", "react"), "react"),
    ("sdk/packages/renderer-hooks/package.json", ("devDependencies", "react-dom"), "react_dom"),
    ("sdk/packages/renderer-hooks/package.json", ("devDependencies", "@types/react"), "types_react"),
    ("sdk/packages/renderer-hooks/package.json", ("devDependencies", "@types/react-dom"), "types_react_dom"),
    ("sdk/packages/renderer-hooks/package.json", ("overrides", "esbuild"), "esbuild"),
    ("frontend/package.json", ("dependencies", "esbuild-wasm"), "esbuild"),
)
# Every node-version these files set is the contract's "node".
WORKFLOW_GLOBS = (".github/workflows/*.yml", ".github/actions/*/action.yml")
_NODE_VERSION = re.compile(
    r"^(?P<lead>[ \t-]*node-version:[ \t]*)(?P<quote>['\"]?)(?P<value>[^'\"\n#]*?)(?P=quote)(?P<tail>[ \t]*(?:#.*)?)$",
    re.M)
_DECODER = json.JSONDecoder()
_SPACE = re.compile(r"\s*")


def facts(root: Path) -> dict[str, str]:
    toolchain = json.loads((root / TOOLCHAIN).read_text(encoding="utf-8"))
    bundle = json.loads((root / "sdk" / toolchain["esbuild_version_source"]).read_text(encoding="utf-8"))
    return {**toolchain, "esbuild": bundle["esbuild_version"]}


# -- package.json ------------------------------------------------------------------
def _members(text: str, start: int):
    """(key, value start, value end) for each member of the object at ``start``."""
    i = _SPACE.match(text, start + 1).end()
    if text[i] == "}":
        return
    while True:
        key, i = json.decoder.scanstring(text, i + 1)
        i = _SPACE.match(text, i).end()
        if text[i] != ":":
            raise ValueError(f"expected ':' at offset {i}")
        i = _SPACE.match(text, i + 1).end()
        _, end = _DECODER.raw_decode(text, i)
        yield key, i, end
        i = _SPACE.match(text, end).end()
        if text[i] == "}":
            return
        i = _SPACE.match(text, i + 1).end()


def value_span(text: str, keys: tuple[str, ...]) -> tuple[int, int] | None:
    """Where the value at ``keys`` sits in a JSON document's text."""
    start = _SPACE.match(text).end()
    for depth, key in enumerate(keys):
        if text[start] != "{":
            return None
        span = next(((s, e) for name, s, e in _members(text, start) if name == key), None)
        if span is None:
            return None
        if depth == len(keys) - 1:
            return span
        start = span[0]
    return None


def _manifest_problems(root: Path, values: dict[str, str]) -> list[str]:
    problems = []
    for manifest, keys, fact in MANIFEST_FIELDS:
        data = json.loads((root / manifest).read_text(encoding="utf-8"))
        have = data
        for key in keys:
            have = have.get(key) if isinstance(have, dict) else None
        if have != values[fact]:
            problems.append(f"{manifest} {'.'.join(keys)} is {have!r}; the contract says {values[fact]!r}")
    return problems


def _lock_problems(root: Path, values: dict[str, str]) -> dict[str, list[str]]:
    """{manifest directory: problems} for each lockfile that disagrees."""
    out: dict[str, list[str]] = {}
    for manifest, (section, name), fact in MANIFEST_FIELDS:
        directory = Path(manifest).parent.as_posix()
        lock_path = f"{directory}/package-lock.json"
        packages = json.loads((root / lock_path).read_text(encoding="utf-8"))["packages"]
        want = values[fact]
        if section != "overrides":  # npm records overrides only in what they resolve
            declared = (packages[""].get(section) or {}).get(name)
            if declared != want:
                out.setdefault(directory, []).append(f"{lock_path} root {section}.{name} is {declared!r}; "
                                                     f"the contract says {want!r}")
        installed = (packages.get(f"node_modules/{name}") or {}).get("version")
        if installed != want:
            out.setdefault(directory, []).append(f"{lock_path} node_modules/{name} is {installed!r}; "
                                                 f"the contract says {want!r}")
    return out


# -- workflows ---------------------------------------------------------------------
def _workflows(root: Path) -> list[Path]:
    return sorted(path for pattern in WORKFLOW_GLOBS for path in root.glob(pattern))


def _workflow_problems(root: Path, node: str) -> list[str]:
    problems, found = [], 0
    for path in _workflows(root):
        text = path.read_text(encoding="utf-8")
        for match in _NODE_VERSION.finditer(text):
            found += 1
            if match["value"] != node:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{path.relative_to(root).as_posix()}:{line} node-version is "
                                f"{match['value']!r}; the contract says {node!r}")
    if not found:
        problems.append("no node-version in " + ", ".join(WORKFLOW_GLOBS))
    return problems


def problems(root: Path = ROOT) -> list[str]:
    values = facts(root)
    return (_manifest_problems(root, values)
            + [line for lines in _lock_problems(root, values).values() for line in lines]
            + _workflow_problems(root, values["node"]))


# -- rewriting ---------------------------------------------------------------------
def rewrite(root: Path = ROOT) -> list[str]:
    """Write the contracts' values into every dependent field; one line per edit."""
    values, edits = facts(root), []
    for manifest in dict.fromkeys(manifest for manifest, _, _ in MANIFEST_FIELDS):
        path = root / manifest
        text = original = path.read_text(encoding="utf-8")
        for name, keys, fact in MANIFEST_FIELDS:
            if name != manifest:
                continue
            span = value_span(text, keys)
            if span is None:
                raise SystemExit(f"sync_toolchain: {manifest} has no {'.'.join(keys)}; add it by hand")
            new = json.dumps(values[fact])
            if text[span[0]:span[1]] != new:
                edits.append(f"{manifest} {'.'.join(keys)}: {text[span[0]:span[1]]} -> {new}")
                text = text[:span[0]] + new + text[span[1]:]
        if text != original:
            path.write_text(text, encoding="utf-8")

    def node(match: re.Match) -> str:
        quote = match["quote"] or "'"  # a plain scalar may not start with '>'
        return f"{match['lead']}{quote}{values['node']}{quote}{match['tail']}"

    for path in _workflows(root):
        text = path.read_text(encoding="utf-8")
        new = _NODE_VERSION.sub(lambda m: m.group(0) if m["value"] == values["node"] else node(m), text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            edits.append(f"{path.relative_to(root).as_posix()} node-version -> {values['node']!r}")
    return edits


def _node_satisfies(version: str, spec: str) -> bool:
    """Whether ``version`` is inside a range of space-separated comparators
    (``>=1.2.3 <2``), alternatives split by ``||``."""
    def parts(text: str) -> tuple[int, ...]:
        numbers = [int(part) for part in text.split(".")[:3]]
        return tuple(numbers + [0] * (3 - len(numbers)))

    have = parts(version.strip().lstrip("v"))
    checks = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
              "<": lambda a, b: a < b, "=": lambda a, b: a == b, "": lambda a, b: a == b}
    for alternative in spec.split("||"):
        terms = re.findall(r"(>=|<=|>|<|=)?\s*(\d+(?:\.\d+){0,2})", alternative)
        if terms and all(checks[op](have, parts(bound)) for op, bound in terms):
            return True
    return False


def _refresh(root: Path, directories: list[str], node: str) -> str | None:
    """Run ``npm install --package-lock-only`` in each directory; the reason it
    could not, else None."""
    npm, binary = shutil.which("npm"), shutil.which("node")
    if npm is None or binary is None:
        return "npm or node is not on PATH"
    have = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout.strip()
    if not _node_satisfies(have, node):
        return f"Node {have} is outside the contract's {node}"
    for directory in directories:
        print(f"sync_toolchain: npm install --package-lock-only in {directory}", flush=True)
        result = subprocess.run([npm, "install", "--package-lock-only"], cwd=root / directory)
        if result.returncode:
            return f"npm install --package-lock-only failed in {directory}"
    return None


def main(argv: list[str]) -> int:
    if argv not in ([], ["--check"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if argv == ["--check"]:
        found = problems()
        for line in found:
            print(line)
        if found:
            print(f"The toolchain fields differ from the contracts. Run: {COMMAND}")
            return 1
        print("sync_toolchain: every manifest, lockfile and workflow matches the contracts")
        return 0
    for line in rewrite():
        print("sync_toolchain: " + line)
    values = facts(ROOT)
    stale = sorted(_lock_problems(ROOT, values))
    if stale:
        print("sync_toolchain: lockfiles to refresh: " + ", ".join(f"{d}/package-lock.json" for d in stale),
              flush=True)
        reason = _refresh(ROOT, stale, values["node"])
        if reason:
            print(f"sync_toolchain: refused: {reason}. Under Node {values['node']} run:")
            for directory in stale:
                print(f"  (cd {directory} && npm install --package-lock-only)")
            return 1
    left = problems()
    for line in left:
        print(line)
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
