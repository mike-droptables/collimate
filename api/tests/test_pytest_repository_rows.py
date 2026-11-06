"""A version's repositories as rows: storage, the snapshot's summary, and
every ``cells`` writer carrying, copying, re-keying or dropping them (design
§3.2, §5)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

import pytest

from collimate_server.database import get_db_connection
from collimate_server.repository_state import RepositoryState
from conftest import channel_home
from test_pytest_options_identity import CHANNEL, client as client, make_stack, merge, seed_version, split


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def state(head="ref: refs/heads/main", *, config=None, refs=None, extra=None) -> RepositoryState:
    """A normalized state: its manifest over the parts it names."""
    parts, named = {}, {}

    def add(name, body):
        digest = hashlib.sha256(body).hexdigest()
        parts[digest] = body
        named[name] = digest
    add("refs", refs if refs is not None else f"{'1' * 40} refs/heads/main\n".encode())
    if config is not None:
        add("config", _canonical(sorted(config)))
    for name, body in (extra or {}).items():
        add(name, body)
    manifest = _canonical({"head": head, "parts": named})
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    parts[manifest_hash] = manifest
    return RepositoryState(manifest_hash, parts)


@pytest.fixture
def db(tmp_path):
    import collimate_server.migrations as migrations
    conn = sqlite3.connect(str(tmp_path / f"{uuid.uuid4()}.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.apply(conn, migrations.CHANNEL_MIGRATIONS)
    for version in ("v1", "v2", "v3"):
        conn.execute("INSERT INTO cells(id) VALUES (?)", (version,))
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_rows_store_every_part_once_and_load_back_whole(db):
    from collimate_server.repository_rows import load_state, rows_of, write_rows
    main = state(extra={"reflogs/HEAD": b"log\n"})
    other = state("ref: refs/heads/dev", refs=main.part("refs"))
    write_rows(db, "v1", {"": main, "lib/vendored": other})
    write_rows(db, "v2", {"": main})
    assert rows_of(db, "v1") == {"": main.manifest_hash, "lib/vendored": other.manifest_hash}
    assert db.execute("SELECT COUNT(*) FROM repository_parts").fetchone()[0] == len(main.parts | other.parts)
    loaded = load_state(db, main.manifest_hash)
    assert loaded == RepositoryState(main.manifest_hash, dict(main.parts))
    assert loaded.part("reflogs/HEAD") == b"log\n"
    write_rows(db, "v1", {"lib/vendored": None, "": other})
    assert rows_of(db, "v1") == {"": other.manifest_hash}
    assert rows_of(db, "v2") == {"": main.manifest_hash}
    with pytest.raises(LookupError):
        load_state(db, "0" * 64)


@pytest.mark.parametrize("repo_path", [".git", "a/.GIT", "_runs/x", "node_modules", "a/../b", "/a", ".collrun"])
def test_rows_refuse_a_path_that_cannot_hold_a_repository(db, repo_path):
    from collimate_server.repository_rows import copy_rows, write_rows
    with pytest.raises(ValueError, match="not a repository path"):
        write_rows(db, "v1", {repo_path: state()})
    with pytest.raises(ValueError, match="not a repository path"):
        copy_rows(db, "v1", {repo_path: "0" * 64})


def test_rows_refuse_a_state_whose_parts_do_not_hold(db):
    from collimate_server.repository_rows import write_rows
    good = state()
    forged = RepositoryState(good.manifest_hash, {**good.parts, "f" * 64: b"not this"})
    with pytest.raises(ValueError, match="does not match"):
        write_rows(db, "v1", {"": forged})
    partial = RepositoryState(good.manifest_hash, {good.manifest_hash: good.parts[good.manifest_hash]})
    with pytest.raises(ValueError, match="lacks parts"):
        write_rows(db, "v1", {"": partial})
    assert db.execute("SELECT COUNT(*) FROM cell_repositories").fetchone()[0] == 0


def test_carry_forward_takes_the_parents_rows_then_the_patch(db):
    from collimate_server.repository_rows import carry_forward, rows_of, write_rows
    a, b, c = state(), state("ref: refs/heads/b"), state("2" * 40)
    write_rows(db, "v1", {"": a, "x": b})
    carry_forward(db, "v1", "v2", {})
    assert rows_of(db, "v2") == rows_of(db, "v1")
    carry_forward(db, "v1", "v3", {"x": None, "y/z": c, "": b})
    assert rows_of(db, "v3") == {"": b.manifest_hash, "y/z": c.manifest_hash}
    assert rows_of(db, "v1") == {"": a.manifest_hash, "x": b.manifest_hash}


@pytest.mark.parametrize("head,config,expected", [
    ("ref: refs/heads/main", [["branch.main.remote", "origin"], ["branch.main.merge", "refs/heads/main"]],
     {"branch": "main", "upstream": "origin/main", "detached": False}),
    ("ref: refs/heads/feat/x.y", [["Branch.feat/x.y.Remote", "fork"], ["BRANCH.feat/x.y.MERGE", "refs/heads/up"]],
     {"branch": "feat/x.y", "upstream": "fork/up", "detached": False}),
    ("ref: refs/heads/topic", [["branch.topic.remote", "."], ["branch.topic.merge", "refs/heads/main"]],
     {"branch": "topic", "upstream": "main", "detached": False}),
    ("ref: refs/heads/main", [["branch.Main.remote", "origin"], ["branch.Main.merge", "refs/heads/main"]],
     {"branch": "main", "upstream": None, "detached": False}),
    ("ref: refs/heads/main", [["branch.main.remote", "origin"]],
     {"branch": "main", "upstream": None, "detached": False}),
    # git's upstream is the first branch.<b>.merge (2.31 and 2.53 alike).
    ("ref: refs/heads/main", [["branch.main.merge", "refs/heads/a"], ["branch.main.remote", "o"],
                              ["branch.main.merge", "refs/heads/b"]],
     {"branch": "main", "upstream": "o/a", "detached": False}),
    ("ref: refs/heads/main", None, {"branch": "main", "upstream": None, "detached": False}),
    ("3" * 40, [["branch.main.remote", "origin"], ["branch.main.merge", "refs/heads/main"]],
     {"branch": None, "upstream": None, "detached": True}),
])
def test_summary_reads_the_head_and_the_branch_upstream(head, config, expected):
    from collimate_server.repository_rows import summary
    assert summary(state(head, config=config)) == expected


def test_snapshot_entries_are_shallowest_first_and_capped(db, monkeypatch):
    import collimate_server.repository_rows as repository_rows
    up = [["branch.main.merge", "refs/heads/main"], ["branch.main.remote", "origin"]]
    repository_rows.write_rows(db, "v1", {"b/c": state(), "a": state(config=up), "": state("4" * 40), "a/b": state()})
    entries, overflow = repository_rows.snapshot_entries(db, "v1")
    assert [e["path"] for e in entries] == ["", "a", "a/b", "b/c"] and overflow == 0
    assert entries[1] == {"path": "a", "branch": "main", "upstream": "origin/main", "ahead": None,
                          "behind": None, "detached": False}
    assert list(entries[0]) == ["path", "branch", "upstream", "ahead", "behind", "detached"]
    assert entries[0]["detached"] is True
    monkeypatch.setattr(repository_rows, "MAX_REPOSITORIES_PER_CELL", 2)
    entries, overflow = repository_rows.snapshot_entries(db, "v1")
    assert [e["path"] for e in entries] == ["", "a"] and overflow == 2
    assert repository_rows.snapshot_entries(db, "v2") == ([], 0)


def test_imported_cells_publish_their_adopted_repositories(fresh_cwd):
    from collimate_server.import_plan import initialize_imported_cells
    from collimate_server.models import Cell, VirtualFile
    from collimate_server.repository_rows import load_state, rows_of
    root, nested = state(), state("ref: refs/heads/dev")
    cells = [Cell(name="A", files=[VirtualFile(name="x.txt", content="x")]),
             Cell(name="B", files=[VirtualFile(name="y.txt", content="y")])]
    conn = get_db_connection(str(uuid.uuid4()))
    try:
        initialize_imported_cells(conn, cells, {str(cells[0].id): {
            "": (root.manifest_hash, dict(root.parts)), "lib": (nested.manifest_hash, dict(nested.parts))}})
        assert rows_of(conn, str(cells[0].id)) == {"": root.manifest_hash, "lib": nested.manifest_hash}
        assert rows_of(conn, str(cells[1].id)) == {}
        assert load_state(conn, nested.manifest_hash).parts == nested.parts
        initialize_imported_cells(conn, [Cell(name="C", files=[])])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Every cells writer
# ---------------------------------------------------------------------------

def seed_rows(cell_id, rows):
    """Give ``cell_id``'s tip (a version made now if it has none) ``rows``."""
    from collimate_server.repository_rows import write_rows
    version = seed_version(CHANNEL, cell_id)
    conn = get_db_connection(CHANNEL)
    try:
        write_rows(conn, version, rows)
        conn.commit()
    finally:
        conn.close()
    return version


def system_run():
    run_id = str(uuid.uuid4())
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT INTO runs(id,prompt,status) VALUES (?,'fixture','completed')", (run_id,))
        conn.commit()
    finally:
        conn.close()
    return run_id


def tip_rows(cell_id):
    from collimate_server.repository_rows import rows_of
    from collimate_server.versions import find_tip
    conn = get_db_connection(CHANNEL)
    try:
        return rows_of(conn, find_tip(conn, cell_id))
    finally:
        conn.close()


def test_a_version_carries_its_parents_rows_and_applies_a_patch(client):
    from collimate_server.cell_commits import commit_cell_change
    from collimate_server.repository_rows import rows_of
    _, _, (cid,) = make_stack([("Repo", {"a.txt": "A"})])
    repo, nested = state(), state("ref: refs/heads/dev")
    first = seed_rows(cid, {"": repo})
    second = commit_cell_change(CHANNEL, cid, system_run(), writes={"b.txt": "B"})
    assert second and tip_rows(cid) == {"": repo.manifest_hash}
    from collimate_server.cell_commits import cell_commit_lock
    from collimate_server.persistence import repository_run, write_repository_version
    with cell_commit_lock(cid):
        conn = get_db_connection(CHANNEL)
        try:
            run_id = repository_run(conn, CHANNEL, cid, "Git state")
            version = write_repository_version(conn, cid, run_id, {"": None, "lib": nested}, label="Git state")
            conn.commit()
            assert rows_of(conn, first) == {"": repo.manifest_hash}
            assert rows_of(conn, version) == {"lib": nested.manifest_hash}
        finally:
            conn.close()
    assert tip_rows(cid) == {"lib": nested.manifest_hash}


def test_copies_get_genesis_rows_and_a_spawned_build_none(client):
    from collimate_server.cell_files import record_duplicates
    from collimate_server.models import Cell
    _, stack, (cid,) = make_stack([("Repo", {"a.txt": "A"})])
    repo = state()
    seed_rows(cid, {"": repo, "sub": repo})
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/duplicate", json={})
    assert response.status_code == 200, response.text
    copy = response.json()["new_cell_id"]
    assert tip_rows(copy) == {"": repo.manifest_hash, "sub": repo.manifest_hash}
    conn = get_db_connection(CHANNEL)
    try:
        assert conn.execute("SELECT source_type FROM cells WHERE id=?", (copy,)).fetchone()[0] == "genesis"
    finally:
        conn.close()
    build = Cell(name="Build", files=list(stack.cells[0].files))
    stack.cells.append(build)
    record_duplicates(CHANNEL, [(cid, str(build.id))], {}, repositories=False)
    assert tip_rows(str(build.id)) == {}
    conn = get_db_connection(CHANNEL)
    try:
        assert conn.execute("SELECT 1 FROM cells WHERE id=?", (str(build.id),)).fetchone() is None
    finally:
        conn.close()


def test_duplicate_stack_gives_each_copy_its_sources_rows(client):
    _, stack, ids = make_stack([("One", {"a.txt": "A"}), ("Two", {"b.txt": "B"})])
    one, two = state(), state("ref: refs/heads/two")
    seed_rows(ids[0], {"": one})
    seed_rows(ids[1], {"x": two})
    response = client.post(f"/api/stacks/{CHANNEL}/{stack.id}/duplicate_stack")
    assert response.status_code == 200, response.text
    from collimate_server.state import state as app_state
    stacks = app_state.channels[0].main_stacks
    copies = stacks[stacks.index(stack) + 1].cells
    assert [tip_rows(str(c.id)) for c in copies] == [{"": one.manifest_hash}, {"x": two.manifest_hash}]


def test_run_in_new_cell_clones_the_rows(client):
    from collimate_server.cell_commits import clone_cell_for_run
    _, _, (cid,) = make_stack([("Repo", {"a.txt": "A"})])
    repo = state()
    seed_rows(cid, {"": repo})
    clone, _stack = clone_cell_for_run(CHANNEL, cid)
    assert tip_rows(clone) == {"": repo.manifest_hash}
    _, _, (plain,) = make_stack([("Plain", {"b.txt": "B"})])
    assert tip_rows(clone_cell_for_run(CHANNEL, plain)[0]) == {}


def test_fork_carries_the_versions_rows(client):
    from collimate_server.cell_commits import commit_cell_change
    _, _, (cid,) = make_stack([("Repo", {"a.txt": "A"})])
    old, new = state(), state("ref: refs/heads/later")
    first = seed_rows(cid, {"": old})
    later = commit_cell_change(CHANNEL, cid, system_run(), writes={"b.txt": "B"})
    seed_rows_on(later, {"": new})
    for version, expected in ((first, old), (later, new)):
        response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/fork", json={"target_cell_id": version})
        assert response.status_code == 200, response.text
        assert tip_rows(response.json()["new_cell_id"]) == {"": expected.manifest_hash}


def seed_rows_on(version, rows):
    from collimate_server.repository_rows import write_rows
    conn = get_db_connection(CHANNEL)
    try:
        write_rows(conn, version, rows)
        conn.commit()
    finally:
        conn.close()


def test_merge_then_split_puts_each_repository_back_at_the_root(client):
    _, stack, ids = make_stack([("First", {"a.txt": "A", "lib/x.py": "x"}), ("Second", {"b.txt": "B"})])
    first, nested, second = state(), state("ref: refs/heads/lib"), state("5" * 40)
    seed_rows(ids[0], {"": first, "lib": nested})
    seed_rows(ids[1], {"": second})
    merged = merge(client, stack)
    assert tip_rows(merged) == {"First": first.manifest_hash, "First/lib": nested.manifest_hash,
                                "Second": second.manifest_hash}
    response = split(client, merged)
    assert response.status_code == 200, response.text
    assert response.json()["new_cell_ids"] == ids
    assert tip_rows(ids[0]) == {"": first.manifest_hash, "lib": nested.manifest_hash}
    assert tip_rows(ids[1]) == {"": second.manifest_hash}


def test_split_moves_a_row_only_with_all_its_files(client):
    import yaml
    declaration = yaml.safe_dump({"options": [{"name": "Docs", "root": "docs", "files": ["**"]},
                                              {"name": "Site", "files": ["site/**"]}], "rest": "keep"})
    _, _, (cid,) = make_stack([("Mixed", {".options_stack": declaration, "docs/a.md": "a", "docs/b.md": "b",
                                          "site/index.html": "i", "split/x": "x", "split/y": "y",
                                          "other.txt": "o"})])
    docs, site, kept = state(), state("ref: refs/heads/site"), state("6" * 40)
    seed_rows(cid, {"docs": docs, "site": site, "split": kept})
    response = split(client, cid)
    assert response.status_code == 200, response.text
    outputs = response.json()["new_cell_ids"]
    assert tip_rows(outputs[0]) == {"": docs.manifest_hash}
    assert tip_rows(outputs[1]) == {"site": site.manifest_hash}
    assert tip_rows(cid) == {"split": kept.manifest_hash}


def test_merge_refuses_a_repository_its_folder_cannot_hold(client, monkeypatch):
    # sanitize_folder never yields such a folder; the re-key check holds
    # the rule on its own.
    import collimate_server.routes.options  # noqa: F401 (loads routes.options)
    from collimate_server import routes
    monkeypatch.setattr(routes.options, "sanitize_folder", lambda name, fallback: name)
    _, stack, ids = make_stack([("node_modules", {"a.txt": "A"}), ("Second", {"b.txt": "B"})])
    seed_rows(ids[0], {"": state()})
    response = client.post(f"/api/channels/{CHANNEL}/merge", json={"stack_id": str(stack.id)})
    assert response.status_code == 409 and "cannot live at 'node_modules'" in response.text
    assert [str(c.id) for c in stack.cells] == ids


@pytest.mark.parametrize("name", ["git~1", "GIT~1", "\u200c.git"])
def test_merge_gives_a_cell_named_as_git_its_fallback_folder(client, name):
    _, stack, ids = make_stack([(name, {"a.txt": "A"}), ("Second", {"b.txt": "B"})])
    merged = merge(client, stack)
    assert {p for p in _current(merged) if p != ".options_stack"} == {f"cell-{ids[0][:8]}/a.txt", "Second/b.txt"}


def test_split_logs_a_repository_whose_files_land_where_it_cannot_live(caplog):
    from collimate_server.routes.options import _split_rows
    options = [{"name": "Vendored", "files": {"node_modules/a.txt": "lib/a.txt"}}]
    with caplog.at_level("INFO"):
        placed, moved = _split_rows("abcdef1234", {"lib/a.txt": "h"}, options, {"lib": "s"})
    assert placed == [{}] and moved == set()
    assert "split abcdef12: the repository at 'lib' stays: 'node_modules' of 'Vendored'" in caplog.text


def _current(cell_id):
    from collimate_server.persistence import snapshot_files
    conn = get_db_connection(CHANNEL)
    try:
        return {f["path"]: f["file_revision"] for f in snapshot_files(conn, cell_id)}
    finally:
        conn.close()


def _mutate(client, cell_id, *, deletes=(), puts=(), **folders):
    current, writer = _current(cell_id), str(uuid.uuid4())
    mutations = [{"path": p, "writer_id": writer, "edit_seq": i + 1, "base_revision": current[p], "kind": "delete"}
                 for i, p in enumerate(deletes)]
    mutations += [{"path": p, "writer_id": writer, "edit_seq": len(mutations) + i + 1,
                   "base_revision": current.get(p, 0), "kind": "put", "content_text": body}
                  for i, (p, body) in enumerate(puts)]
    return client.post(f"/api/channels/{CHANNEL}/cells/{cell_id}/files/mutate",
                       json={"operation_id": str(uuid.uuid4()), "mutations": mutations, **folders})


def test_folder_delete_drops_its_rows_and_rename_rekeys_them_in_one_version(client):
    _, _, (cid,) = make_stack([("Repo", {"top.txt": "t", "app/a.txt": "a", "app/lib/b.txt": "b",
                                         "keep/c.txt": "c"})])
    app, lib, keep = state(), state("ref: refs/heads/lib"), state("7" * 40)
    tip = seed_rows(cid, {"app": app, "app/lib": lib, "keep": keep})
    response = _mutate(client, cid, deletes=["app/a.txt", "app/lib/b.txt"],
                       puts=[("src/a.txt", "a"), ("src/lib/b.txt", "b")],
                       folder_moves=[{"source": "app", "target": "src"}])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    assert tip_rows(cid) == {"src": app.manifest_hash, "src/lib": lib.manifest_hash, "keep": keep.manifest_hash}
    conn = get_db_connection(CHANNEL)
    try:
        from collimate_server.versions import find_tip, tip_file_map
        renamed = find_tip(conn, cid)
        assert conn.execute("SELECT parent_cell_id FROM cell_edges WHERE child_cell_id=?", (renamed,)).fetchone()[0] == tip
        assert set(tip_file_map(conn, renamed)) == {"top.txt", "src/a.txt", "src/lib/b.txt", "keep/c.txt"}
        assert conn.execute("SELECT version_name FROM cells WHERE id=?", (renamed,)).fetchone()[0] == "Folder renamed"
    finally:
        conn.close()
    response = _mutate(client, cid, deletes=["src/lib/b.txt"], folder_deletes=["src/lib"])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    assert tip_rows(cid) == {"src": app.manifest_hash, "keep": keep.manifest_hash}
    before = tip_rows(cid)
    response = _mutate(client, cid, deletes=["keep/c.txt"])
    assert response.status_code == 200 and tip_rows(cid) == before


def test_a_folder_operation_without_rows_writes_no_version(client):
    _, _, (cid,) = make_stack([("Plain", {"a/x.txt": "x"})])
    tip = seed_version(CHANNEL, cid)
    assert _mutate(client, cid, deletes=["a/x.txt"], folder_deletes=["a"]).status_code == 200
    conn = get_db_connection(CHANNEL)
    try:
        from collimate_server.versions import find_tip
        assert find_tip(conn, cid) == tip
    finally:
        conn.close()


def test_a_file_less_repository_folder_is_renamed_or_deleted_by_folder_operations_alone(client):
    _, _, (cid,) = make_stack([("Repo", {"a.txt": "a"})])
    empty, gone = state(), state("c" * 40)
    seed_rows(cid, {"empty": empty, "gone": gone})
    response = _mutate(client, cid, folder_moves=[{"source": "empty", "target": "moved"}])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    assert tip_rows(cid) == {"moved": empty.manifest_hash, "gone": gone.manifest_hash}
    assert _mutate(client, cid, folder_deletes=["gone"]).status_code == 200
    assert tip_rows(cid) == {"moved": empty.manifest_hash}
    assert set(_current(cid)) == {"a.txt"}
    assert _mutate(client, cid).status_code == 422


def test_a_folder_operation_carries_every_file_its_repository_folder_holds(client):
    _, _, (cid,) = make_stack([("Repo", {"app/a.txt": "a", "app/b.txt": "b", "x.txt": "x"})])
    seed_rows(cid, {"app": state()})
    before, rows = _current(cid), tip_rows(cid)
    for response in (
        _mutate(client, cid, deletes=["app/a.txt"], folder_deletes=["app"]),
        _mutate(client, cid, deletes=["app/a.txt", "app/b.txt"], puts=[("src/a.txt", "a")],
                folder_moves=[{"source": "app", "target": "src"}]),
        _mutate(client, cid, deletes=["x.txt"], folder_moves=[{"source": "app", "target": "src"}]),
    ):
        assert response.status_code == 409 and "folder's files changed" in response.text
        assert _current(cid) == before and tip_rows(cid) == rows
    # A folder without rows is the files' business alone.
    assert _mutate(client, cid, deletes=["x.txt"], folder_deletes=["elsewhere"]).status_code == 200


@pytest.mark.parametrize("target,needle", [("other", "already has a repository at 'other'"),
                                           ("node_modules", "cannot live at 'node_modules'")])
def test_a_rename_onto_a_path_that_cannot_take_the_row_refuses_whole(client, target, needle):
    _, _, (cid,) = make_stack([("Repo", {"app/a.txt": "a", "other/o.txt": "o"})])
    seed_rows(cid, {"app": state(), "other": state("8" * 40)})
    before, rows = _current(cid), tip_rows(cid)
    response = _mutate(client, cid, deletes=["app/a.txt"], puts=[(f"{target}/a.txt", "a")],
                       folder_moves=[{"source": "app", "target": target}])
    assert response.status_code == 409 and needle in response.text
    assert _current(cid) == before and tip_rows(cid) == rows


@pytest.mark.parametrize("folders", [
    {"folder_deletes": [".git"]}, {"folder_deletes": ["a/../b"]}, {"folder_deletes": ["a", "a/b"]},
    {"folder_moves": [{"source": "a", "target": "a/b"}]}, {"folder_moves": [{"source": "a", "target": ".GIT"}]},
])
def test_folder_operations_are_validated(client, folders):
    _, _, (cid,) = make_stack([("Repo", {"a/x.txt": "x"})])
    assert _mutate(client, cid, deletes=["a/x.txt"], **folders).status_code == 422


def test_a_folder_moved_up_into_a_folder_of_its_own_name_is_valid():
    from collimate_server.persistence import FileOperation
    FileOperation(operation_id="o", mutations=[], folder_moves=[{"source": "a/a", "target": "a"}])
    FileOperation(operation_id="o", mutations=[], folder_moves=[{"source": "x/a/a", "target": "x/a"}])


def test_a_leased_cell_refuses_a_folder_operation_on_its_rows(client):
    import collimate_server.repository_leases as repository_leases
    _, _, (cid,) = make_stack([("Repo", {"app/a.txt": "a", "b.txt": "b"})])
    seed_rows(cid, {"app": state()})
    repository_leases.claim(CHANNEL, cid, "abcdef12-run", "Terminal")
    response = _mutate(client, cid, deletes=["app/a.txt"], folder_deletes=["app"])
    assert response.status_code == 409 and "git is in use by run abcdef12" in response.text
    assert _mutate(client, cid, deletes=["b.txt"]).status_code == 200


def _move(client, source, target, files):
    current = {cid: _current(cid) for cid in (source, target)}
    return client.post(f"/api/channels/{CHANNEL}/files/move", json={
        "operation_id": str(uuid.uuid4()), "source_cell_id": source, "target_cell_id": target,
        "writer_id": "w", "edit_seq": 1,
        "files": [{"source_path": s, "target_path": t, "source_revision": current[source][s],
                   "target_revision": current[target].get(t, 0)} for s, t in files]})


def test_a_whole_repository_moves_between_cells_with_its_rows(client):
    _, _, (src, dst) = make_stack([("Source", {"app/a.txt": "a", "app/.collkeep": "", "app/lib/b.txt": "b",
                                               "rest.txt": "r"}),
                                   ("Target", {"t.txt": "t"})])
    app, lib, root = state(), state("ref: refs/heads/lib"), state("9" * 40)
    seed_rows(src, {"app": app, "app/lib": lib, "": root})
    response = _move(client, src, dst, [("app/a.txt", "in/app/a.txt"), ("app/.collkeep", "in/app/.collkeep"),
                                       ("app/lib/b.txt", "in/app/lib/b.txt")])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    assert tip_rows(src) == {"": root.manifest_hash}
    assert tip_rows(dst) == {"in/app": app.manifest_hash, "in/app/lib": lib.manifest_hash}


def test_partial_moves_carry_no_rows_and_a_target_row_refuses(client):
    _, _, (src, dst) = make_stack([("Source", {"app/a.txt": "a", "app/b.txt": "b", "lib/c.txt": "c"}),
                                   ("Target", {"lib/t.txt": "t"})])
    app, lib, theirs = state(), state("ref: refs/heads/lib"), state("a" * 40)
    seed_rows(src, {"app": app, "lib": lib})
    seed_rows(dst, {"lib": theirs})
    response = _move(client, src, dst, [("app/a.txt", "app/a.txt")])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    assert tip_rows(src) == {"app": app.manifest_hash, "lib": lib.manifest_hash}
    assert tip_rows(dst) == {"lib": theirs.manifest_hash}
    before = (_current(src), _current(dst))
    response = _move(client, src, dst, [("lib/c.txt", "lib/c.txt")])
    assert response.status_code == 409 and "already has a repository at 'lib'" in response.text
    assert (_current(src), _current(dst)) == before


@pytest.mark.parametrize("leased", ["source", "target"])
def test_a_leased_cell_refuses_every_move_between_cells(client, leased):
    import collimate_server.repository_leases as repository_leases
    _, _, (src, dst) = make_stack([("Source", {"a.txt": "a"}), ("Target", {"t.txt": "t"})])
    held = src if leased == "source" else dst
    repository_leases.claim(CHANNEL, held, "abcdef12-run", "Terminal")
    before = (_current(src), _current(dst))
    response = _move(client, src, dst, [("a.txt", "a.txt")])
    assert response.status_code == 409 and "git is in use by run abcdef12" in response.text
    assert (_current(src), _current(dst)) == before


def test_a_move_pins_a_never_versioned_targets_files_before_they_change(client):
    from collimate_server.versions import find_tip, tip_file_map
    _, _, (src, dst) = make_stack([("Source", {"app/a.txt": "a"}), ("Target", {"t.txt": "t"})])
    app = state()
    seed_rows(src, {"app": app})
    response = _move(client, src, dst, [("app/a.txt", "app/a.txt")])
    assert response.status_code == 200 and response.json()["outcome"] == "accepted", response.text
    conn = get_db_connection(CHANNEL)
    try:
        assert set(tip_file_map(conn, dst)) == {"t.txt"}
        assert set(tip_file_map(conn, find_tip(conn, dst))) == {"t.txt", "app/a.txt"}
    finally:
        conn.close()
    assert tip_rows(dst) == {"app": app.manifest_hash} and tip_rows(src) == {}


def test_the_state_snapshot_lists_each_cells_repositories(client):
    _, _, (cid, plain) = make_stack([("Repo", {"a.txt": "A"}), ("Plain", {"b.txt": "B"})])
    up = [["branch.main.merge", "refs/heads/main"], ["branch.main.remote", "origin"]]
    seed_rows(cid, {"": state(config=up), "vendor/x": state("b" * 40)})
    cells = {c["id"]: c for s in client.get("/api/state").json()["channels"][0]["main_stacks"] for c in s["cells"]}
    assert cells[cid]["repositories"] == [
        {"path": "", "branch": "main", "upstream": "origin/main", "ahead": None, "behind": None, "detached": False},
        {"path": "vendor/x", "branch": None, "upstream": None, "ahead": None, "behind": None, "detached": True}]
    assert cells[plain]["repositories"] == []
    assert "repositories_overflow" not in cells[cid]


def test_the_sent_channel_counts_ahead_behind_and_shows_its_rescues(client, monkeypatch, tmp_path):
    import collimate_server.git_store as git_store
    from collimate_server.state import file_metadata_state
    _, _, (cid,) = make_stack([("Repo", {"a.txt": "A"})])
    up = [["branch.main.merge", "refs/heads/main"], ["branch.main.remote", "origin"]]
    seed_rows(cid, {"": state(config=up)})
    asked = []

    def counted(channel, repo_path, state_hash, load):
        asked.append((channel, repo_path, load().manifest_hash == state_hash))
        return {"ahead": 2, "behind": 0}

    monkeypatch.setattr(git_store, "ahead_behind", counted)
    lost = tmp_path / "lost" / ".git"
    lost.mkdir(parents=True)
    git_store.rescue(CHANNEL, cid, "lost", str(lost), run_id="r" * 8, base_state_hash=None, reason="objects refused")
    cells = {c["id"]: c for s in client.get("/api/state").json()["channels"][0]["main_stacks"] for c in s["cells"]}
    assert cells[cid]["repositories"] == [
        {"path": "", "branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 0, "detached": False},
        {"path": "lost", "branch": None, "upstream": None, "ahead": None, "behind": None, "detached": False,
         "error": {"reason": "objects refused", "rescued": True}}]
    assert asked == [(CHANNEL, "", True)]
    # A channel not sent in full reads the cache alone.
    from collimate_server.genesis import snapshot_app_state
    from collimate_server.state import state as app_state
    asked.clear()
    file_metadata_state(snapshot_app_state(app_state))
    assert asked == []


# ---------------------------------------------------------------------------
# A pre-31 .git row is never read, copied or exported
# ---------------------------------------------------------------------------

def test_reads_skip_a_stored_git_row(client):
    from collimate_server.cas import channel_store
    from collimate_server.persistence import snapshot_files
    from collimate_server.versions import find_tip, tip_file_map, version_file_entries, version_file_rows
    _, _, (cid,) = make_stack([("Old", {"a.txt": "A"})])
    tip = seed_version(CHANNEL, cid)
    digest = channel_store(CHANNEL).store_bytes(b"ref: refs/heads/main\n")
    conn = get_db_connection(CHANNEL)
    try:
        conn.execute("INSERT OR IGNORE INTO blobs(hash,size) VALUES (?,21)", (digest,))
        for path in (".git/HEAD", "x/.GIT/config"):
            conn.execute("INSERT INTO cell_files(cell_id,filepath,blob_hash,encoding,language) VALUES (?,?,?,?,?)",
                         (tip, path, digest, "utf-8", "text"))
            conn.execute("INSERT INTO cell_file_current(root_id,filepath,blob_hash,encoding,language) "
                         "SELECT root_id,?,?,'utf-8','text' FROM cell_file_versions WHERE version_id=?",
                         (path, digest, tip))
            conn.execute("INSERT INTO file_revisions(cell_id,path,revision,base_revision,body_hash,deleted,language,"
                         "encoding,writer_id,edit_seq,operation_id) VALUES (?,?,1,0,?,0,'text','utf-8','w',1,'o')",
                         (cid, path, digest))
            conn.execute("INSERT INTO working_files VALUES (?,?,1)", (cid, path))
        conn.commit()
        assert [f["path"] for f in snapshot_files(conn, cid)] == ["a.txt"]
        assert [f["path"] for f in snapshot_files(conn, cid, include_deleted=True)] == ["a.txt"]
        assert set(tip_file_map(conn, find_tip(conn, cid))) == {"a.txt"}
        assert [f["path"] for f in version_file_entries(conn, tip)] == ["a.txt"]
        assert [r["filepath"] for r in version_file_rows(conn, tip)] == ["a.txt"]
    finally:
        conn.close()
    response = client.post(f"/api/channels/{CHANNEL}/cells/{cid}/duplicate", json={})
    assert response.status_code == 200, response.text
    copy = response.json()["new_cell_id"]
    assert set(_current(copy)) == {"a.txt"}
    events = client.get(f"/api/channels/{CHANNEL}/cells/{cid}/journal").json()
    assert all(not any(".git" in path.casefold() for path in event.get("files") or []) for event in events)
    assert any("a.txt" in (event.get("files") or []) for event in events)


@pytest.mark.parametrize("path", [".git/HEAD", "a/.Git/x", "GIT~1/y", ".git./z", ".collrun/x"])
def test_writes_refuse_a_reserved_path(client, path):
    _, _, (cid,) = make_stack([("Cell", {"a.txt": "A"})])
    assert _mutate(client, cid, puts=[(path, "x")]).status_code == 422
    from collimate_server.persistence import FileMutation
    with pytest.raises(ValueError):
        FileMutation(path=path, writer_id="w", edit_seq=1, base_revision=0, kind="delete")


@pytest.mark.parametrize("name", [".git.", ".git ", ".GIT", "git~1"])
def test_an_export_never_writes_a_git_entry(fresh_cwd, name):
    from types import SimpleNamespace
    from collimate_server.cas import channel_store
    import collimate_server.transfer_export as transfer_export
    store = channel_store(channel_home())
    digest = store.store_bytes(b"x")
    files = [SimpleNamespace(name=f"{name}/HEAD", body_hash=digest, encoding="utf-8"),
             SimpleNamespace(name=name, body_hash=digest, encoding="utf-8"),
             SimpleNamespace(name="ok.txt", body_hash=digest, encoding="utf-8")]
    tree = transfer_export._Builder(store)
    transfer_export._add_cell(tree, "Cell", transfer_export._Folder(), files, None)
    assert [e.path for e in tree.entries] == ["Cell/ok.txt"]
    # Nor does a name the export makes safe (a cell's, a stack's): the only
    # .git an export writes is a repository's own.
    from collimate_server.reserved_paths import is_dotgit_segment
    assert not is_dotgit_segment(transfer_export.safe_name(name, "file"))
    assert transfer_export.safe_name(name, "file").startswith("_")


@pytest.mark.parametrize("repositories,status", [
    ([{"repo_path": "", "token": "t1"}, {"repo_path": "lib", "token": "t2"}], None),
    ([{"repo_path": "lib/.git", "token": "t"}], 400),
    ([{"repo_path": "node_modules", "token": "t"}], 400),
    ([{"repo_path": "a", "token": "t"}, {"repo_path": "a", "token": "u"}], 400),
])
def test_an_import_names_its_repositories_by_valid_unique_paths(fresh_cwd, repositories, status):
    from fastapi import HTTPException
    from collimate_server.transfer_import import ImportCell, _cell
    spec = ImportCell.model_validate({"name": "C", "repositories": repositories})
    if status is None:
        assert _cell(spec, str(uuid.uuid4())).name == "C"
    else:
        with pytest.raises(HTTPException) as refused:
            _cell(spec, str(uuid.uuid4()))
        assert refused.value.status_code == status
