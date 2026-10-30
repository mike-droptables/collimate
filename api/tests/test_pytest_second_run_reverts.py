"""Second-run revert reproduction: a live_files generator re-reads its own
write and logs any content that isn't what it wrote. Clean on a fresh cell;
the report says the SECOND run on the same cell sees its files revert."""
from __future__ import annotations

import json
import shutil
import time
import uuid

import pytest

from conftest import install_runner

CHANNEL = str(uuid.uuid5(uuid.NAMESPACE_URL, "tests:second-run-reverts:channel"))


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    from database import get_db_connection
    with TestClient(api_app) as http:
        channel = Channel(id=uuid.UUID(CHANNEL), name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(str(channel.id)).close()
        http.channel_id = CHANNEL
        yield http


def _put(client, cell, path, text):
    from database import get_db_connection
    from persistence import current_file
    conn = get_db_connection(CHANNEL)
    try:
        current = current_file(conn, cell, path)
    finally:
        conn.close()
    revision = current['revision'] if current else 0
    response = client.post(f'/api/channels/{CHANNEL}/cells/{cell}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': path, 'kind': 'put', 'writer_id': 'browser-repro',
            'edit_seq': revision + 1, 'base_revision': revision, 'content_text': text}]})
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'accepted'


e2e = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; the E2E pipeline needs it",
)

_ENTRY = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import asyncio\n"
    "import json\n"
    "import gen\n"
    "async def run():\n"
    "    marker = f'MARKER-{(gen.prompt or \"?\").strip()}'\n"
    "    reverts = []\n"
    "    async with gen.live_files():\n"
    "        # A window BEFORE the first commit — the race a mid-run\n"
    "        # checkpoint used to win, forking the version chain.\n"
    "        await asyncio.sleep(2.0)\n"
    "        gen.update_file('probe.txt', marker)\n"
    "        await gen.commit()\n"
    "        for _ in range(14):\n"
    "            await asyncio.sleep(0.5)\n"
    "            cur = gen.read_file('probe.txt')\n"
    "            if cur != marker:\n"
    "                reverts.append(str(cur)[:60])\n"
    "                gen.update_file('probe.txt', marker)\n"
    "                await gen.commit()\n"
    "    gen.update_file('reverts.json', json.dumps(reverts))\n"
    "    await gen.commit()\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)


def _wait_terminal(client, run_id, timeout_s=90.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/api/channels/{CHANNEL}/runs/{run_id}/events_since?since_id=0")
        if r.json().get("is_terminal"):
            return
        time.sleep(0.3)
    raise AssertionError("run never terminal")


def _cell_file(client, cell_id, name):
    r = client.get(f"/api/channels/{CHANNEL}/cells/{cell_id}/files/{name}")
    return r.text if r.status_code == 200 else None


def _save_prompt(client, runner, prompt):
    draft = client.get(f"/api/runners/{runner}").json()
    draft["runner"]["prompt"] = prompt
    saved = client.put(f"/api/runners/{runner}", json={
        "runner": draft["runner"], "files": draft["files"], "revision": draft["revision"],
    })
    assert saved.status_code == 200, saved.text
    return saved.json()["revision"]


def _run(client, runner, cell_id, prompt):
    revision = _save_prompt(client, runner, prompt)
    r = client.post(f"/api/channels/{CHANNEL}/runs", json={
        "runner": runner, "cell_id": cell_id, "runner_revision": revision,
    })
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    _wait_terminal(client, rid)
    return rid


@e2e
def test_second_run_workspace_never_reverts(client):
    runner, _g = install_runner(
        generator="revprobe", runner="revprobe",
        entrypoint_body=_ENTRY,
    )
    ch = client.post(f"/api/stacks/{CHANNEL}/create").json()
    cell = ch["main_stacks"][-1]["cells"][0]["id"]

    _run(client, runner, cell, "one")
    time.sleep(0.5)
    r1 = json.loads(_cell_file(client, cell, "reverts.json"))

    # The second run consumes accepted feedback from the first run's file.
    _put(client, cell, 'probe.txt', 'MARKER-one plus user feedback')
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cell}/checkpoint").status_code == 200

    revision = _save_prompt(client, runner, "two")
    r = client.post(f"/api/channels/{CHANNEL}/runs", json={
        "runner": runner, "cell_id": cell, "runner_revision": revision,
    })
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    # Sibling writes and checkpoint requests arrive before and after the
    # run's first commit. They must never fork history or revert its mirror.
    time.sleep(.8)
    _put(client, cell, 'feedback.txt', 'accepted while the run starts')
    cp = client.post(f"/api/channels/{CHANNEL}/cells/{cell}/checkpoint")
    assert cp.json().get('created') in (False, None), cp.json()
    time.sleep(2)
    _put(client, cell, 'review.txt', 'independent review notes')
    assert client.post(f"/api/channels/{CHANNEL}/cells/{cell}/checkpoint").status_code == 200
    _wait_terminal(client, rid)
    time.sleep(0.5)
    r2 = json.loads(_cell_file(client, cell, "reverts.json"))

    # Diagnostics on failure: the version graph, to see a fork.
    from database import get_db_connection
    conn = get_db_connection(CHANNEL)
    edges = conn.execute(
        "SELECT parent_cell_id, child_cell_id FROM cell_edges "
        "WHERE parent_key='previous_version'").fetchall()
    children: dict = {}
    for e in edges:
        children.setdefault(e["parent_cell_id"], []).append(e["child_cell_id"])
    forks = {p: c for p, c in children.items() if len(c) > 1}
    conn.close()

    assert r1 == [], f"run 1 reverted: {r1}"
    assert r2 == [], (
        f"run 2 reverted: {r2}; probe now: {_cell_file(client, cell, 'probe.txt')!r}; "
        f"version forks: {forks}")
    # The chain must stay LINEAR — a fork is the disease even before the
    # mirror turns it into visible reverts.
    assert not forks, f"version chain forked: {forks}"
    assert _cell_file(client, cell, "probe.txt") == "MARKER-two"
    assert _cell_file(client, cell, "feedback.txt") == "accepted while the run starts"
    assert _cell_file(client, cell, "review.txt") == "independent review notes"
