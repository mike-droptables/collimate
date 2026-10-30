"""A run's file touches: ``versions.file_changes`` over one accepted run
write, carried by its commit event's ``changes`` (every host commit carries
none)."""
from __future__ import annotations

import json

import test_pytest_run_folders as support
from test_pytest_run_folders import CHANNEL, _accept_file, _emit, _mem
from versions import file_changes

client, cell, live = support.client, support.cell, support.live


def touches(before, after, paths=None):
    return [(c["path"], c["op"], c["folder"]) for c in
            file_changes(before, after, paths if paths is not None else set(before) | set(after))]


def test_file_changes_classifies_create_edit_delete_and_skips_unchanged():
    before = {"a.md": "1", "b.md": "2", "c.md": "3"}
    after = {"a.md": "1", "b.md": "22", "d.md": "4"}
    assert touches(before, after) == [("b.md", "edit", False), ("c.md", "delete", False), ("d.md", "create", False)]
    assert touches(before, after, ["a.md", "b.md"]) == [("b.md", "edit", False)]


def test_file_changes_drops_run_paths():
    after = {"_runs/x-11111111/chat/main.json": "1", "_runs/x-11111111/x-11111111.run": "2", "out.md": "3"}
    assert touches({}, after) == [("out.md", "create", False)]


def test_collkeep_markers_are_folders_never_files():
    assert touches({}, {"empty/.collkeep": "e"}) == [("empty", "create", True)]
    assert touches({"empty/.collkeep": "e"}, {}) == [("empty", "delete", True)]
    held = {"docs/a.md": "1"}
    assert touches(held, {**held, "docs/.collkeep": "e"}) == []
    assert touches({**held, "docs/.collkeep": "e"}, held) == []


def test_prefix_transitions_create_and_delete_folders_shallow_first_and_deep_first():
    assert touches({}, {"a/b/c.md": "1", "a/d.md": "2"}) == [
        ("a", "create", True), ("a/b", "create", True), ("a/b/c.md", "create", False), ("a/d.md", "create", False)]
    assert touches({"a/b/c.md": "1", "z.md": "2"}, {"z.md": "2"}) == [
        ("a/b/c.md", "delete", False), ("a/b", "delete", True), ("a", "delete", True)]
    assert touches({"a/x.md": "1"}, {"a/y.md": "1"}) == [("a/x.md", "delete", False), ("a/y.md", "create", False)]


def _commit_payloads(run_id):
    from database import get_db_connection
    conn = get_db_connection(CHANNEL)
    try:
        return [json.loads(row["payload"]) for row in conn.execute(
            "SELECT payload FROM run_events WHERE run_id=? AND event_type='commit' ORDER BY id", (run_id,))]
    finally:
        conn.close()


def test_a_run_commit_payload_carries_only_its_own_changes(cell, live):
    run = live.start(cell)
    _accept_file(cell, "user-draft.md", "the user's pending edit")
    _emit(run, artifacts=[_mem("notes/run.md", "the run's"), _mem(f"{run.folder}/chat/main.json", "[]")])
    (payload,) = _commit_payloads(run.run_id)
    assert payload["changes"] == [{"path": "notes", "op": "create", "folder": True},
                                  {"path": "notes/run.md", "op": "create", "folder": False}]


def test_host_commits_carry_empty_changes(cell, live):
    from cell_commits import run_card, sweep_run_folder, write_run_start
    run = live.start(cell)
    card = run_card(run_id=run.run_id, runner_id="synth", runner_name="Synth", generator="synth", label=run.label)
    write_run_start(CHANNEL, cell, card, {"starting.md": "placeholder"})
    _emit(run, artifacts=[_mem(f"{run.folder}/x.txt", "x")])
    sweep_run_folder(CHANNEL, cell, run.run_id, run.label)
    payloads = _commit_payloads(run.run_id)
    assert len(payloads) == 3
    assert [p["changes"] for p in payloads] == [[], [], []]


def test_commit_log_line_is_unchanged_by_changes():
    from process_manager.log_format import format_log_line
    bare = {"cell_id": "v1", "name": "Notes", "sequence_num": 2, "file_count": 3}
    with_changes = {**bare, "changes": [{"path": "a.md", "op": "create", "folder": False}]}
    at = "2026-10-24 12:00:00"
    line = format_log_line("commit", json.dumps(bare), at)
    assert line is not None and format_log_line("commit", json.dumps(with_changes), at) == line
