"""The demo runner against the real app: a synthetic timeline run in-process.

``ProcessManager.start`` is swapped for the shim's ``start`` exactly as
``demo_runner.install()`` does in the browser, and a throwaway generator's
timeline exercises the ``demo`` contract end to end: work files land in the
cell, UI files land in the run's own ``_runs/<label>/`` folder only for the
declared types, the chat and terminal surfaces register by run, a
reveal reaches the channel as the host's ``reveal_file`` event, and the run
end sweeps the folder into the run's Logs. Two runs share one cell; stopping
one leaves the other's folder alone.

The timeline asserts from the inside; a failed assertion fails its run, whose
log carries the traceback.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import yaml

SHIM_DIR = Path(__file__).resolve().parents[2] / "shim"
if str(SHIM_DIR) not in sys.path:
    sys.path.insert(0, str(SHIM_DIR))

from collimate_playground import demo_runner  # noqa: E402
from conftest import create_demo_channel  # noqa: E402

GEN = "probe-gen"
RUNNER = "probe-runner"

MANIFEST = {
    "id": GEN,
    "name": "Probe",
    "version": "1.0.0",
    "summary": "A synthetic timeline for the demo runner's tests.",
    "entrypoint": "timeline.py",
    "ui_files": [".chat", ".json", ".terminal"],
    "main_config": "settings.set_yaml",
    "draft_files": ["settings.set_yaml"],
    "required_files": [],
    "config_folders": [],
    "account_requirements": {},
}

SETTINGS = {"fields": [{
    "key": "gate", "type": "text", "label": "Gate",
    "description": "A file whose appearance ends the run.", "default": "", "advanced": False,
}]}

TIMELINE = '''
import asyncio
import json
import os


async def play(demo):
    demo.phase("spinning_up", "probe")
    assert demo.folder == "_runs/" + demo.label
    (cell,) = demo.inputs
    assert "notes.md" in cell["files"], cell
    assert not any(p.startswith("_runs/") for p in cell["files"]), sorted(cell["files"])

    demo.write("work/out.md", "# out\\n")
    chat = demo.chat_session("Probe chat")
    demo.update_ui_file(chat.transcript, json.dumps({"messages": []}))
    term = demo.terminal("Probe shell")
    await demo.commit()
    await demo.reveal_file(chat.pointer_path)
    await chat.start_turn()
    await chat.delta("hello")
    await chat.end_turn()
    await term.write("probe$ ")

    for bad in ("notes.txt", "../escape.chat", demo.label + ".run"):
        try:
            demo.update_ui_file(bad, "x")
        except ValueError:
            pass
        else:
            raise AssertionError("update_ui_file accepted " + bad)
    try:
        demo.write(demo.run_path("sneaky.chat"), "x")
    except PermissionError:
        pass
    else:
        raise AssertionError("write accepted a path under the run folder")

    # A file the shim's checks would never stage still meets the host's rule.
    demo._stage(demo.run_path("stray.txt"), "x")
    try:
        await demo.commit()
    except PermissionError as exc:
        assert "ui_type" in str(exc), exc
    else:
        raise AssertionError("the host accepted an undeclared UI file type")

    demo.phase("awaiting_input", "waiting for the gate")
    while not os.path.exists(demo.params["gate"]):
        await demo.sleep(0.05)
    demo.finish(success=True, message="probe done")
'''


@pytest.fixture
def probe_tree(tmp_path):
    root = tmp_path / "generators"
    gen = root / "probe-cell" / GEN
    gen.mkdir(parents=True)
    (root / "probe-cell" / "cell.yaml").write_text(yaml.safe_dump({"name": "Probe generators", "icon": "sparkles"}))
    (gen / "definition.generator").write_text(yaml.safe_dump(MANIFEST, sort_keys=False))
    (gen / "settings.set_yaml").write_text(yaml.safe_dump(SETTINGS, sort_keys=False))
    (gen / "timeline.py").write_text(TIMELINE)
    return root


@pytest.fixture
def demo_app(app_client, probe_tree, tmp_path, monkeypatch):
    """The app with the demo runner installed, one channel holding the probe
    runner and a ``Notes`` cell. Yields ``(client, channel_id, cell_id, gate,
    reveals)``; ``reveals`` collects every ``reveal_file`` broadcast."""
    client = app_client(probe_tree)
    # install() patches os.kill & co. and ProcessManager.start; monkeypatch
    # restores both when the test ends.
    monkeypatch.setattr(os, "kill", os.kill)
    monkeypatch.setattr(os, "killpg", os.killpg)
    monkeypatch.setattr(os, "getpgid", os.getpgid)
    monkeypatch.setattr(demo_runner, "_signals_installed", False)
    from process_manager.manager import ProcessManager
    monkeypatch.setattr(ProcessManager, "start", ProcessManager.start)
    demo_runner.install()

    import events
    reveals: list[dict] = []
    native_broadcast = events.broadcast

    def recording_broadcast(channel, payload, *args, **kwargs):
        if isinstance(payload, dict) and payload.get("type") == "reveal_file":
            reveals.append(payload)
        return native_broadcast(channel, payload, *args, **kwargs)

    monkeypatch.setattr(events, "broadcast", recording_broadcast)

    gate = tmp_path / "gate"
    draft = {"runner": {
        "id": RUNNER, "generator": GEN, "name": "Probe runner", "description": "", "tags": [],
        "prompt": "", "settings": {"settings.set_yaml": {"gate": str(gate)}},
        "accounts": {}, "folders": [], "permissions": {}, "packages": {},
    }, "files": []}
    created = create_demo_channel(client, "Probe", [draft])
    assert created.status_code == 200, created.text
    from state import state
    (channel,) = state.channels
    channel_id = str(channel.id)
    stored = client.post("/api/blob/stream", content=b"# Notes\n")
    assert stored.status_code == 200, stored.text
    made = client.post(f"/api/channels/{channel_id}/import", json={"insert_index": None, "stacks": [{"name": "", "cells": [
        {"name": "Notes", "files": [{"path": "notes.md", "blob_hash": stored.json()["blob_hash"]}], "dirs": []},
    ]}]})
    assert made.status_code == 200, made.text
    yield client, channel_id, made.json()["cell_ids"][0], gate, reveals


def _cell(client, cell_id: str) -> dict:
    for ch in client.get("/api/state").json()["channels"]:
        for stack in ch["main_stacks"]:
            for cell in stack["cells"]:
                if cell["id"] == cell_id:
                    return cell
    raise AssertionError(f"cell {cell_id} is gone")


def _files(client, cell_id: str) -> set[str]:
    return {f["name"] for f in _cell(client, cell_id).get("files", [])}


def _wait(what: str, check, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _start(client, channel_id: str, cell_id: str) -> dict:
    started = client.post(f"/api/channels/{channel_id}/runs", json={"runner": RUNNER, "cell_id": cell_id})
    assert started.status_code == 200, started.text
    return started.json()


def _run(client, channel_id: str, run_id: str) -> dict:
    return client.get(f"/api/channels/{channel_id}/runs/{run_id}").json()["run"]


def _ended(client, channel_id: str, run_id: str) -> dict:
    return _wait(f"run {run_id} to end",
                 lambda: (r := _run(client, channel_id, run_id))["status"] != "running" and r)


def _surfaces(folder: str) -> set[str]:
    return {f"{folder}/chat/conversation.chat", f"{folder}/chat/main.json",
            f"{folder}/terminal/session.terminal"}


def test_a_demo_run_writes_its_folder_reveals_and_is_swept__P6F01(demo_app):
    from sessions.chat import get_inbox
    client, channel_id, cell_id, gate, reveals = demo_app

    run = _start(client, channel_id, cell_id)
    run_id, label, folder = run["run_id"], run["label"], run["folder"]
    assert folder == f"_runs/{label}" and run["cell_id"] == cell_id

    card = f"{folder}/{label}.run"
    live = _surfaces(folder) | {card, "work/out.md", "notes.md"}
    _wait("the run's files", lambda: live <= _files(client, cell_id))
    assert f"{folder}/stray.txt" not in _files(client, cell_id)
    (row,) = client.get(f"/api/channels/{channel_id}/runs/active").json()
    assert (row["run_id"], row["cell_id"], row["label"], row["folder"]) == (run_id, cell_id, label, folder)
    _wait("the reveal", lambda: reveals)
    assert reveals == [{"type": "reveal_file", "run_id": run_id, "cell_id": cell_id,
                        "path": f"{folder}/chat/conversation.chat"}]
    inbox = get_inbox(run_id)
    assert inbox is not None and inbox.surface == f"{folder}/chat/conversation.chat"

    gate.write_text("go")
    final = _ended(client, channel_id, run_id)
    assert final["status"] == "completed", final

    files = _wait("the sweep", lambda: (f := _files(client, cell_id)) and not any(p.startswith("_runs/") for p in f) and f)
    assert files == {"notes.md", "work/out.md"}
    logs = {a["path"] for a in client.get(f"/api/channels/{channel_id}/runs/{run_id}/artifacts").json()["artifacts"]
            if a["category"] == "log"}
    assert _surfaces(folder) <= logs
    assert get_inbox(run_id) is None


def test_two_demo_runs_share_a_cell_and_stop_one_at_a_time__P6F01(demo_app):
    client, channel_id, cell_id, gate, _reveals = demo_app

    first = _start(client, channel_id, cell_id)
    _wait("the first run's surfaces", lambda: _surfaces(first["folder"]) <= _files(client, cell_id))
    second = _start(client, channel_id, cell_id)
    assert second["label"] != first["label"]
    _wait("the second run's surfaces", lambda: _surfaces(second["folder"]) <= _files(client, cell_id))
    active = {r["run_id"]: r["folder"] for r in client.get(f"/api/channels/{channel_id}/runs/active").json()}
    assert active == {first["run_id"]: first["folder"], second["run_id"]: second["folder"]}

    stopped = client.delete(f"/api/channels/{channel_id}/runs/{first['run_id']}")
    assert stopped.status_code == 200, stopped.text
    assert _ended(client, channel_id, first["run_id"])["status"] == "stopped"
    files = _wait("the first run's sweep",
                  lambda: (f := _files(client, cell_id)) and not any(p.startswith(first["folder"] + "/") for p in f) and f)
    assert _surfaces(second["folder"]) <= files

    gate.write_text("go")
    assert _ended(client, channel_id, second["run_id"])["status"] == "completed"
    _wait("the second run's sweep",
          lambda: not any(p.startswith("_runs/") for p in _files(client, cell_id)))


def test_signal_zero_probes_a_fake_process_without_stopping_it(monkeypatch):
    import asyncio
    import signal

    monkeypatch.setattr(os, "kill", os.kill)
    monkeypatch.setattr(os, "killpg", os.killpg)
    monkeypatch.setattr(os, "getpgid", os.getpgid)
    monkeypatch.setattr(demo_runner, "_signals_installed", False)
    demo_runner._install_signal_routing()

    async def main():
        proc = demo_runner.FakeProcess("probe-run")
        proc.run(asyncio.sleep(30))
        await asyncio.sleep(0)
        os.kill(proc.pid, 0)
        await asyncio.sleep(0)
        assert not proc.cancelled and proc.returncode is None
        os.kill(proc.pid, signal.SIGTERM)
        assert await proc.wait() == -signal.SIGTERM

    asyncio.run(main())


def test_the_demo_terminal_ignores_malformed_control_frames__P6F01__F0363(app_client, probe_tree):
    from collimate_playground import terminal

    client = app_client(probe_tree)
    app = sys.modules["main"].app
    terminal.register_route(app)
    # Ahead of the static-files catch-all a local frontend build mounts.
    app.router.routes.insert(0, app.router.routes.pop())
    term = terminal.create_session("probe-run")
    try:
        for path, code, reason in [
            ("/api/terminals/missing?token=wrong", 4404, "no such session"),
            (f"{term.ws_path}?token=wrong", 4401, "bad token"),
        ]:
            with client.websocket_connect(path) as ws:
                assert ws.receive() == {"type": "websocket.close", "code": code, "reason": reason}
        term.attached = True
        with client.websocket_connect(f"{term.ws_path}?token={term.token}") as ws:
            assert ws.receive() == {
                "type": "websocket.close", "code": 4409, "reason": "session already attached",
            }
        term.attached = False
        with client.websocket_connect(f"{term.ws_path}?token={term.token}") as ws:
            ws.send_text("[1]")
            ws.send_text('{"t": "resize", "cols": "x", "rows": null}')
            ws.send_text('{"t": "resize", "cols": 120, "rows": 40}')
            _wait("the resize", lambda: (term.cols, term.rows) == (120, 40), timeout=5.0)
            assert term.attached
    finally:
        term.close()
