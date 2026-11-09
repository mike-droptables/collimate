"""Plan 1105 P1: every channel is one self-contained folder in its location.

``<location>/<uuid>/`` holds channel.json, channel.db (its layout and cell
metadata journal), its git store and its own body store; nothing about a
channel stays in app.db but the active channel id. These tests see the
resolver as the server does (``real_channel_homes``)."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.real_channel_homes

ALLOWED = {"channel.json", "channel.db", "channel.db-wal", "channel.db-shm", "git", "git-rescue",
           "objects", "objects.trash", "cell-state"}


def _create(client, name="Folder") -> str:
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _folder(fresh_cwd: Path, channel_id: str) -> Path:
    return fresh_cwd / "Channels" / channel_id


def _db(fresh_cwd: Path, channel_id: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_folder(fresh_cwd, channel_id) / "channel.db")
    conn.row_factory = sqlite3.Row
    return conn


def _add_cell(channel_id: str, name: str = "Note"):
    from collimate_server.models import Cell, Stack
    from collimate_server.state import get_channel_by_id, persist_state, flush_persist_state
    channel = get_channel_by_id(channel_id)
    cell = Cell(name=name)
    channel.main_stacks.append(Stack(name="Main", cells=[cell]))
    persist_state()
    flush_persist_state()
    return cell


def test_a_channel_folder_holds_exactly_its_layout(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        channel_id = _create(client)
        folder = _folder(fresh_cwd, channel_id)
        assert folder.is_dir()
        names = {entry.name for entry in folder.iterdir()}
        assert {"channel.json", "channel.db"} <= names <= ALLOWED
        manifest = json.loads((folder / "channel.json").read_text())
        from collimate_server.channel_paths import installation_id
        assert manifest == {"format": 1, "channel_id": channel_id, "name": "Folder", "archived": False,
                            "archived_at": None, "installation_id": installation_id(),
                            "created_at": manifest["created_at"]}
        # A body uploaded to the channel lands in its own store.
        digest = client.post(f"/api/channels/{channel_id}/blob", content=b"body bytes").json()["blob_hash"]
        assert (folder / "objects" / digest[:2] / digest[2:]).read_bytes() == b"body bytes"
        assert client.get(f"/api/channels/{channel_id}/blob/{digest}").content == b"body bytes"
        # Nothing is kept for the channel under the data dir.
        assert not (fresh_cwd / ".collimate" / "channels").exists()
        assert not (fresh_cwd / ".collimate" / "objects").exists()
    # No staging folder survives the create.
    assert not any(entry.name.endswith(".moving") for entry in (fresh_cwd / "Channels").iterdir())


def test_a_blob_of_one_channel_is_not_served_by_another(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        first, second = _create(client, "One"), _create(client, "Two")
        digest = client.post(f"/api/channels/{first}/blob", content=b"only here").json()["blob_hash"]
        assert client.get(f"/api/channels/{second}/blob/{digest}").status_code == 404
        assert client.post("/api/blob", content=b"x").status_code in (404, 405)


def test_rename_layout_and_cell_journal_live_in_the_folder(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        channel_id = _create(client, "Before")
        assert client.put(f"/api/channels/{channel_id}/rename", params={"name": "After"}).status_code == 200
        manifest = json.loads((_folder(fresh_cwd, channel_id) / "channel.json").read_text())
        assert manifest["name"] == "After"
        cell = _add_cell(channel_id)
        response = client.patch(f"/api/channels/{channel_id}/cells/{cell.id}/meta", json={"name": "Renamed"})
        assert response.status_code == 200, response.text
        conn = _db(fresh_cwd, channel_id)
        try:
            layout = json.loads(conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()[0])
            events = conn.execute("SELECT cell_id, old_name, new_name FROM cell_rename_events").fetchall()
            heads = conn.execute("SELECT cell_id FROM cell_metadata_heads").fetchall()
        finally:
            conn.close()
        assert layout["format"] == "channel-layout-v1" and layout["id"] == channel_id and "name" not in layout
        assert [cell["name"] for stack in layout["main_stacks"] for cell in stack["cells"]] == ["Renamed"]
        assert [tuple(row) for row in events] == [(str(cell.id), "Note", "Renamed")]
        assert str(cell.id) in {row[0] for row in heads}
        app_db = sqlite3.connect(fresh_cwd / ".collimate" / "app.db")
        try:
            tables = {row[0] for row in app_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            saved = json.loads(app_db.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0])
        finally:
            app_db.close()
        assert not tables & {"cell_rename_events", "cell_metadata_heads", "cell_metadata_operations"}
        assert saved == {"storage_format": "sql-layout-v3", "active_channel_id": channel_id}
    # A restart reads the name and layout back from the folder.
    with TestClient(api_app) as client:
        listed = client.get("/api/channels").json()
        assert listed["channels"] == [{"id": channel_id, "name": "After"}]
        assert listed["active_channel_id"] == channel_id
        from collimate_server.state import get_channel_by_id
        restored = get_channel_by_id(channel_id)
        assert [c.name for s in restored.main_stacks for c in s.cells] == ["Renamed"]


def test_delete_frees_the_folder_and_refuses_while_pins_are_held(api_app, fresh_cwd):
    from collimate_server.cas import channel_store
    with TestClient(api_app) as client:
        channel_id = _create(client)
        digest = client.post(f"/api/channels/{channel_id}/blob", content=b"exported").json()["blob_hash"]
        store = channel_store(channel_id)
        with store.pin_blobs([digest]):
            refused = client.delete(f"/api/channels/{channel_id}")
            assert refused.status_code == 409 and "export" in refused.text
        assert _folder(fresh_cwd, channel_id).is_dir()
        assert client.delete(f"/api/channels/{channel_id}").status_code == 200
        assert not _folder(fresh_cwd, channel_id).exists()
        assert not [p for p in (fresh_cwd / "Channels").iterdir() if p.name.startswith(".deleting-")]
        assert client.get(f"/api/channels/{channel_id}/settings").status_code == 404
        assert client.get("/api/channels").json()["channels"] == []


def test_a_deleting_leftover_and_a_moving_staging_are_removed_at_startup(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        kept = _create(client)
    location = fresh_cwd / "Channels"
    (location / ".deleting-00000000-0000-4000-8000-000000000000-abcd").mkdir()
    (location / "00000000-0000-4000-8000-000000000001.moving").mkdir()
    with TestClient(api_app) as client:
        assert [c["id"] for c in client.get("/api/channels").json()["channels"]] == [kept]
    assert sorted(p.name for p in location.iterdir() if not p.name.startswith(".collimate")) == [kept]


def test_a_folder_that_does_not_open_is_parked(api_app, fresh_cwd):
    location = fresh_cwd / "Channels"
    with TestClient(api_app) as client:
        _create(client)
    stray = location / "notes"
    stray.mkdir()
    (stray / "readme.txt").write_text("mine")
    with TestClient(api_app):
        pass
    assert not stray.exists()
    parked = list((location / ".orphaned").iterdir())
    assert len(parked) == 1 and (parked[0] / "readme.txt").read_text() == "mine"


def test_split_copy_and_config_duplicate_bodies_into_the_target(api_app, fresh_cwd):
    """A body crossing into another channel's store is copied and
    verified: the copy is independent of the source's collection."""
    from collimate_server.cas import channel_store, copy_bodies
    with TestClient(api_app) as client:
        source, target = _create(client, "Source"), _create(client, "Target")
        digest = client.post(f"/api/channels/{source}/blob", content=b"\x00binary body").json()["blob_hash"]
        copy_bodies(channel_store(source), channel_store(target), [digest])
        assert channel_store(target).require_blob(digest) == b"\x00binary body"
        assert channel_store(target).blob_path(digest) != channel_store(source).blob_path(digest)
        assert Path(channel_store(target).blob_path(digest)).stat().st_ino != \
            Path(channel_store(source).blob_path(digest)).stat().st_ino
        # A new channel's config from a source channel's main stacks brings
        # the bodies of their binary files into its own store.
        from collimate_server.models import Cell, Stack, VirtualFile
        from collimate_server.persistence import initialize_files
        from collimate_server.database import get_db_connection
        from collimate_server.state import get_channel_by_id, persist_state, flush_persist_state
        channel = get_channel_by_id(source)
        cell = Cell(name="Pictures", files=[VirtualFile(name="a.bin", content="", blob_hash=digest)])
        conn = get_db_connection(source)
        try:
            initialize_files(conn, str(cell.id), cell.files)
            conn.commit()
        finally:
            conn.close()
        channel.main_stacks.append(Stack(name="Main", cells=[cell]))
        persist_state()
        flush_persist_state()
        response = client.post("/api/channels", params={"name": "Assembled"},
                               json={"config": {"main_cells": {"channel_id": source}}})
        assert response.status_code == 200, response.text
        assembled = response.json()["id"]
        assert channel_store(assembled).require_blob(digest) == b"\x00binary body"
        # The source can go; the copy stays readable.
        assert client.delete(f"/api/channels/{source}").status_code == 200
        assert channel_store(assembled).require_blob(digest) == b"\x00binary body"


def test_one_unreadable_channel_does_not_stop_another_s_collection(api_app, fresh_cwd):
    import collimate_server.cas_gc as cas_gc
    with TestClient(api_app) as client:
        good, bad = _create(client, "Good"), _create(client, "Bad")
        (_folder(fresh_cwd, bad) / "channel.db-wal").unlink(missing_ok=True)
        from collimate_server.database import forget_db
        forget_db(bad)
        (_folder(fresh_cwd, bad) / "channel.db").write_bytes(b"not a database at all" * 100)
        result = cas_gc.run_gc()
        outcomes = {entry["id"]: entry for entry in result["channels"]}
        assert outcomes[bad].get("aborted") is True
        assert not outcomes[good].get("aborted")
        assert not result["app_store"].get("aborted")


def test_a_missing_location_leaves_its_channels_unavailable_and_creates_nothing(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        channel_id = _create(client)
    location = fresh_cwd / "Channels"
    unplugged = fresh_cwd / "Unplugged"
    location.rename(unplugged)
    with TestClient(api_app) as client:
        assert client.get("/api/channels").json()["channels"] == []
        refused = client.post("/api/channels", params={"name": "New"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "no_default_location"
    assert not location.exists()
    assert (unplugged / channel_id / "channel.db").is_file()
    unplugged.rename(location)
    with TestClient(api_app) as client:
        assert [c["id"] for c in client.get("/api/channels").json()["channels"]] == [channel_id]


def test_an_invalid_layout_leaves_only_that_channel_unavailable(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        good, bad = _create(client, "Good"), _create(client, "Bad")
    conn = _db(fresh_cwd, bad)
    try:
        conn.execute("UPDATE ui_state SET value=? WHERE key='layout'", (json.dumps({"format": "nonsense"}),))
        conn.commit()
    finally:
        conn.close()
    with TestClient(api_app) as client:
        listed = client.get("/api/channels").json()
        assert [c["id"] for c in listed["channels"]] == [good] and "unavailable" not in listed
        managed = {c["id"]: c for c in client.get("/api/channels/manage").json()["channels"]}
        assert (managed[bad]["name"], managed[bad]["state"]) == ("Bad", "unavailable") and managed[bad]["reason"]
        assert managed[good]["state"] == "ok"
        refused = client.get(f"/api/channels/{bad}/settings")
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "channel_unavailable"
    conn = _db(fresh_cwd, bad)
    try:
        assert json.loads(conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()[0]) == \
            {"format": "nonsense"}
    finally:
        conn.close()


def test_an_old_layout_data_dir_starts_with_one_line_and_nothing_read(api_app, fresh_cwd, caplog):
    old = fresh_cwd / ".collimate"
    (old / "channels").mkdir(parents=True)
    (old / "channels" / "11111111-1111-4111-8111-111111111111.db").write_bytes(b"old")
    (old / "objects").mkdir()
    with caplog.at_level(logging.WARNING), TestClient(api_app) as client:
        assert client.get("/api/channels").json()["channels"] == []
    lines = [r.getMessage() for r in caplog.records if "previous storage layout" in r.getMessage()]
    assert len(lines) == 1 and "can be removed by hand" in lines[0]
    assert (old / "channels" / "11111111-1111-4111-8111-111111111111.db").read_bytes() == b"old"


def test_a_cell_state_dir_is_in_the_channel_folder(api_app, fresh_cwd):
    import collimate_server.channel_paths as channel_paths
    with TestClient(api_app) as client:
        channel_id = _create(client)
        cell = "22222222-2222-4222-8222-222222222222"
        assert channel_paths.cell_state_dir(channel_id, cell) == _folder(fresh_cwd, channel_id) / "cell-state" / cell
        with pytest.raises(channel_paths.ChannelNotFound):
            channel_paths.cell_state_dir("33333333-3333-4333-8333-333333333333", cell)


def test_a_run_in_a_cell_keeps_its_state_in_the_channel_folder(client, fresh_cwd):
    """The host hands a run with a cell its state dir in the channel folder
    (``COLLIMATE_CELL_STATE``); two runs of one cell see the same one, so
    what the first leaves there the second finds (an Agent's session)."""
    import time
    from conftest import install_runner
    script = (
        "# /// script\n# requires-python = \">=3.10\"\n# dependencies = []\n# ///\n"
        "import os, pathlib\n"
        "state = pathlib.Path(os.environ['COLLIMATE_CELL_STATE'])\n"
        "state.mkdir(parents=True, exist_ok=True)\n"
        "seen = (state / 'turns').read_text() if (state / 'turns').exists() else ''\n"
        "(state / 'turns').write_text(seen + 'x')\n"
        "print('CELL_STATE', state, 'TURNS', len(seen) + 1)\n"
    )
    runner, _ = install_runner(generator="state_probe", entrypoint_body=script)
    cell_id = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    outputs = []
    for _ in range(2):
        response = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell_id})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 90
        while True:
            result = client.get(f"/api/channels/{client.channel_id}/runs/{run_id}/events_since?since_id=0").json()
            if result.get("is_terminal") or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        assert result["status"] == "completed", result
        outputs.append("".join(e["payload"] for e in result["events"] if e["type"] == "stdout"))
    state = _folder(fresh_cwd, client.channel_id) / "cell-state" / cell_id
    assert f"CELL_STATE {state} TURNS 1" in outputs[0] and f"CELL_STATE {state} TURNS 2" in outputs[1]
    assert (state / "turns").read_text() == "xx"


def _symlinked_home(fresh_cwd: Path, monkeypatch) -> Path:
    """macOS's layout under ``$TMPDIR``: ``/var/folders`` is a symlink to
    ``/private/var/folders``. The data dir is entered through the link (so
    ``os.getcwd()`` is the resolved form) and the default location is named
    through it (kept as given), so SQLite's resolved database paths and the
    location's path name one folder two ways. Answers the linked root."""
    real = fresh_cwd / "private" / "var"
    (real / "data").mkdir(parents=True)
    link = fresh_cwd / "var"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(link / "data")
    monkeypatch.setenv("COLLIMATE_CHANNELS", str(link / "HOME" / "Collimate" / "Channels"))
    return link


def test_a_location_named_through_a_symlink_creates_imports_renames_and_deletes(api_app, fresh_cwd, monkeypatch):
    """v0.1.1's mac legs answered the packaged server's first channel create
    500: the new channel DB's first commit walked its publication directories
    from SQLite's resolved path up to the data dir (``boundary_of`` found no
    location holding it as written) and raised ValueError, "is not in the
    subpath of"."""
    link = _symlinked_home(fresh_cwd, monkeypatch)
    location = link / "HOME" / "Collimate" / "Channels"
    with TestClient(api_app) as client:
        channel_id = _create(client, "Run probe")
        folder = location / channel_id
        assert (folder / "channel.json").is_file() and (folder / "channel.db").is_file()
        streamed = client.post(f"/api/channels/{channel_id}/blob/stream", content=b"# notes\n")
        assert streamed.status_code == 200, streamed.text
        digest = streamed.json()["blob_hash"]
        assert (folder / "objects" / digest[:2] / digest[2:]).read_bytes() == b"# notes\n"
        imported = client.post(f"/api/channels/{channel_id}/import", json={"insert_index": None, "stacks": [
            {"name": "Notes", "cells": [{"name": "Notes", "files": [{"path": "notes.md", "blob_hash": digest}],
                                         "dirs": []}]}]})
        assert imported.status_code == 200, imported.text
        assert client.put(f"/api/channels/{channel_id}/rename", params={"name": "After"}).status_code == 200
        assert json.loads((folder / "channel.json").read_text())["name"] == "After"
        assert client.delete(f"/api/channels/{channel_id}").status_code == 200
        assert not folder.exists()
        assert not any(entry.name.endswith(".moving") for entry in location.iterdir())


def test_a_resolved_path_in_a_linked_location_has_that_location_as_its_boundary(api_app, fresh_cwd, monkeypatch):
    import os
    link = _symlinked_home(fresh_cwd, monkeypatch)
    location = link / "HOME" / "Collimate" / "Channels"
    with TestClient(api_app):
        from collimate_server.channel_paths import boundary_of
        assert boundary_of(location / "a-channel" / "channel.db") == str(location)
        assert boundary_of(Path(os.path.realpath(location)) / "a-channel" / "channel.db") == str(location)
        assert boundary_of(link / "data" / ".collimate" / "app.db") == os.getcwd()


def test_a_publication_walk_follows_the_real_folders_when_named_two_ways(tmp_path):
    from collimate_server.cas import publication_directories
    real = tmp_path / "private" / "var"
    (real / "location" / "channel").mkdir(parents=True)
    (real / "elsewhere").mkdir()
    link = tmp_path / "var"
    link.symlink_to(real, target_is_directory=True)
    # Named one way, the walk is the names as given.
    assert publication_directories(link / "location" / "channel", link / "location") == (
        str(link / "location" / "channel"), str(link / "location"))
    # Named two ways, it is the real folders, either way round.
    real_walk = (str(real / "location" / "channel"), str(real / "location"))
    assert publication_directories(real / "location" / "channel", link / "location") == real_walk
    assert publication_directories(link / "location" / "channel", real / "location") == real_walk
    # A directory outside the boundary is still refused.
    with pytest.raises(ValueError):
        publication_directories(real / "elsewhere", link / "location")
