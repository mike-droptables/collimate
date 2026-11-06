"""Plan 1105 S7 (P5): moving a channel's folder to another location.

``POST /api/channels/{id}/move {location_id}`` answers a job; one
filesystem is one rename, another is a copy into ``<uuid>.moving`` that is
verified, switched in under the locks and the source removed. A failed
verify keeps the source, the channel's writes are refused while it moves
(``channel_moving``), and every crash point recovers at the next start to
exactly one copy. A second filesystem is simulated: ``os.rename`` refusing
with EXDEV, or ``channel_move._same_filesystem`` answering False."""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.real_channel_homes

BODY = b"a body that travels with its channel"


class Crash(BaseException):
    """The server dying at a crash point (not an Exception: the job's own
    failure handling never sees it, as it would not see a killed process)."""


def _create(client, name="Mover") -> str:
    response = client.post("/api/channels", params={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _channel_with_body(client) -> tuple[str, str]:
    channel_id = _create(client)
    response = client.post(f"/api/channels/{channel_id}/blob", content=BODY)
    assert response.status_code == 200, response.text
    return channel_id, response.json()["blob_hash"]


def _second_location(client, root: Path, name="Second") -> str:
    response = client.post("/api/storage/locations", json={"path": str(root / name)})
    assert response.status_code == 201, response.text
    return response.json()["location"]["id"]


def _wait(client, job_id: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/storage/jobs/{job_id}").json()
        if job["state"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"the move job {job_id} did not finish")


def _move(client, channel_id: str, location_id: str) -> dict:
    response = client.post(f"/api/channels/{channel_id}/move", json={"location_id": location_id})
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["job_id"])


def _copies(root: Path, channel_id: str) -> list[Path]:
    """Every folder of the channel in both locations, staging and deleting
    ones included."""
    found = []
    for location in (root / "Channels", root / "Second"):
        if location.is_dir():
            found += [entry for entry in location.iterdir() if channel_id in entry.name]
    return sorted(found)


def _assert_usable(client, channel_id: str, digest: str) -> None:
    listed = [channel["id"] for channel in client.get("/api/channels").json()["channels"]]
    assert channel_id in listed
    assert client.get(f"/api/channels/{channel_id}/blob/{digest}").content == BODY
    # A write lands: the channel is writable again.
    assert client.put(f"/api/channels/{channel_id}/rename", params={"name": "Moved"}).status_code == 200


def _cross_filesystem(monkeypatch):
    import collimate_server.channel_move as channel_move
    monkeypatch.setattr(channel_move, "_same_filesystem", lambda _source, _destination: False)


def test_one_filesystem_is_one_rename(api_app, fresh_cwd):
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)
        job = _move(client, channel_id, second)
        assert job["state"] == "done" and job["method"] == "rename" and "error" not in job
        assert job["kind"] == "move" and job["channel_id"] == channel_id and job["to_location_id"] == second
        assert job["bytes_done"] == job["bytes_total"] > 0
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Second" / channel_id]
        _assert_usable(client, channel_id, digest)
    # A restart finds it in its new location.
    with TestClient(api_app) as client:
        assert client.get("/api/channels").json()["channels"] == [{"id": channel_id, "name": "Moved"}]
        locations = {entry["id"]: entry for entry in client.get("/api/storage/locations").json()["locations"]}
        assert locations[second]["channel_count"] == 1


def test_another_filesystem_copies_verifies_and_switches(api_app, fresh_cwd, monkeypatch):
    import collimate_server.channel_move as channel_move
    real_rename = os.rename

    def rename(source, target):
        # The folder's own rename crosses a filesystem; everything inside
        # one location still renames.
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(source, target)
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)
        monkeypatch.setattr(channel_move.os, "rename", rename)
        job = _move(client, channel_id, second)
        assert job["state"] == "done" and job["method"] == "copy", job
        assert job["bytes_done"] == job["bytes_total"] > len(BODY)
        assert job["files_done"] == job["files_total"] > 2
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Second" / channel_id]
        folder = fresh_cwd / "Second" / channel_id
        assert not (folder / ".collimate-move.json").exists()
        assert (folder / "objects" / digest[:2] / digest[2:]).read_bytes() == BODY
        conn = sqlite3.connect(folder / "channel.db")
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("SELECT value FROM ui_state WHERE key='layout'").fetchone()
        finally:
            conn.close()
        _assert_usable(client, channel_id, digest)


def test_a_failed_verify_keeps_the_source(api_app, fresh_cwd, monkeypatch):
    import collimate_server.channel_move as channel_move
    _cross_filesystem(monkeypatch)
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)

        def corrupt(step):
            if step == "copied":
                body = fresh_cwd / "Second" / f"{channel_id}.moving" / "objects" / digest[:2] / digest[2:]
                body.chmod(0o644)
                body.write_bytes(b"x" * len(BODY))
        monkeypatch.setattr(channel_move, "_crash_point", corrupt)
        job = _move(client, channel_id, second)
        assert job["state"] == "failed" and job["error"]["code"] == "verify_failed", job
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Channels" / channel_id]
        _assert_usable(client, channel_id, digest)


def test_a_channel_changed_during_the_copy_keeps_the_source(api_app, fresh_cwd, monkeypatch):
    import collimate_server.channel_move as channel_move
    _cross_filesystem(monkeypatch)
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)

        def write_behind_the_guard(step):
            if step == "verified":
                (fresh_cwd / "Channels" / channel_id / "cell-state").mkdir(exist_ok=True)
                (fresh_cwd / "Channels" / channel_id / "cell-state" / "late").write_text("late")
        monkeypatch.setattr(channel_move, "_crash_point", write_behind_the_guard)
        job = _move(client, channel_id, second)
        assert job["state"] == "failed" and job["error"]["code"] == "channel_changed", job
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Channels" / channel_id]
        _assert_usable(client, channel_id, digest)


CRASH_POINTS = [
    # (step, where the one copy is after the next start)
    ("copying", "Channels"),
    ("copied", "Channels"),
    ("verified", "Channels"),
    ("recorded", "Channels"),
    ("renamed_destination", "Second"),
    ("renamed_source", "Second"),
    ("unrecorded", "Second"),
]


@pytest.mark.parametrize("step,survivor", CRASH_POINTS)
def test_each_crash_point_recovers_to_exactly_one_copy(api_app, fresh_cwd, monkeypatch, step, survivor):
    import collimate_server.channel_move as channel_move
    _cross_filesystem(monkeypatch)
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)
        size, files = channel_move.folder_size(fresh_cwd / "Channels" / channel_id)
        channel_move.check(channel_id, second, size)
        job = channel_move.begin(channel_id, second, size, files)

        def crash(at):
            if at == step:
                raise Crash(at)
        monkeypatch.setattr(channel_move, "_crash_point", crash)
        with pytest.raises(Crash):
            channel_move.run(job)
    # The next start's reconcile: one folder, no staging, deleting or record.
    with TestClient(api_app) as client:
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / survivor / channel_id]
        assert not (fresh_cwd / survivor / channel_id / ".collimate-move.json").exists()
        _assert_usable(client, channel_id, digest)


def test_refusals(api_app, fresh_cwd, monkeypatch):
    import collimate_server.channel_move as channel_move
    import collimate_server.run_registry as run_registry
    from collimate_server.cas import channel_store
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)

        def refused(location_id=second):
            response = client.post(f"/api/channels/{channel_id}/move", json={"location_id": location_id})
            return response.status_code, (response.json()["detail"] or {}).get("code")
        # Live runs.
        busy = run_registry.busy_channels
        monkeypatch.setattr(run_registry, "busy_channels", lambda: {channel_id})
        assert refused() == (409, "runs_live")
        monkeypatch.setattr(run_registry, "busy_channels", busy)
        # Held pins (an export streaming).
        with channel_store(channel_id).pin_blobs([digest]):
            assert refused() == (409, "pins_held")
        # The location it is in; an unknown one.
        here = next(entry["id"] for entry in client.get("/api/storage/locations").json()["locations"]
                    if entry["is_default"])
        assert refused(here) == (409, "same_location")
        assert refused("nope")[0] == 404
        # Too little free space on another filesystem.
        same, free = channel_move._same_filesystem, channel_move._free_bytes
        _cross_filesystem(monkeypatch)
        monkeypatch.setattr(channel_move, "_free_bytes", lambda _path: 10)
        assert refused() == (409, "insufficient_space")
        monkeypatch.setattr(channel_move, "_same_filesystem", same)
        monkeypatch.setattr(channel_move, "_free_bytes", free)
        # A foreign destination: another installation claimed it.
        marker_path = fresh_cwd / "Second" / ".collimate-location.json"
        marker = json.loads(marker_path.read_text())
        marker_path.write_text(json.dumps({**marker, "installation_id": "someone-else",
                                           "data_dir": str(fresh_cwd / "elsewhere")}))
        assert client.get("/api/storage/locations").status_code == 200
        assert refused() == (409, "location_foreign")
        marker_path.write_text(json.dumps(marker))
        # A missing destination.
        third = _second_location(client, fresh_cwd, "Third")
        (fresh_cwd / "Third" / ".collimate-location.json").unlink()
        (fresh_cwd / "Third").rmdir()
        client.get("/api/storage/locations")
        assert refused(third) == (409, "location_missing")
        # Nothing moved.
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Channels" / channel_id]
        assert client.get("/api/storage/jobs/unknown").status_code == 404


def test_writes_are_refused_during_the_copy(api_app, fresh_cwd, monkeypatch):
    import collimate_server.channel_move as channel_move
    _cross_filesystem(monkeypatch)
    seen = {}
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)

        def during(step):
            if step == "copied":
                write = client.post(f"/api/channels/{channel_id}/blob", content=b"late")
                seen["write"] = (write.status_code, write.json()["detail"]["code"])
                rename = client.put(f"/api/channels/{channel_id}/rename", params={"name": "No"})
                seen["rename"] = rename.status_code
                again = client.post(f"/api/channels/{channel_id}/move", json={"location_id": second})
                seen["move"] = again.status_code
                archive = client.post(f"/api/channels/{channel_id}/archive")
                seen["archive"] = (archive.status_code, archive.json()["detail"]["code"])
                seen["read"] = client.get(f"/api/channels/{channel_id}/blob/{digest}").content
                seen["listed"] = channel_id in [c["id"] for c in client.get("/api/channels").json()["channels"]]
        monkeypatch.setattr(channel_move, "_crash_point", during)
        job = _move(client, channel_id, second)
        assert job["state"] == "done", job
        assert seen == {"write": (409, "channel_moving"), "rename": 409, "move": 409,
                        "archive": (409, "channel_moving"), "read": BODY, "listed": True}
        _assert_usable(client, channel_id, digest)


def test_an_archived_channel_moves(api_app, fresh_cwd, monkeypatch):
    # Move is in ARCHIVE_SAFE: an archived channel moves whole, its flag
    # in the channel.json that travels with it.
    _cross_filesystem(monkeypatch)
    with TestClient(api_app) as client:
        channel_id, digest = _channel_with_body(client)
        second = _second_location(client, fresh_cwd)
        archive = client.post(f"/api/channels/{channel_id}/archive")
        assert archive.status_code == 200, archive.text
        job = _move(client, channel_id, second)
        assert job["state"] == "done", job
        moved = json.loads((fresh_cwd / "Second" / channel_id / "channel.json").read_text())
        assert moved["archived"] is True
        assert _copies(fresh_cwd, channel_id) == [fresh_cwd / "Second" / channel_id]
