"""Real IPC commits publish accepted revisions and notify subscribers."""

import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest


@pytest.fixture
def ipc_cell(api_app, fresh_cwd):
    from database import get_db_connection
    from models import Cell, Channel, Stack
    from process_manager import ProcessManager
    from runner_permissions import UNRESTRICTED
    from state import state

    channel_name = "test-ipc-broadcast"
    run_id = str(uuid.uuid4())
    cell = Cell(name="Generating...")
    channel = Channel(name=channel_name, main_stacks=[Stack(cells=[cell])])
    from genesis import ensure_capabilities_stack
    ensure_capabilities_stack(channel)
    state.channels.append(channel)
    state.active_channel_id = channel.id
    channel_name = str(channel.id)
    conn = get_db_connection(channel_name)
    conn.execute(
        "INSERT INTO runs (id, prompt, status) VALUES (?, '', 'running')", (run_id,)
    )
    conn.commit()
    conn.close()
    workspace, app_dir = fresh_cwd / "workspace", fresh_cwd / "app"
    (workspace / "chat").mkdir(parents=True)
    app_dir.mkdir()
    (workspace / "chat/main.json").write_text(
        json.dumps([{"role": "user", "content": "hi"}])
    )
    manager = ProcessManager(
        channel=channel_name,
        run_id=run_id,
        app_dir=str(app_dir),
        workspace_dir=str(workspace),
        entrypoint="chat.py",
        generator_name="chat",
        cell_id=str(cell.id),
        permissions=UNRESTRICTED,
    )
    return SimpleNamespace(
        manager=manager, channel=channel, cell=cell, name=channel_name
    )


async def _update(manager):
    await manager._handle_ipc(
        json.dumps(
            {
                "req_id": "test-request",
                "op": "update_cell",
                "operation_id": str(uuid.uuid4()),
                "writer_id": str(uuid.uuid4()),
                "edit_seq": 1,
                "base_revisions": {"chat/main.json": 0},
                "artifacts": [
                    {
                        "type": "workspace",
                        "cell_path": "chat/main.json",
                        "local_path": "chat/main.json",
                    }
                ],
                "metadata": {"name": "Chat"},
            }
        )
    )


async def test_first_ipc_commit_broadcasts_cell_files_on_main_loop__F1898_F1905(
    ipc_cell,
):
    from events import subscribe, unsubscribe

    queue = subscribe(ipc_cell.name)
    try:
        await _update(ipc_cell.manager)
        # The ordinary state publication is coalesced over a 0.25s burst.
        await asyncio.sleep(0.4)
        received = []
        while not queue.empty():
            received.append(queue.get_nowait())
        assert any(event.get("type") == "dag_commit" for event in received)
        snapshots = [
            event for event in received if event.get("type") == "state_snapshot"
        ]
        assert len(snapshots) == 1, received
        cells = [
            cell
            for channel in snapshots[0]["state"]["channels"]
            for stack_list in ("main_stacks", "nav_stacks")
            for stack in channel.get(stack_list, [])
            for cell in stack.get("cells", [])
        ]
        matching = [cell for cell in cells if cell.get("id") == str(ipc_cell.cell.id)]
        assert matching, "the first state snapshot must include the run's cell"
        assert any(
            file.get("name", "").endswith("chat/main.json")
            for file in matching[0]["files"]
        )
    finally:
        unsubscribe(ipc_cell.name, queue)


async def test_ipc_commit_publishes_accepted_revision__F1898_F1905(ipc_cell):
    await _update(ipc_cell.manager)
    matching = [
        cell
        for stack in ipc_cell.channel.main_stacks
        for cell in stack.cells
        if str(cell.id) == str(ipc_cell.cell.id)
    ]
    assert matching, "committed cell must remain in application state"
    from database import get_db_connection
    from persistence import snapshot_files
    from cas import require_blob
    with get_db_connection(ipc_cell.name) as conn:
        [accepted] = snapshot_files(conn, str(ipc_cell.cell.id))
    assert accepted["file_revision"] == 1
    assert accepted["path"] == "chat/main.json"
    assert json.loads(require_blob(accepted["body_hash"])) == [{"role": "user", "content": "hi"}]
    assert matching[0].files[0].file_revision == accepted["file_revision"]
