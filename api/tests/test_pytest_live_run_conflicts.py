"""A run's writes over a user's save while the run is live (end to end,
through ``uv run``): the user's bytes always stay visible and recoverable.

A synthetic generator holds at an action until the test has saved over one
of its files from a browser writer, then writes that file and unrelated
ones: by plain commits on its start's base, or through the live mirror from
a buffer it read before the mirror pulled the save.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid

import pytest

from conftest import install_runner
from collimate_server.database import get_db_connection

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; the live-run E2E needs it",
)

_HEADER = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import asyncio, json, os\n"
    "import gen\n"
)

# A plain-commit run: its base for a.md is the run's start, so its write
# after the user's save conflicts. The outcomes go out in the phase message:
# a wedged run lands no file to report them in.
_COMMITS = _HEADER + (
    "async def attempt(outcomes, key):\n"
    "    try:\n"
    "        await gen.commit()\n"
    "        outcomes[key] = 'landed'\n"
    "    except Exception as exc:\n"
    "        outcomes[key] = type(exc).__name__\n"
    "async def run():\n"
    "    gen.update_ui_file('own.txt', 'conflict probe')\n"
    "    await gen.commit()\n"
    "    gen.phase('awaiting_input', 'ready')\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "    outcomes = {}\n"
    "    gen.update_file('a.md', 'run edit')\n"
    "    await attempt(outcomes, 'a')\n"
    "    gen.update_file('b.md', 'run unrelated')\n"
    "    await attempt(outcomes, 'b')\n"
    "    gen.update_file('c.md', 'run later')\n"
    "    await attempt(outcomes, 'c')\n"
    "    gen.update_file('a.md', 'run edit again')\n"
    "    gen.update_file('d.md', 'run sibling')\n"
    "    await attempt(outcomes, 'a+d')\n"
    "    gen.phase('awaiting_input', json.dumps(outcomes))\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)

# A run told that the user's save refused its write: it reports the facts,
# retries plainly (another candidate), then acknowledges the save it was told
# of after the user saved again (a conflict again, with the new revision),
# and finally replaces the revision that refusal named.
_ACKNOWLEDGED = _HEADER + (
    "def facts(exc):\n"
    "    c = exc.conflicts['a.md']\n"
    "    return {'revision': c.revision, 'writer': c.writer, 'kind': c.writer_kind,\n"
    "            'candidates': list(c.candidates), 'told': 'replace_revision=%d' % c.revision in str(exc),\n"
    "            'newer': c.read(), 'open': sorted(gen.file_conflicts())}\n"
    "async def attempt(outcomes, key, **kw):\n"
    "    try:\n"
    "        await gen.commit(**kw)\n"
    "        outcomes[key] = 'landed'\n"
    "    except gen.FileConflictError as exc:\n"
    "        outcomes[key] = facts(exc)\n"
    "    except Exception as exc:\n"
    "        outcomes[key] = type(exc).__name__ + ': ' + str(exc)\n"
    "async def run():\n"
    "    gen.update_ui_file('own.txt', 'acknowledged probe')\n"
    "    await gen.commit()\n"
    "    gen.phase('awaiting_input', 'ready')\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "    outcomes = {}\n"
    "    gen.update_file('a.md', 'run edit')\n"
    "    await attempt(outcomes, 'first')\n"
    "    gen.update_file('a.md', 'run edit again')\n"
    "    await attempt(outcomes, 'plain')\n"
    "    gen.phase('awaiting_input', json.dumps(outcomes))\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "    outcomes = {}\n"
    "    told = outcomes['told'] = gen.file_conflicts()['a.md'].revision\n"
    "    gen.update_file('a.md', 'run deliberate', replace_revision=told)\n"
    "    await attempt(outcomes, 'stale')\n"
    "    told = gen.file_conflicts()['a.md'].revision\n"
    "    gen.update_file('a.md', 'run deliberate', replace_revision=told)\n"
    "    await attempt(outcomes, 'acknowledged')\n"
    "    outcomes['open'] = sorted(gen.file_conflicts())\n"
    "    gen.update_file('a.md', 'run later')\n"
    "    await attempt(outcomes, 'later')\n"
    "    gen.phase('awaiting_input', json.dumps(outcomes))\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)

# A run told that the user's save refused its write accepts that save: the
# user's bytes go back on the user's revision and close the run's candidate.
_ACCEPTS = _HEADER + (
    "async def run():\n"
    "    gen.update_ui_file('own.txt', 'accepts probe')\n"
    "    await gen.commit()\n"
    "    gen.phase('awaiting_input', 'ready')\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "    outcomes = {}\n"
    "    gen.update_file('a.md', 'run edit')\n"
    "    try:\n"
    "        await gen.commit()\n"
    "        outcomes['first'] = 'landed'\n"
    "    except gen.FileConflictError as exc:\n"
    "        outcomes['first'] = exc.conflicts['a.md'].revision\n"
    "        exc.conflicts['a.md'].accept()\n"
    "    await gen.commit()\n"
    "    outcomes['open'] = sorted(gen.file_conflicts())\n"
    "    gen.phase('awaiting_input', json.dumps(outcomes))\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)

# A live-files run whose process keeps a buffer it read before the user's
# save (an editor, an agent's file tool), lets the mirror pull the save,
# then writes the buffer back.
_STALE_BUFFER = _HEADER + (
    "async def run():\n"
    "    path = os.path.join(os.environ['COLLIMATE_WORKSPACE'], 'a.md')\n"
    "    async with gen.live_files(interval=0.2) as mirror:\n"
    "        gen.update_ui_file('own.txt', 'stale probe')\n"
    "        await gen.commit()\n"
    "        with open(path) as fh:\n"
    "            buffer = fh.read()\n"
    "        gen.phase('awaiting_input', 'ready')\n"
    "        await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "        pulled = None\n"
    "        for _ in range(200):\n"
    "            with open(path) as fh:\n"
    "                pulled = fh.read()\n"
    "            if pulled != buffer:\n"
    "                break\n"
    "            await asyncio.sleep(0.05)\n"
    "        with open(path, 'w') as fh:\n"
    "            fh.write(buffer + ' + run edit')\n"
    "        await mirror.flush()\n"
    "        gen.phase('awaiting_input', json.dumps({'pulled': pulled}))\n"
    "        await gen.wait_for_action_event(action_id='go', timeout=120)\n"
    "if __name__ == '__main__':\n"
    "    gen.main(run)\n"
)


def _poll(pred, timeout_s: float, label: str):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {label}")


def _current(client, cell, path):
    from collimate_server.persistence import current_file
    conn = get_db_connection(client.channel_id)
    try:
        return current_file(conn, cell, path)
    finally:
        conn.close()


def _put(client, cell, path, text, writer):
    current = _current(client, cell, path)
    revision = current['revision'] if current else 0
    response = client.post(f'/api/channels/{client.channel_id}/cells/{cell}/files/mutate', json={
        'operation_id': str(uuid.uuid4()), 'mutations': [{
            'path': path, 'kind': 'put', 'writer_id': writer,
            'edit_seq': revision + 1, 'base_revision': revision, 'content_text': text}]})
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'accepted', response.json()
    return response.json()['files'][0]['file_revision']


def _text(client, cell, path):
    response = client.get(f"/api/channels/{client.channel_id}/cells/{cell}/files/{path}")
    return response.text if response.status_code == 200 else None


def _phase_message(client, run_id):
    rows = client.get(f"/api/channels/{client.channel_id}/runs/active").json()
    row = next((r for r in rows if r["run_id"] == run_id), None)
    return row and row["phase"] == "awaiting_input" and row["phase_message"]


def _active(client):
    return {r["run_id"] for r in client.get(f"/api/channels/{client.channel_id}/runs/active").json()}


def _go(client, cell, run):
    click = client.post(f"/api/channels/{client.channel_id}/cells/{cell}/actions", json={
        "action_id": "go", "source": f"{run['folder']}/own.txt"})
    assert click.status_code == 200, click.text


def _start(client, entry, generator):
    runner, _ = install_runner(generator=generator, runner=generator.replace("_", "-"),
                               entrypoint_body=entry, gen_manifest_extra="ui_files: ['.txt']\n")
    cell = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]["id"]
    _put(client, cell, 'a.md', 'seed', 'browser-seed')
    started = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell})
    assert started.status_code == 200, started.text
    run = started.json()
    assert _poll(lambda: _phase_message(client, run["run_id"]), 90, "the run to be ready") == "ready"
    return cell, run


def _versions_holding(client, cell, path, text):
    """Ids of the cell's history versions whose ``path`` is ``text``."""
    from collimate_server.cell_files import walk_versions
    from collimate_server.versions import tip_file_map
    digest = hashlib.sha256(text.encode()).hexdigest()
    conn = get_db_connection(client.channel_id)
    try:
        return [v for v in walk_versions(conn, cell) if tip_file_map(conn, v).get(path) == digest]
    finally:
        conn.close()


def _receipts(client, cell):
    conn = get_db_connection(client.channel_id)
    try:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT result_json FROM file_operations WHERE cell_id=? ORDER BY rowid", (cell,))]
    finally:
        conn.close()


def _open_conflicts(client, cell, path):
    conn = get_db_connection(client.channel_id)
    try:
        return conn.execute("SELECT * FROM file_conflicts WHERE cell_id=? AND path=? "
                            "AND resolution_operation_id IS NULL", (cell, path)).fetchall()
    finally:
        conn.close()


def test_a_conflicted_flush_never_blocks_the_runs_later_writes(client):
    cell, run = _start(client, _COMMITS, "conflict_probe")
    _put(client, cell, 'a.md', 'user save', 'browser-user')
    _go(client, cell, run)
    outcomes = json.loads(_poll(lambda: (lambda m: m if m and m != "ready" else None)(
        _phase_message(client, run["run_id"])), 60, "the run's outcomes"))
    _go(client, cell, run)
    _poll(lambda: run["run_id"] not in _active(client), 60, "the run to end")

    # The conflict is the run's answer for a.md only: its later, unrelated
    # writes land, and so does a fresh sibling of a.md in one flush.
    assert outcomes == {'a': 'FileConflictError', 'b': 'landed', 'c': 'landed',
                        'a+d': 'FileConflictError'}, outcomes
    assert _text(client, cell, 'b.md') == 'run unrelated'
    assert _text(client, cell, 'c.md') == 'run later'
    assert _text(client, cell, 'd.md') == 'run sibling'
    # The user's save stands, each of the run's edits of a.md is a candidate
    # beside it (its base stays the revision it read), and the sibling's
    # collateral candidate is resolved by its landing.
    assert _text(client, cell, 'a.md') == 'user save'
    candidates = _open_conflicts(client, cell, 'a.md')
    assert len(candidates) == 2
    from collimate_server.persistence import read_file
    assert sorted(read_file(client.channel_id, cell, 'a.md', revision=c['candidate_revision'])[1]
                  for c in candidates) == [b'run edit', b'run edit again']
    assert _open_conflicts(client, cell, 'd.md') == []


def test_a_save_the_runs_stale_write_replaces_stays_in_history(client):
    cell, run = _start(client, _STALE_BUFFER, "stale_probe")
    _put(client, cell, 'a.md', 'user save', 'browser-user')
    _go(client, cell, run)
    reported = json.loads(_poll(lambda: (lambda m: m if m and m != "ready" else None)(
        _phase_message(client, run["run_id"])), 60, "the run's stale write"))
    _go(client, cell, run)
    _poll(lambda: run["run_id"] not in _active(client), 60, "the run to end")

    # The mirror pulled the save, so the run's write from its older buffer
    # carried the save's revision as its base and replaced it.
    assert reported == {'pulled': 'user save'}
    assert _text(client, cell, 'a.md') == 'seed + run edit'
    assert _open_conflicts(client, cell, 'a.md') == []
    # The user's bytes are still the user's: a version of the cell holds
    # them, a manual edit (no checkpoint runs while the run is live), and
    # the run's write is the next version on the one line of history.
    held = _versions_holding(client, cell, 'a.md', 'user save')
    assert held, "only an unversioned file revision records the user's save"
    from collimate_server.cell_files import walk_versions
    conn = get_db_connection(client.channel_id)
    try:
        row = conn.execute("SELECT source_type FROM cells WHERE id=?", (held[0],)).fetchone()
        chain = walk_versions(conn, cell)
        forks = conn.execute(
            "SELECT parent_cell_id FROM cell_edges WHERE parent_key='previous_version' "
            "GROUP BY parent_cell_id HAVING COUNT(*) > 1").fetchall()
    finally:
        conn.close()
    assert row["source_type"] == "manual_edit"
    after = chain[chain.index(held[-1]) + 1]
    assert after in _versions_holding(client, cell, 'a.md', 'seed + run edit')
    assert forks == []


def _outcomes(client, run, previous):
    return json.loads(_poll(lambda: (lambda m: m if m and m != previous else None)(
        _phase_message(client, run["run_id"])), 60, "the run's outcomes"))


def test_a_run_told_of_a_newer_save_replaces_it_only_by_naming_its_revision(client):
    cell, run = _start(client, _ACKNOWLEDGED, "acknowledged_probe")
    saved = _put(client, cell, 'a.md', 'user save', 'browser-user')
    _go(client, cell, run)
    told = _outcomes(client, run, "ready")

    # The refusal reaches the run with its facts: the newer revision, that
    # the user wrote it, the run's own candidate, the way to replace it, and
    # the newer bytes (read without becoming the run's base). A plain retry
    # is another candidate beside the first.
    first, plain = told['first'], told['plain']
    assert first == {'revision': saved, 'writer': 'the user', 'kind': 'user',
                     'candidates': first['candidates'], 'told': True, 'newer': 'user save',
                     'open': ['a.md']}, first
    assert len(first['candidates']) == 1
    assert plain['revision'] == saved and plain['candidates'][0] == first['candidates'][0]
    assert len(plain['candidates']) == 2
    assert _text(client, cell, 'a.md') == 'user save'
    assert len(_open_conflicts(client, cell, 'a.md')) == 2

    # The user saves again before the run acknowledges the save it was told
    # of: naming that older revision is a conflict again, with the new facts.
    again = _put(client, cell, 'a.md', 'user save again', 'browser-user')
    _go(client, cell, run)
    outcomes = _outcomes(client, run, json.dumps(told))
    _go(client, cell, run)
    _poll(lambda: run["run_id"] not in _active(client), 60, "the run to end")
    assert outcomes['told'] == saved
    assert outcomes['stale']['revision'] == again and len(outcomes['stale']['candidates']) == 3
    assert outcomes['stale']['newer'] == 'user save again'
    # Naming the revision it was told of lands: the run's candidates close,
    # no refusal stays open, and the run's later writes are ordinary ones.
    assert outcomes['acknowledged'] == 'landed' and outcomes['open'] == []
    assert outcomes['later'] == 'landed'
    assert _text(client, cell, 'a.md') == 'run later'
    assert _open_conflicts(client, cell, 'a.md') == []
    # The acknowledged write's receipt (and so its file_changes event) names
    # the candidates it closed, for every open document still showing them.
    assert [sorted(r['resolved']) for r in _receipts(client, cell) if r.get('resolved')] == [
        sorted(outcomes['stale']['candidates'])]

    # History: the user's last save is a Manual Edit, and the run's
    # acknowledged version on top of it records the revision it replaced.
    history = client.get(f"/api/channels/{client.channel_id}/cells/{cell}/history").json()
    versions = [v for node in reversed(history['nodes']) for v in node['versions']]
    marked = [v for v in versions if v.get('replaced')]
    assert len(marked) == 1, versions
    assert marked[0]['replaced'] == [{'path': 'a.md', 'revision': again, 'writer_id': 'browser-user',
                                      'writer': {'kind': 'user'},
                                      # The run's later write is the revision after it.
                                      'file_revision': _current(client, cell, 'a.md')['revision'] - 1}]
    assert marked[0]['cell_id'] in _versions_holding(client, cell, 'a.md', 'run deliberate')
    before = versions[versions.index(marked[0]) - 1]
    assert before['source_type'] == 'manual_edit'
    assert before['cell_id'] in _versions_holding(client, cell, 'a.md', 'user save again')
    assert any(v['source_type'] == 'manual_edit' and v['cell_id'] in
               _versions_holding(client, cell, 'a.md', 'user save') for v in versions)
    touches = client.get(f"/api/channels/{client.channel_id}/runs/{run['run_id']}/file_touches").json()['touches']
    assert [t.get('replaced', {}).get('revision') for t in touches if t['path'] == 'a.md'] == [again, None]


def test_a_run_that_accepts_the_newer_save_closes_its_candidates(client):
    """Accepting the user's version is a conditional resolution on the
    user's revision, as the editor's "Use server version" is: the bytes
    stay the user's and no candidate of the run is left open for review."""
    cell, run = _start(client, _ACCEPTS, "accepts_probe")
    saved = _put(client, cell, 'a.md', 'user save', 'browser-user')
    _go(client, cell, run)
    outcomes = _outcomes(client, run, "ready")
    _go(client, cell, run)
    _poll(lambda: run["run_id"] not in _active(client), 60, "the run to end")
    assert outcomes == {'first': saved, 'open': []}
    assert _text(client, cell, 'a.md') == 'user save'
    assert _current(client, cell, 'a.md')['revision'] == saved + 2
    assert _open_conflicts(client, cell, 'a.md') == []
