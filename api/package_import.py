"""Package admission for cells entering the Capabilities stack: imports, a
new channel's seeding and the suggested catalog's adds.

Every admitted definition is verified by the app's built-in handlers
(``builtin_packages``). A package that takes a built-in id, type or surface,
or an id another capability cell already holds, is held and reported, never
admitted. An admitted handler keeps its ``derived/`` folder, but its
``derived:`` line always goes: a build is in use only once it is accepted in
this install. Each admitted package carries its provenance record
(``provenance``), written when an import plan publishes it.

A cell's shared helpers resolve here, whatever its source: the ``_*``
libraries its ``cell.yaml`` names under ``shared:`` are copied in from the
installed helper set (:func:`installed_helpers`), each replacing the cell's
folder of that name; a name the set does not hold refuses the cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

import yaml
from fastapi import HTTPException
from collimate.definitions import discover_definitions
from collimate.handlers import validate_handler
from models import Cell
from package_approval import (
    CellSnapshot,
    PackageConflict,
    admit_package,
    check_target,
    vfiles,
)
from provenance import Source


def _snapshot_files(cell):
    return CellSnapshot(
        str(cell.id),
        "",
        {
            f.name: ("blob", f.blob_hash) if f.blob_hash else ("text", f.content)
            for f in cell.files
        },
        {f.name: f.language for f in cell.files},
        {f.name for f in cell.files if f.blob_hash},
    )


def capabilities_stacks(channel) -> list:
    """The channel's Capabilities stack as a list: empty only for a channel
    still being created, before ``genesis.ensure_capabilities_stack`` ran
    on it."""
    from cell_placement import is_capabilities_stack, iter_stacks
    return [s for s in iter_stacks(channel) if is_capabilities_stack(s)]


def capture_sources(channel):
    """Freeze the Capabilities stack's accepted files under cell→state lock
    order; answers a detached candidate channel holding them and the
    stack's signature (its id, type and each cell's identity and refs)."""
    from cell_commits import cell_commit_locks
    from package_approval import snapshot_cell
    from state import state_lock

    with state_lock:
        cells = [c for s in capabilities_stacks(channel) for c in s.cells]
    with cell_commit_locks(str(c.id) for c in cells):
        with state_lock:
            clone = channel.model_copy(deep=True)
        snapshots = {str(c.id): snapshot_cell(channel, str(c.id)) for c in cells}
        signature = [
            (
                str(s.id),
                s.stack_type,
                [
                    (
                        str(c.id),
                        c.name,
                        c.icon,
                        snapshots[str(c.id)].tip,
                        snapshots[str(c.id)].refs,
                        snapshots[str(c.id)].binary_paths,
                        snapshots[str(c.id)].languages,
                    )
                    for c in s.cells
                ],
            )
            for s in capabilities_stacks(clone)
        ]
    # CAS reads/hashing/validation run without holding mutable source locks.
    for stack in [*clone.nav_stacks, *clone.main_stacks]:
        for cell in stack.cells:
            snap = snapshots.get(str(cell.id))
            if snap is not None:
                cell.files = vfiles(
                    snap.bytes(),
                    languages=snap.languages,
                    binary_paths=snap.binary_paths,
                )
    return clone, signature


def _targets(channel):
    from cell_placement import capabilities_stack_cells
    return [_snapshot_files(c) for c in capabilities_stack_cells(channel)]


def declared_definition_ids(files) -> set[tuple[str, str]]:
    """The ``(kind, id)`` pairs the cell's visible packages declare, across the
    four definition kinds — what a runner names its generator by, what the
    catalogs key packages by, and what must stay unique in a channel."""
    by_path = {file.name: file for file in files}
    ids: set[tuple[str, str]] = set()
    for package in discover_definitions(by_path).packages:
        try:
            data = yaml.safe_load(by_path[package.definition_path].content or "")
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            ids.add((package.kind, data["id"]))
    return ids


def declared_packages(candidate) -> set[tuple[str, str]]:
    """``(kind, id)`` of every package the capability cells of a
    ``capture_sources`` candidate declare: the ids admission holds."""
    from cell_placement import capabilities_stack_cells
    return {pair for cell in capabilities_stack_cells(candidate) for pair in declared_definition_ids(cell.files)}


def admitted_files(kind: str, definition: str, files: dict[str, bytes]) -> dict[str, bytes]:
    """A package's folder-relative ``files`` as admission copies them into a
    cell: a handler's ``derived:`` line goes (``definition`` is the
    definition file's name)."""
    if kind == "handler":
        raw = yaml.safe_load(files[definition])
        if isinstance(raw, dict) and "derived" in raw:
            raw = dict(raw)
            raw.pop("derived")
            return {**files, definition: yaml.safe_dump(raw, sort_keys=False).encode()}
    return files


def package_identities(bodies: dict[str, bytes]) -> set[tuple[str, str, str]]:
    """``(kind, id, content hash)`` of each package the cell files
    ``bodies`` (cell path to bytes) hold, hashed as admission would copy it
    (:func:`admitted_files`): what a provenance record of its admission
    says. A definition that does not parse to an id is left out."""
    from collimate._host.package_hash import content_hash
    identities = set()
    for package in discover_definitions(bodies).packages:
        prefix = package.prefix + "/"
        own = {p[len(prefix):]: b for p, b in bodies.items() if p.startswith(prefix)}
        definition = package.definition_path.rsplit("/", 1)[-1]
        try:
            manifest = yaml.safe_load(own[definition])
            if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str):
                continue
            own = admitted_files(package.kind, definition, own)
        except yaml.YAMLError:
            continue
        identities.add((package.kind, manifest["id"], content_hash(own)))
    return identities


def installed_helpers() -> Path:
    """The installed helper set: the bundled content's ``generators/`` tree,
    whose ``_*`` libraries a cell's ``cell.yaml`` names under ``shared:``."""
    import genesis
    return Path(genesis.bundled_dir("content")) / "generators"


def _with_helpers(name: str, order: list[str], bodies: dict[str, bytes], languages: dict[str, str],
                  binary_paths: set[str]):
    """An arriving cell's files with the shared helpers its ``cell.yaml``
    names resolved from the installed helper set: each library's folder in
    the cell replaced by the installed copy, appended after the cell's own
    files in ``order``. A cell without a ``cell.yaml`` is unchanged."""
    from collimate._host.packaging import CELL_MANIFEST, shared_libraries
    if CELL_MANIFEST not in bodies:
        return order, bodies, languages, binary_paths
    try:
        libraries = shared_libraries(bodies[CELL_MANIFEST], installed_helpers())
    except ValueError as exc:
        raise ValueError(f"Cell {name!r}: {exc}") from exc
    if not libraries:
        return order, bodies, languages, binary_paths
    prefixes = tuple(library + "/" for library in libraries)
    order = [path for path in order if not path.startswith(prefixes)]
    bodies = {p: b for p, b in bodies.items() if not p.startswith(prefixes)}
    languages = {p: lang for p, lang in languages.items() if not p.startswith(prefixes)}
    binary_paths = {p for p in binary_paths if not p.startswith(prefixes)}
    for spec in (spec for specs in libraries.values() for spec in specs):
        order.append(spec.name)
        bodies[spec.name] = spec.data if spec.data is not None else spec.content.encode("utf-8")
        languages[spec.name] = spec.language
        if spec.data is not None:
            binary_paths.add(spec.name)
    return order, bodies, languages, binary_paths


def cell_bytes(cell) -> dict[str, bytes]:
    """The cell's files as cell path to bytes (a binary file read from the
    CAS)."""
    return _snapshot_files(cell).bytes()


@dataclass
class PreparedPackages:
    """Validated copies for one Capabilities stack. ``bundles`` holds each
    admitted renderer's compiled ``(source hash, bytes)`` and ``provenance``
    each admitted package's record (``provenance.package_record``) from
    ``source``, both stored when an import plan publishes the copies
    (``ImportPlan.publish``)."""
    signature: list
    stack_id: str
    cells: list
    skipped: list
    renderer_ids: list
    bundles: list
    source: Source
    provenance: list


def prepare_packages(channel, incoming, *, source: Source, source_state=None):
    """Validate every incoming cell for the Capabilities stack before
    attaching any; held packages are reported. Each cell's shared helpers
    resolve from the installed helper set first (:func:`_with_helpers`).
    ``source`` is where the cells come from, recorded as each admitted
    package's provenance. ``source_state`` is an import plan's
    ``(candidate, signature)``; the candidate holds the stack
    (``genesis.ensure_capabilities_stack``)."""
    from builtin_packages import held_reason
    from cell_placement import capabilities_stack
    from provenance import package_record

    candidate, signature = capture_sources(channel) if source_state is None else source_state
    candidate = candidate.model_copy(deep=True)
    target = capabilities_stack(candidate)
    prepared, skipped, renderer_ids, bundles, records, pending = [], [], [], [], [], []
    file_orders = {}
    existing_cell_ids = {
        str(c.id)
        for s in [*candidate.nav_stacks, *candidate.main_stacks]
        for c in s.cells
    }
    for arriving in incoming:
        snap = _snapshot_files(arriving)
        order, bodies, languages, binary_paths = _with_helpers(
            arriving.name, [f.name for f in arriving.files], snap.bytes(), snap.languages, snap.binary_paths)
        discovery = discover_definitions(bodies)
        if discovery.problems:
            raise ValueError("; ".join(p.message for p in discovery.problems))
        packages = discovery.packages
        copy = Cell(
            id=uuid.uuid4() if str(arriving.id) in existing_cell_ids else arriving.id,
            name=arriving.name,
            icon=arriving.icon,
            files=[],
        )
        extras = {
            p: b
            for p, b in bodies.items()
            if not p.startswith("_runs/")
            and not any(p.startswith(package.prefix + "/") for package in packages)
        }
        if packages or extras:
            prepared.append(copy)
            file_orders[str(copy.id)] = {name: i for i, name in enumerate(order)}
        copy.files = vfiles(extras, languages=languages, binary_paths=binary_paths)
        for package in packages:
            prefix = package.prefix + "/"
            files = {
                p[len(prefix) :]: b for p, b in bodies.items() if p.startswith(prefix)
            }
            pending.append((package, files, copy, snap.cell_id, languages, binary_paths))
    # A runner may appear before its generator in incoming cell ordering.
    pending.sort(key=lambda row: row[0].kind == "runner")
    for package, files, cell, source_cell_id, languages, binary_paths in pending:
        definition = package.definition_path.rsplit("/", 1)[-1]
        manifest = yaml.safe_load(files[definition])
        if not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str):
            raise ValueError("Definition needs an id")
        if package.kind == "handler" and held_reason("handler", manifest) is None:
            # One that takes a built-in id, type or surface is held below as
            # it stands; any other is validated as a user handler (a reserved
            # type is a listed built-in's alone).
            manifest = validate_handler(manifest)
        claimed = {"kind": package.kind, "id": manifest["id"], "manifest": manifest}
        # A held package is never copied or executed.
        try:
            check_target(candidate, target, _targets(candidate), claimed, None)
        except PackageConflict as exc:
            skipped.append(
                {
                    "cell_id": source_cell_id,
                    "path": package.definition_path,
                    "id": claimed["id"],
                    "reason": str(exc),
                }
            )
            continue
        admitted = admit_package(candidate, package.definition_path, files)
        files = admitted_files(package.kind, definition, files)
        records.append(package_record(str(cell.id), package.prefix, package.kind,
                                      yaml.safe_load(files[definition]), files))
        if cell not in target.cells:
            target.cells.append(cell)
        cell.files.extend(
            vfiles(files, prefix=package.prefix + "/", languages=languages, binary_paths=binary_paths)
        )
        if package.kind == "renderer":
            renderer_ids.append(admitted["id"])
            bundles.append(admitted["bundle"])
    for cell in prepared:
        cell.files.sort(key=lambda file: file_orders[str(cell.id)][file.name])
    return PreparedPackages(
        signature,
        str(target.id),
        [c for c in prepared if c.files],
        skipped,
        renderer_ids,
        bundles,
        source,
        records,
    )


def apply_packages(channel, prepared):
    from import_plan import ImportPlan
    plan = ImportPlan(channel)
    result = plan.add_packages(prepared)
    plan.publish()
    return result


def attach_packages(channel, prepared):
    """Append validated copies to the Capabilities stack of a private import
    proposal; no SQL or locks."""
    stack = next(
        (s for s in [*channel.nav_stacks, *channel.main_stacks] if str(s.id) == prepared.stack_id),
        None,
    )
    if stack is None:
        raise HTTPException(409, "Capabilities changed during import; retry")
    stack.cells.extend(prepared.cells)
    return {'loaded_cells': len(prepared.cells), 'skipped': prepared.skipped}
