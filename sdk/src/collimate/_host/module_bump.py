# SPDX-License-Identifier: MIT
"""Move the shipped renderers' exact upstream pins: ``python -m collimate._host.module_mirror bump``.

A pin is an ``https://esm.sh/<package>@<version>`` or
``https://cdn.jsdelivr.net/npm/<package>@<version>`` literal with an exact
version, or a ``<package>@<version>`` inside such a URL's ``deps=`` list, in
the shipped sources of a renderer or built-in package (its non-test code and
its ``mirror_assets.json``), or the runtime's React: ``react`` and
``react_dom`` in ``sdk/shared/contracts/toolchain.json``, which the runtime
build reads. For each pinned package the npm registry's ``latest``
dist-tag is the candidate. A move is a
patch, minor or major by semver, with npm's caret rule for 0.x releases (a
change in the leading non-zero component is major: 0.14 to 0.15 is major).
Patch and minor moves are applied by default; a major only with ``major``,
else the package moves to the newest release on its own major line.

Packages that must load as one move together, as a family: every npm scope
(``@milkdown/*``, ``@embedpdf/*``, ``@xterm/*``, ``@assistant-ui/*``) and
:data:`FAMILIES` (react with react-dom). Members pinned at one shared
version move to one version every member publishes; members on their own
versions each move to their newest, together or not at all. Any move whose
registry metadata declares another pinned package with a range the other's
resulting version falls outside is skipped, with its family (the declared
range wins, so the dependency stays). A package pinned only inside ``deps=``
lists moves with its family (``@milkdown/core`` with the ``@milkdown/*`` URLs);
one whose family no URL pins (``prosemirror-model``, ``codemirror``,
``radix-ui``: floors a list holds on purpose) moves only when ``only`` names
it.

Applying rewrites every occurrence of ``<package>@<old>`` in those files,
inside ``deps=`` lists too, so a family moves in its URLs and in every list
that pins it at once, and in the docs that quote the pins (:data:`DOC_GLOBS`,
never read for pins). A moved runtime React is written to ``toolchain.json``;
then ``sdk/scripts/sync_toolchain.py`` rewrites ``sdk/packages/renderer-hooks``'
manifest and lockfile from it, ``npm ci`` installs it there and the renderer
runtime is rebuilt from it. The bump refreezes the module snapshot with every
floating key re-resolved, so the whole graph resolves at one moment and a
package shared through ranges stays one version, and after a doc edit runs
``sdk/scripts/sync_skill_references.py``. Its rewrite never touches
``node_modules``, test fixtures or snapshot bodies.

A bump is atomic with its freeze: a moved family whose new roots are still
not frozen afterwards (upstream could not build them, and no local esm.sh
did) is reverted, the rest re-planned around it (a declared range it binds
holds the others too), and the snapshot frozen again; the command then says
which family it reverted and exits non-zero. ``esm_builder`` is the
freeze's local esm.sh (``module_mirror.freeze``).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import module_mirror

REGISTRY = "https://registry.npmjs.org/"
# Relative to the workspace root, like every location a bump reports.
SDK_DIR = Path("sdk")
# The runtime's React: its npm names and their toolchain.json keys.
TOOLCHAIN = SDK_DIR / "shared" / "contracts" / "toolchain.json"
RUNTIME_PINS = {"react": "react", "react-dom": "react_dom"}
SYNC_TOOLCHAIN = SDK_DIR / "scripts" / "sync_toolchain.py"
RENDERER_HOOKS = SDK_DIR / "packages" / "renderer-hooks"
RUNTIME_BUILD = SDK_DIR / "scripts" / "build_renderer_runtime.mjs"
# Docs that quote the shipped pins: rewritten with them, never read for pins.
DOC_GLOBS = ("sdk/shared/docs/*.md", "content/renderers/README.md", "sdk/packages/renderer-hooks/README.md")
SYNC_SKILL_REFERENCES = SDK_DIR / "scripts" / "sync_skill_references.py"
# Families beyond a shared npm scope: their first member names the family.
FAMILIES: tuple[tuple[str, ...], ...] = (("react", "react-dom"),)

_NAME = r"(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*"
_VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"
_END = r"(?![0-9A-Za-z_]|\.[0-9A-Za-z])"
# A pin: the package an exact-version upstream URL names.
_PIN = re.compile(r"https://(?:esm\.sh|cdn\.jsdelivr\.net/npm)/(" + _NAME + r")@(" + _VERSION + r")" + _END)
# A ``deps=`` list on an upstream URL, up to the next query parameter.
_DEPS = re.compile(r"https://(?:esm\.sh|cdn\.jsdelivr\.net/npm)/[^'\"`\s?]*\?(?:[^'\"`\s]*&)?deps=([^&'\"`\s]+)")
_DEPS_ENTRY = re.compile(r"(" + _NAME + r")@(" + _VERSION + r")")
# Where a pin sits only inside a ``deps=`` list: ``<file>:<line> (deps=)``.
DEPS_MARK = " (deps=)"
# Any ``<package>@<version>`` token, so ``react@1`` inside ``@scope/react@1``
# is the scoped package, never react.
_TOKEN = re.compile(r"(?<![\w.-])(" + _NAME + r")@(" + _VERSION + r")" + _END)
_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")


# -- semver ----------------------------------------------------------------------
def parse_version(version: str) -> tuple[int, int, int, str] | None:
    match = _SEMVER.fullmatch(version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4) or ""


def _stable(version: str) -> tuple[int, int, int] | None:
    parsed = parse_version(version)
    return parsed[:3] if parsed and not parsed[3] else None


def classify(old: str, new: str) -> str:
    """``patch``, ``minor`` or ``major`` for a move from ``old`` to ``new``.
    For 0.x the leading non-zero component is the major (npm's caret rule)."""
    o, n = parse_version(old), parse_version(new)
    if o is None or n is None:
        return "major"
    if n[0] != o[0] or (o[0] == 0 and n[1] != o[1]) or (o[0] == 0 and o[1] == 0 and n[2] != o[2]):
        return "major"
    return "minor" if n[1] != o[1] else "patch"


def _partial(text: str) -> list[int | None]:
    parts = text.split(".")
    out: list[int | None] = []
    for part in parts[:3]:
        out.append(None if part in ("x", "X", "*", "") else int(part))
    while len(out) < 3:
        out.append(None)
    return out


def _bounds(op: str, text: str) -> list[tuple[str, tuple[int, int, int]]]:
    """The comparator set a single range term stands for."""
    core = re.split(r"[-+]", text, maxsplit=1)[0]
    major, minor, patch = _partial(core)
    if major is None:
        return []
    low = (major, minor or 0, patch or 0)
    if op == "^":
        if major > 0 or minor is None:
            high = (major + 1, 0, 0)
        elif minor > 0 or patch is None:
            high = (0, minor + 1, 0)
        else:
            high = (0, 0, patch + 1)
        return [(">=", low), ("<", high)]
    if op == "~":
        high = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        return [(">=", low), ("<", high)]
    if minor is None or patch is None:  # 1, 1.x, 1.2: an x-range
        high = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
        if op in ("", "="):
            return [(">=", low), ("<", high)]
        if op == ">":
            return [(">=", high)]
        if op == "<=":
            return [("<", high)]
        return [(op, low)]
    return [(op or "=", low)]


def _holds(version: tuple[int, int, int], op: str, bound: tuple[int, int, int]) -> bool:
    return {"=": version == bound, ">": version > bound, ">=": version >= bound,
            "<": version < bound, "<=": version <= bound}[op]


def satisfies(version: str, spec: str) -> bool:
    """Whether a stable ``version`` is inside an npm range ``spec``. A spec
    this does not understand (a tag, a URL, an alias) is satisfied: only a
    range that clearly excludes the version blocks a move."""
    parsed = _stable(version)
    if parsed is None:
        return True
    try:
        for alternative in spec.split("||"):
            alternative = alternative.strip()
            hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", alternative)
            if hyphen:
                terms = [(">=", hyphen.group(1)), ("<=", hyphen.group(2))]
            else:
                terms = []
                for token in re.findall(r"(\^|~>?|>=|<=|>|<|=)?\s*([^\s^~<>=]+)", alternative):
                    terms.append((token[0].replace("~>", "~"), token[1]))
            if not terms:
                return True  # "" and "*" admit everything
            bounds = [bound for op, text in terms for bound in _bounds(op, text.lstrip("v"))]
            if all(_holds(parsed, op, bound) for op, bound in bounds):
                return True
        return False
    except (ValueError, KeyError):
        return True


# -- pins ------------------------------------------------------------------------
def shipped_files(workspace: Path) -> list[Path]:
    """The files a bump reads and rewrites: each package's shipped code and
    ``mirror_assets.json``."""
    workspace = Path(workspace)
    files: list[Path] = []
    for package in module_mirror.shipped_packages(workspace):
        files.extend(module_mirror.package_sources(package))
        if (package / module_mirror.MIRROR_ASSETS).is_file():
            files.append(package / module_mirror.MIRROR_ASSETS)
    return files


def doc_files(workspace: Path) -> list[Path]:
    """The docs a bump rewrites with the pins they quote (:data:`DOC_GLOBS`)."""
    return sorted(path for pattern in DOC_GLOBS for path in Path(workspace).glob(pattern))


def _toolchain_key(key: str) -> re.Pattern:
    return re.compile(r'^([ \t]*"' + re.escape(key) + r'"[ \t]*:[ \t]*")([^"]*)(")', re.M)


def find_pins(workspace: Path) -> dict[str, dict[str, list[str]]]:
    """{package: {exact version: [file:line, …]}} over :func:`shipped_files`:
    each URL pin, and each exact entry of a URL's ``deps=`` list (its
    location ends in :data:`DEPS_MARK`); and the runtime's React from
    ``toolchain.json``."""
    workspace = Path(workspace)
    pins: dict[str, dict[str, list[str]]] = {}
    for path in shipped_files(workspace):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{path.relative_to(workspace).as_posix()}:{number}"
            for match in _PIN.finditer(line):
                pins.setdefault(match.group(1), {}).setdefault(match.group(2), []).append(where)
            for deps in _DEPS.finditer(line):
                for entry in deps.group(1).split(","):
                    exact = _DEPS_ENTRY.fullmatch(entry)
                    if exact:
                        pins.setdefault(exact.group(1), {}).setdefault(exact.group(2), []).append(where + DEPS_MARK)
    if (workspace / TOOLCHAIN).is_file():
        text = (workspace / TOOLCHAIN).read_text(encoding="utf-8")
        for name, key in RUNTIME_PINS.items():
            match = _toolchain_key(key).search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                pins.setdefault(name, {}).setdefault(match.group(2), []).append(f"{TOOLCHAIN.as_posix()}:{line}")
    return pins


def family_of(name: str) -> str:
    for family in FAMILIES:
        if name in family:
            return family[0]
    return name.split("/", 1)[0] if name.startswith("@") else name


# -- the registry ----------------------------------------------------------------
class RegistryError(Exception):
    """A package the registry does not know, or could not answer for."""


def _fetch_packument(name: str) -> dict:
    """The registry's abbreviated metadata for ``name`` (the tests replace this)."""
    request = urllib.request.Request(
        REGISTRY + name.replace("/", "%2F"),
        headers={"User-Agent": module_mirror.CONTRACT["user_agent"],
                 "Accept": "application/vnd.npm.install-v1+json"})
    try:
        with urllib.request.urlopen(request, timeout=float(module_mirror.CONTRACT["timeout_seconds"])) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RegistryError("not on the npm registry" if error.code == 404 else f"npm registry HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise RegistryError(f"npm registry unreachable: {error}") from None


@dataclass
class Package:
    name: str
    olds: tuple[str, ...]
    where: tuple[str, ...]
    family: str
    latest: str | None = None
    versions: dict = field(default_factory=dict)
    target: str | None = None
    move_class: str = ""
    status: str = ""  # "applied" / "skipped" / "current"
    reason: str = ""

    @property
    def moves(self) -> bool:
        return self.status == "applied"

    @property
    def in_deps_only(self) -> bool:
        """Pinned only inside ``deps=`` lists, never on a URL of its own."""
        return bool(self.where) and all(where.endswith(DEPS_MARK) for where in self.where)

    def declared(self, version: str) -> dict[str, str]:
        meta = self.versions.get(version) or {}
        return {**(meta.get("dependencies") or {}), **(meta.get("peerDependencies") or {})}


def _newest(package: Package, versions: Iterable[str], allow_major: bool, ceiling: tuple | None) -> str | None:
    old = max(_stable(version) or (0, 0, 0) for version in package.olds)
    candidates = [v for v in versions if (s := _stable(v)) and s > old and (ceiling is None or s <= ceiling)]
    if not allow_major:
        candidates = [v for v in candidates if all(classify(o, v) != "major" for o in package.olds)]
    return max(candidates, key=_stable) if candidates else None


def _class_of(package: Package, target: str) -> str:
    order = ("patch", "minor", "major")
    return max((classify(old, target) for old in package.olds), key=order.index)


def plan(pins: dict[str, dict[str, list[str]]], *, allow_major: bool = False,
         only: Iterable[str] = (), fetch: Callable[[str], dict] | None = None,
         hold: Mapping[str, str] | None = None) -> list[Package]:
    """Every pinned package with its target and verdict, in table order.
    ``only`` limits the moves to those packages and their families; every
    pinned package's declared ranges still hold them. ``hold`` maps families
    a bump reverted to the reason: they stay, and so does any move a range
    they declare binds."""
    fetch = fetch or _fetch_packument
    packages = {name: Package(name, tuple(sorted(found, key=lambda v: _stable(v) or (0, 0, 0))),
                              tuple(where for spots in found.values() for where in spots), family_of(name))
                for name, found in sorted(pins.items())}
    wanted = set(only)
    unknown = [Package(name, (), (), family_of(name), status="skipped", reason="not pinned in the shipped sources")
               for name in sorted(wanted - set(packages))]
    families = {packages[name].family for name in wanted & set(packages)}
    for package in packages.values():
        try:
            document = fetch(package.name)
            package.versions = document.get("versions") or {}
            package.latest = (document.get("dist-tags") or {}).get("latest")
            if not package.latest or not package.versions:
                raise RegistryError("the registry lists no latest release")
        except RegistryError as error:
            package.status, package.reason = "skipped", str(error)
        except Exception as error:  # noqa: BLE001 — any fetch failure is reported, not raised
            package.status, package.reason = "skipped", f"npm registry: {error}"
    groups: dict[str, list[Package]] = {}
    for package in packages.values():
        if not wanted or package.family in families:
            groups.setdefault(package.family, []).append(package)
    for family, members in groups.items():
        if family not in families and all(member.in_deps_only for member in members):
            for member in members:
                if member.status != "skipped":
                    member.status = "skipped"
                    member.reason = f"pinned only inside deps= lists; --only {member.name} moves it"
            continue
        _plan_family(family, members, allow_major)
        if hold and family in hold:
            for member in members:
                if member.moves:
                    member.status, member.reason = "reverted", hold[family]
    _hold_declared_ranges(list(packages.values()), groups)
    chosen = [package for members in groups.values() for package in members]
    return unknown + sorted(chosen, key=lambda p: (p.family, p.name))


def _plan_family(family: str, members: list[Package], allow_major: bool) -> None:
    failed = [member for member in members if member.status == "skipped"]
    if failed:
        for member in members:
            if member not in failed:
                member.status, member.reason = "skipped", f"family {family}: {failed[0].name}: {failed[0].reason}"
        return
    olds = {old for member in members for old in member.olds}
    if len(members) > 1 and len(olds) == 1:
        # One shared version: one version every member publishes, no newer
        # than any member's latest.
        ceiling = min(_stable(member.latest) or (0, 0, 0) for member in members)
        common = set.intersection(*(set(member.versions) for member in members))
        target = _newest(members[0], common, allow_major, ceiling)
        newest = _newest(members[0], common, True, ceiling)
        choices = {member.name: (target, newest) for member in members}
    else:
        choices = {member.name: (_newest(member, member.versions, allow_major, _stable(member.latest)),
                                 _newest(member, member.versions, True, _stable(member.latest)))
                   for member in members}
    for member in members:
        target, newest = choices[member.name]
        member.target = target
        if target is None:
            member.status = "skipped" if newest else "current"
            member.reason = f"{newest} is a major; pass --major" if newest else "up to date"
        else:
            member.move_class = _class_of(member, target)
            member.status = "applied"
            member.reason = f"{newest} is a major; pass --major" if newest != target else ""
        if len(members) > 1:
            member.reason = "; ".join(part for part in (f"family {family}", member.reason) if part)


def _graphs(package: Package) -> set[str]:
    """The module graphs a pin is part of: each package folder pinning it
    (``content/renderers/<cell>/<name>``, ``builtins/<cell>/<name>``), or
    every graph for the runtime's pins."""
    graphs = set()
    for where in package.where:
        path = where.rsplit(":", 1)[0]
        if path == TOOLCHAIN.as_posix():
            graphs.add("*")
            continue
        tree = next(tree for tree in module_mirror.SHIPPED_TREES if path.startswith(tree + "/"))
        graphs.add("/".join(path.split("/")[:tree.count("/") + 3]))
    return graphs


def _shares_graph(dependent: Package, dependency: Package) -> bool:
    """Whether a declared range binds: the two load in one module graph (one
    family, one package folder, or the runtime's, which every renderer loads),
    or the dependency is a package every graph must share."""
    if dependent.family == dependency.family or module_mirror._single_instance(dependency.name):
        return True
    one, other = _graphs(dependent), _graphs(dependency)
    return "*" in one or "*" in other or bool(one & other)


def _hold_declared_ranges(packages: list[Package], groups: dict[str, list[Package]]) -> None:
    """Skip moves that fall outside a range another pinned package in the
    same module graph declares for them, until none does; a move's family is
    skipped with it."""
    by_name = {package.name: package for package in packages}

    def final(package: Package) -> tuple[str, ...]:
        return (package.target,) if package.moves and package.target else package.olds

    def block(package: Package, reason: str) -> None:
        for member in groups.get(package.family, [package]):
            if member.moves:
                member.status, member.reason = "skipped", reason

    changed = True
    while changed:
        changed = False
        for dependent in packages:
            for dependent_version in final(dependent):
                for name, spec in dependent.declared(dependent_version).items():
                    dependency = by_name.get(name)
                    if dependency is None or dependency is dependent or not _shares_graph(dependent, dependency):
                        continue
                    outside = [v for v in final(dependency) if not satisfies(v, spec)]
                    if not outside:
                        continue
                    if dependency.moves:
                        block(dependency, f"{dependent.name}@{dependent_version} declares {name} {spec}")
                    elif dependent.moves:
                        block(dependent, f"{dependent.name}@{dependent_version} declares {name} {spec}; "
                                         f"{name} stays {', '.join(outside)}")
                    else:
                        continue  # the pins already disagreed; not this bump's doing
                    changed = True
                    break
                if changed:
                    break
            if changed:
                break


# -- applying ----------------------------------------------------------------------
def rewrite(workspace: Path, moves: dict[str, tuple[tuple[str, ...], str]]) -> list[Path]:
    """Rewrite every ``<package>@<old>`` token in :func:`shipped_files` and
    :func:`doc_files` to ``<package>@<target>``, and a moved runtime React in
    ``toolchain.json``; returns the files changed."""
    workspace = Path(workspace)
    changed: list[Path] = []

    def replace(match: re.Match) -> str:
        name, version = match.group(1), match.group(2)
        move = moves.get(name)
        return f"{name}@{move[1]}" if move and version in move[0] else match.group(0)

    for path in [*shipped_files(workspace), *doc_files(workspace)]:
        text = path.read_text(encoding="utf-8")
        new = _TOKEN.sub(replace, text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path)
    toolchain = workspace / TOOLCHAIN
    if toolchain.is_file():
        text = new = toolchain.read_text(encoding="utf-8")
        for name, key in RUNTIME_PINS.items():
            move = moves.get(name)
            if move:
                new = _toolchain_key(key).sub(
                    lambda m, move=move: m.group(1) + move[1] + m.group(3) if m.group(2) in move[0] else m.group(0),
                    new, count=1)
        if new != text:
            toolchain.write_text(new, encoding="utf-8")
            changed.append(toolchain)
    return changed


def _prepare_runtime(workspace: Path) -> None:
    """Carry ``toolchain.json``'s React into the runtime: ``sync_toolchain.py``
    rewrites ``sdk/packages/renderer-hooks``' manifest and lockfile from it,
    ``npm ci`` installs it there, and ``sdk/shared/renderer-runtime`` is
    rebuilt from that install."""
    workspace = Path(workspace)
    subprocess.run([sys.executable, str(workspace / SYNC_TOOLCHAIN)], cwd=workspace, check=True)
    subprocess.run(["npm", "ci"], cwd=workspace / RENDERER_HOOKS, check=True)
    subprocess.run(["node", str(workspace / RUNTIME_BUILD)], cwd=workspace / SDK_DIR, check=True)


def _sync_skill_references(workspace: Path) -> None:
    """Regenerate the guides' mirrors after a doc edit."""
    workspace = Path(workspace)
    subprocess.run([sys.executable, str(workspace / SYNC_SKILL_REFERENCES)], cwd=workspace, check=True)


def _refreeze(workspace: Path, log: Callable[[str], None], esm_builder: str | None = None) -> list[str]:
    module_mirror.freeze(workspace, refresh_floating=True, esm_builder=esm_builder, log=log)
    return module_mirror.snapshot_problems(workspace)


def _unfrozen_roots(workspace: Path) -> dict[str, str]:
    """The snapshot's roots it holds no entry for, with the reason."""
    try:
        snapshot = module_mirror.read_snapshot(Path(workspace) / module_mirror.SNAPSHOT_DIR)
    except (ValueError, OSError):
        return {}
    return {root: snapshot.unavailable.get(root, "not frozen") for root in snapshot.roots
            if root not in snapshot.entries}


def _failed_families(moves: Mapping[str, tuple[tuple[str, ...], str]], unfrozen: Mapping[str, str]) -> dict[str, str]:
    """{family: reason} for each moved family a still-unfrozen root names at
    its new version (as a URL pin or inside a ``deps=`` list)."""
    failed: dict[str, list[str]] = {}
    for root in sorted(unfrozen):
        for match in _TOKEN.finditer(root):
            name, version = match.group(1), match.group(2)
            if name in moves and moves[name][1] == version:
                failed.setdefault(family_of(name), [])
                if root not in failed[family_of(name)]:
                    failed[family_of(name)].append(root)
    return {family: (f"reverted: {len(roots)} root(s) still unfrozen after the freeze, e.g. "
                     f"{roots[0]}: {unfrozen[roots[0]]}") for family, roots in failed.items()}


def table(rows: list[Package], *, dry_run: bool) -> str:
    header = ("package", "old", "new", "class", "latest", "result")
    lines = [header]
    for row in rows:
        verdict = "would apply" if dry_run and row.status == "applied" else row.status
        result = verdict + (f" ({row.reason})" if row.reason else "")
        lines.append((row.name, ", ".join(row.olds) or "-", row.target if row.moves else "-",
                      row.move_class if row.moves else "-", row.latest or "-", result))
    widths = [max(len(line[i]) for line in lines) for i in range(5)]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line[:5])) + "  " + line[5]
                     for line in lines)


def bump(workspace: Path, *, allow_major: bool = False, dry_run: bool = False, only: Iterable[str] = (),
         esm_builder: str | None = None, log: Callable[[str], None] = print) -> int:
    """Plan, print and (unless ``dry_run``) apply a bump; 1 when the refrozen
    snapshot has problems, a family was reverted (its new roots stayed
    unfrozen), or the runtime's preparation or the guides' mirrors failed."""
    workspace = Path(workspace)
    pins = find_pins(workspace)
    documents: dict[str, dict] = {}

    def fetch(name: str) -> dict:
        if name not in documents:
            documents[name] = _fetch_packument(name)
        return documents[name]

    held: dict[str, str] = {}
    rows = plan(pins, allow_major=allow_major, only=only, fetch=fetch)
    log(table(rows, dry_run=dry_run))
    moves = {row.name: (row.olds, row.target) for row in rows if row.moves and row.target}
    if dry_run or not moves:
        log("bump: dry run, nothing written" if dry_run else "bump: nothing to move")
        return 0
    toolchain = workspace / TOOLCHAIN
    docs = doc_files(workspace)
    originals = {path: path.read_bytes()
                 for path in [*shipped_files(workspace), *docs, toolchain] if path.is_file()}
    # The toolchain.json bytes renderer-hooks' manifest, install and the runtime were last prepared from.
    prepared = originals.get(toolchain)
    while True:
        for path, data in originals.items():
            if path.read_bytes() != data:
                path.write_bytes(data)
        changed = rewrite(workspace, moves) if moves else []
        log(f"bump: rewrote {len(changed)} file(s): " + ", ".join(p.relative_to(workspace).as_posix() for p in changed))
        if toolchain in originals and toolchain.read_bytes() != prepared:
            try:
                _prepare_runtime(workspace)
            except (OSError, subprocess.CalledProcessError) as error:
                log(f"bump: the renderer runtime was not prepared ({error}); {TOOLCHAIN.as_posix()} holds the "
                    f"runtime's React: run python3 {SYNC_TOOLCHAIN.as_posix()}, npm ci in "
                    f"{RENDERER_HOOKS.as_posix()}, node {RUNTIME_BUILD.as_posix()}, then "
                    "python -m collimate._host.module_mirror freeze")
                return 1
            prepared = toolchain.read_bytes()
        problems = _refreeze(workspace, log, esm_builder)
        failed = _failed_families(moves, _unfrozen_roots(workspace)) if moves else {}
        if not failed:
            break
        for family, reason in sorted(failed.items()):
            log(f"bump: {family} {reason}")
        held.update(failed)
        rows = plan(pins, allow_major=allow_major, only=only, fetch=fetch, hold=held)
        moves = {row.name: (row.olds, row.target) for row in rows if row.moves and row.target}
    if held:
        log(table(rows, dry_run=False))
    mirrors_failed = False
    if any(path.read_bytes() != originals[path] for path in docs):
        try:
            _sync_skill_references(workspace)
        except (OSError, subprocess.CalledProcessError) as error:
            mirrors_failed = True
            log(f"bump: the guides' mirrors did not regenerate ({error}): run python3 "
                f"{SYNC_SKILL_REFERENCES.as_posix()}")
    for problem in problems:
        log(problem)
    log("module snapshot: complete" if not problems else f"bump: the refrozen snapshot has {len(problems)} problem(s)")
    if held:
        log("bump: reverted " + ", ".join(sorted(held)) + ": their new roots stayed unfrozen (rerun with "
            "--esm-builder, or when upstream builds them)")
    return 1 if problems or held or mirrors_failed else 0
