"""The built-in cards head the Capabilities stack, and the Definitions card's
overlay holds definition files: the examples a channel adds to the built-in
generator and runner handlers. The catalogs read only the cells after the
cards, so an added example never serves as a generator or a runner, and it is
never listed as a problem.
"""
from __future__ import annotations

import yaml

GENERATOR = ("id: synth-gen\nname: Synth\nentrypoint: main.py\nmain_config: settings.set_yaml\n")
RUNNER = yaml.safe_dump({"id": "synth-run", "name": "Synth run", "generator": "synth-gen", "description": "",
                         "tags": [], "prompt": "", "settings": {"settings.set_yaml": {}}, "accounts": {},
                         "folders": [], "permissions": {}, "packages": {}}, sort_keys=False)


def _builtin_for(path: str) -> str:
    from builtin_packages import builtin_handlers
    from collimate.handlers import handler_for
    return next(handler.id for handler in builtin_handlers().values()
                if handler_for([handler.manifest], path) is not None)


def test_added_examples_of_the_built_in_generator_and_runner_handlers_never_serve(client):
    from builtin_packages import handler_card
    from capability_problems import capability_problems
    from cell_placement import capabilities_stack
    from database import get_db_connection
    from models import Cell, Stack, VirtualFile
    from persistence import initialize_files
    from state import find_active_channel, persist_state
    import runners

    channel = find_active_channel()
    source = Cell(name="Drafts", files=[VirtualFile(name="synth.generator", content=GENERATOR),
                                        VirtualFile(name="synth-run.runner", content=RUNNER)])
    with get_db_connection(str(channel.id)) as conn:
        initialize_files(conn, str(source.id), source.files)
    channel.main_stacks.append(Stack(name="Board", cells=[source]))
    persist_state()
    for path in ("synth.generator", "synth-run.runner"):
        handler_id = _builtin_for(path)
        response = client.post(f"/api/channels/{channel.id}/builtin/handlers/{handler_id}/examples",
                               json={"source_cell_id": str(source.id), "paths": [path],
                                     "description": "An added example", "expect": "valid", "feedback": ""})
        assert response.status_code == 200, response.text

    card = handler_card(channel)
    card_cell = next(c for c in capabilities_stack(channel).cells if str(c.id) == card)
    from persistence import snapshot_files
    with get_db_connection(str(channel.id)) as conn:
        overlay = {f["path"] for f in snapshot_files(conn, card)}
    assert any(p.endswith("/synth.generator") for p in overlay), overlay
    assert any(p.endswith("/synth-run.runner") for p in overlay), overlay
    # The card holds them as ordinary definition files: only the walker's
    # skipping of the built-in cards keeps them out of the catalogs.
    assert card_cell.builtin
    assert {"synth-gen", "synth-run"} <= {
        yaml.safe_load(file.content).get("id") for kind in ("generator", "runner")
        for _, file in runners.definition_files(card_cell, kind)}

    assert "synth-gen" not in {g["name"] for g in client.get("/api/generators").json()}
    assert "synth-gen" not in {g["id"] for g in client.get("/api/runner_generators").json()}
    assert "synth-run" not in {r["id"] for r in client.get("/api/runners").json()}
    try:
        runners.find_runner(channel, "synth-run")
    except runners.RunnerError as exc:
        assert exc.status == 404
    else:
        raise AssertionError("an added runner example was found as a runner")
    assert not [row for row in capability_problems(channel) if row["cell_id"] == card]
