"""Stack mutations use their URL channel even while another channel is active."""
import uuid
from urllib.parse import quote

import pytest


OPERATIONS = [
    "create", "insert_blank_cell", "remove", "remove_cell_by_id", "minimize",
    "restore_minimized", "rename", "move_cell_to/1/stack",
    "move_stack_to/1/stack", "duplicate_cell_in_stack/0", "duplicate_stack",
    "cell_meta",
]


def _request(client, operation, channel, stack):
    root = f"/api/stacks/{quote(channel, safe='')}"
    if operation == "create":
        return client.post(f"{root}/create")
    if operation == "cell_meta":
        return client.patch(
            f"/api/channels/{quote(channel, safe='')}/cells/{stack.cells[0].id}/meta",
            json={"name": "Changed cell"},
        )
    if operation == "remove_cell_by_id":
        return client.post(
            f"/api/channels/{quote(channel, safe='')}/cells/{stack.cells[0].id}/remove",
        )
    if operation == "rename":
        return client.put(f"{root}/{stack.id}/rename", params={"name": "Changed stack"})
    kwargs = {}
    if operation == "insert_blank_cell":
        kwargs = {"json": {"name": "Blank"}}
    if operation.startswith("move_cell_to/"):
        kwargs = {"params": {"cell_id": str(stack.cells[0].id)}}
    return client.post(f"{root}/{stack.id}/{operation}", **kwargs)


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    from database import get_db_connection

    with TestClient(api_app) as http:
        channel = Channel(name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        yield http


@pytest.fixture
def layout(client):
    from models import Cell, Channel, Stack
    from state import get_active_channel, state

    active = get_active_channel()
    source = Stack(name="Source", cells=[Cell(name="First"), Cell(name="Second")])
    destination = Stack(name="Destination", cells=[Cell(name="Third")])
    from database import get_db_connection
    target = Channel(name="Target Channel", main_stacks=[source, destination])
    state.channels.append(target)
    get_db_connection(str(target.id)).close()
    return active, target, source


def _prepare(operation, target, source):
    from cell_placement import CAPABILITIES_STACK_TYPE
    if operation in {"minimize", "restore_minimized"}:
        source.stack_type = CAPABILITIES_STACK_TYPE
    if operation == "restore_minimized":
        target.main_stacks.remove(source)
        target.nav_stacks.append(source)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_stack_mutation_uses_its_inactive_channel__F0287(client, layout, operation):
    from state import state

    active, target, source = layout
    _prepare(operation, target, source)
    active_before = active.model_dump(mode="json")
    target_before = target.model_dump(mode="json")
    response = _request(client, operation, str(target.id), source)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(source.cells[0].id if operation == "cell_meta" else target.id)
    assert target.model_dump(mode="json") != target_before
    assert active.model_dump(mode="json") == active_before
    assert state.active_channel_id == active.id


@pytest.mark.parametrize("operation", OPERATIONS)
def test_stack_mutation_rejects_unknown_channel__F0287(client, layout, operation):
    active, target, source = layout
    _prepare(operation, target, source)
    before = [ch.model_dump(mode="json") for ch in (active, target)]
    unknown_id = str(uuid.uuid4())
    response = _request(client, operation, unknown_id, source)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"No such channel: {unknown_id}"
    assert [ch.model_dump(mode="json") for ch in (active, target)] == before


@pytest.mark.parametrize("operation", [x for x in OPERATIONS if x != "create"])
def test_stack_from_another_channel_is_never_mutated__F0287(client, layout, operation):
    active, target, source = layout
    _prepare(operation, target, source)
    before = [ch.model_dump(mode="json") for ch in (active, target)]
    response = _request(client, operation, str(active.id), source)
    assert response.status_code in {200, 404}, response.text
    if response.status_code == 200:
        assert response.json()["id"] == str(active.id)
    assert [ch.model_dump(mode="json") for ch in (active, target)] == before


def test_stack_openapi_requires_channel_and_body_cell_identity__F0287_D22(client):
    paths = client.get("/openapi.json").json()["paths"]
    stack_paths = {p: methods for p, methods in paths.items() if p.startswith("/api/stacks/")}
    assert len(stack_paths) == 10
    for path, methods in stack_paths.items():
        assert path.startswith("/api/stacks/{channel}/")
        for operation in methods.values():
            assert any(p["name"] == "channel" and p["in"] == "path" and p["required"]
                       for p in operation["parameters"])
    assert "/api/channels/{channel}/cells/update_top" not in paths
    update = paths["/api/channels/{channel}/cells/{cell_id}/meta"]["patch"]
    assert {p["name"] for p in update["parameters"] if p['in'] == 'path'} == {"channel", "cell_id"}
    assert update["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CellMetaPatch",
    }
