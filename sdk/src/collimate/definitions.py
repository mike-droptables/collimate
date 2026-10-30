# SPDX-License-Identifier: MIT
"""Pure cell-relative discovery of definition package boundaries.

All four definition kinds establish a boundary before consumers select a kind.
The containing folder owns nested definition files as ordinary package content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from .pointers import is_run_path

DefinitionKind = Literal["generator", "renderer", "runner", "handler"]
_KINDS: tuple[DefinitionKind, ...] = ("generator", "renderer", "runner", "handler")


@dataclass(frozen=True)
class DefinitionPackage:
    kind: DefinitionKind
    definition_path: str
    prefix: str


@dataclass(frozen=True)
class DefinitionProblem:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class DefinitionDiscovery:
    packages: tuple[DefinitionPackage, ...]
    problems: tuple[DefinitionProblem, ...]


def _valid_path(path: str) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith("/")
            and "\\" not in path and "\x00" not in path
            and re.match(r"^[A-Za-z]:", path) is None
            and all(part not in ("", ".", "..") for part in path.split("/")))


def discover_definitions(paths: Iterable[str]) -> DefinitionDiscovery:
    """Discover shallowest unambiguous packages, retaining sibling problems.

    Paths are exact relative POSIX file names, never normalized. Root definition
    files are refused. Only the top-level host run band is excluded; an ordinary
    nested ``_runs`` folder is content like any other folder.
    """
    problems: list[DefinitionProblem] = []
    groups: dict[str, list[tuple[DefinitionKind, str]]] = {}
    for path in sorted(set(paths)):
        if not _valid_path(path):
            problems.append(DefinitionProblem("definition-path-invalid", path,
                                              f"Invalid cell-relative file path: {path!r}"))
            continue
        if is_run_path(path):
            continue
        name = path.rsplit("/", 1)[-1]
        kind = next((k for k in _KINDS if name.endswith("." + k) and name != "." + k), None)
        if kind is None:
            continue
        if "/" not in path:
            problems.append(DefinitionProblem("definition-at-root", path,
                                              "Definition files must be inside a package folder"))
            continue
        prefix = path.rsplit("/", 1)[0]
        groups.setdefault(prefix, []).append((kind, path))
    packages: list[DefinitionPackage] = []
    boundaries: list[str] = []
    for prefix in sorted(groups, key=lambda p: (p.count("/"), p)):
        if any(prefix.startswith(parent + "/") for parent in boundaries):
            continue
        boundaries.append(prefix)
        entries = groups[prefix]
        if len(entries) != 1:
            problems.append(DefinitionProblem("definition-folder-ambiguous", prefix,
                                              "A package folder must hold exactly one definition file"))
            continue
        kind, path = entries[0]
        packages.append(DefinitionPackage(kind, path, prefix))
    return DefinitionDiscovery(tuple(packages), tuple(sorted(problems, key=lambda p: (p.path, p.code))))


def package_files(package: DefinitionPackage, paths: Iterable[str]) -> tuple[str, ...]:
    """Exact paths below this discovered package prefix, including nested content."""
    if not _valid_path(package.prefix) or is_run_path(package.prefix):
        raise ValueError("Invalid definition package prefix")
    return tuple(sorted({path for path in paths if _valid_path(path)
                         and path.startswith(package.prefix + "/") and not is_run_path(path)}))


__all__ = ["DefinitionKind", "DefinitionPackage", "DefinitionProblem",
           "DefinitionDiscovery", "discover_definitions", "package_files"]
