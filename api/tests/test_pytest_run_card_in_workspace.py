"""The run card is in the run's workspace before the generator starts.

The accept commits the card to the cell before it freezes the cell, so the
workspace is hydrated from a tip that already holds it. Without it in the
workspace the run's live-files mirror would open with the card on the cell
side only, and its first sync would report the card as an edit from outside
the run: a file-mode run answered that with a spurious opening turn.
"""
from __future__ import annotations

import os

import pytest

from conftest import install_runner
from database import get_db_connection
from fake_manager import FakeManager
from run_folders import run_card_path
from run_spawn import run_row, wait_spawned


def _canvas_cell(client) -> str:
    ch = client.post(f"/api/stacks/{client.channel_id}/create").json()
    return ch["main_stacks"][-1]["cells"][0]["id"]


def _tip_bytes(channel_id: str, cell_id: str, path: str) -> bytes:
    from cas import blob_path
    from versions import find_tip, tip_file_map
    conn = get_db_connection(channel_id)
    try:
        blob = tip_file_map(conn, find_tip(conn, cell_id))[path]
    finally:
        conn.close()
    with open(blob_path(blob), "rb") as fh:
        return fh.read()


@pytest.mark.parametrize("new_cell", [False, True])
def test_the_workspace_holds_the_run_card_when_the_generator_starts(client, monkeypatch, new_cell):
    import routes.runs.create as create

    seen = {}

    class _Recording(FakeManager):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.label = create.mint_run_label(kwargs["runner_id"], kwargs["run_id"])

        async def start(self):
            card = run_card_path(self.label)
            dest = os.path.join(self.workspace_dir, card)
            seen["card"] = card
            seen["cell"] = self.cell_id
            seen["workspace"] = open(dest, "rb").read() if os.path.isfile(dest) else None
            seen["committed"] = _tip_bytes(client.channel_id, self.cell_id, card)
            raise RuntimeError("generator start recorded")

    monkeypatch.setattr(create, "ProcessManager", _Recording)
    runner, _gen = install_runner(generator="card_ws", runner="card_ws")
    cid = _canvas_cell(client)
    r = client.post(f"/api/channels/{client.channel_id}/runs",
                    json={"runner": runner, "cell_id": cid, "new_cell": new_cell})
    assert r.status_code == 200, r.text
    wait_spawned(r.json()["run_id"])
    assert "generator start recorded" in run_row(client.channel_id, r.json()["run_id"])["failure_reason"]
    assert seen["workspace"] is not None, f"{seen['card']} missing from the workspace at start"
    assert seen["workspace"] == seen["committed"]
