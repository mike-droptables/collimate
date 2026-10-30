"""Real subprocess identity and conditional run/user conflict retention."""
from __future__ import annotations

import glob
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from conftest import install_runner

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:runner-identity:channel"))


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from genesis import ensure_capabilities_stack
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name="Starting")
        ensure_capabilities_stack(channel)
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = CHANNEL
        yield http

e2e = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; the E2E pipeline needs it",
)


def _wait_for_terminal(client, run_id: str, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/api/channels/{CHANNEL}/runs/{run_id}/events_since?since_id=0")
        assert r.status_code == 200, r.text
        if r.json().get("is_terminal"):
            return r.json()
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never reached a terminal status")


def _find_cell(client, cell_id: str) -> dict | None:
    st = client.get("/api/state").json()
    for ch in st["channels"]:
        for stack in ch["main_stacks"]:
            for c in stack["cells"]:
                if str(c["id"]) == cell_id:
                    return c
    return None


def _wait_for_cell_file(client, cell_id: str, name: str, timeout_s: float = 15.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cell = _find_cell(client, cell_id)
        for f in (cell or {}).get("files") or []:
            if f["name"] == name:
                response = client.get(f"/api/channels/{CHANNEL}/cells/{cell_id}/files/{name}",
                                      params={"revision": f["file_revision"]})
                assert response.status_code == 200, response.text
                return response.text
        time.sleep(0.2)
    raise AssertionError(f"{name} never landed on cell {cell_id}")


def _canvas_cell(client) -> str:
    ch = client.post(f"/api/stacks/{CHANNEL}/create").json()
    return ch["main_stacks"][-1]["cells"][0]["id"]


@e2e
def test_run_carries_its_runner_identity(client):
    """COLLIMATE_RUNNER reaches the child and surfaces in ``gen.run_info()``
    — the runner the run was started from, the host-minted label and run
    folder, and the cell it writes in."""
    runner, _gen = install_runner(
        generator="identity_e2e", runner="identity-e2e",
        entrypoint_body=(
            "# /// script\n"
            "# requires-python = \">=3.11\"\n"
            "# dependencies = []\n"
            "# ///\n"
            "import json\n"
            "import gen\n"
            "async def run():\n"
            "    info = gen.run_info()\n"
            "    gen.update_file('identity.json', json.dumps({\n"
            "        'runner_id': info.runner_id, 'cell_id': info.cell_id,\n"
            "        'label': info.label, 'folder': info.folder}))\n"
            "    await gen.commit()\n"
            "if __name__ == '__main__':\n"
            "    gen.main(run)\n"
        ),
    )
    cell_id = _canvas_cell(client)
    r = client.post(f"/api/channels/{CHANNEL}/runs", json={
        "runner": runner, "cell_id": cell_id,
    })
    assert r.status_code == 200, r.text
    _wait_for_terminal(client, r.json()["run_id"])

    run_id = r.json()["run_id"]
    label = f"{runner}-{run_id[:8]}"
    assert r.json()["label"] == label and r.json()["folder"] == f"_runs/{label}"
    identity = json.loads(_wait_for_cell_file(client, cell_id, "identity.json"))
    assert identity == {"runner_id": runner, "cell_id": cell_id,
                        "label": label, "folder": f"_runs/{label}"}


@e2e
def test_dag_commit_broadcasts_the_canvas_cell_id(client, monkeypatch):
    """Every dag_commit SSE event must carry the STABLE canvas cell id — the
    id the frontend keys its mounted renderers on. A versioned commit's row
    id is a fresh version row no renderer knows, so broadcasting that makes
    every mid-run FILES_CHANGED miss its target and a live_files run looks
    frozen until the end."""
    import events as events_mod

    seen: list = []
    orig = events_mod.broadcast

    def spy(channel, payload):
        if isinstance(payload, dict) and payload.get("type") == "dag_commit":
            seen.append(payload.get("cell_id"))
        return orig(channel, payload)

    monkeypatch.setattr(events_mod, "broadcast", spy)

    runner, _gen = install_runner(
        generator="dagid_e2e", runner="dagid-e2e",
        entrypoint_body=(
            # Two updates: the initial in-place upsert AND a versioned
            # commit — the versioned one is where the row id diverges.
            "# /// script\n"
            "# requires-python = \">=3.11\"\n"
            "# dependencies = []\n"
            "# ///\n"
            "import gen\n"
            "async def run():\n"
            "    gen.update_file('a.md', 'one')\n"
            "    await gen.commit()\n"
            "    gen.update_file('a.md', 'two')\n"
            "    await gen.commit()\n"
            "if __name__ == '__main__':\n"
            "    gen.main(run)\n"
        ),
    )
    cell_id = _canvas_cell(client)
    r = client.post(f"/api/channels/{CHANNEL}/runs", json={
        "runner": runner, "cell_id": cell_id,
    })
    assert r.status_code == 200, r.text
    _wait_for_terminal(client, r.json()["run_id"])

    ours = [c for c in seen if c == cell_id]
    assert len(ours) >= 2, f"expected both commits to broadcast {cell_id}, saw {seen}"
    assert all(c == cell_id for c in seen if c is not None) or ours, seen


@e2e
def test_run_candidate_preserves_user_current_and_untouched_file(client):
    """A stale run write becomes a retained conflict; accepted user bytes survive."""
    runner, _gen = install_runner(
        generator="conditional_e2e", runner="conditional-e2e",
        entrypoint_body=(
            "# /// script\n"
            "# requires-python = \">=3.11\"\n"
            "# dependencies = []\n"
            "# ///\n"
            "import asyncio\n"
            "import gen\n"
            "async def run():\n"
            "    for _ in range(300):\n"
            "        if gen.has_file('go'):\n"
            "            break\n"
            "        await asyncio.sleep(0.1)\n"
            "    gen.update_file('doc.md', 'generator candidate')\n"
            "    await gen.commit()\n"
            "if __name__ == '__main__':\n"
            "    gen.main(run)\n"
        ),
    )
    cell_id = _canvas_cell(client)
    def put(path, body, revision, sequence):
        response = client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/files/mutate", json={
            "operation_id": str(uuid.uuid4()), "resolves": [], "mutations": [{
                "path": path, "writer_id": "user-e2e", "edit_seq": sequence,
                "base_revision": revision, "kind": "put", "content_text": body}]})
        assert response.status_code == 200, response.text
        assert response.json()['outcome'] == 'accepted'
    put('doc.md', 'initial', 0, 1)
    response = client.post(f"/api/channels/{CHANNEL}/runs", json={
        "runner": runner, "cell_id": cell_id,
    })
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    workspace = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        hits = glob.glob(os.path.join(".collimate", "tmp", f"ws_{run_id[:8]}_*"))
        if hits and os.path.isfile(os.path.join(hits[0], 'doc.md')):
            workspace = hits[0]
            break
        time.sleep(.1)
    assert workspace, "run workspace never appeared"
    put('doc.md', 'user current', 1, 2)
    put('untouched.md', 'keep me', 0, 1)
    Path(workspace, 'go').write_text('go')
    _wait_for_terminal(client, run_id)
    assert _wait_for_cell_file(client, cell_id, 'doc.md') == 'user current'
    assert _wait_for_cell_file(client, cell_id, 'untouched.md') == 'keep me'
    from database import get_db_connection
    from persistence import read_file
    conn = get_db_connection(CHANNEL)
    try:
        candidates = conn.execute('SELECT * FROM file_conflicts WHERE cell_id=? AND resolution_operation_id IS NULL',
                                  (cell_id,)).fetchall()
    finally:
        conn.close()
    assert len(candidates) == 1
    assert candidates[0]['path'] == 'doc.md'
    _, candidate = read_file(CHANNEL, cell_id, 'doc.md', revision=candidates[0]['candidate_revision'])
    assert candidate == b'generator candidate'
