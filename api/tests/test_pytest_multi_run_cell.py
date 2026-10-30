"""Two real runs live on one cell (end to end, through ``uv run``).

Both start on the same cell: each gets its own label, run folder and run
card; the second sees the first's folder (read access) but its write into it
is refused; a renderer action reaches the run that registered its file, and
an ended run's file reaches none; stopping one leaves the other running with
its folder intact; each run's end sweeps its folder (card included) and keeps
the swept files in its Logs; the version chain stays one line. A synthetic
generator reads its identity from the host's environment.
"""
from __future__ import annotations

import json
import shutil
import time

import pytest

from conftest import install_runner
from database import get_db_connection

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not available on PATH; the multi-run E2E needs it",
)

_PROBE = (
    "# /// script\n"
    "# requires-python = \">=3.11\"\n"
    "# dependencies = []\n"
    "# ///\n"
    "import json, os\n"
    "import gen\n"
    "async def run():\n"
    "    me = json.loads(os.environ['COLLIMATE_RUNNER'])\n"
    "    ws = os.environ['COLLIMATE_WORKSPACE']\n"
    "    runs_dir = os.path.join(ws, '_runs')\n"
    "    others = sorted(d for d in os.listdir(runs_dir) if d != me['label']) \\\n"
    "        if os.path.isdir(runs_dir) else []\n"
    "    own = gen.update_ui_file('own.txt', me['label'])\n"
    "    gen.update_file(me['label'] + '.seen', ','.join(others))\n"
    "    try:\n"
    "        await gen.commit()\n"
    "    except PermissionError:\n"
    "        pass\n"
    "    for other in others:\n"
    "        try:\n"
    "            gen.update_file('_runs/' + other + '/intrusion.txt', 'mine now')\n"
    "            await gen.commit()\n"
    "        except PermissionError:\n"
    "            pass\n"
    "    gen.phase('awaiting_input', 'waiting')\n"
    "    await gen.wait_for_action_event(action_id='go', timeout=120)\n"
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


def _state_files(client, cell_id: str) -> set[str]:
    for ch in client.get("/api/state").json()["channels"]:
        for stack in ch["main_stacks"]:
            for c in stack["cells"]:
                if c["id"] == cell_id:
                    return {f["name"] for f in c["files"]}
    raise AssertionError(f"cell {cell_id} is gone")


def _tip_files(client, cell_id: str) -> dict[str, str]:
    from cell_commits import blob_bytes
    from versions import find_tip, tip_file_map
    conn = get_db_connection(client.channel_id)
    try:
        return {p: (blob_bytes(h) or b"").decode("utf-8", "replace")
                for p, h in tip_file_map(conn, find_tip(conn, cell_id)).items()}
    finally:
        conn.close()


def _active(client) -> dict:
    return {r["run_id"]: r for r in client.get(f"/api/channels/{client.channel_id}/runs/active").json()}


def test_two_runs_share_one_cell__D22(client):
    runner, _ = install_runner(generator="multi_probe", runner="multi-probe",
                               entrypoint_body=_PROBE, gen_manifest_extra="ui_files: ['.txt']\n")
    created = client.post(f"/api/stacks/{client.channel_id}/create").json()["main_stacks"][-1]["cells"][0]
    cell = created["id"]

    first = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell})
    assert first.status_code == 200, first.text
    a = first.json()
    assert a["cell_id"] == cell and "stack_id" not in a
    assert a["label"] == f"multi-probe-{a['run_id'][:8]}"
    assert a["folder"] == f"_runs/{a['label']}"
    _poll(lambda: f"{a['folder']}/own.txt" in _tip_files(client, cell), 90, "the first run's own file")

    second = client.post(f"/api/channels/{client.channel_id}/runs", json={"runner": runner, "cell_id": cell})
    assert second.status_code == 200, second.text
    b = second.json()
    assert b["label"] != a["label"]
    files = _poll(lambda: (lambda f: f if f"{b['label']}.seen" in f else None)(_tip_files(client, cell)),
                  90, "the second run's files")

    # Each run's card and folder; the second saw the first's folder and could
    # not write into it.
    for run in (a, b):
        card = json.loads(files[f"{run['folder']}/{run['label']}.run"])
        assert (card["run_id"], card["label"], card["folder"]) == (
            run["run_id"], run["label"], run["folder"])
        assert card["runner"] == {"id": "multi-probe", "name": run["runner_name"]}
        assert files[f"{run['folder']}/own.txt"] == run["label"]
    assert files[f"{b['label']}.seen"] == a["label"]
    assert f"{a['folder']}/intrusion.txt" not in files

    # Both live, with the reattach row shape.
    active = _poll(lambda: (lambda r: r if {a["run_id"], b["run_id"]} <= set(r)
                            and all(r[x]["phase"] == "awaiting_input"
                                    for x in (a["run_id"], b["run_id"])) else None)(_active(client)),
                   60, "both runs awaiting input")
    row = active[a["run_id"]]
    assert set(row) == {"run_id", "cell_id", "cell_name", "runner_id", "runner_name", "label",
                        "folder", "generator_name", "started_at", "listening", "phase",
                        "phase_message", "stopping", "surfaces", "effective_grant", "adopted",
                        "harness", "harness_label", "cli_locus"}
    assert row["cli_locus"] is None                  # not a CLI run
    assert row["stopping"] is False and row["listening"] is False
    assert row["cell_name"] == created["name"]
    assert row["started_at"].endswith("Z")
    assert (row["cell_id"], row["label"], row["folder"]) == (cell, a["label"], a["folder"])
    for run in (a, b):
        assert active[run['run_id']]['surfaces'] == []

    # A renderer action goes to the run whose folder holds its source: the
    # first run ends,
    # the second keeps running with its folder intact.
    click = client.post(f"/api/channels/{client.channel_id}/cells/{cell}/actions", json={
        "action_id": "go", "source": f"{a['folder']}/own.txt"})
    assert click.status_code == 200, click.text
    _poll(lambda: a["run_id"] not in _active(client), 60, "the first run to end")
    _poll(lambda: not any(p.startswith(a["folder"] + "/") for p in _tip_files(client, cell)),
          30, "the first run's folder to be swept")
    assert b["run_id"] in _active(client)
    files = _tip_files(client, cell)
    assert f"{b['folder']}/{b['label']}.run" in files
    assert not any(p.startswith(a["folder"] + "/") for p in _state_files(client, cell))
    assert f"{a['label']}.seen" in files  # a work file outside the folder stays

    # The swept files are in the ended run's Logs.
    arts = client.get(f"/api/channels/{client.channel_id}/runs/{a['run_id']}/artifacts").json()
    logs = {x["path"] for x in arts["artifacts"] if x["category"] == "log"}
    assert {f"{a['folder']}/own.txt", f"{a['folder']}/{a['label']}.run"} <= logs

    # The ended run's file is owned by no live run any more; the second
    # run's own file still reaches it.
    stale = client.post(f"/api/channels/{client.channel_id}/cells/{cell}/actions", json={
        "action_id": "go", "source": f"{a['folder']}/own.txt"})
    assert stale.status_code == 409
    assert "no live run owns" in stale.json()["detail"]
    go = client.post(f"/api/channels/{client.channel_id}/cells/{cell}/actions", json={
        "action_id": "go", "source": f"{b['folder']}/own.txt"})
    assert go.status_code == 200, go.text
    _poll(lambda: b["run_id"] not in _active(client), 60, "the second run to end")
    _poll(lambda: not any(p.startswith("_runs/") for p in _tip_files(client, cell)),
          30, "every run folder to be swept")

    conn = get_db_connection(client.channel_id)
    try:
        forks = conn.execute(
            "SELECT parent_cell_id FROM cell_edges WHERE parent_key='previous_version' "
            "GROUP BY parent_cell_id HAVING COUNT(*) > 1").fetchall()
        statuses = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM runs WHERE runner_id=?", (runner,))}
    finally:
        conn.close()
    assert forks == [], "two runs on one cell forked the version chain"
    assert statuses == {a["run_id"]: "completed", b["run_id"]: "completed"}
