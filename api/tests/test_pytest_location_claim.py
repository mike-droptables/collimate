"""Plan 1105 S2a (P5): the locations routes and claiming a location.

``/api/storage/locations`` lists, checks, adds, claims, sets the one
default and removes an empty location. A folder another installation owns
is added only by claiming it (refused while its owner's server runs; an
owner that cannot be checked needs ``confirm_unchecked``, D18), and a
location claimed away from this installation shows ``foreign`` with Claim
as its only action and its channels unlisted."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.real_channel_homes


def _installation(data_dir: Path) -> str:
    conn = sqlite3.connect(data_dir / ".collimate" / "app.db")
    try:
        return conn.execute("SELECT id FROM installation").fetchone()[0]
    finally:
        conn.close()


def _defaults(data_dir: Path) -> list[str]:
    conn = sqlite3.connect(data_dir / ".collimate" / "app.db")
    try:
        return [row[0] for row in conn.execute("SELECT id FROM locations WHERE is_default = 1")]
    finally:
        conn.close()


def _fresh_app(monkeypatch, data_dir: Path, channels: Path):
    """The app for another data dir in this process, its default location
    ``channels``."""
    from conftest import reset_api_modules
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(data_dir)
    monkeypatch.setenv("COLLIMATE_CHANNELS", str(channels))
    reset_api_modules()
    return importlib.import_module("collimate_server.main").app


def _create(client, name="Channel") -> str:
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _owner_with_a_channel(monkeypatch, tmp_path: Path) -> tuple[Path, Path, str]:
    """An installation at ``tmp_path/owner`` owning ``tmp_path/Shared`` with
    one channel; answers its data dir, the location and the channel."""
    owner, shared = tmp_path / "owner", tmp_path / "Shared"
    with TestClient(_fresh_app(monkeypatch, owner, shared)) as client:
        return owner, shared, _create(client, "Owned")


def _locations(client) -> dict[str, dict]:
    response = client.get("/api/storage/locations")
    assert response.status_code == 200, response.text
    return {entry["path"]: entry for entry in response.json()["locations"]}


def _listed(client) -> list[str]:
    return [channel["id"] for channel in client.get("/api/channels").json()["channels"]]


def _code(response) -> str:
    return response.json()["detail"]["code"]


def _add(client, path: Path, **flags):
    return client.post("/api/storage/locations", json={"path": str(path), **flags})


def test_the_validation_codes(api_app, fresh_cwd, monkeypatch):
    import collimate_server.locations as locations
    with TestClient(api_app) as client:
        def checked(path) -> dict:
            response = client.post("/api/storage/locations/check", json={"path": str(path)})
            assert response.status_code == 200, response.text
            return response.json()

        def code(path) -> str | None:
            found = checked(path)
            assert found["ok"] is (found.get("error") is None)
            return (found.get("error") or {}).get("code")
        assert code("relative/Channels") == "not_absolute"
        a_file = fresh_cwd / "a-file"
        a_file.write_text("x")
        assert code(a_file) == "not_directory"
        assert code(a_file / "below") == "not_directory"
        if os.geteuid() != 0:
            locked = fresh_cwd / "locked"
            locked.mkdir(mode=0o500)
            assert code(locked) == "not_writable"
            assert code(locked / "new") == "not_writable"
        assert code(fresh_cwd / ".collimate" / "inside") == "inside_data_dir"
        assert code(fresh_cwd) == "inside_data_dir"
        assert code(fresh_cwd / "Channels" / "nested") == "overlaps_location"
        busy = fresh_cwd / "Documents"
        busy.mkdir()
        (busy / "notes.txt").write_text("mine")
        assert code(busy) == "not_empty"
        fresh = checked(fresh_cwd / "Fresh" / "Channels")
        assert fresh["ok"] and fresh["creates"] and "error" not in fresh
        assert checked(busy.parent / "Empty")["creates"]
        # A network filesystem warns and is still allowed.
        monkeypatch.setattr(locations, "_mount_type", lambda _path: "nfs4")
        warned = checked(fresh_cwd / "Fresh")
        assert warned["ok"] and warned["warning"]["code"] == "network_filesystem"
        monkeypatch.setattr(locations, "_mount_type", lambda _path: "ext4")
        # Add refuses what check refuses, with the code.
        refused = _add(client, busy)
        assert refused.status_code == 400 and _code(refused) == "not_empty"
        assert _add(client, "relative").status_code == 400
        added = _add(client, fresh_cwd / "Fresh" / "Channels")
        assert added.status_code == 201, added.text
        assert added.json()["location"]["state"] == "ok"
        assert code(fresh_cwd / "Fresh" / "Channels") == "overlaps_location"
        assert code(fresh_cwd / "Fresh") == "overlaps_location"


def test_check_reports_the_owner_and_add_without_claim_is_refused(monkeypatch, tmp_path):
    owner, shared, _channel = _owner_with_a_channel(monkeypatch, tmp_path)
    claimer = tmp_path / "claimer"
    with TestClient(_fresh_app(monkeypatch, claimer, tmp_path / "Mine")) as client:
        found = client.post("/api/storage/locations/check", json={"path": str(shared)}).json()
        assert found["ok"] is False and found["error"]["code"] == "location_owned"
        assert found["owned_by"] == {"installation_id": _installation(owner), "data_dir": str(owner),
                                     "channel_count": 1, "owner_running": False}
        refused = _add(client, shared)
        assert refused.status_code == 409 and _code(refused) == "location_owned"
        assert str(shared) not in _locations(client)
        assert _listed(client) == []
    marker = json.loads((shared / ".collimate-location.json").read_text())
    assert marker["installation_id"] == _installation(owner)


def test_claim_rewrites_the_marker_and_adopts_the_channels_reporting_duplicates(monkeypatch, tmp_path):
    owner, shared, owned = _owner_with_a_channel(monkeypatch, tmp_path)
    claimer = tmp_path / "claimer"
    app = _fresh_app(monkeypatch, claimer, tmp_path / "Mine")
    with TestClient(app) as client:
        mine = _create(client, "Mine")
        # A copy of a channel this installation already has, in the location.
        shutil.copytree(tmp_path / "Mine" / mine, shared / mine)
        response = _add(client, shared, claim=True)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["claimed"] == {"adopted": [owned], "skipped": [{"id": mine, "reason": "duplicate_channel_id"}]}
        assert body["location"]["state"] == "ok" and body["location"]["path"] == str(shared)
        assert body["location"]["is_default"] is False
        assert sorted(_listed(client)) == sorted([owned, mine])
        assert client.get(f"/api/channels/{owned}/tags").status_code == 200
    marker = json.loads((shared / ".collimate-location.json").read_text())
    assert marker["installation_id"] == _installation(claimer) and marker["data_dir"] == str(claimer)
    assert marker["claimed_at"]
    with TestClient(app) as client:
        assert sorted(_listed(client)) == sorted([owned, mine])
        assert _locations(client)[str(shared)]["state"] == "ok"


def test_claim_is_refused_while_the_owner_runs(monkeypatch, tmp_path):
    from collimate_server.data_dir_lock import acquire, release
    owner, shared, _channel = _owner_with_a_channel(monkeypatch, tmp_path)
    held = acquire(str(owner / ".collimate" / "server.lock"))
    try:
        with TestClient(_fresh_app(monkeypatch, tmp_path / "claimer", tmp_path / "Mine")) as client:
            found = client.post("/api/storage/locations/check", json={"path": str(shared)}).json()
            assert found["owned_by"]["owner_running"] is True
            refused = _add(client, shared, claim=True)
            assert refused.status_code == 409 and _code(refused) == "owner_running"
            assert refused.json()["detail"]["message"] == f"Stop the Collimate server at {owner} first."
            assert _listed(client) == []
    finally:
        release(held)
    assert json.loads((shared / ".collimate-location.json").read_text())["installation_id"] == \
        _installation(owner)


def test_a_location_claimed_away_shows_foreign_with_only_claim(api_app, fresh_cwd, tmp_path):
    second = fresh_cwd / "Second"
    with TestClient(api_app) as client:
        added = _add(client, second, default=True).json()["location"]
        away = _create(client, "Away")
        assert (second / away).is_dir()
        home = _locations(client)[str(fresh_cwd / "Channels")]["id"]
        assert client.put(f"/api/storage/locations/{home}/default").status_code == 200
        stays = _create(client, "Stays")
        # Another installation, on a machine this one cannot check, claims it.
        marker_path = second / ".collimate-location.json"
        marker = json.loads(marker_path.read_text())
        gone = tmp_path / "elsewhere"
        marker_path.write_text(json.dumps({**marker, "installation_id": "another-installation",
                                           "data_dir": str(gone)}))
        foreign = _locations(client)[str(second)]
        assert foreign["state"] == "foreign" and foreign["actions"] == ["claim"]
        assert foreign["owner_data_dir"] == str(gone) and foreign["channel_count"] == 1
        assert _listed(client) == [stays]
    with TestClient(api_app) as client:
        assert _listed(client) == [stays]
        foreign = _locations(client)[str(second)]
        assert foreign["state"] == "foreign" and foreign["actions"] == ["claim"]
        assert client.get(f"/api/channels/{away}/tags").status_code == 404
        refused = client.put(f"/api/storage/locations/{added['id']}/default")
        assert refused.status_code == 409 and _code(refused) == "location_foreign"
        unchecked = client.post(f"/api/storage/locations/{added['id']}/claim")
        assert unchecked.status_code == 409 and _code(unchecked) == "owner_unchecked"
        assert unchecked.json()["detail"]["owner_data_dir"] == str(gone)
        claimed = client.post(f"/api/storage/locations/{added['id']}/claim", json={"confirm_unchecked": True})
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["adopted"] == [away] and claimed.json()["skipped"] == []
        assert claimed.json()["location"]["state"] == "ok"
        assert sorted(_listed(client)) == sorted([stays, away])
        again = client.post(f"/api/storage/locations/{added['id']}/claim")
        assert again.status_code == 409 and _code(again) == "not_foreign"
    assert json.loads((second / ".collimate-location.json").read_text())["installation_id"] == \
        _installation(fresh_cwd)


def test_only_an_empty_location_can_be_removed(api_app, fresh_cwd):
    second = fresh_cwd / "Second"
    with TestClient(api_app) as client:
        added = _add(client, second, default=True).json()["location"]
        channel = _create(client, "Held")
        home = _locations(client)[str(fresh_cwd / "Channels")]["id"]
        refused = client.delete(f"/api/storage/locations/{added['id']}")
        assert refused.status_code == 409 and _code(refused) == "location_is_default"
        assert client.put(f"/api/storage/locations/{home}/default").status_code == 200
        held = client.delete(f"/api/storage/locations/{added['id']}")
        assert held.status_code == 409 and _code(held) == "location_not_empty"
        assert "remove" not in _locations(client)[str(second)]["actions"]
        last = client.delete(f"/api/storage/locations/{home}")
        assert last.status_code == 409 and _code(last) == "location_is_default"
        assert client.delete(f"/api/channels/{channel}").status_code == 200
        assert "remove" in _locations(client)[str(second)]["actions"]
        assert client.delete(f"/api/storage/locations/{added['id']}").status_code == 204
        assert str(second) not in _locations(client)
        assert not second.exists()
        assert client.delete(f"/api/storage/locations/{added['id']}").status_code == 404
        # A foreign entry is just dropped from the list, its folder untouched.
        third = fresh_cwd / "Third"
        listed = _add(client, third).json()["location"]
        marker_path = third / ".collimate-location.json"
        marker_path.write_text(json.dumps({**json.loads(marker_path.read_text()), "installation_id": "other"}))
        assert _locations(client)[str(third)]["state"] == "foreign"
        assert client.delete(f"/api/storage/locations/{listed['id']}").status_code == 204
        assert marker_path.is_file() and str(third) not in _locations(client)


def test_there_is_exactly_one_default(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        home = _locations(client)[str(fresh_cwd / "Channels")]
        assert home["is_default"] and _defaults(fresh_cwd) == [home["id"]]
        second = _add(client, fresh_cwd / "Second", default=True).json()["location"]
        assert second["is_default"]
        listed = _locations(client)
        assert [entry["id"] for entry in listed.values() if entry["is_default"]] == [second["id"]]
        assert _defaults(fresh_cwd) == [second["id"]]
        # New channels go to the default; the default changing moves none.
        channel = _create(client, "New")
        assert (fresh_cwd / "Second" / channel).is_dir()
        third = _add(client, fresh_cwd / "Third").json()["location"]
        assert third["is_default"] is False and _defaults(fresh_cwd) == [second["id"]]
        made = client.put(f"/api/storage/locations/{home['id']}/default")
        assert made.status_code == 200 and made.json()["is_default"] is True
        assert _defaults(fresh_cwd) == [home["id"]]
        assert [entry["id"] for entry in _locations(client).values() if entry["is_default"]] == [home["id"]]
        assert (fresh_cwd / "Second" / channel).is_dir()
        # A missing location cannot be the default.
        (fresh_cwd / "Third" / ".collimate-location.json").unlink()
        (fresh_cwd / "Third").rmdir()
        missing = client.put(f"/api/storage/locations/{third['id']}/default")
        assert missing.status_code == 409 and _code(missing) == "location_missing"
        assert _defaults(fresh_cwd) == [home["id"]]
        assert client.put("/api/storage/locations/nope/default").status_code == 404
