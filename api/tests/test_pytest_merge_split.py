"""D07 structural operations: effective files, refusal atomicity and races."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from database import get_db_connection
from test_pytest_options_identity import CHANNEL, client as client, make_stack, merge, seed_version, split, accept_file


def declaration(*, rest="keep", root="choice", files=None):
    option = {"name": "Candidate", "files": files or ["**"]}
    if root is not None:
        option["root"] = root
    return yaml.safe_dump({"options": [option], "rest": rest})


def snapshot(ch):
    conn = get_db_connection(CHANNEL)
    try:
        tables = {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                  for table in ("runs", "cells", "cell_edges", "cell_files", "working_files", "file_revisions", "file_conflicts", "blobs", "run_events",
                                "cell_moves", "removed_cells")}
    finally:
        conn.close()
    return ch.model_dump(), tables, sorted(str(p) for p in Path(".collimate/objects").rglob("*") if p.is_file())



@pytest.mark.parametrize("contents,extra,needle", [
    ("[invalid", {}, "YAML"),
    (declaration(root="missing"), {}, "Candidate"),
    (declaration(files=["*.py"]), {}, "Candidate"),
    (declaration(root=None, files=["nothing"]), {}, "Candidate"),
    (declaration(files=["**", "../a.txt"]), {"a.txt": "outside"}, "collision"),
    (declaration(rest="each"), {"a.txt": "outside"}, "collision"),
])
def test_invalid_split_has_no_trace__D07(client, contents, extra, needle):
    ch, _, ids = make_stack([("Source", {"pick.options_stack": contents, "choice/a.txt": "A", **extra})])
    before = snapshot(ch)
    response = split(client, ids[0])
    assert response.status_code == 409, response.text
    assert needle in response.text
    assert snapshot(ch) == before


def test_selected_protected_definition_refuses_without_fallback__D07(client):
    ch, _, ids = make_stack([("Source", {"_runs/a/x.options_stack": declaration(), "z/z/z.options_stack": declaration(), "choice/a.txt": "A"})])
    before = snapshot(ch)
    response = split(client, ids[0])
    assert response.status_code == 409 and "protected" in response.text
    assert snapshot(ch) == before


@pytest.mark.parametrize("rest", ["keep", "drop", "each"])
@pytest.mark.parametrize("protected", [False, True])
def test_rest_policy_and_protected_files__D07(client, rest, protected):
    files = {"pick.options_stack": declaration(rest=rest), "choice/a.txt": "A", "report.txt": "report"}
    if protected:
        files["_runs/stopped/status.html"] = "status"
        files["_runs/stopped/status.review_comments"] = "comments"
    _, stack, ids = make_stack([("Source", files)])
    response = split(client, ids[0])
    assert response.status_code == 200, response.text
    retain = rest == "keep" or protected
    assert len(stack.cells) == (2 if retain else 1)
    option = stack.cells[-1]
    assert {f.name for f in option.files} == ({"a.txt", "report.txt"} if rest == "each" else {"a.txt"})
    if retain:
        assert str(stack.cells[0].id) == ids[0]
        expected = {"report.txt"} if rest == "keep" else set()
        if protected:
            expected |= {"_runs/stopped/status.html", "_runs/stopped/status.review_comments"}
        assert {f.name for f in stack.cells[0].files} == expected


def test_split_uses_live_files_and_accepted_edits_and_tombstones__D07(client):
    _, stack, ids = make_stack([("Source", {"pick.options_stack": "invalid", "choice/a.txt": "old", "choice/deleted.txt": "gone"})])
    seed_version(CHANNEL, ids[0])
    accept_file(ids[0], "choice/live.txt", "new accepted")
    accept_file(ids[0], "pick.options_stack", declaration())
    accept_file(ids[0], "choice/a.txt", "draft")
    accept_file(ids[0], "choice/deleted.txt")
    response = split(client, ids[0])
    assert response.status_code == 200, response.text
    assert {f.name: f.content for f in stack.cells[-1].files} == {"a.txt": "draft", "live.txt": "new accepted"}
    conn = get_db_connection(CHANNEL)
    assert not any(f['path'] == 'choice/deleted.txt' for f in __import__('persistence').snapshot_files(conn, ids[0]))
    conn.close()


def test_deleted_definition_accepted_refuses_without_consuming_accepteds__D07(client):
    ch, _, ids = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a.txt": "A"})])
    accept_file(ids[0], "pick.options_stack")
    before = snapshot(ch)
    assert split(client, ids[0]).status_code == 409
    assert snapshot(ch) == before


def test_merge_declaration_names_folders_accepteds_binary_and_ordinary_files__D07(client):
    from cas import read_blob
    old_name = "_cell.yaml"
    _, stack, ids = make_stack([("Same", {"a.txt": "A", old_name: "ordinary"}), ("Same", {"b.bin": b"\x00\xff"})])
    seed_version(CHANNEL, ids[0])
    accept_file(ids[0], "live.txt", "new")
    accept_file(ids[0], "a.txt", "draft")
    merge(client, stack)
    files = {f.name: f for f in stack.cells[0].files}
    assert files["Same/a.txt"].content == "draft"
    assert files["Same/live.txt"].content == "new"
    assert files[f"Same/{old_name}"].content == "ordinary"
    assert read_blob(files["Same-2/b.bin"].blob_hash) == b"\x00\xff"
    assert yaml.safe_load(files[".options_stack"].content)["options"] == [
        {"name": "Same", "root": "Same", "files": ["**"]},
        {"name": "Same", "root": "Same-2", "files": ["**"]},
    ]


def test_old_layout_alone_cannot_split__D07(client):
    ch, _, ids = make_stack([("Source", {"choice/_cell.yaml": "name: Choice", "choice/a.txt": "A"})])
    before = snapshot(ch)
    assert split(client, ids[0]).status_code == 409
    assert snapshot(ch) == before


def test_merge_empty_exportable_input_refuses_before_mutation__D07(client):
    ch, stack, _ = make_stack([("Empty", {"_runs/dead/a.chat": "dead"}), ("Other", {"a": "a"})])
    before = snapshot(ch)
    response = client.post(f"/api/channels/{CHANNEL}/merge", json={"stack_id": str(stack.id)})
    assert response.status_code == 409 and "Empty" in response.text
    assert snapshot(ch) == before


@pytest.mark.parametrize("operation", ["merge", "split"])
@pytest.mark.parametrize("kind", ["capabilities", "running"])
def test_capabilities_and_live_runs_refused_without_trace__D07(client, operation, kind):
    from cell_placement import CAPABILITIES_STACK_TYPE
    from run_registry import active_managers
    stack_type = CAPABILITIES_STACK_TYPE if kind == "capabilities" else "standard"
    ch, stack, ids = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a": "a"}), ("Other", {"b": "b"})], stack_type=stack_type)
    if kind == "running":
        active_managers["test-options-run"] = SimpleNamespace(cell_id=ids[0])
    try:
        before = snapshot(ch)
        response = client.post(f"/api/channels/{CHANNEL}/{operation}", json={"stack_id": str(stack.id)} if operation == "merge" else {"cell_id": ids[0]})
        assert response.status_code == (409 if kind == "running" else 400)
        if kind == "capabilities":
            verb = "merged" if operation == "merge" else "split"
            assert response.json()["detail"] == f"Capabilities cells are not {verb}"
        assert snapshot(ch) == before
    finally:
        active_managers.pop("test-options-run", None)


@pytest.mark.parametrize("change", ["join", "remove_stack", "replace_cell", "meta", "run", "channel"])
def test_merge_revalidates_canvas_before_commit__F0236_D07(client, monkeypatch, change):
    import routes.options as module
    from datetime import timedelta
    from models import Cell
    import run_registry as registry
    from state import state
    ch, stack, ids = make_stack([("First", {"a": "a"}), ("Second", {"b": "b"})])
    real_prepare = module._mapped_files
    changed = False
    def prepare(*args, **kwargs):
        nonlocal changed
        result = real_prepare(*args, **kwargs)
        if not changed:
            changed = True
            if change == "join":
                stack.cells.append(Cell(name="Joined"))
            elif change == "remove_stack":
                ch.main_stacks.remove(stack)
            elif change == "replace_cell":
                stack.cells[0] = stack.cells[0].model_copy(update={"name": "edited"})
            elif change == "meta":
                stack.cells[0].updated_at += timedelta(seconds=1)
            elif change == "run":
                registry.reserve("options-race", ids[0])
            else:
                state.channels[state.channels.index(ch)] = ch.model_copy(deep=True)
        return result
    monkeypatch.setattr(module, "_mapped_files", prepare)
    conn = get_db_connection(CHANNEL)
    before = conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    try:
        response = client.post(f"/api/channels/{CHANNEL}/merge", json={"stack_id": str(stack.id)})
        assert response.status_code == 409, response.text
        assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == before
        assert all(str(c.id) in ids or c.name == "Joined" for c in stack.cells)
    finally:
        registry.release("options-race")
        conn.close()


def test_split_revalidates_source_and_preserves_existing_chains__F0236_D07(client, monkeypatch):
    import routes.options as module
    _, stack, ids = make_stack([("First", {"a": "a"}), ("Second", {"b": "b"})])
    source = merge(client, stack)
    real_prepare = module._mapped_files
    def prepare(*args, **kwargs):
        result = real_prepare(*args, **kwargs)
        stack.cells.clear()
        return result
    monkeypatch.setattr(module, "_mapped_files", prepare)
    response = split(client, source)
    assert response.status_code == 409, response.text
    conn = get_db_connection(CHANNEL)
    assert conn.execute("SELECT COUNT(*) FROM cell_moves WHERE op='split'").fetchone()[0] == 0
    conn.close()


def test_accepted_race_refuses_without_deleting_late_accepted__D07(client, monkeypatch):
    import routes.options as module
    _, _, ids = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a": "a"})])
    real = module._mapped_files
    def prepare(*args, **kwargs):
        result = real(*args, **kwargs)
        accept_file(ids[0], "choice/a", "late")
        return result
    monkeypatch.setattr(module, "_mapped_files", prepare)
    assert split(client, ids[0]).status_code == 409
    conn = get_db_connection(CHANNEL)
    assert __import__('persistence').read_file(CHANNEL, ids[0], 'choice/a')[1] == b'late'
    conn.close()


def test_accepted_tombstone_changes_selected_definition_by_depth_then_path__D07(client):
    from models import VirtualFile
    ch, stack, ids = make_stack([("Source", {
        "a.options_stack": "invalid root wins until deleted",
        "nested/b.options_stack": declaration(root="choice"),
        "nested/a.options_stack": declaration(root="choice", files=["a.txt"]),
        "nested/choice/a.txt": "A", "nested/choice/b.txt": "B",
    })])
    stack.cells[0].files.append(VirtualFile(name="deep/deeper/options.options_stack", content="invalid"))
    accept_file(ids[0], "a.options_stack")
    response = split(client, ids[0])
    assert response.status_code == 200, response.text
    assert {f.name: f.content for f in stack.cells[-1].files} == {"a.txt": "A"}
    assert "nested/b.options_stack" in {f.name for f in stack.cells[0].files}
    assert "nested/a.options_stack" not in {f.name for f in stack.cells[0].files}


def test_missing_binary_blob_refuses_before_mutation__D07(client):
    from cas import store_bytes, blob_path
    ch, stack, ids = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a": "A"})])
    digest = store_bytes(b'\xffmissing')
    accept_file(ids[0], 'choice/missing.bin', blob=digest)
    Path(blob_path(digest)).unlink()
    before = snapshot(ch)
    response = split(client, ids[0])
    assert response.status_code == 409 and "missing" in response.text.lower()
    assert snapshot(ch) == before


def test_merge_folder_cannot_collide_with_root_definition__D07(client):
    _, stack, _ = make_stack([(" .options_stack", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    names = {f.name for f in stack.cells[0].files}
    assert not any(p.startswith(".options_stack/") for p in names)
    assert ".options_stack-2/a" in names
    assert split(client, source).status_code == 200


class _CommitBoundary:
    """A connection whose commit lets a late accepted edit land right after it."""

    def __init__(self, conn, cell_id):
        self.conn, self.cell_id = conn, cell_id

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def commit(self):
        self.conn.commit()
        if self.conn.execute("SELECT 1 FROM cell_moves LIMIT 1").fetchone():
            accept_file(self.cell_id, "late.txt", "edit at commit boundary")


def _records():
    conn = get_db_connection(CHANNEL)
    try:
        return (conn.execute("SELECT COUNT(*) FROM cell_moves").fetchone()[0],
                [r[0] for r in conn.execute("SELECT cell_id FROM removed_cells ORDER BY cell_id")])
    finally:
        conn.close()


def test_merge_that_stays_off_the_canvas_leaves_no_lineage_or_removed_record_O03(client, monkeypatch):
    import routes.options as module
    _, stack, ids = make_stack([("First", {"a.txt": "a"}), ("Second", {"b.txt": "b"})])
    real = module.get_db_connection
    monkeypatch.setattr(module, "get_db_connection", lambda *a, **k: _CommitBoundary(real(*a, **k), ids[0]))
    response = client.post(f"/api/channels/{CHANNEL}/merge", json={"stack_id": str(stack.id)})
    assert response.status_code == 409, response.text
    assert [str(c.id) for c in stack.cells] == ids
    assert _records() == (0, [])


def test_split_that_stays_off_the_canvas_keeps_the_shelf_as_it_was_O03(client, monkeypatch):
    import routes.options as module
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    before = _records()
    assert before[1] == sorted(ids)
    real = module.get_db_connection
    monkeypatch.setattr(module, "get_db_connection", lambda *a, **k: _CommitBoundary(real(*a, **k), source))
    assert split(client, source).status_code == 409
    assert [str(c.id) for c in stack.cells] == [source]
    assert _records() == before
