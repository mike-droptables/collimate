"""The channel's libraries: where a cell's ``shared:`` names resolve.

A library is a ``.library`` package at the top of a cell
(:mod:`collimate._host.packaging.libraries`). A generator's cell names the
libraries its generators import under ``shared:`` in its ``cell.yaml``; each
name resolves to the library of that id on the channel's Capabilities stack,
else to the one the app ships (the bundled content's ``generators/`` tree).
Admission refuses a cell naming a library that resolves nowhere
(:func:`missing`), and a run's staging places each resolved library beside
the generator's folder (:func:`resolve`). Nothing copies a library into a
generator's cell: a library lives once, as a definition of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from collimate._host.packaging import CELL_MANIFEST, bundled_libraries, library_id, read_library, shared_names
from collimate.definitions import discover_definitions

#: Where a resolved library came from.
CAPABILITIES = "capabilities"
BUNDLED = "bundled"


class LibraryProblem(ValueError):
    """A ``shared:`` name that resolves nowhere, or to two libraries."""


@dataclass(frozen=True)
class ResolvedLibrary:
    """One resolved ``shared:`` name: where it came from (``capabilities``
    with the holding cell's id, or ``bundled``) and its files, named
    ``<name>/…`` as a cell holds them (``VirtualFile``)."""

    name: str
    origin: str
    cell_id: str | None
    files: tuple


def bundled_root() -> Path:
    """The libraries the app ships: the bundled content's ``generators/``."""
    import collimate_server.genesis as genesis
    return Path(genesis.bundled_dir("content")) / "generators"


def bundled_ids() -> set[str]:
    return set(bundled_libraries(bundled_root()))


def _text(file) -> str | None:
    """A ``VirtualFile``'s text. A binary file (its body only in a store) is
    no manifest a library or a ``shared:`` list can be read from, and reads
    as absent."""
    if getattr(file, "blob_hash", None):
        return None
    return file.content


def cell_shared(files) -> list[str]:
    """The ``shared:`` names of a cell whose files (``VirtualFile`` objects)
    are ``files``. Raises ``ValueError`` for a malformed ``cell.yaml``."""
    manifest = next((f for f in files if f.name == CELL_MANIFEST), None)
    return shared_names(None if manifest is None else _text(manifest))


def shared_of_bodies(bodies: Mapping[str, bytes]) -> list[str]:
    """The ``shared:`` names of a cell whose files are ``bodies`` (cell path to
    bytes)."""
    return shared_names(bodies.get(CELL_MANIFEST))


def ids_in_bodies(bodies: Mapping[str, bytes]) -> set[str]:
    """The ids of the well-placed libraries among a cell's ``bodies``."""
    found = set()
    for package in discover_definitions(bodies).packages:
        if package.kind == "library":
            identifier = library_id(package.definition_path, bodies[package.definition_path])
            if identifier is not None:
                found.add(identifier)
    return found


def packages(cells) -> dict[str, list[tuple]]:
    """Library id to ``(cell, prefix)`` of every well-placed library package
    the cells (``Cell`` objects, their files in memory) hold."""
    found: dict[str, list[tuple]] = {}
    for cell in cells:
        by_name = {f.name: f for f in cell.files}
        for package in discover_definitions(by_name).packages:
            if package.kind != "library":
                continue
            identifier = library_id(package.definition_path, _text(by_name[package.definition_path]) or "")
            if identifier is not None:
                found.setdefault(identifier, []).append((cell, package.prefix))
    return found


def ids_on(cells) -> set[str]:
    """The ids of the libraries the cells hold."""
    return set(packages(cells))


def missing(names: Iterable[str], available: set[str]) -> list[str]:
    """The ``names`` that neither ``available`` (the library ids a channel
    holds) nor the app's libraries resolve, in order."""
    names = list(names)
    unresolved = [name for name in names if name not in available]
    if not unresolved:
        return []
    shipped = bundled_ids()
    return [name for name in unresolved if name not in shipped]


def unresolved_message(owner: str, names: list[str]) -> str:
    quoted = ", ".join(f"‘{name}’" for name in names)
    noun = "library" if len(names) == 1 else "libraries"
    return (f"{owner} names the {noun} {quoted} under shared: in its cell.yaml, which neither this "
            f"channel's Capabilities nor the app's libraries hold; add it to Capabilities first")


def resolve(cells, names: Iterable[str], *, store=None, bundled_files: bool = True) -> list[ResolvedLibrary]:
    """Each of ``names`` resolved against the Capabilities cells ``cells``
    (``cell_placement.capabilities_stack_cells``), then the app's libraries;
    ``bundled_files=False`` resolves an app library without reading its
    files (they are the install's, the same for every run). An app
    library's binary files are stored into ``store``, the body store of the
    cells' channel. Raises
    :class:`LibraryProblem` for a name that resolves nowhere or to two
    libraries on Capabilities."""
    names = list(names)
    if not names:
        return []
    from collimate_server.models import VirtualFile
    held = packages(cells)
    resolved: list[ResolvedLibrary] = []
    for name in names:
        holders = held.get(name, [])
        if len(holders) > 1:
            raise LibraryProblem(f"Two libraries on Capabilities have the id ‘{name}’; rename one")
        if holders:
            cell, prefix = holders[0]
            files = tuple(f for f in cell.files if f.name.startswith(prefix + "/"))
            resolved.append(ResolvedLibrary(name, CAPABILITIES, str(cell.id), files))
            continue
        if not bundled_files:
            if name not in bundled_ids():
                raise LibraryProblem(unresolved_message("The generator's cell", [name]))
            resolved.append(ResolvedLibrary(name, BUNDLED, None, ()))
            continue
        specs = read_library(bundled_root(), name)
        if specs is None:
            raise LibraryProblem(unresolved_message("The generator's cell", [name]))
        files = []
        for spec in specs:
            if spec.data is not None:
                if store is None:
                    raise ValueError("an app library with binary files needs its channel's body store")
                files.append(VirtualFile(name=spec.name, blob_hash=store.store_bytes(spec.data), language=spec.language))
            else:
                files.append(VirtualFile(name=spec.name, content=spec.content, language=spec.language))
        resolved.append(ResolvedLibrary(name, BUNDLED, None, tuple(files)))
    return resolved


def _manifest(body) -> dict:
    import yaml
    try:
        data = yaml.safe_load(body or "")
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _public(identifier: str, manifest: dict) -> dict:
    return {"id": identifier,
            "name": manifest.get("name") if isinstance(manifest.get("name"), str) else identifier,
            "description": manifest.get("description") if isinstance(manifest.get("description"), str) else "",
            "version": manifest.get("version") if isinstance(manifest.get("version"), str) else None}


def catalog(cells) -> dict:
    """The libraries the Capabilities cells ``cells`` use: ``{libraries:
    [{id, name, description, version, cell_id, folder, named_by}], app: [{id,
    name, description, version, named_by}]}``, each library on Capabilities
    (by id, then cell), then each library of the app a cell names that
    Capabilities does not hold (it resolves to the app's). ``named_by`` lists
    the ids of the cells whose ``cell.yaml`` names it."""
    from collimate._host.packaging.libraries import LIBRARY_SUFFIX
    held = packages(cells)
    named: dict[str, list[str]] = {}
    for cell in cells:
        try:
            names = cell_shared(cell.files)
        except ValueError:
            continue
        for name in names:
            named.setdefault(name, []).append(str(cell.id))
    libraries = []
    for identifier in sorted(held):
        for cell, prefix in held[identifier]:
            definition = next((f for f in cell.files if f.name.startswith(prefix + "/")
                               and "/" not in f.name[len(prefix) + 1:] and f.name.endswith(LIBRARY_SUFFIX)), None)
            libraries.append({**_public(identifier, _manifest(None if definition is None else _text(definition))),
                              "cell_id": str(cell.id), "folder": prefix, "named_by": named.get(identifier, [])})
    app = []
    for name in sorted(set(named) - set(held)):
        specs = read_library(bundled_root(), name)
        if specs is None:
            continue
        definition = next((s for s in specs if s.name.count("/") == 1 and s.name.endswith(LIBRARY_SUFFIX)), None)
        app.append({**_public(name, _manifest(None if definition is None else definition.content)),
                    "named_by": named[name]})
    return {"libraries": libraries, "app": app}


__all__ = ["BUNDLED", "CAPABILITIES", "LibraryProblem", "ResolvedLibrary", "bundled_ids", "bundled_root",
           "catalog", "cell_shared", "ids_in_bodies", "ids_on", "missing", "packages", "resolve", "shared_of_bodies",
           "unresolved_message"]
