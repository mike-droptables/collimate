"""Plan 1105 P1: channel locations and their ownership at startup.

A location's marker names the one installation that owns it. An
installation that owns no location takes the default path
(``--channels-dir``, ``$COLLIMATE_CHANNELS``, else ``<data dir>/Channels``,
so data dirs started with nothing else never share one); one whose marker
names another installation refuses the start (exit 78 with a
machine-readable stderr line and one ``Collimate could not start`` line,
never a traceback or the running printout) unless ``--claim-channels``,
which is itself refused while that installation's server runs."""
from __future__ import annotations

import importlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
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


def _fresh_app(monkeypatch, data_dir: Path):
    """The app for another data dir in this process (its own lifespan)."""
    from conftest import reset_api_modules
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(data_dir)
    reset_api_modules()
    return importlib.import_module("collimate_server.main").app


def _owner_with_a_channel(monkeypatch, tmp_path: Path, location: Path) -> tuple[Path, str]:
    """A data dir ``owner`` whose installation owns ``location`` and has one
    channel there; answers the data dir and the channel id."""
    owner = tmp_path / "owner"
    monkeypatch.setenv("COLLIMATE_CHANNELS", str(location))
    with TestClient(_fresh_app(monkeypatch, owner)) as client:
        response = client.post("/api/channels", params={"name": "Owned"})
        assert response.status_code == 200, response.text
        return owner, response.json()["id"]


def test_a_fresh_start_creates_the_default_location_and_its_marker(api_app, fresh_cwd):
    with TestClient(api_app):
        pass
    location = fresh_cwd / "Channels"
    marker = json.loads((location / ".collimate-location.json").read_text())
    assert marker["format"] == 1 and marker["installation_id"] == _installation(fresh_cwd)
    assert marker["data_dir"] == str(fresh_cwd) and marker["claimed_at"] is None
    conn = sqlite3.connect(fresh_cwd / ".collimate" / "app.db")
    try:
        rows = conn.execute("SELECT id, path, is_default FROM locations").fetchall()
    finally:
        conn.close()
    assert rows == [(marker["location_id"], str(location), 1)]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _cli_env(**extra: str) -> dict[str, str]:
    """The CLI's environment: no channel location named, so each data dir
    takes its own default."""
    env = {key: value for key, value in os.environ.items() if key != "COLLIMATE_CHANNELS"}
    return {**env, "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", **extra}


def test_a_data_dir_with_no_location_named_creates_its_own_channels(monkeypatch, tmp_path):
    monkeypatch.delenv("COLLIMATE_CHANNELS")
    data_dir = tmp_path / "data"
    with TestClient(_fresh_app(monkeypatch, data_dir)) as client:
        created = client.post("/api/channels", params={"name": "Own"}).json()["id"]
    location = data_dir / "Channels"
    marker = json.loads((location / ".collimate-location.json").read_text())
    assert marker["installation_id"] == _installation(data_dir) and marker["data_dir"] == str(data_dir)
    assert (location / created / "channel.json").is_file()
    assert not (tmp_path / "Channels").exists()


def test_two_data_dirs_started_with_only_their_data_dir_both_serve(tmp_path):
    """The user's scenario: two servers, each started with nothing but its
    own --data-dir (and port), run side by side, each in its own location."""
    servers = []
    try:
        for name in ("one", "two"):
            port = _free_port()
            out = open(tmp_path / f"{name}.log", "w")
            servers.append((tmp_path / name, port, out, subprocess.Popen(
                [sys.executable, "-m", "collimate_server", "--data-dir", str(tmp_path / name), "--port", str(port)],
                env=_cli_env(), stdout=out, stderr=subprocess.STDOUT, text=True)))
        for data_dir, port, out, process in servers:
            deadline = time.monotonic() + 120
            while True:
                assert process.poll() is None, (tmp_path / f"{data_dir.name}.log").read_text()[-2000:]
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as answer:
                        if answer.status == 200:
                            break
                except OSError:
                    pass
                assert time.monotonic() < deadline, "no /health in 120 s"
                time.sleep(0.25)
    finally:
        for _data_dir, _port, out, process in servers:
            process.terminate()
            process.wait(timeout=60)
            out.close()
    for data_dir, port, _out, _process in servers:
        log = (tmp_path / f"{data_dir.name}.log").read_text()
        location = data_dir / "Channels"
        assert f"created the channel location {location}" in log
        assert f"http://localhost:{port}" in log and "collimate_startup_refused" not in log
        marker = json.loads((location / ".collimate-location.json").read_text())
        assert marker["installation_id"] == _installation(data_dir)
    assert _installation(tmp_path / "one") != _installation(tmp_path / "two")


def _refused():
    """The StartupRefused of the app module imported last (``_fresh_app``)."""
    return importlib.import_module("collimate_server.main").StartupRefused


def test_a_foreign_default_refuses_the_start(monkeypatch, tmp_path, capsys):
    from collimate_server.channel_paths import refusal_message
    location = tmp_path / "Shared"
    owner, _channel = _owner_with_a_channel(monkeypatch, tmp_path, location)
    capsys.readouterr()
    app = _fresh_app(monkeypatch, tmp_path / "second")
    with pytest.raises(_refused()) as refused:
        with TestClient(app):
            pass
    message = str(refused.value)
    assert message == refusal_message(location, str(owner), 1)
    assert message.startswith("The channel location ")
    assert message.endswith(
        f" belongs to another Collimate installation (data dir {owner}, 1 channel). Start with "
        "--claim-channels to make this installation its owner and adopt its channels (the other "
        "installation then shows the location as owned by another installation), or with --channels-dir "
        "PATH to keep this installation's channels in a new location at PATH.")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line == {"collimate_startup_refused": "location_owned", "path": str(location),
                    "owner_data_dir": str(owner), "channels": 1}
    # Nothing was written into the foreign location.
    marker = json.loads((location / ".collimate-location.json").read_text())
    assert marker["installation_id"] == _installation(owner)


def test_the_cli_exits_78_on_a_foreign_default(monkeypatch, tmp_path):
    location = tmp_path / "Shared"
    _owner_with_a_channel(monkeypatch, tmp_path, location)
    done = subprocess.run([sys.executable, "-m", "collimate_server", "--data-dir", str(tmp_path / "cli"),
                           "--port", str(_free_port())], env=_cli_env(COLLIMATE_CHANNELS=str(location)),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 78, done.stderr[-2000:]
    refusals = [json.loads(line) for line in done.stderr.splitlines()
                if line.startswith('{"collimate_startup_refused"')]
    assert refusals and refusals[0]["path"] == str(location) and refusals[0]["channels"] == 1
    # One line says why; no traceback, and nothing announced a running server.
    reasons = [line for line in done.stderr.splitlines() if "Collimate could not start: " in line]
    assert len(reasons) == 1 and "belongs to another Collimate installation" in reasons[0]
    assert "Traceback" not in done.stderr and "Application startup failed" not in done.stderr
    assert "Collimate is running" not in done.stdout


def test_a_port_in_use_never_announces_a_running_server(tmp_path):
    """The printout waits for uvicorn's bind, which follows the lifespan's
    startup: a taken port exits without it."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        done = subprocess.run([sys.executable, "-m", "collimate_server", "--data-dir", str(tmp_path / "cli"),
                               "--port", str(port)], env=_cli_env(), capture_output=True, text=True, timeout=120)
    assert done.returncode != 0, done.stderr[-2000:]
    assert "address already in use" in done.stderr.lower(), done.stderr[-2000:]
    assert "Collimate is running" not in done.stdout


def test_channels_dir_uses_a_new_location(monkeypatch, tmp_path):
    import collimate_server.channel_paths as channel_paths
    location = tmp_path / "Shared"
    _owner_with_a_channel(monkeypatch, tmp_path, location)
    own = tmp_path / "Mine"
    app = _fresh_app(monkeypatch, tmp_path / "second")
    channel_paths.configure_startup(channels_dir=str(own), claim=False)
    with TestClient(app) as client:
        assert client.get("/api/channels").json()["channels"] == []
        created = client.post("/api/channels", params={"name": "Mine"}).json()["id"]
    assert (own / created / "channel.json").is_file()
    assert json.loads((own / ".collimate-location.json").read_text())["installation_id"] == \
        _installation(tmp_path / "second")


def test_claim_channels_adopts_them_and_refuses_while_the_owner_runs(monkeypatch, tmp_path):
    import collimate_server.channel_paths as channel_paths
    from collimate_server.data_dir_lock import acquire, release
    location = tmp_path / "Shared"
    owner, channel_id = _owner_with_a_channel(monkeypatch, tmp_path, location)
    claimer = tmp_path / "claimer"
    held = acquire(str(owner / ".collimate" / "server.lock"))
    try:
        app = _fresh_app(monkeypatch, claimer)
        channel_paths.configure_startup(channels_dir=None, claim=True)
        with pytest.raises(_refused(), match=f"Stop the Collimate server at {owner} first"):
            with TestClient(app):
                pass
    finally:
        release(held)
    assert json.loads((location / ".collimate-location.json").read_text())["installation_id"] == \
        _installation(owner)
    app = _fresh_app(monkeypatch, claimer)
    channel_paths.configure_startup(channels_dir=None, claim=True)
    with TestClient(app) as client:
        assert client.get("/api/channels").json()["channels"] == [{"id": channel_id, "name": "Owned"}]
    marker = json.loads((location / ".collimate-location.json").read_text())
    assert marker["installation_id"] == _installation(claimer) and marker["data_dir"] == str(claimer)
    assert marker["claimed_at"]
    # The previous owner now has no location of its own: it refuses.
    channel_paths.configure_startup(channels_dir=None, claim=False)
    app = _fresh_app(monkeypatch, owner)
    with pytest.raises(_refused(), match="belongs to another Collimate installation"):
        with TestClient(app):
            pass


def test_a_claim_whose_owner_cannot_be_checked_needs_confirmation(monkeypatch, tmp_path):
    import shutil
    import collimate_server.channel_paths as channel_paths
    location = tmp_path / "Shared"
    owner, channel_id = _owner_with_a_channel(monkeypatch, tmp_path, location)
    shutil.rmtree(owner)
    _fresh_app(monkeypatch, tmp_path / "claimer")
    from collimate_server.database import get_global_db_connection
    get_global_db_connection().close()
    with pytest.raises(channel_paths.OwnerUnchecked):
        channel_paths.claim(location)
    claimed = channel_paths.claim(location, confirm_unchecked=True, default=True)
    assert claimed.adopted == [channel_id] and claimed.skipped == []


def test_the_flags_are_ignored_once_a_location_is_owned(api_app, fresh_cwd, caplog):
    import logging
    import collimate_server.channel_paths as channel_paths
    with TestClient(api_app):
        pass
    other = fresh_cwd / "Elsewhere"
    channel_paths.configure_startup(channels_dir=str(other), claim=True)
    with caplog.at_level(logging.WARNING), TestClient(api_app):
        pass
    assert not other.exists()
    assert any("ignored" in r.getMessage() and "Locations tab" in r.getMessage() for r in caplog.records)


def test_the_help_documents_both_flags():
    done = subprocess.run([sys.executable, "-m", "collimate_server", "--help"], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0
    assert "--channels-dir PATH" in done.stdout and "--claim-channels" in done.stdout
    assert "COLLIMATE_CHANNELS" in done.stdout and "78" in done.stdout
