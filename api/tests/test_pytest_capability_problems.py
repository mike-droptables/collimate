"""``capability_problems(channel)``: every file on the Capabilities stack
that is not a working capability, by kind, each once. Synthetic packages:
generator 'synth-gen', renderer 'synth-view', handler 'memo' on '.memo'.
"""
from __future__ import annotations

import yaml

from conftest import add_capabilities_cell, install_handler, install_test_renderer


def _cell(name, files):
    from models import Cell, VirtualFile
    return Cell(name=name, files=[VirtualFile(name=path, content=body) for path, body in files.items()])


def test_a_healthy_stack_has_no_problems(client):
    from capability_problems import capability_problems
    from state import find_active_channel
    channel = find_active_channel()
    install_handler(channel, identity="memo", ext=".memo")
    install_test_renderer(renderer_id="synth-view", extensions=[".memo"])
    add_capabilities_cell(channel, _cell("Synth", {
        "synth-gen/synth.generator": "id: synth-gen\nname: Synth\nentrypoint: main.py\nmain_config: settings.set_yaml\n",
        "synth-gen/settings.set_yaml": "fields: []\n",
        "synth-gen/main.py": "print('ok')\n"}))
    assert capability_problems(channel) == []


def test_problems_of_every_kind_are_listed_once_with_their_cell(client):
    from builtin_packages import SURFACE_EXTENSIONS
    from capability_problems import capability_problems
    from state import find_active_channel
    channel = find_active_channel()
    broken = _cell("Broken", {
        # A definition at the cell root is no package.
        "loose.generator": "id: loose\n",
        # A generator without its main config.
        "synth-gen/synth.generator": "id: synth-gen\nname: Synth\nentrypoint: main.py\nmain_config: settings.set_yaml\n",
        "synth-gen/main.py": "print('ok')\n",
        # A handler claiming a built-in renderer's surface.
        "memo/memo.handler": yaml.safe_dump({"id": "memo", "name": "Memo", "description": "d", "guidance": "g",
                                             "types": [{"ext": sorted(SURFACE_EXTENSIONS)[0]}], "examples": []}),
    })
    add_capabilities_cell(channel, broken)
    install_test_renderer(renderer_id="synth-view", extensions=[sorted(SURFACE_EXTENSIONS)[0]])

    rows = capability_problems(channel)
    assert all(set(row) == {"cell_id", "path", "kind", "error"} for row in rows)
    by_path = {}
    for row in rows:
        by_path.setdefault(row["path"], []).append(row)
    assert len(by_path["loose.generator"]) == 1, rows
    assert by_path["loose.generator"][0]["kind"] == "definition"
    assert by_path["loose.generator"][0]["cell_id"] == str(broken.id)
    (generator,) = by_path["synth-gen/synth.generator"]
    assert generator["kind"] == "generator" and "settings.set_yaml" in generator["error"]
    (handler,) = by_path["memo/memo.handler"]
    assert handler["kind"] == "handler" and "surface" in handler["error"]
    (renderer,) = by_path["synth-view/view.renderer"]
    assert renderer["kind"] == "renderer" and "surface" in renderer["error"]
    assert [row["kind"] for row in rows] == sorted(
        (row["kind"] for row in rows), key=["definition", "generator", "renderer", "handler"].index)


def test_problems_follow_the_cell_on_and_off_capabilities(client):
    """Only the Capabilities cells are read: a broken package on the board is
    no problem of the stack, and moved on it is."""
    from capability_problems import capability_problems
    from cell_placement import capabilities_stack
    from models import Stack
    from state import find_active_channel
    channel = find_active_channel()
    cell = _cell("Loose", {"loose.renderer": "id: loose\n"})
    add_capabilities_cell(channel, cell)
    assert [row["path"] for row in capability_problems(channel)] == ["loose.renderer"]
    capabilities_stack(channel).cells.remove(cell)
    channel.main_stacks.append(Stack(name="Board", cells=[cell]))
    assert capability_problems(channel) == []
