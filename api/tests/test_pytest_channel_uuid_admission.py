"""UUID-only catalog admission never confuses labels, filenames, or identities."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest


@pytest.mark.parametrize("value", ["Starting", "starting", "a-b", "../escape", "", None, 4,
    "A3CA4B68-C5C6-4F94-BF42-06DDF73FD350", "a3ca4b68c5c64f94bf4206ddf73fd350"])
def test_database_parser_refuses_every_noncanonical_identity_before_creating_files(fresh_cwd, value):
    from database import canonical_channel_id, get_db_connection
    with pytest.raises(ValueError):
        canonical_channel_id(value)
    with pytest.raises(ValueError):
        get_db_connection(value)
    assert not (fresh_cwd / ".collimate").exists()


def test_trusted_offline_uuid_opens_without_catalog_admission(fresh_cwd, monkeypatch):
    import state
    from database import canonical_channel_id, get_db_connection
    from models import AppState
    identity = uuid4()
    monkeypatch.setattr(state, "state", AppState())
    assert canonical_channel_id(identity) == str(identity)
    connection = get_db_connection(identity)
    connection.close()
    assert (fresh_cwd / ".collimate/channels" / f"{identity}.db").exists()
    with pytest.raises(HTTPException) as caught:
        state.require_channel(str(identity))
    assert caught.value.status_code == 404


@pytest.fixture
def registered_client(api_app):
    import state
    from database import get_db_connection
    from models import Channel, Cell, Stack, VirtualFile
    from persistence import initialize_files
    primary = Channel(name="Identical label", main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name="a.txt", content="primary")])])])
    other = Channel(name=primary.name, main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name="a.txt", content="other")])])])
    with TestClient(api_app) as client:
        state.state.channels.extend([primary, other])
        state.state.active_channel_id = primary.id
        for channel in (primary, other):
            connection = get_db_connection(channel.id)
            try:
                cell = channel.main_stacks[0].cells[0]
                initialize_files(connection, str(cell.id), cell.files)
                connection.commit()
            finally:
                connection.close()
        yield client, primary, other


@pytest.mark.parametrize("route", ["settings", "pick_sessions", "runs/active", "state/stream", "cell_display"])
@pytest.mark.parametrize("address", ["Identical label", "identical_label", "not-a-uuid"])
def test_name_and_slug_http_addresses_refuse_without_database_or_subscription(registered_client, route, address):
    client, _primary, _other = registered_client
    import events
    before = set(Path(".collimate/channels").glob("*.db"))
    response = client.get(f"/api/channels/{address}/{route}")
    assert response.status_code == 404, response.text
    assert set(Path(".collimate/channels").glob("*.db")) == before
    assert address not in events._subscribers


def test_unknown_uuid_with_a_database_still_cannot_enter_http_or_sse(registered_client):
    client, _primary, _other = registered_client
    from database import get_db_connection
    identity = str(uuid4())
    connection = get_db_connection(identity)
    connection.close()
    for route in ("settings", "pick_sessions", "runs/active", "state/stream"):
        response = client.get(f"/api/channels/{identity}/{route}")
        assert response.status_code == 404, response.text
    import events
    assert identity not in events._subscribers


@pytest.mark.parametrize('form', ['uppercase', 'compact'])
def test_registered_uuid_has_no_noncanonical_wire_aliases(registered_client, form):
    client, primary, _other = registered_client
    import events
    import state
    from auth_middleware import cell_file_caps
    address = str(primary.id).upper() if form == 'uppercase' else primary.id.hex
    original_name = primary.name
    original_active = state.state.active_channel_id
    before = set(Path('.collimate/channels').glob('*.db'))
    for route in ('settings', 'pick_sessions', 'runs/active', 'state/stream', 'cell_display'):
        response = client.get(f'/api/channels/{address}/{route}')
        assert response.status_code == 404, response.text
    selected = client.post(f'/api/channels/{address}/select')
    renamed = client.put(f'/api/channels/{address}/rename', params={'name': 'Alias'})
    assert selected.status_code == 404, selected.text
    assert renamed.status_code == 404, renamed.text
    with pytest.raises(HTTPException) as caught:
        cell_file_caps.cap(address, str(primary.main_stacks[0].cells[0].id))
    assert caught.value.status_code == 404
    assert primary.name == original_name
    assert state.state.active_channel_id == original_active
    assert set(Path('.collimate/channels').glob('*.db')) == before
    assert address not in events._subscribers


def test_run_admission_refuses_label_before_busy_reservation_or_database(registered_client):
    client, primary, _other = registered_client
    from run_registry import busy_channels
    before = set(Path('.collimate/channels').glob('*.db'))
    response = client.post(f'/api/channels/{primary.name}/runs', json={
        'runner': 'unused', 'cell_id': str(primary.main_stacks[0].cells[0].id),
    })
    assert response.status_code == 404, response.text
    assert busy_channels() == set()
    assert set(Path('.collimate/channels').glob('*.db')) == before


def test_run_reservations_require_canonical_uuid_without_catalog(fresh_cwd):
    from run_registry import busy_channels, channel_busy
    identity = str(uuid4())
    with channel_busy(identity):
        assert busy_channels() == {identity}
    assert busy_channels() == set()
    with pytest.raises(ValueError):
        with channel_busy('Starting'):
            pytest.fail('name must not enter UUID reservation')
    assert busy_channels() == set()
    assert not (fresh_cwd / '.collimate').exists()


def test_duplicate_labels_keep_independent_bytes_and_capabilities_across_rename(registered_client):
    client, primary, other = registered_client
    import state
    from auth_middleware import cell_file_caps
    for channel, text in [(primary, "primary"), (other, "other")]:
        cell_id = str(channel.main_stacks[0].cells[0].id)
        capability = cell_file_caps.cap(str(channel.id), cell_id)
        response = client.get(f"/api/cellcap/{capability}/channels/{channel.id}/cells/{cell_id}/files/a.txt")
        assert response.status_code == 200 and response.text == text
        wrong = client.get(f"/api/cellcap/{capability}/channels/{other.id if channel is primary else primary.id}/cells/{cell_id}/files/a.txt")
        assert wrong.status_code == 403
    primary_cell = str(primary.main_stacks[0].cells[0].id)
    capability = cell_file_caps.cap(str(primary.id), primary_cell)
    response = client.put(f"/api/channels/{primary.id}/rename", params={"name": "!!!"})
    assert response.status_code == 200, response.text
    assert cell_file_caps.cap(str(primary.id), primary_cell) == capability
    assert state.get_channel_by_id(str(primary.id)) is primary
    assert state.get_channel_by_id(primary.name) is None
    with pytest.raises(HTTPException):
        cell_file_caps.cap(primary.name, primary_cell)


def test_sse_admission_precedes_response_and_subscribe(registered_client):
    _client, primary, _other = registered_client
    from routes.channels import stream_channel_state
    import events
    for invalid in (primary.name, str(uuid4())):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(stream_channel_state(invalid))
        assert caught.value.status_code == 404
        assert invalid not in events._subscribers
    response = asyncio.run(stream_channel_state(str(primary.id)))
    assert response.media_type == "text/event-stream"
    assert str(primary.id) not in events._subscribers
