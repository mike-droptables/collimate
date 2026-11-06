"""The Capabilities stack's problems: files on it that are not working
capabilities.

Every package on the Capabilities stack is in use, whichever cell holds it,
and cells reach the stack by moves and direct writes as well as admission. A
package that cannot serve is left out of its catalog; this listing is where
the manage view shows it. It merges, per Capabilities cell:

- ``definition``: definition files the boundary analysis refuses
  (``collimate.definitions.discover_definitions``);
- ``generator``: generator packages the generator index leaves out
  (``runners.generator_index``);
- ``renderer``: user renderers the renderer inventory skips, a claimed
  built-in among them (``routes.blobs.renderer_inventory_problems``);
- ``handler``: the handler catalog's errors (``handlers.catalog``);
- ``library``: a ``cell.yaml`` whose ``shared:`` names a library neither
  Capabilities nor the app holds, or that cannot be read, and two libraries
  on Capabilities with one id (``libraries``).

Each row is ``{cell_id, path, kind, error}``; a row two sources report alike
is listed once.
"""
from __future__ import annotations

from typing import Literal

ProblemKind = Literal["definition", "generator", "renderer", "handler", "library"]


def capability_problems(channel) -> list[dict]:
    """The problems of ``channel``'s Capabilities stack, in source order:
    definitions, generators, renderers, handlers, then libraries."""
    from collimate.definitions import discover_definitions
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.handlers import catalog
    from collimate_server.routes.blobs import renderer_inventory_problems
    from collimate_server.run_folders import is_run_path
    from collimate_server.runners import generator_index
    from collimate_server.state import state_lock

    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: ProblemKind, problems) -> None:
        for problem in problems:
            key = (str(problem["cell_id"]), problem["path"], problem["error"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"cell_id": key[0], "path": key[1], "kind": kind, "error": key[2]})

    with state_lock:
        cells = capabilities_stack_cells(channel)
        definitions = [{"cell_id": str(cell.id), "path": problem.path, "error": problem.message}
                       for cell in cells
                       for problem in discover_definitions(
                           f.name for f in cell.files if not is_run_path(f.name)).problems]
        generators: list[dict] = []
        generator_index(cells, problems=generators)
        libraries = library_problems(cells)
    add("definition", definitions)
    add("generator", generators)
    add("renderer", renderer_inventory_problems(channel))
    add("handler", catalog(channel).errors)
    add("library", libraries)
    return rows


def library_problems(cells) -> list[dict]:
    """The ``shared:`` names of the Capabilities cells ``cells`` that resolve
    nowhere (or a ``cell.yaml`` that cannot be read), and the library ids two
    cells hold."""
    from collimate._host.packaging import CELL_MANIFEST
    from collimate_server.libraries import cell_shared, missing, packages as library_packages, unresolved_message
    rows = []
    packages = library_packages(cells)
    for identifier, holders in packages.items():
        if len(holders) > 1:
            for cell, prefix in holders:
                rows.append({"cell_id": str(cell.id), "path": prefix,
                             "error": f"Two libraries on Capabilities have the id ‘{identifier}’; rename one"})
    for cell in cells:
        try:
            unresolved = missing(cell_shared(cell.files), set(packages))
        except ValueError as exc:
            rows.append({"cell_id": str(cell.id), "path": CELL_MANIFEST, "error": str(exc)})
            continue
        if unresolved:
            rows.append({"cell_id": str(cell.id), "path": CELL_MANIFEST,
                         "error": unresolved_message("This cell", unresolved)})
    return rows
