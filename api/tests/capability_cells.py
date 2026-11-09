"""Synthetic Capabilities cells for the whole-cell doors (Copy to channel,
the user catalog, the update path): a library cell, a generator cell naming
it under ``shared:`` with a runner, and helpers to place and edit them."""
from __future__ import annotations

import yaml

from collimate_server.models import Cell, VirtualFile
from suggested_trees import MAIN


def generator_cell(gid: str = "greeter", *, shared=("_greetings",), runner: bool = True,
                   body: str = "from _greetings import NAME\nprint(NAME)\n", name: str = "Greeter") -> Cell:
    """A cell holding the generator ``gid`` (whose code is ``body``), a
    ``cell.yaml`` naming ``shared`` and, with ``runner``, the runner
    ``hello`` of it."""
    files = [
        VirtualFile(name=f"{gid}/choice.generator", content=yaml.safe_dump({
            "id": gid, "name": gid.title(), "summary": "Synthetic generator", "entrypoint": "main.py",
            "main_config": MAIN})),
        VirtualFile(name=f"{gid}/main.py", content=body),
        VirtualFile(name=f"{gid}/{MAIN}", content="fields: []\n"),
        VirtualFile(name="cell.yaml", content=yaml.safe_dump({"shared": list(shared)})),
    ]
    if runner:
        files.append(VirtualFile(name="hello/hello.runner", content=yaml.safe_dump({
            "id": "hello", "generator": gid, "name": "Hello", "description": "", "tags": [], "prompt": "",
            "settings": {MAIN: {}}, "folders": [], "accounts": {}, "permissions": {}, "packages": {}},
            sort_keys=False)))
    return Cell(name=name, files=files)


def library_cell(name: str = "_greetings", body: str = "NAME = 'source library'\n") -> Cell:
    """A cell holding the library ``name``."""
    return Cell(name="Greetings", files=[
        VirtualFile(name=f"{name}/{name}.library",
                    content=yaml.safe_dump({"id": name, "name": "Greetings", "description": "Shared greetings"})),
        VirtualFile(name=f"{name}/__init__.py", content=body)])


def place(channel_id: str, *cells: Cell) -> None:
    """Append ``cells`` to the channel's Capabilities as accepted files."""
    from conftest import add_capabilities_cell
    from collimate_server.state import persist_state, require_channel
    for cell in cells:
        add_capabilities_cell(require_channel(channel_id), cell)
    persist_state()


def edit(channel_id, cell, path: str, body: str) -> None:
    """Write ``body`` at ``path`` of the cell as an accepted source edit."""
    from collimate_server.cell_commits import commit_source_cell
    from collimate_server.cell_placement import find_cell
    from collimate_server.state import require_channel
    channel = require_channel(channel_id)
    live = find_cell(channel, str(cell.id))[1]
    files = [f for f in live.files if f.name != path] + [VirtualFile(name=path, content=body)]
    commit_source_cell(channel, live, files)


def capability_cells(channel_id) -> dict:
    """The channel's Capabilities cells by name."""
    from collimate_server.cell_placement import capabilities_stack_cells
    from collimate_server.state import require_channel
    return {cell.name: cell for cell in capabilities_stack_cells(require_channel(channel_id))}


def staged(channel_id, generator_id: str) -> dict[str, str]:
    """What a run of ``generator_id`` on the channel stages: path to text."""
    from collimate_server.run_staging import snapshot_generator_source
    return {rel: file.content for rel, file in snapshot_generator_source(generator_id, channel_id)[1]}
