"""D07 structural operations preserve exact local logical identities."""
from __future__ import annotations

import pytest

from database import get_db_connection

CHANNEL = "19c2b40a-d5da-43c8-abfc-ebc5d0441f6d"


@pytest.fixture
def client(api_app):
    from fastapi.testclient import TestClient
    from models import Channel
    from state import state
    with TestClient(api_app) as client:
        channel = Channel(id=CHANNEL, name="Starting")
        state.channels = [channel]
        state.active_channel_id = channel.id
        get_db_connection(CHANNEL).close()
        yield client


def accept_file(cell_id, path, text=None, blob=None, language=None):
    from models import VirtualFile
    from persistence import snapshot_files, restore_snapshot, publish_files
    from versions import build_virtual_files
    with get_db_connection(CHANNEL) as conn:
        files = [f for f in build_virtual_files(snapshot_files(conn, cell_id)) if f.name != path]
        if text is not None or blob is not None:
            entry = VirtualFile(name=path, content=text or "", blob_hash=blob)
            if language is not None:
                entry.language = language
            files.append(entry)
        receipt = restore_snapshot(conn, cell_id, files, writer_id="host:test")
        conn.commit()
    publish_files(CHANNEL, cell_id, receipt, persist=False)



def seed_version(channel, cell_id):
    import uuid
    from cell_commits import cell_commit_lock
    from versions import resolve_cell_version
    run_id = str(uuid.uuid4())
    conn = get_db_connection(channel)
    try:
        with cell_commit_lock(cell_id):
            conn.execute("INSERT INTO runs(id,prompt,status) VALUES(?,'fixture','completed')", (run_id,))
            version, _promoted = resolve_cell_version(
                conn, channel, cell_id, run_id, __import__("persistence").snapshot_files(conn, cell_id),
            )
            conn.commit()
            return version
    finally:
        conn.close()


def make_stack(cells, *, stack_type="standard"):
    from cas import store_bytes
    from models import Cell, Stack, VirtualFile
    from state import state
    ch = next(c for c in state.channels if c.id == state.active_channel_id)
    built = []
    for name, files in cells:
        vfiles = [VirtualFile(name=p, content="", blob_hash=store_bytes(v))
                  if isinstance(v, bytes) else VirtualFile(name=p, content=v)
                  for p, v in files.items()]
        built.append(Cell(name=name, files=vfiles))
    stack = Stack(name="Choices", cells=built, stack_type=stack_type)
    ch.main_stacks.append(stack)
    from persistence import initialize_files
    with get_db_connection(CHANNEL) as conn:
        for cell in built:
            initialize_files(conn, str(cell.id), cell.files)
        conn.commit()
    return ch, stack, [str(c.id) for c in built]


def merge(client, stack):
    response = client.post(f"/api/channels/{CHANNEL}/merge", json={"stack_id": str(stack.id)})
    assert response.status_code == 200, response.text
    return response.json()["new_cell_id"]


def split(client, cell_id):
    return client.post(f"/api/channels/{CHANNEL}/split", json={"cell_id": cell_id})


def moves(op):
    """The channel's ``op`` lineage rows, oldest first."""
    conn = get_db_connection(CHANNEL)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT op_id, from_cell, from_version, to_cell, to_version, ord FROM cell_moves "
            "WHERE op=? ORDER BY id", (op,))]
    finally:
        conn.close()


def test_merge_split_restores_logical_ids_and_appends_actual_versions__D07(client):
    _, stack, ids = make_stack([("First", {"a.txt": "A"}), ("Second", {"b.bin": b"\xff\x00"})])
    source = merge(client, stack)
    response = split(client, source)
    assert response.status_code == 200, response.text
    assert response.json()["new_cell_ids"] == ids
    assert [str(c.id) for c in stack.cells] == [source, *ids]
    assert stack.cells[0].files == []
    conn = get_db_connection(CHANNEL)
    try:
        appended = []
        for original in ids:
            row = conn.execute("SELECT child_cell_id FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'", (original,)).fetchone()
            assert row and row[0] != original
            appended.append(row[0])
    finally:
        conn.close()
    assert [(m[1], m[2], m[3], m[4]) for m in moves("split")] == [(source, source, original, version)
                                                                   for original, version in zip(ids, appended)]


def append_version(cell_id):
    """A real append with unchanged bytes, so tip identity is tested separately."""
    import uuid
    from cell_commits import cell_commit_lock
    from versions import insert_version, tip_file_map
    from versions import find_tip
    conn = get_db_connection(CHANNEL)
    try:
        with cell_commit_lock(cell_id):
            tip = find_tip(conn, cell_id)
            run = str(uuid.uuid4())
            conn.execute("INSERT INTO runs(id,status) VALUES(?,'completed')", (run,))
            files = tip_file_map(conn, tip)
            version, _ = insert_version(conn, tip_id=tip, parent_files=files, run_id=run,
                                        files=files, file_entries=__import__("versions").version_file_entries(conn, tip), version_name="Another", source_type="manual_edit",
                                        generator_name="manual_edit")
            conn.commit()
            return version
    finally:
        conn.close()


def test_restore_reads_merge_moves_but_links_from_materialized_tip__D07_O03(client):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    intermediate = append_version(source)
    accept_file(source, "report.txt", "accepted added file")
    response = split(client, source)
    assert response.status_code == 200, response.text
    assert response.json()["new_cell_ids"] == ids
    conn = get_db_connection(CHANNEL)
    try:
        parents = {m[2] for m in moves("split")}
        assert len(parents) == 1 and source not in parents and intermediate not in parents
        parent = next(iter(parents))
        assert conn.execute("SELECT 1 FROM cell_edges WHERE parent_cell_id=? AND child_cell_id=? AND parent_key='previous_version'", (intermediate, parent)).fetchone()
        assert conn.execute("SELECT 1 FROM cell_files WHERE cell_id=? AND filepath='report.txt'", (parent,)).fetchone()
        assert [f.name for f in stack.cells[0].files] == ["report.txt"]
    finally:
        conn.close()


def test_restore_uses_original_chain_head_not_original_merge_tip__D07(client):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    for cid in ids:
        seed_version(CHANNEL, cid)
    tips = [append_version(cid) for cid in ids]
    source = merge(client, stack)
    response = split(client, source)
    assert response.status_code == 200 and response.json()["new_cell_ids"] == ids
    conn = get_db_connection(CHANNEL)
    for tip in tips:
        assert conn.execute("SELECT 1 FROM cell_edges WHERE parent_cell_id=? AND parent_key='previous_version'", (tip,)).fetchone()
    conn.close()


@pytest.mark.parametrize("change", ["bytes", "path", "main", "nav"])
def test_ineligible_option_gets_new_identity__D07(client, change):
    import uuid
    from models import Stack
    ch, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    original = stack.cells[0]
    if change == "interior":
        seed_version(CHANNEL, ids[0])
        mounted = append_version(ids[0])
    else:
        mounted = ids[0]
    source = merge(client, stack)
    if change in ("bytes", "path"):
        file = next(f for f in stack.cells[0].files if f.name == "First/a")
        if change == "bytes":
            accept_file(source, file.name, "changed")
        else:
            accept_file(source, file.name)
            accept_file(source, "First/renamed", "A")
    elif change == "tip":
        append_version(ids[0])
    else:
        occupied = original.model_copy(update={"id": uuid.UUID(mounted)})
        target = ch.main_stacks if change == "main" else ch.nav_stacks
        target.append(Stack(cells=[occupied]))
    response = split(client, source)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert outputs[0] not in ids and outputs[1] == ids[1]
    # A new identity is its own first version.
    assert [(m[3], m[5]) for m in moves("split")] == [(outputs[0], 0), (outputs[1], 1)]
    assert moves("split")[0][4] == outputs[0]


def test_duplicate_options_claim_identical_candidates_once_in_numeric_order__D07(client):
    import yaml
    _, stack, ids = make_stack([("First", {"a": "same"}), ("Second", {"a": "same"})])
    source = merge(client, stack)
    declaration = next(f for f in stack.cells[0].files if f.name == ".options_stack")
    accept_file(source, declaration.name, yaml.safe_dump({"options": [
        {"name": name, "root": "First", "files": ["**"]} for name in ("Renamed", "Again", "Third")
    ], "rest": "drop"}))
    response = split(client, source)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert outputs[:2] == ids and outputs[2] not in ids
    assert [c.name for c in stack.cells] == ["Renamed", "Again", "Third"]


@pytest.mark.parametrize("branch", [False, True])
def test_import_or_branched_clone_never_follows_merge_ancestry__D07(client, branch):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    if branch:
        response = client.post(f"/api/channels/{CHANNEL}/cells/{source}/fork", json={"target_cell_id": source})
        assert response.status_code == 200, response.text
        target = response.json()["new_cell_id"]
    else:
        _, _, copied = make_stack([("Imported", {f.name: f.content for f in stack.cells[0].files})])
        target = copied[0]
    response = split(client, target)
    assert response.status_code == 200, response.text
    assert set(response.json()["new_cell_ids"]).isdisjoint(ids)


def test_failed_restore_rolls_back_all_versions_and_preserves_accepted_files__D07(client, monkeypatch):
    import routes.options as module
    from test_pytest_merge_split import snapshot
    ch, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    accept_file(source, "report.txt", "report")
    before = snapshot(ch)
    real = module.insert_version
    calls = 0
    def fail_after_first_restore(*args, **kwargs):
        nonlocal calls
        result = real(*args, **kwargs)
        if kwargs["source_type"] == "split":
            calls += 1
            if calls == 2:
                raise RuntimeError("injected transaction failure")
        return result
    monkeypatch.setattr(module, "insert_version", fail_after_first_restore)
    with pytest.raises(RuntimeError, match="injected"):
        split(client, source)
    assert snapshot(ch)[:2] == before[:2]
    conn = get_db_connection(CHANNEL)
    from versions import find_tip
    assert [find_tip(conn, cid) for cid in ids] == ids
    conn.close()


def test_busy_database_refuses_without_waiting_under_state_lock__D07(client):
    import time
    from test_pytest_merge_split import declaration, snapshot
    ch, _, ids = make_stack([("Source", {"pick.options_stack": declaration(), "choice/a": "A"})])
    before = snapshot(ch)
    conn = get_db_connection(CHANNEL)
    conn.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        response = split(client, ids[0])
        elapsed = time.monotonic() - start
        assert response.status_code == 409 and "retry" in response.text
        assert elapsed < 2, elapsed
    finally:
        conn.rollback()
        conn.close()
    assert snapshot(ch) == before


def test_never_versioned_merge_inputs_use_genesis_source_type__D07_F0207(client):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    merge(client, stack)
    conn = get_db_connection(CHANNEL)
    try:
        assert [conn.execute("SELECT source_type FROM cells WHERE id=?", (cid,)).fetchone()[0] for cid in ids] == ["genesis", "genesis"]
    finally:
        conn.close()


def test_structural_mapping_preserves_explicit_language__D07_F0048(client):
    _, stack, _ = make_stack([("First", {"custom.unknown": "A"}), ("Second", {"data.bin": b"\xff"})])
    accept_file(str(stack.cells[0].id), "custom.unknown", "A", language="custom-language")
    accept_file(str(stack.cells[1].id), "data.bin", blob=stack.cells[1].files[0].blob_hash, language="binary")
    source = merge(client, stack)
    files = {f.name: f for f in stack.cells[0].files}
    assert files["First/custom.unknown"].language == "custom-language"
    assert files["Second/data.bin"].language == "binary"
    response = split(client, source)
    assert response.status_code == 200, response.text
    assert stack.cells[1].files[0].language == "custom-language"
    assert stack.cells[2].files[0].language == "binary"


@pytest.mark.parametrize("change", ["tip", "visible"])
def test_restore_eligibility_rechecked_after_preparation__D07(client, monkeypatch, change):
    import routes.options as module
    from models import Stack
    ch, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    original = stack.cells[0]
    source = merge(client, stack)
    real = module._mapped_files
    changed = False
    def prepare(*args, **kwargs):
        nonlocal changed
        result = real(*args, **kwargs)
        if not changed:
            changed = True
            if change == "tip":
                append_version(ids[0])
            else:
                ch.nav_stacks.append(Stack(cells=[original]))
        return result
    monkeypatch.setattr(module, "_mapped_files", prepare)
    response = split(client, source)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert (outputs == ids) if change == "tip" else (outputs[0] not in ids and outputs[1] == ids[1])


# ---------------------------------------------------------------------------
# O03: the lineage record and the removed record replace the version edges
# ---------------------------------------------------------------------------

def _tips(cell_ids):
    from versions import find_tip
    conn = get_db_connection(CHANNEL)
    try:
        return [find_tip(conn, cid) for cid in cell_ids]
    finally:
        conn.close()


def _edge_keys():
    conn = get_db_connection(CHANNEL)
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT parent_key FROM cell_edges")}
    finally:
        conn.close()


def _removed():
    import removed_cells
    conn = get_db_connection(CHANNEL)
    try:
        return {r.cell_id: r for r in removed_cells.list_removed(conn)}
    finally:
        conn.close()


def test_merge_records_one_move_per_input_and_shelves_the_inputs_O03(client):
    _, stack, ids = make_stack([("First", {"a.txt": "A"}), ("Second", {"b.bin": b"\xff\x00"}),
                                ("Third", {"c.txt": "C"})])
    seed_version(CHANNEL, ids[0])
    append_version(ids[0])
    merged = merge(client, stack)
    run = get_db_connection(CHANNEL)
    try:
        op_id = run.execute("SELECT run_id FROM cells WHERE id=?", (merged,)).fetchone()[0]
        assert run.execute("SELECT generator_name FROM runs WHERE id=?", (op_id,)).fetchone()[0] == "merge"
        keys = {r[0] for r in run.execute("SELECT DISTINCT parent_key FROM cell_edges")}
    finally:
        run.close()
    assert moves("merge") == [(op_id, cid, tip, merged, merged, i)
                              for i, (cid, tip) in enumerate(zip(ids, _tips(ids)))]
    assert keys <= {"previous_version", "input_0"}
    removed = _removed()
    assert set(removed) == set(ids)
    assert {r.reason for r in removed.values()} == {"merged"}
    assert {r.stack_cell_count for r in removed.values()} == {3}
    assert removed[ids[0]].name == "First" and removed[ids[0]].stack_name == "Choices"
    from versions import version_file_entries
    with get_db_connection(CHANNEL) as conn:
        from versions import find_tip
        assert next(f for f in version_file_entries(conn, find_tip(conn, ids[1]))
                    if f['path'] == 'b.bin')['encoding'] == 'binary'
    assert removed[ids[0]].removed_at.endswith("+00:00")


def test_split_restores_identities_from_merge_moves_and_unshelves_them_O03(client):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    response = split(client, source)
    assert response.status_code == 200, response.text
    assert response.json()["new_cell_ids"] == ids
    assert [m[3] for m in moves("split")] == ids
    assert len({m[0] for m in moves("split")}) == 1
    assert _edge_keys() <= {"previous_version", "input_0"}
    # The restored inputs are back on the canvas; the merged cell keeps its
    # remainder (rest: keep), so nothing is shelved.
    assert _removed() == {}


def test_split_without_merge_moves_gives_new_identities_O03(client):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    source = merge(client, stack)
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("DELETE FROM cell_moves WHERE op='merge'")
        conn.commit()
    finally:
        conn.close()
    response = split(client, source)
    assert response.status_code == 200, response.text
    assert set(response.json()["new_cell_ids"]).isdisjoint(ids)
    assert set(_removed()) == set(ids)


def test_split_without_remainder_shelves_its_source_O03(client):
    import yaml
    _, stack, ids = make_stack([("Picker", {
        "pick.options_stack": yaml.safe_dump({"options": [
            {"name": "One", "root": "one", "files": ["**"]},
            {"name": "Two", "root": "two", "files": ["**"]}], "rest": "drop"}),
        "one/a.txt": "A", "two/b.txt": "B"}), ("Other", {"c.txt": "C"})])
    response = split(client, ids[0])
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert [str(c.id) for c in stack.cells] == [*outputs, ids[1]]
    [source_tip] = _tips([ids[0]])
    assert [(m[1], m[2], m[3], m[5]) for m in moves("split")] == [
        (ids[0], source_tip, out, i) for i, out in enumerate(outputs)]
    assert _edge_keys() <= {"previous_version", "input_0"}
    record = _removed()[ids[0]]
    assert (record.reason, record.stack_cell_count, record.name) == ("split", 2, "Picker")


@pytest.fixture
def lineage_events(monkeypatch):
    """The ``cell_ids`` of every ``lineage_changed`` the channel's SSE sends."""
    import events
    sent = []
    original = events.broadcast

    def record(channel, event):
        if event.get("type") == "lineage_changed":
            sent.append(event["cell_ids"])
        return original(channel, event)

    monkeypatch.setattr(events, "broadcast", record)
    return sent


def test_merge_and_split_announce_their_inputs_and_results_O04(client, lineage_events):
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    merged = merge(client, stack)
    assert lineage_events == [[*ids, merged]]
    response = split(client, merged)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert outputs == ids
    assert lineage_events[1:] == [[merged, *outputs]]


def test_a_split_that_restores_a_merge_input_keeps_the_inputs_make_O04(client):
    """Through the real routes: the restored input's split move lands on a
    child version of its own chain, so its journal and the channel journal
    still hold its make."""
    _, stack, ids = make_stack([("First", {"a": "A"}), ("Second", {"b": "B"})])
    seed_version(CHANNEL, ids[0])
    merged = merge(client, stack)
    assert split(client, merged).status_code == 200
    [restore] = [m for m in moves("split") if m[3] == ids[0]]
    assert restore[4] not in (None, ids[0])
    own = client.get(f"/api/channels/{CHANNEL}/cells/{ids[0]}/journal").json()
    assert [e["kind"] for e in own].count("make") == 1
    everything = client.get(f"/api/channels/{CHANNEL}/journal").json()
    assert ("make", [ids[0]]) in [(e["kind"], [c["cell_id"] for c in e["cells"]]) for e in everything]
