"""A cell's journal (O04): makes, edits, runs and moves, newest first, every
time UTC ISO with an offset. Synthetic fixtures only: version rows, runs and
moves are written straight into the channel database."""
import json
from uuid import uuid4

import pytest

def _conn(channel_id):
    from database import get_db_connection
    return get_db_connection(channel_id)


def utc_local(*args):
    """A layout time (naive local) for the UTC moment ``args``."""
    from datetime import datetime, timezone
    return datetime(*args, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def add_cell(client, name, files=None):
    from models import Cell, Stack, VirtualFile
    from state import get_active_channel
    ch = get_active_channel()
    cell = Cell(name=name, files=[VirtualFile(name=k, content=v) for k, v in (files or {}).items()])
    stack = Stack(name="Project", cells=[cell])
    ch.main_stacks.append(stack)
    from persistence import initialize_files
    conn = _conn(client.channel_id)
    initialize_files(conn, str(cell.id), cell.files)
    conn.commit()
    conn.close()
    return stack, cell


def system_run(conn, prompt, generator_name=None):
    run_id = str(uuid4())
    conn.execute("INSERT INTO runs(id, prompt, status, generator_name) VALUES(?, ?, 'completed', ?)",
                 (run_id, prompt, generator_name))
    return run_id


def generator_run(conn, cell_id, *, runner="alpha", name="Alpha", created, ended=None,
                  status="completed", note=None, outcome=None):
    run_id = str(uuid4())
    conn.execute(
        "INSERT INTO runs(id, prompt, status, kind, target_cell_id, runner_id, runner_snapshot, "
        "created_at, ended_at, note, outcome) VALUES(?, '', ?, 'generator', ?, ?, ?, ?, ?, ?, ?)",
        (run_id, status, cell_id, runner, json.dumps({"name": name}), created, ended, note, outcome))
    return run_id


def genesis(conn, cell_id, files, at):
    from versions import synthesize_genesis_row
    from models import VirtualFile
    from persistence import initialize_files
    run_id = system_run(conn, "genesis-snapshot")
    entries = [VirtualFile(name=k, content=v) for k, v in files.items()]
    initialize_files(conn, cell_id, entries)
    synthesize_genesis_row(conn, cell_id, run_id, 1, entries)
    conn.execute("UPDATE cells SET created_at=? WHERE id=?", (at, cell_id))


def version(conn, cell_id, run_id, changes, *, at, source_type="manual_edit", generator_name="manual_edit"):
    from cell_commits import cell_commit_lock
    from versions import write_version_from_overlay
    with cell_commit_lock(cell_id):
        vid = write_version_from_overlay(conn, cell_id, run_id, [(p, "set", v) for p, v in changes.items()],
                                         version_name="v", source_type=source_type,
                                         generator_name=generator_name)
    conn.execute("UPDATE cells SET created_at=? WHERE id=?", (at, vid))
    return vid


def journal(client, cell_id=None, **params):
    url = (f"/api/channels/{client.channel_id}/cells/{cell_id}/journal"
           if cell_id else f"/api/channels/{client.channel_id}/journal")
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def story(client):
    """One cell with every kind of version and event, plus the cells its
    moves name."""
    import cell_moves
    _, cell = add_cell(client, "Alpha notes", {"notes.md": "one"})
    cell.created_at = utc_local(2026, 10, 4, 9, 0, 0)
    cid = str(cell.id)
    conn = _conn(client.channel_id)
    ids = {"cell": cid}
    genesis(conn, cid, {"notes.md": "one"}, "2026-10-04 09:01:00")
    ids["edit"] = version(conn, cid, system_run(conn, "checkpoint"), {"notes.md": "two"}, at="2026-10-04 09:10:00")
    run = generator_run(conn, cid, created="2026-10-04 10:00:00", ended="2026-10-04T10:05:00+00:00",
                        note="probe passed", outcome="pass")
    ids["run"] = run
    # The run-start flush files the user's edit under the generator run.
    ids["jit"] = version(conn, cid, run, {"notes.md": "three"}, at="2026-10-04 10:00:00")
    version(conn, cid, run, {"_runs/alpha-1/alpha-1.run": "card"}, at="2026-10-04 10:00:01",
            source_type="generator", generator_name="alpha")
    ids["output"] = version(conn, cid, run, {"out/result.md": "result", "notes.md": "four"},
                            at="2026-10-04 10:03:00", source_type="generator", generator_name="alpha")
    version(conn, cid, run, {"_runs/alpha-1/log.txt": "end"}, at="2026-10-04 10:05:00",
            source_type="generator", generator_name="alpha")
    version(conn, cid, system_run(conn, "run folder sweep"), {"_runs/x/y": "swept"},
            at="2026-10-04 10:06:00", generator_name="host")
    version(conn, cid, system_run(conn, "merge 2 cells", "merge"), {"notes.md": "five"},
            at="2026-10-04 11:00:00")
    copy, other, merged = str(uuid4()), str(uuid4()), str(uuid4())
    ids.update(copy=copy, other=other, merged=merged)
    dup = system_run(conn, "duplicate")
    cell_moves.record_move(conn, op="duplicate", op_id=dup, from_cell=cid, from_version=ids["edit"], to_cell=copy)
    merge = system_run(conn, "merge 2 cells", "merge")
    for i, source in enumerate((cid, other)):
        cell_moves.record_move(conn, op="merge", op_id=merge, from_cell=source,
                               from_version=ids["output"] if source == cid else None, to_cell=merged, ord=i)
    conn.execute("UPDATE cell_moves SET created_at='2026-10-04 10:30:00' WHERE op='duplicate'")
    conn.execute("UPDATE cell_moves SET created_at='2026-10-04 11:00:00' WHERE op='merge'")
    conn.commit()
    conn.close()
    return ids


def test_cell_journal_tells_makes_edits_runs_and_moves_newest_first_O04(client, story):
    events = journal(client, story["cell"])
    assert [e["kind"] for e in events] == ["merge", "duplicate", "run", "edit", "edit", "make"]
    assert all(e["t"].endswith("+00:00") for e in events)
    assert [e["t"] for e in events] == sorted((e["t"] for e in events), reverse=True)
    merge, dup, run, jit, edit, make = events

    assert [c["cell_id"] for c in merge["cells"]] == [story["cell"], story["other"], story["merged"]]
    assert merge["by"]["kind"] == "user" and merge["run"] is None
    assert merge["cells"][0] == {"cell_id": story["cell"], "name": "Alpha notes", "status": "open"}
    assert {c["status"] for c in merge["cells"][1:]} == {"removed"}
    assert [c["cell_id"] for c in dup["cells"]] == [story["cell"], story["copy"]]

    assert run["by"] == {"kind": "runner", "runner_id": "alpha", "runner_name": "Alpha"}
    assert run["t"] == "2026-10-04T10:05:00+00:00"
    assert run["run"] == {"run_id": story["run"], "runner_id": "alpha", "runner_name": "Alpha",
                          "status": "completed", "outcome": "pass", "note": "probe passed",
                          "started_at": "2026-10-04T10:00:00+00:00", "ended_at": "2026-10-04T10:05:00+00:00",
                          "files_written": ["notes.md", "out/result.md"]}
    assert run["files"] == ["notes.md", "out/result.md"] and run["version_id"] == story["output"]

    # The flush under the run's id is still the user's edit (by source_type).
    assert jit["version_id"] == story["jit"] and jit["by"]["kind"] == "user" and jit["files"] == ["notes.md"]
    assert edit["version_id"] == story["edit"] and edit["t"] == "2026-10-04T09:10:00+00:00"
    assert make["by"]["kind"] == "user" and make["files"] == ["notes.md"]
    assert make["cells"] == [{"cell_id": story["cell"], "name": "Alpha notes", "status": "open"}]


def test_journal_pages_by_before_and_limit_without_splitting_a_second_O04(client, story):
    first = journal(client, story["cell"], limit=2)
    assert [e["kind"] for e in first] == ["merge", "duplicate"]
    rest = journal(client, story["cell"], before=first[-1]["t"], limit=2)
    assert [e["kind"] for e in rest] == ["run", "edit"]
    conn = _conn(client.channel_id)
    conn.execute("UPDATE cells SET created_at='2026-10-04 10:05:00' WHERE id=?", (story["jit"],))
    conn.commit()
    conn.close()
    tied = journal(client, story["cell"], before=first[-1]["t"], limit=1)
    assert [e["kind"] for e in tied] in (["run", "edit"], ["edit", "run"])
    assert client.get(f"/api/channels/{client.channel_id}/cells/{story['cell']}/journal",
                      params={"before": "2026-10-04T10:00:00"}).status_code == 422
    assert client.get(f"/api/channels/{client.channel_id}/cells/{story['cell']}/journal",
                      params={"limit": 0}).status_code == 422


def test_run_in_new_cell_is_one_event_on_both_cells_and_the_clone_has_no_make_O04(client):
    import cell_moves
    _, source = add_cell(client, "Frame", {"frame.md": "axes"})
    source.created_at = utc_local(2026, 10, 5, 7, 0, 0)
    _, clone = add_cell(client, "Frame", {"frame.md": "axes"})
    conn = _conn(client.channel_id)
    run = generator_run(conn, str(clone.id), runner="sower", name="Idea sower",
                        created="2026-10-05 08:00:00", status="running")
    genesis(conn, str(clone.id), {"frame.md": "axes"}, "2026-10-05 08:00:00")
    conn.execute("UPDATE cells SET run_id=? WHERE id=?", (run, str(clone.id)))
    cell_moves.record_move(conn, op="run_cell", op_id=run, from_cell=str(source.id),
                           from_version=None, to_cell=str(clone.id))
    conn.commit()
    conn.close()
    on_source = journal(client, str(source.id))
    on_clone = journal(client, str(clone.id))
    assert [e["kind"] for e in on_source] == ["run_cell", "make"]
    assert [e["kind"] for e in on_clone] == ["run_cell"]
    event = on_clone[0]
    assert event == on_source[0]
    assert [c["cell_id"] for c in event["cells"]] == [str(source.id), str(clone.id)]
    assert event["run"]["status"] == "running" and event["run"]["ended_at"] is None
    assert event["t"] == event["run"]["started_at"] == "2026-10-05T08:00:00+00:00"
    assert event["by"] == {"kind": "runner", "runner_id": "sower", "runner_name": "Idea sower"}


def test_channel_journal_serves_every_cell_O04(client, story):
    _, second = add_cell(client, "Second", {"b.md": "b"})
    conn = _conn(client.channel_id)
    genesis(conn, str(second.id), {"b.md": "b"}, "2026-10-04 12:00:00")
    version(conn, str(second.id), system_run(conn, "checkpoint"), {"b.md": "bb"}, at="2026-10-04 12:30:00")
    conn.commit()
    conn.close()
    events = journal(client)
    pairs = [(e["kind"], [c["cell_id"] for c in e["cells"]]) for e in events]
    assert ("edit", [str(second.id)]) in pairs and ("make", [str(second.id)]) in pairs
    assert ("run", [story["cell"]]) in pairs and ("make", [story["cell"]]) in pairs
    # The cells a move made have no make of their own.
    assert all(kind != "make" or cells[0] not in (story["copy"], story["merged"]) for kind, cells in pairs)
    assert [e["t"] for e in events] == sorted((e["t"] for e in events), reverse=True)
    # A page never splits a second: the newest page holds every event of it.
    newest = journal(client, limit=1)
    assert newest == [e for e in events if e["t"] == events[0]["t"]]
    # Paged at a fixture time, clear of the seeded cells' clock times.
    cut = next(e["t"] for e in events if e["kind"] == "merge")
    older = journal(client, before=cut)
    assert older and older == [e for e in events if e["t"] < cut]


def test_journal_hides_removal_checkpoints_and_a_removed_cell_keeps_its_journal_O04(client):
    stack, cell = add_cell(client, "Notes", {"a.md": "a"})
    conn = _conn(client.channel_id)
    genesis(conn, str(cell.id), {"a.md": "a"}, "2026-10-04 08:00:00")
    conn.commit()
    conn.close()
    base = client.get(f"/api/channels/{client.channel_id}/cells/{cell.id}/files").json()["files"][0]
    saved = client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/files/mutate", json={
        "operation_id": str(uuid4()), "mutations": [{"path": "a.md", "kind": "put",
        "base_revision": base["file_revision"], "writer_id": "journal-fixture", "edit_seq": 1,
        "content_text": "uncheckpointed"}]})
    assert saved.status_code == 200 and saved.json()["outcome"] == "accepted"
    assert client.post(f"/api/channels/{client.channel_id}/cells/{cell.id}/remove").status_code == 200
    conn = _conn(client.channel_id)
    assert conn.execute("SELECT COUNT(*) FROM cell_file_versions WHERE root_id=?", (str(cell.id),)).fetchone()[0] == 2
    conn.close()
    events = journal(client, str(cell.id))
    assert [e["kind"] for e in events] == ["make"]
    assert events[0]["cells"] == [{"cell_id": str(cell.id), "name": "Notes", "status": "removed"}]


def test_a_split_that_restores_a_merge_input_keeps_its_make_O04(client):
    """Merge A and B into M, then split M back: A comes back on its own chain
    (the split move's to_version is a child version of A), a fresh output
    starts its own. A keeps its make; only the fresh output was made by the
    split."""
    import cell_moves
    _, cell = add_cell(client, "Alpha", {"a.md": "a"})
    cell.created_at = utc_local(2026, 10, 5, 9, 0, 0)
    _, fresh = add_cell(client, "Fresh", {"f.md": "f"})
    fresh.created_at = utc_local(2026, 10, 5, 11, 0, 0)
    a, merged, out = str(cell.id), str(uuid4()), str(fresh.id)
    conn = _conn(client.channel_id)
    genesis(conn, a, {"a.md": "a"}, "2026-10-05 09:00:00")
    merge = system_run(conn, "merge 2 cells", "merge")
    genesis(conn, merged, {"a.md": "a", "b.md": "b"}, "2026-10-05 10:00:00")
    for i, source in enumerate((a, str(uuid4()))):
        cell_moves.record_move(conn, op="merge", op_id=merge, from_cell=source, from_version=None,
                               to_cell=merged, to_version=merged, ord=i)
    split = system_run(conn, "split 2 options", "split")
    restored = version(conn, a, split, {"a.md": "a2"}, at="2026-10-05 11:00:00",
                       source_type="split", generator_name="split")
    genesis(conn, out, {"f.md": "f"}, "2026-10-05 11:00:00")
    cell_moves.record_move(conn, op="split", op_id=split, from_cell=merged, from_version=merged,
                           to_cell=a, to_version=restored, ord=0)
    cell_moves.record_move(conn, op="split", op_id=split, from_cell=merged, from_version=merged,
                           to_cell=out, to_version=out, ord=1)
    conn.execute("UPDATE cell_moves SET created_at='2026-10-05 10:00:00' WHERE op='merge'")
    conn.execute("UPDATE cell_moves SET created_at='2026-10-05 11:00:00' WHERE op='split'")
    conn.commit()
    conn.close()

    own = [(e["kind"], [c["cell_id"] for c in e["cells"]]) for e in journal(client, a)]
    assert [kind for kind, _ in own] == ["split", "merge", "make"]
    assert own[-1] == ("make", [a])
    pairs = [(e["kind"], [c["cell_id"] for c in e["cells"]]) for e in journal(client)]
    assert ("make", [a]) in pairs
    assert ("make", [out]) not in pairs and ("make", [merged]) not in pairs
    assert [e["kind"] for e in journal(client, out)] == ["split"]


def test_metadata_patch_records_cell_names_but_stack_rename_has_no_event(client):
    stack, cell = add_cell(client, "Initial title", {"a.txt": "unchanged"})
    conn = _conn(client.channel_id)
    before = (conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0],
              conn.execute("SELECT COUNT(*) FROM file_revisions").fetchone()[0])
    conn.close()
    response = client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": "New title"})
    assert response.status_code == 200, response.text
    assert client.put(f"/api/stacks/{client.channel_id}/{stack.id}/rename", params={"name": "Column title"}).status_code == 200
    renamed = [e for e in journal(client, str(cell.id)) if e["kind"] == "cell_renamed"]
    assert len(renamed) == 1
    assert (renamed[0]["old_name"], renamed[0]["new_name"]) == ("Initial title", "New title")
    assert renamed[0]["by"]["kind"] == "user"
    assert client.patch(f"/api/channels/{client.channel_id}/cells/{cell.id}/meta", json={"name": "New title"}).status_code == 200
    assert len([e for e in journal(client, str(cell.id)) if e["kind"] == "cell_renamed"]) == 1
    conn = _conn(client.channel_id)
    after = (conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0],
             conn.execute("SELECT COUNT(*) FROM file_revisions").fetchone()[0])
    conn.close()
    assert after == before
