"""Plan 1105 P4: archived channels on the server (S6).

An archived channel is read-only: one global dependency
(``channel_guard``) refuses every write to it with 409
``channel_archived`` before the route validates its parameters or body,
``ARCHIVE_SAFE`` names what it still allows, and ``cell_write_scope`` and
run creation refuse the same way when the guard is bypassed. The flag lives
in the channel's ``channel.json``. Every mutating route is enumerated from
``app.routes``, so a new one must be classified here."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.real_channel_homes

READ = {"GET", "HEAD", "OPTIONS"}


def _routes():
    """``(writes, sockets)``: every non-read HTTP route as ``(method, path,
    body required)`` and every websocket route's path, from the app."""
    from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
    from collimate_server.main import app
    writes, sockets = [], []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            required = any(param.field_info.is_required() for param in route.dependant.body_params)
            writes += [(method, route.path, required) for method in sorted(route.methods - READ)]
        elif isinstance(route, APIWebSocketRoute):
            sockets.append(route.path)
    return writes, sockets


WRITES, SOCKETS = _routes()
from collimate_server.channel_guard import ACTIVE_CHANNEL_ROUTES, ARCHIVE_SAFE  # noqa: E402

# Mutating routes that act on no channel, each with why.
CHANNEL_FREE = {
    "/auth/session": "the browser session",
    "/api/channels": "creates a channel",
    "/api/import/zip": "the new-channel page's staging store",
    "/api/accounts": "installation accounts",
    "/api/accounts/check": "installation accounts",
    "/api/accounts/{identity}": "installation accounts",
    "/api/accounts/{identity}/models": "installation accounts",
    "/api/account-recoveries": "installation accounts",
    "/api/catalog/suggestions": "the user catalog (app.db)",
    "/api/catalog/suggestions/{suggestion_id}": "the user catalog (app.db)",
    "/api/catalog/entries/{entry_id}": "the user catalog (app.db)",
    "/api/renderers/{renderer_id}/bundle": "the app store's bundle cache (a read of the channel needs it)",
    "/api/renderers/{renderer_id}/offline": "the app store's module mirror",
    "/api/system/client": "the server's client record",
    "/api/system/shutdown": "the server",
    "/api/system/stop_all": "every run",
}
STORAGE_PREFIX = "/api/storage/"
# Websockets with no channel parameter attach to a live run's session, and
# no run lives on an archived channel (archive refuses while runs are live,
# a run start refuses an archived channel).
SOCKETS_FREE = {"/api/chats/{chat_id}", "/api/terminals/{terminal_id}"}


def _channel_param(path: str) -> bool:
    return "{channel}" in path or "{channel_id}" in path


def _filled(path: str, channel: str) -> str:
    def value(match: re.Match) -> str:
        name = match.group(1)
        if name in ("channel", "channel_id"):
            return channel
        if name == "port":
            return "8000"
        if "index" in name:
            return "0"
        if name == "mode":
            return "before"
        if name == "path":
            return "x"
        return str(uuid.uuid4())
    return re.sub(r"\{(\w+)(?::\w+)?\}", value, path)


def _create(client, name: str) -> str:
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _archive(client, channel: str) -> dict:
    response = client.post(f"/api/channels/{channel}/archive")
    assert response.status_code == 200, response.text
    return response.json()


def _refused(response, channel: str, name: str) -> None:
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.json()["detail"] == {
        "code": "channel_archived", "channel_id": channel, "name": name,
        "message": "This channel is archived and read-only. Unarchive it to change it."}


def _is_refusal(response) -> bool:
    if response.status_code != 409:
        return False
    detail = response.json().get("detail")
    return isinstance(detail, dict) and detail.get("code") == "channel_archived"


def _manifest(fresh_cwd: Path, channel: str) -> dict:
    return json.loads((fresh_cwd / "Channels" / channel / "channel.json").read_text())


@pytest.fixture
def archived(api_app, fresh_cwd):
    """``(client, archived id, live id)``: the archived channel is "Shelf",
    the live one "Desk" (active)."""
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        desk = _create(client, "Desk")
        _archive(client, shelf)
        yield client, shelf, desk


def test_every_mutating_route_is_classified():
    paths = {path for _, path, _ in WRITES}
    unclassified = {path for path in paths if not _channel_param(path) and path not in ACTIVE_CHANNEL_ROUTES
                    and path not in CHANNEL_FREE and not path.startswith(STORAGE_PREFIX)}
    assert not unclassified, f"classify these mutating routes in channel_guard or here: {sorted(unclassified)}"
    assert not set(CHANNEL_FREE) - paths, "CHANNEL_FREE names a route the app no longer serves"
    assert ACTIVE_CHANNEL_ROUTES <= paths
    assert ARCHIVE_SAFE <= {(method, path) for method, path, _ in WRITES}
    assert {path for path in SOCKETS if not _channel_param(path)} == SOCKETS_FREE


def test_archive_safe_is_exactly_what_d9_allows():
    """Rename, export, copy-out and publish-to-catalog, which read the
    channel and write elsewhere; archive, unarchive, move, delete and
    select. Each has its own test below (move's in
    test_pytest_channel_move.test_an_archived_channel_moves)."""
    channel = "/api/channels/{channel_id}"
    cell = "/api/channels/{channel}/capabilities/cells/{cell_id}"
    assert ARCHIVE_SAFE == {
        ("POST", f"{channel}/archive"), ("POST", f"{channel}/unarchive"), ("DELETE", channel),
        ("POST", "/api/channels/{channel}/move"),
        ("PUT", f"{channel}/rename"), ("POST", f"{channel}/select"),
        ("DELETE", "/api/channels/{channel}/export/{token}"),
        ("POST", f"{cell}/copy/review"), ("POST", f"{cell}/copy"),
        ("POST", f"{cell}/publish/review"), ("POST", f"{cell}/publish"),
    }


GUARDED = [(method, path, required) for method, path, required in WRITES
           if _channel_param(path) and (method, path) not in ARCHIVE_SAFE]


@pytest.mark.parametrize("method,path,required", GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED])
def test_a_write_to_an_archived_channel_is_refused_before_validation(archived, method, path, required):
    client, shelf, desk = archived
    # No body and made-up ids: without the guard a route needing a body
    # answers 422, as the live channel shows.
    _refused(client.request(method, _filled(path, shelf)), shelf, "Shelf")
    control = client.request(method, _filled(path, desk))
    assert not _is_refusal(control), control.text
    if required:
        assert control.status_code == 422, (control.status_code, control.text)


ACTIVE = [(method, path, required) for method, path, required in WRITES if path in ACTIVE_CHANNEL_ROUTES]


@pytest.mark.parametrize("method,path,required", ACTIVE, ids=[f"{m} {p}" for m, p, _ in ACTIVE])
def test_an_active_channel_route_is_refused_while_the_active_channel_is_archived(archived, method, path, required):
    client, shelf, desk = archived
    assert client.post(f"/api/channels/{shelf}/select").status_code == 200
    _refused(client.request(method, _filled(path, shelf)), shelf, "Shelf")
    assert client.post(f"/api/channels/{desk}/select").status_code == 200
    control = client.request(method, _filled(path, desk))
    assert not _is_refusal(control), control.text
    if required:
        assert control.status_code == 422, (control.status_code, control.text)


def test_a_websocket_to_an_archived_channel_is_closed_with_its_code(archived):
    client, shelf, _ = archived
    for path in SOCKETS:
        if not _channel_param(path):
            continue
        with client.websocket_connect(_filled(path, shelf)) as socket:
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
        assert (closed.value.code, closed.value.reason) == (4409, "channel_archived")


def test_archive_writes_the_manifest_and_answers_the_manage_row(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        desk = _create(client, "Desk")
        row = _archive(client, shelf)
        manifest = _manifest(fresh_cwd, shelf)
        assert manifest["archived"] is True and manifest["archived_at"]
        assert row == {"id": shelf, "name": "Shelf", "archived": True, "archived_at": manifest["archived_at"],
                       "location_id": row["location_id"], "path": str(fresh_cwd / "Channels" / shelf),
                       "state": "ok", "reason": None, "active": False, "bytes": row["bytes"],
                       "files": row["files"], "computed_at": row["computed_at"]}
        assert row["bytes"] > 0  # the storage survey's entry, sized at creation
        # Archiving again changes nothing.
        assert _archive(client, shelf) == row
        # Unarchive is allowed on the archived channel and makes it writable.
        unarchived = client.post(f"/api/channels/{shelf}/unarchive")
        assert unarchived.status_code == 200, unarchived.text
        assert unarchived.json()["archived"] is False and unarchived.json()["archived_at"] is None
        assert _manifest(fresh_cwd, shelf)["archived"] is False
        assert client.post(f"/api/stacks/{shelf}/create").status_code == 200
        assert client.post(f"/api/channels/{desk}/unarchive").json()["archived"] is False


def test_archive_refuses_while_runs_are_live(api_app, fresh_cwd):
    from collimate_server.run_registry import channel_busy
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        with channel_busy(shelf):
            refused = client.post(f"/api/channels/{shelf}/archive")
        assert refused.status_code == 409 and "still running" in refused.text
        assert _manifest(fresh_cwd, shelf)["archived"] is False
        _archive(client, shelf)


def test_the_flag_survives_a_restart(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        desk = _create(client, "Desk")
        _archive(client, shelf)
    with TestClient(api_app) as client:
        assert client.get("/api/channels").json()["channels"] == [{"id": desk, "name": "Desk"}]
        _refused(client.post(f"/api/stacks/{shelf}/create"), shelf, "Shelf")
        assert client.post(f"/api/channels/{shelf}/unarchive").status_code == 200
    with TestClient(api_app) as client:
        assert [c["id"] for c in client.get("/api/channels").json()["channels"]] == [shelf, desk]


def test_listings_exclude_archived_channels_but_the_active_one(archived):
    client, shelf, desk = archived
    listed = client.get("/api/channels").json()
    assert listed["channels"] == [{"id": desk, "name": "Desk"}] and listed["active_channel_id"] == desk
    snapshot = client.get("/api/state").json()
    assert [c["id"] for c in snapshot["channels"]] == [desk]
    assert "archived" not in snapshot["channels"][0]
    # Opened from the Archived tab, it is the active channel, read-only.
    assert client.post(f"/api/channels/{shelf}/select").status_code == 200
    snapshot = client.get("/api/state").json()
    assert snapshot["active_channel_id"] == shelf
    entries = {c["id"]: c for c in snapshot["channels"]}
    assert set(entries) == {shelf, desk}
    assert entries[shelf]["archived"] is True and "main_stacks" in entries[shelf]
    assert entries[desk] == {"id": desk, "name": "Desk"}
    assert client.get("/api/channels").json()["channels"] == [{"id": desk, "name": "Desk"}]
    # Reads of it still answer.
    for path in (f"/api/channels/{shelf}/settings", f"/api/channels/{shelf}/pick_sessions",
                 f"/api/channels/{shelf}/export/manifest?scope=all", f"/api/channels/{shelf}/export.zip?scope=all"):
        assert client.get(path).status_code == 200, path


def test_archiving_the_active_channel_moves_to_the_last_selected_one(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        first = _create(client, "First")
        second = _create(client, "Second")
        third = _create(client, "Third")
        assert client.post(f"/api/channels/{first}/select").status_code == 200
        assert client.post(f"/api/channels/{third}/select").status_code == 200
        assert _archive(client, third)["active"] is False
        assert client.get("/api/channels").json()["active_channel_id"] == first
        _archive(client, second)
        _archive(client, first)
        # No channel left that is not archived: the window shows the page.
        assert client.get("/api/channels").json() == {"channels": [], "active_channel_id": None}


def test_archive_drops_the_channels_pick_sessions(archived):
    client, shelf, desk = archived
    assert client.post(f"/api/channels/{shelf}/unarchive").status_code == 200
    cell = client.post(f"/api/stacks/{shelf}/create").json()["main_stacks"][0]["cells"][0]["id"]
    saved = client.put(f"/api/channels/{shelf}/pick_sessions/{cell}", json={"kind": "files", "query": "x"})
    assert saved.status_code == 200, saved.text
    assert set(client.get(f"/api/channels/{shelf}/pick_sessions").json()) == {cell}
    _archive(client, shelf)
    assert client.get(f"/api/channels/{shelf}/pick_sessions").json() == {}


def test_rename_select_and_export_token_delete_are_allowed(archived, fresh_cwd):
    client, shelf, _ = archived
    renamed = client.put(f"/api/channels/{shelf}/rename", params={"name": "Attic"})
    assert renamed.status_code == 200, renamed.text
    assert _manifest(fresh_cwd, shelf)["name"] == "Attic"
    assert _manifest(fresh_cwd, shelf)["archived"] is True
    assert client.post(f"/api/channels/{shelf}/select").status_code == 200
    manifest = client.get(f"/api/channels/{shelf}/export/manifest", params={"scope": "all"})
    assert manifest.status_code == 200, manifest.text
    # Nothing staged (no repository): any token answers 204 all the same.
    token = manifest.json()["token"] or "none-staged"
    assert client.delete(f"/api/channels/{shelf}/export/{token}").status_code == 204


def test_delete_works_on_an_archived_channel(archived, fresh_cwd):
    client, shelf, desk = archived
    assert client.post(f"/api/channels/{shelf}/select").status_code == 200
    deleted = client.delete(f"/api/channels/{shelf}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["active_channel_id"] == desk
    assert not (fresh_cwd / "Channels" / shelf).exists()


@pytest.fixture
def archived_with_cells(api_app, fresh_cwd):
    """``(client, archived id, live id, generator cell)``: the archived
    channel's Capabilities hold a library and a generator cell naming it."""
    from capability_cells import generator_cell, library_cell, place
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        generator, lib = generator_cell(), library_cell()
        place(shelf, lib, generator)
        desk = _create(client, "Desk")
        _archive(client, shelf)
        yield client, shelf, desk, generator


def test_copy_out_to_another_channel_is_allowed(archived_with_cells):
    from capability_cells import capability_cells
    client, shelf, desk, generator = archived_with_cells
    url = f"/api/channels/{shelf}/capabilities/cells/{generator.id}/copy"
    review = client.post(url + "/review", json={"target_channel_id": desk})
    assert review.status_code == 200, review.text
    copied = client.post(url, json={"target_channel_id": desk, "token": review.json()["token"]})
    assert copied.status_code == 200, copied.text
    assert {"Greeter", "Greetings"} <= set(capability_cells(desk))


def test_a_copy_to_an_archived_channel_is_refused(archived_with_cells):
    client, shelf, desk, _ = archived_with_cells
    assert client.post(f"/api/channels/{shelf}/unarchive").status_code == 200
    _archive(client, desk)
    from capability_cells import generator_cell, place
    generator = generator_cell()
    place(shelf, generator)
    review = client.post(f"/api/channels/{shelf}/capabilities/cells/{generator.id}/copy/review",
                         json={"target_channel_id": desk})
    _refused(review, desk, "Desk")


def test_publish_to_catalog_is_allowed(archived_with_cells):
    client, shelf, _, generator = archived_with_cells
    url = f"/api/channels/{shelf}/capabilities/cells/{generator.id}/publish"
    review = client.post(url + "/review")
    assert review.status_code == 200, review.text
    published = client.post(url, json={"token": review.json()["token"]})
    assert published.status_code == 200, published.text


def test_the_write_scope_and_run_start_refuse_when_the_guard_is_bypassed(api_app, fresh_cwd):
    """The backstop: a write that slips past the route guard still meets
    ``ChannelArchived`` in ``cell_write_scope`` and in run creation, with
    the same answer."""
    import collimate_server.channel_guard as channel_guard
    from collimate_server.cell_write_scope import cell_read_scope, cell_write_scope
    with TestClient(api_app) as client:
        shelf = _create(client, "Shelf")
        stack = client.post(f"/api/stacks/{shelf}/create").json()["main_stacks"][0]
        _archive(client, shelf)
        with pytest.raises(channel_guard.ChannelArchived):
            with cell_write_scope(shelf, []):
                pass
        with cell_read_scope([]):
            pass
        api_app.dependency_overrides[channel_guard.channel_guard] = lambda: None
        try:
            _refused(client.post(f"/api/stacks/{shelf}/{stack['id']}/remove"), shelf, "Shelf")
            run = client.post(f"/api/channels/{shelf}/runs",
                              json={"runner": "absent", "cell_id": stack["cells"][0]["id"]})
            _refused(run, shelf, "Shelf")
        finally:
            api_app.dependency_overrides.clear()
        # The stack is still there.
        from collimate_server.state import get_channel_by_id
        assert [str(s.id) for s in get_channel_by_id(shelf).main_stacks] == [stack["id"]]


async def test_the_stream_of_an_archived_channel_carries_its_flag(fresh_cwd, monkeypatch):
    """The channel's SSE stream receives a ``state_delta`` setting
    ``archived: true`` when it is archived (the open window turns
    read-only), and one removing it when it is unarchived."""
    from conftest import channel_home
    import collimate_server.state as state
    from collimate_server.genesis import ensure_capabilities_stack
    from collimate_server.models import AppState, Channel
    from collimate_server.routes.channels import _archive_channel, _unarchive_channel, stream_channel_state
    shelf, desk = Channel(name="Shelf"), Channel(name="Desk")
    for channel in (shelf, desk):
        ensure_capabilities_stack(channel)
        channel_home(channel)
    monkeypatch.setattr(state, "state", AppState(channels=[shelf, desk], active_channel_id=desk.id))
    response = await stream_channel_state(str(shelf.id))
    stream = response.body_iterator

    async def next_state() -> dict:
        while True:
            line = await asyncio.wait_for(anext(stream), 5)
            if line.startswith("data: "):
                event = json.loads(line.removeprefix("data: "))
                if event["type"].startswith("state_"):
                    return event

    try:
        baseline = await next_state()
        assert baseline["type"] == "state_snapshot"
        assert "archived" not in next(c for c in baseline["state"]["channels"] if c["id"] == str(shelf.id))
        selector = {"key": "id", "value": str(shelf.id)}
        await _archive_channel(shelf)
        delta = await next_state()
        assert delta["type"] == "state_delta"
        assert {"op": "set", "path": ["channels", selector, "archived"], "value": True} in delta["patches"]
        await _unarchive_channel(shelf)
        delta = await next_state()
        assert {"op": "remove", "path": ["channels", selector, "archived"]} in delta["patches"]
    finally:
        await stream.aclose()


def test_a_moving_channel_refuses_every_write_before_its_archive_allowances(archived):
    """The moving state comes first in ``channel_guard.STATES`` and allows
    nothing: an archived channel that is moving refuses even rename, with
    ``channel_moving``; reads go on."""
    import collimate_server.channel_guard as channel_guard
    client, shelf, desk = archived
    channel_guard.mark_moving(shelf)
    try:
        for method, url in (("PUT", f"/api/channels/{shelf}/rename?name=No"),
                            ("POST", f"/api/channels/{shelf}/unarchive"),
                            ("POST", f"/api/stacks/{shelf}/create")):
            refused = client.request(method, url)
            assert refused.status_code == 409 and refused.json()["detail"]["code"] == "channel_moving", url
        assert client.get(f"/api/channels/{shelf}/settings").status_code == 200
        with pytest.raises(channel_guard.ChannelMoving):
            channel_guard.require_writable(shelf)
    finally:
        channel_guard.clear_moving(shelf)
    assert client.post(f"/api/channels/{shelf}/unarchive").status_code == 200
