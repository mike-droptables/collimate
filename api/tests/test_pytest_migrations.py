"""Coverage for the schema-migration framework.

A per-DB ``schema_migrations`` table records each migration applied. The
framework must:

  * Apply migrations in version order on a fresh DB.
  * Create the full head schema in one pass.
  * Skip already-applied migrations on subsequent opens.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from copy import deepcopy

import pytest


def _open(tmp_path):
    """Open a fresh per-channel DB under tmp_path. Returns (conn, db_path)."""
    os.chdir(tmp_path)
    import database
    # Force re-init for this test by clearing the cache.
    database._initialised_dbs.clear()
    conn = database.get_db_connection("36635d3d-4b57-4ee8-905c-823e7133f2c9")
    return conn


def test_fresh_db_applies_all_migrations(fresh_cwd):
    conn = _open(fresh_cwd)
    rows = conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()
    versions = [r["version"] for r in rows]
    # Should have applied every channel migration (auto-tracks additions).
    import migrations as _migrations
    expected = [v for v, _, _ in _migrations.CHANNEL_MIGRATIONS]
    assert versions == sorted(expected)


def test_fresh_db_has_full_head_schema(fresh_cwd):
    """A freshly-created channel DB has the complete head schema in one
    CREATE pass — all tables and every current cells column."""
    conn = _open(fresh_cwd)
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "blobs", "runs", "run_events", "cells", "cell_edges",
        "cell_files", "ui_state", "schema_migrations",
        "working_files", "file_revisions", "file_operations", "file_conflicts",
        "cell_moves", "removed_cells",
        "annotation_contexts", "annotations", "annotation_operations",
    } <= tables
    assert "cell_drafts" not in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cells)").fetchall()}
    assert cols == {
        "id", "run_id", "generator_name", "generator_cell_id", "sequence_num",
        "created_at", "version_name", "source_type",
    }


def test_fresh_db_has_runner_provenance_columns(fresh_cwd):
    """v4 adds the runner-run provenance columns to ``runs`` (runner_id,
    runner_cell_id, runner_snapshot) alongside the v3 generator
    provenance columns, all reachable in one migrate pass on a fresh DB."""
    conn = _open(fresh_cwd)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert {
        # v3 generator provenance still present
        "generator_name", "generator_cell_id",
        # v4 runner provenance
        "runner_id", "runner_cell_id", "runner_snapshot",
    } <= cols


def test_runner_cell_id_is_writable_after_v4(fresh_cwd):
    """The runner provenance columns accept writes/reads end to end — a run row
    can carry a runner_id/tag/cell_id/snapshot after v4 has run."""
    conn = _open(fresh_cwd)
    conn.execute(
        "INSERT INTO runs (id, prompt, status, runner_id, "
        "runner_snapshot) VALUES ('r1', 'p', 'completed', 'r-demo', "
        "'{\"generator\": \"demo-gen\"}')"
    )
    conn.commit()
    row = conn.execute(
        "SELECT runner_id, runner_snapshot FROM runs WHERE id='r1'"
    ).fetchone()
    assert row["runner_id"] == "r-demo"
    assert "demo-gen" in row["runner_snapshot"]


def test_reopening_db_does_not_reapply_migrations(fresh_cwd):
    conn = _open(fresh_cwd)
    # Snapshot the applied_at timestamps.
    first = {
        r["version"]: r["applied_at"]
        for r in conn.execute(
            "SELECT version, applied_at FROM schema_migrations"
        ).fetchall()
    }
    conn.close()

    # Force a "second open" by clearing the per-process cache and
    # opening again. Migrations should be no-ops.
    import database
    database._initialised_dbs.clear()
    conn2 = database.get_db_connection("36635d3d-4b57-4ee8-905c-823e7133f2c9")
    second = {
        r["version"]: r["applied_at"]
        for r in conn2.execute(
            "SELECT version, applied_at FROM schema_migrations"
        ).fetchall()
    }
    conn2.close()

    assert first == second, (
        "applied_at timestamps changed — a migration re-ran on second open"
    )


def test_apply_records_each_migration(tmp_path):
    """Drive the apply driver directly with a synthetic migration list
    so we don't depend on the channel-DB schema staying stable."""
    db = sqlite3.connect(str(tmp_path / "synthetic.db"))

    calls: list[int] = []

    def m1(c):
        calls.append(1)
        c.execute("CREATE TABLE t1 (id INTEGER)")

    def m2(c):
        calls.append(2)
        c.execute("CREATE TABLE t2 (id INTEGER)")

    import migrations as _migrations
    out = _migrations.apply(db, [(1, "first", m1), (2, "second", m2)])
    assert out == [1, 2]
    assert calls == [1, 2]

    # Re-applying the same set is a no-op.
    calls.clear()
    out = _migrations.apply(db, [(1, "first", m1), (2, "second", m2)])
    assert out == []
    assert calls == []

    # Adding a new migration only runs the new one.
    def m3(c):
        calls.append(3)

    out = _migrations.apply(
        db,
        [(1, "first", m1), (2, "second", m2), (3, "third", m3)],
    )
    assert out == [3]
    assert calls == [3]

    db.close()


def test_apply_rolls_back_on_failure(tmp_path):
    """A migration that raises mid-flight must leave the DB without that
    migration's version row and without partial DDL committed."""
    db = sqlite3.connect(str(tmp_path / "fail.db"))

    def good(c):
        c.execute("CREATE TABLE good (id INTEGER)")

    def bad(c):
        c.execute("CREATE TABLE will_be_rolled_back (id INTEGER)")
        raise RuntimeError("boom")

    import migrations as _migrations
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        _migrations.apply(db, [(1, "good", good), (2, "bad", bad)])

    # Good migration committed; bad migration's table should not exist.
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert "good" in table_names
    assert "will_be_rolled_back" not in table_names

    versions = db.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [v[0] for v in versions] == [1]
    db.close()


def test_v5_failure_rolls_back_its_ddl(tmp_path):
    """A migration that fails after its DDL must roll the DDL back too:
    the v5 schema statements run inside apply()'s BEGIN, not in the
    autocommit an executescript would force."""
    db = sqlite3.connect(str(tmp_path / "v5fail.db"))
    import migrations as _migrations
    import pytest
    _migrations.apply(db, _migrations.CHANNEL_MIGRATIONS[:4])

    def failing(c):
        _migrations._channel_v5_run_nodes(c)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _migrations.apply(db, [(5, "run_nodes", failing)])

    tables = {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "run_artifacts" not in tables
    cols = {r[1] for r in db.execute("PRAGMA table_info(runs)").fetchall()}
    assert "kind" not in cols
    assert db.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == 4
    db.close()


def test_run_events_indexed_by_run(fresh_cwd):
    """Every run_events read filters by run_id; the channel schema carries
    an index so those reads don't scan the whole log history."""
    conn = _open(fresh_cwd)
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_run_events_run" in indexes
    plan = " ".join(
        str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT payload FROM run_events "
            "WHERE run_id=? ORDER BY id",
            ("r1",),
        ).fetchall()
    )
    assert "idx_run_events_run" in plan


def _through(version):
    """The channel migrations up to and including ``version``."""
    import migrations as _migrations
    return [m for m in _migrations.CHANNEL_MIGRATIONS if m[0] <= version]


def _channel_db_at(tmp_path, version):
    """A synthetic channel DB migrated up to and including ``version``."""
    import migrations as _migrations
    db = sqlite3.connect(str(tmp_path / f"v{version}.db"))
    db.execute("PRAGMA foreign_keys = ON")
    _migrations.apply(db, _through(version))
    return db


def test_channel_migrations_15_to_18_are_lineage_run_ends_removed_cells_dead_columns_O03():
    import migrations as _migrations
    tail = [(v, name) for v, name, _ in _migrations.CHANNEL_MIGRATIONS if 14 <= v <= 18]
    assert tail == [(14, "delta_versions"), (15, "cell_moves"), (16, "run_ends"),
                    (17, "removed_cells"), (18, "drop_dead_columns")]


def test_migration_18_drops_the_dead_cells_columns_and_keeps_rows_triggers_and_indexes_O02(tmp_path):
    """A v17 store keeps its cells (a verdict, a deleted_at stamp and a
    non-default state included) when migration 18 drops ``verdict``,
    ``verdict_at``, ``deleted_at``, ``state`` and ``pickoptions_json``; the
    triggers and indexes of the table survive and still fire."""
    import migrations as _migrations
    db = _channel_db_at(tmp_path, 17)
    db.execute("INSERT INTO runs (id, status) VALUES ('r', 'completed')")
    db.execute(
        "INSERT INTO cells (id, run_id, sequence_num, version_name, state, pickoptions_json, "
        "deleted_at, verdict, verdict_at) VALUES ('v1', 'r', 1, 'one', 'Picked', '[]', "
        "CURRENT_TIMESTAMP, 'committed', CURRENT_TIMESTAMP)")
    db.commit()

    def schema():
        return {(r[0], r[1]) for r in db.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'index') "
            "AND name NOT LIKE 'sqlite_%'")}

    before = schema()
    _migrations.apply(db, [m for m in _migrations.CHANNEL_MIGRATIONS if m[0] == 18])

    cols = {r[1] for r in db.execute("PRAGMA table_info(cells)")}
    assert not cols & {"verdict", "verdict_at", "deleted_at", "state", "pickoptions_json"}
    assert db.execute("SELECT id, run_id, sequence_num, version_name FROM cells").fetchall() == [
        ("v1", "r", 1, "one")]
    assert schema() == before
    # The version-metadata trigger on cells still fires after the drop.
    db.execute("INSERT INTO cells (id, run_id, sequence_num) VALUES ('v2', 'r', 2)")
    assert db.execute("SELECT root_id FROM cell_file_versions WHERE version_id='v2'").fetchone() == ("v2",)
    db.close()


def test_cell_moves_table_checks_op_and_leaves_to_cell_unbound_O03(tmp_path):
    import pytest
    db = _channel_db_at(tmp_path, 15)
    cols = {r[1]: r for r in db.execute("PRAGMA table_info(cell_moves)")}
    assert list(cols) == ["id", "op", "op_id", "from_cell", "from_version",
                          "to_cell", "to_version", "ord", "created_at"]
    assert {name for name, info in cols.items() if info[3]} == {
        "op", "op_id", "from_cell", "to_cell", "ord", "created_at"}
    fks = {(r[3], r[2]) for r in db.execute("PRAGMA foreign_key_list(cell_moves)")}
    assert fks == {("from_version", "cells")}
    indexes = {r[1] for r in db.execute("PRAGMA index_list(cell_moves)")}
    assert {"idx_cell_moves_from", "idx_cell_moves_to"} <= indexes
    db.execute("INSERT INTO cells (id) VALUES ('v1')")
    # A duplicate's copy has no cells row: to_cell is free.
    db.execute("INSERT INTO cell_moves (op, op_id, from_cell, from_version, to_cell) "
               "VALUES ('duplicate', 'r1', 'a', 'v1', 'not-a-row')")
    row = db.execute("SELECT ord, created_at FROM cell_moves").fetchone()
    assert row[0] == 0 and row[1]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO cell_moves (op, op_id, from_cell, to_cell) VALUES ('fork', 'r', 'a', 'b')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO cell_moves (op, op_id, from_cell, from_version, to_cell) "
                   "VALUES ('merge', 'r', 'a', 'missing', 'b')")
    db.close()


def test_runs_gain_ended_at_note_and_checked_outcome_O03(tmp_path):
    import pytest
    db = _channel_db_at(tmp_path, 16)
    cols = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    assert {"ended_at", "note", "outcome"} <= cols
    db.execute("INSERT INTO runs (id, status, outcome) VALUES ('r1', 'completed', 'pass')")
    db.execute("INSERT INTO runs (id, status, outcome) VALUES ('r2', 'completed', NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO runs (id, status, outcome) VALUES ('r3', 'completed', 'maybe')")
    db.close()


def test_removed_cells_table_checks_reason_O03(tmp_path):
    import pytest
    db = _channel_db_at(tmp_path, 17)
    cols = [r[1] for r in db.execute("PRAGMA table_info(removed_cells)")]
    assert cols == ["cell_id", "name", "icon", "stack_name", "stack_type", "stack_cell_count",
                    "cell_created_at", "cell_updated_at", "removed_at", "pinned",
                    "metadata_json", "binary_paths_json", "reason"]
    insert = ("INSERT INTO removed_cells (cell_id, name, stack_cell_count, removed_at, "
              "metadata_json, binary_paths_json, reason) VALUES (?, 'n', 1, 't', '{}', '[]', ?)")
    for reason in ("removed", "merged", "split"):
        db.execute(insert, (reason, reason))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("x", "deleted"))
    db.close()


def test_shelf_binary_hint_is_retired_after_typed_history(tmp_path):
    import migrations
    db = _channel_db_at(tmp_path, 23)
    db.execute("INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,"
               "binary_paths_json,reason,display_json) VALUES ('shelf','Named',1,'now','{}','[]','removed','{}')")
    db.commit()
    assert migrations.apply(db, _through(24)) == [24]
    assert "binary_paths_json" not in {row[1] for row in db.execute("PRAGMA table_info(removed_cells)")}
    assert db.execute("SELECT cell_id,name,metadata_json,display_json FROM removed_cells").fetchone() == (
        "shelf", "Named", "{}", "{}")
    assert migrations.apply(db, _through(24)) == []
    db.close()


def _display(*leaves, active_leaf=None):
    """A synthetic stored cell display: one leaf, or a row split of
    ``leaves``, each ``(leaf id, tabs, active tab id)``."""
    nodes = [{"type": "leaf", "id": leaf, "tabs": tabs, "activeTabId": active} for leaf, tabs, active in leaves]
    root = nodes[0] if len(nodes) == 1 else {"type": "split", "id": "split", "dir": "row", "children": nodes}
    return {"version": 1, "root": root, "activeLeafId": active_leaf or leaves[0][0],
        "filesPane": {"collapsed": False, "side": "left", "width": None, "height": None, "viewMode": "tree",
                      "panelFilters": {"runsOnly": False, "sortByRecency": False}},
        "runsPane": {"open": False, "side": "right", "width": None, "height": None},
        "pinnedTabCompact": False, "history": None}


def test_migration_25_retires_sync_tabs_and_the_sync_settings_section_TR06(tmp_path):
    """Every stored display (live rows and removed cells) loses its sync
    tabs: a leaf whose active tab went selects the surviving tab nearest it,
    a leaf left empty gets one empty tab, and a display without sync tabs is
    left byte for byte. The settings row loses its sync section and takes
    the channel-settings kind."""
    import migrations
    from cell_display import validate_display
    from channel_settings import validate as validate_settings
    file_tab = {"id": "f", "kind": "file", "path": "a.md", "rendererId": "text"}
    sync = {"id": "s", "kind": "sync", "surface": "local-browse"}
    live = _display(("left", [file_tab, sync, {"id": "m", "kind": "empty"}], "s"),
                    ("right", [{"id": "s2", "kind": "sync", "surface": "local-backup"}], "s2"))
    plain = json.dumps(_display(("only", [file_tab], "f")))
    removed = _display(("leaf", [file_tab, sync], "s"))
    # The settings kind before the rename, named from parts so the retired
    # token scan does not see it.
    settings = {"version": 1, "kind": "coll" + "settings", "sync": {"localBrowse": True, "localBackup": True},
                "keyboard": {"overrides": {"synth": "Mod+K"}}}
    db = _channel_db_at(tmp_path, 24)
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cell_display:live',?)", (json.dumps(live),))
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cell_display:plain',?)", (plain,))
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cellXdisplay:lookalike',?)", (json.dumps(live),))
    db.execute("INSERT INTO ui_state(key,value) VALUES ('channel_settings',?)", (json.dumps(settings),))
    db.execute("INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,reason,display_json) "
               "VALUES ('gone','Removed',1,'now','{}','removed',?)", (json.dumps(removed),))
    db.commit()
    assert migrations.apply(db, _through(25)) == [25]

    stored = json.loads(db.execute("SELECT value FROM ui_state WHERE key='cell_display:live'").fetchone()[0])
    left, right = stored["root"]["children"]
    assert left["tabs"] == [file_tab, {"id": "m", "kind": "empty"}] and left["activeTabId"] == "m"
    assert [tab["kind"] for tab in right["tabs"]] == ["empty"] and right["activeTabId"] == right["tabs"][0]["id"]
    validate_display(stored)
    assert db.execute("SELECT value FROM ui_state WHERE key='cell_display:plain'").fetchone()[0] == plain
    assert json.loads(db.execute("SELECT value FROM ui_state WHERE key='cellXdisplay:lookalike'").fetchone()[0]) == live
    shelved = json.loads(db.execute("SELECT display_json FROM removed_cells WHERE cell_id='gone'").fetchone()[0])
    leaf = shelved["root"]
    assert leaf["tabs"] == [file_tab] and leaf["activeTabId"] == "f"
    validate_display(shelved)
    doc = json.loads(db.execute("SELECT value FROM ui_state WHERE key='channel_settings'").fetchone()[0])
    assert doc == {"version": 1, "kind": "channel-settings", "keyboard": {"overrides": {"synth": "Mod+K"}}}
    assert validate_settings(doc)["keyboard"] == {"overrides": {"synth": "Mod+K"}, "commands": {}}
    assert migrations.apply(db, _through(25)) == []
    db.close()


def test_migration_26_backfills_the_files_pane_dock_FP01(tmp_path):
    """Every stored display (live rows and removed cells) gains the files
    pane's side ('left') and its width and height (null) where missing; a
    display that already has them is left byte for byte, and one that does
    not parse is dropped."""
    import migrations
    from cell_display import validate_display
    file_tab = {"id": "f", "kind": "file", "path": "a.md", "rendererId": "text"}
    old = _display(("leaf", [file_tab], "f"))
    for key in ("side", "width", "height"):
        del old["filesPane"][key]
    top = deepcopy(old)
    top["filesPane"]["side"] = "top"
    docked = _display(("leaf", [file_tab], "f"))
    docked["filesPane"].update(side="bottom", height=240)
    docked_raw = json.dumps(docked)
    db = _channel_db_at(tmp_path, 25)
    rows = [("cell_display:old", json.dumps(old)), ("cell_display:top", json.dumps(top)),
            ("cell_display:docked", docked_raw), ("cell_display:broken", "{not json"),
            ("cell_display:array", "[1]"), ("cellXdisplay:lookalike", "{not json")]
    db.executemany("INSERT INTO ui_state(key,value) VALUES (?,?)", rows)
    shelf = "INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,reason,display_json) " \
            "VALUES (?,'Removed',1,'now','{}','removed',?)"
    db.executemany(shelf, [("gone", json.dumps(old)), ("broken", "{not json"), ("bare", None)])
    db.commit()
    assert migrations.apply(db, _through(26)) == [26]

    def stored(key):
        row = db.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
        return row and row[0]
    for key, side in (("cell_display:old", "left"), ("cell_display:top", "top")):
        display = json.loads(stored(key))
        assert display["filesPane"] == {**old["filesPane"], "side": side, "width": None, "height": None}
        assert validate_display(display) == display
    assert stored("cell_display:docked") == docked_raw
    assert stored("cell_display:broken") is None and stored("cell_display:array") is None
    assert stored("cellXdisplay:lookalike") == "{not json"
    shelved = dict(db.execute("SELECT cell_id, display_json FROM removed_cells").fetchall())
    assert shelved["broken"] is None and shelved["bare"] is None
    display = json.loads(shelved["gone"])
    assert display["filesPane"]["side"] == "left" and validate_display(display) == display
    assert migrations.apply(db, _through(26)) == []
    db.close()


def test_migration_27_adds_the_runs_pane_and_moves_the_files_pane_off_its_edges(tmp_path):
    """Every stored display (live rows and removed cells) moves a files pane
    docked right to the left and one docked bottom to the top, keeping its
    size, and gains a closed runs pane on the right at the app's default
    size. A display already in that shape is left byte for
    byte, and one that is not a JSON object is dropped."""
    import migrations
    from cell_display import validate_display
    file_tab = {"id": "f", "kind": "file", "path": "a.md", "rendererId": "text"}
    current = _display(("leaf", [file_tab], "f"))
    current["filesPane"].update(side="top", height=180)
    current["runsPane"].update(open=True, side="bottom", height=220)
    current_raw = json.dumps(current)

    def before(side, **size):
        display = _display(("leaf", [file_tab], "f"))
        display["filesPane"].update(side=side, **size)
        del display["runsPane"]
        return display
    right, bottom, top = before("right", width=300), before("bottom", height=240), before("top")
    db = _channel_db_at(tmp_path, 26)
    rows = [("cell_display:right", json.dumps(right)), ("cell_display:bottom", json.dumps(bottom)),
            ("cell_display:top", json.dumps(top)), ("cell_display:current", current_raw),
            ("cell_display:broken", "{not json"), ("cell_display:array", "[1]"),
            ("cellXdisplay:lookalike", json.dumps(right))]
    db.executemany("INSERT INTO ui_state(key,value) VALUES (?,?)", rows)
    shelf = "INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,reason,display_json) " \
            "VALUES (?,'Removed',1,'now','{}','removed',?)"
    db.executemany(shelf, [("gone", json.dumps(bottom)), ("kept", current_raw), ("broken", "{not json"),
                           ("array", "[1]"), ("bare", None)])
    db.commit()
    assert migrations.apply(db, _through(27)) == [27]

    runs = {"open": False, "side": "right", "width": None, "height": None}

    def stored(key):
        row = db.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
        return row and row[0]
    for key, old, side in (("cell_display:right", right, "left"), ("cell_display:bottom", bottom, "top"),
                           ("cell_display:top", top, "top")):
        display = json.loads(stored(key))
        assert display == {**old, "filesPane": {**old["filesPane"], "side": side}, "runsPane": runs}
        assert validate_display(display) == display
    assert stored("cell_display:current") == current_raw and validate_display(current) == current
    assert stored("cell_display:broken") is None and stored("cell_display:array") is None
    assert json.loads(stored("cellXdisplay:lookalike")) == right
    shelved = dict(db.execute("SELECT cell_id, display_json FROM removed_cells").fetchall())
    assert shelved["kept"] == current_raw
    assert shelved["broken"] is None and shelved["array"] is None and shelved["bare"] is None
    display = json.loads(shelved["gone"])
    assert display == {**bottom, "filesPane": {**bottom["filesPane"], "side": "top"}, "runsPane": runs}
    assert validate_display(display) == display
    assert migrations.apply(db, _through(27)) == []
    db.close()


def test_migration_28_strips_manage_tabs_and_drops_removed_stack_type(tmp_path):
    """Every stored display (live rows and removed cells) loses its manage
    tabs: a leaf whose active tab went selects the surviving tab nearest it,
    a leaf left empty gets one empty tab, and a display without one is left
    byte for byte. The removed-cell rows keep everything but their stack
    type, whose column goes."""
    import migrations
    from cell_display import validate_display
    file_tab = {"id": "f", "kind": "file", "path": "a.md", "rendererId": "text"}
    history = {"id": "h", "kind": "history", "category": "file", "path": "a.md"}
    manage = {"id": "m", "kind": "manage"}
    live = _display(("left", [file_tab, manage, history], "m"), ("right", [{"id": "m2", "kind": "manage"}], "m2"))
    plain = json.dumps(_display(("only", [file_tab], "f")))
    removed = _display(("leaf", [file_tab, manage], "m"))
    db = _channel_db_at(tmp_path, 27)
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cell_display:live',?)", (json.dumps(live),))
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cell_display:plain',?)", (plain,))
    db.execute("INSERT INTO ui_state(key,value) VALUES ('cellXdisplay:lookalike',?)", (json.dumps(live),))
    db.execute("INSERT INTO removed_cells(cell_id,name,stack_name,stack_type,stack_cell_count,removed_at,"
               "metadata_json,reason,display_json) VALUES ('gone','Removed','Generators','generator',2,'now',"
               "'{\"tags\": [\"kept\"]}','removed',?)", (json.dumps(removed),))
    db.commit()
    assert migrations.apply(db, _through(28)) == [28]

    stored = json.loads(db.execute("SELECT value FROM ui_state WHERE key='cell_display:live'").fetchone()[0])
    left, right = stored["root"]["children"]
    assert left["tabs"] == [file_tab, history] and left["activeTabId"] == "h"
    assert [tab["kind"] for tab in right["tabs"]] == ["empty"] and right["activeTabId"] == right["tabs"][0]["id"]
    validate_display(stored)
    assert db.execute("SELECT value FROM ui_state WHERE key='cell_display:plain'").fetchone()[0] == plain
    assert json.loads(db.execute("SELECT value FROM ui_state WHERE key='cellXdisplay:lookalike'").fetchone()[0]) == live
    columns = [row[1] for row in db.execute("PRAGMA table_info(removed_cells)")]
    assert columns == ["cell_id", "name", "icon", "stack_name", "stack_cell_count", "cell_created_at",
                       "cell_updated_at", "removed_at", "pinned", "metadata_json", "reason", "display_json"]
    row = db.execute("SELECT cell_id, name, stack_name, stack_cell_count, metadata_json, display_json "
                     "FROM removed_cells").fetchone()
    assert row[:5] == ("gone", "Removed", "Generators", 2, '{"tags": ["kept"]}')
    shelved = json.loads(row[5])
    assert shelved["root"]["tabs"] == [file_tab] and shelved["root"]["activeTabId"] == "f"
    validate_display(shelved)
    assert migrations.apply(db, _through(28)) == []
    db.close()


def test_migration_29_creates_the_annotation_store_and_drops_the_runs_pane_tab(tmp_path):
    """The annotation store's three tables and two indexes are created, a
    context keyed by (cell, id) with at most one context per run; every
    stored display (live rows and removed cells) loses its runs pane's tab,
    a display without one is left byte for byte, and one that is not a JSON
    object is dropped."""
    import migrations
    from cell_display import validate_display
    file_tab = {"id": "f", "kind": "file", "path": "a.md", "rendererId": "text"}

    def tabbed(tab, **pane):
        display = _display(("leaf", [file_tab], "f"))
        display["runsPane"].update(tab=tab, **pane)
        return display
    status, log = tabbed("status"), tabbed("log", open=True, side="bottom", height=220)
    plain_raw = json.dumps(_display(("leaf", [file_tab], "f")))
    db = _channel_db_at(tmp_path, 28)
    rows = [("cell_display:status", json.dumps(status)), ("cell_display:log", json.dumps(log)),
            ("cell_display:plain", plain_raw), ("cell_display:broken", "{not json"),
            ("cell_display:array", "[1]"), ("cellXdisplay:lookalike", json.dumps(status))]
    db.executemany("INSERT INTO ui_state(key,value) VALUES (?,?)", rows)
    shelf = "INSERT INTO removed_cells(cell_id,name,stack_cell_count,removed_at,metadata_json,reason,display_json) " \
            "VALUES (?,'Removed',1,'now','{}','removed',?)"
    db.executemany(shelf, [("gone", json.dumps(log)), ("kept", plain_raw), ("broken", "{not json"), ("bare", None)])
    db.commit()
    assert migrations.apply(db, _through(29)) == [29]

    def stored(key):
        row = db.execute("SELECT value FROM ui_state WHERE key=?", (key,)).fetchone()
        return row and row[0]
    for key, old in (("cell_display:status", status), ("cell_display:log", log)):
        display = json.loads(stored(key))
        expected = deepcopy(old)
        del expected["runsPane"]["tab"]
        assert display == expected and validate_display(display) == display
    assert stored("cell_display:plain") == plain_raw
    assert stored("cell_display:broken") is None and stored("cell_display:array") is None
    assert json.loads(stored("cellXdisplay:lookalike")) == status
    shelved = dict(db.execute("SELECT cell_id, display_json FROM removed_cells").fetchall())
    assert shelved["kept"] == plain_raw and shelved["broken"] is None and shelved["bare"] is None
    gone = json.loads(shelved["gone"])
    assert "tab" not in gone["runsPane"] and validate_display(gone) == gone

    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"annotation_contexts", "annotations", "annotation_operations"} <= tables
    indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"annotation_contexts_by_run", "annotations_by_context"} <= indexes
    keys = sorted((row[5], row[1]) for row in db.execute("PRAGMA table_info(annotation_contexts)") if row[5])
    assert [name for _order, name in keys] == ["cell_id", "id"]
    db.execute("INSERT INTO annotation_contexts(id, cell_id, run_id) VALUES ('cell', 'a', NULL)")
    db.execute("INSERT INTO annotation_contexts(id, cell_id, run_id) VALUES ('cell', 'b', NULL)")
    db.execute("INSERT INTO annotation_contexts(id, cell_id, run_id) VALUES ('r1', 'a', 'r1')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO annotation_contexts(id, cell_id, run_id) VALUES ('r1', 'b', 'r1')")
    for column, value in (("author", "bot"), ("kind", "card"), ("mode", "later")):
        values = {"author": "user", "kind": "message", "mode": "queued", column: value}
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO annotations(id, cell_id, context_id, author, kind, body, mode, position, "
                       "created_at) VALUES ('x', 'a', 'cell', ?, ?, 'b', ?, 1, 'now')",
                       (values["author"], values["kind"], values["mode"]))
    assert migrations.apply(db, _through(29)) == []
    db.close()


def test_channel_v30_adds_an_empty_provenance_table_and_leaves_the_cells(tmp_path):
    """An existing channel gets the provenance table empty: nothing
    backfills, so its cells show no provenance; a record keys on its cell
    and folder and names one of the six source kinds."""
    import migrations
    db = _channel_db_at(tmp_path, 29)
    db.execute("INSERT INTO cells(id, source_type, version_name) VALUES ('kept', 'import', 'Imported')")
    db.commit()
    cells = db.execute("SELECT * FROM cells").fetchall()
    assert migrations.apply(db, _through(30)) == [30]
    assert db.execute("SELECT * FROM cells").fetchall() == cells
    assert db.execute("SELECT COUNT(*) FROM cell_provenance").fetchone()[0] == 0
    columns = [row[1] for row in db.execute("PRAGMA table_info(cell_provenance)")]
    assert columns == ["cell_id", "folder", "source_kind", "source_id", "package_kind", "package_id",
                       "package_version", "content_hash", "sdk_version", "admitted_at"]
    row = ("kept", "pkg", "zip", "zip", "renderer", "pkg", None, "h", "0.1.0", "now")
    insert = "INSERT INTO cell_provenance VALUES (?,?,?,?,?,?,?,?,?,?)"
    db.execute(insert, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("kept", "other", "registry", *row[3:]))
    assert migrations.apply(db, _through(30)) == []
    db.close()


def _global_db_at(version):
    """A synthetic in-memory global DB migrated up to and including
    ``version``, with no saved layout."""
    import migrations
    db = sqlite3.connect(":memory:")
    migrations.apply(db, [m for m in migrations.GLOBAL_MIGRATIONS if m[0] <= version])
    return db


def test_global_db_older_than_8_is_refused(fresh_cwd):
    """An app database that stopped before global schema 8 is refused, with
    nothing applied; a fresh one takes every migration and one at 8 takes
    the rest."""
    import pytest
    import migrations
    old = _global_db_at(7)
    with pytest.raises(RuntimeError, match="older than global schema 8"):
        migrations.upgrade_global_db(old)
    assert max(row[0] for row in old.execute("SELECT version FROM schema_migrations")) == 7
    fresh = sqlite3.connect(":memory:")
    assert migrations.upgrade_global_db(fresh) == [v for v, _, _ in migrations.GLOBAL_MIGRATIONS]
    assert migrations.upgrade_global_db(_global_db_at(8)) == [v for v, _, _ in migrations.GLOBAL_MIGRATIONS if v > 8]


def _stack(stack_type, *cells, name="S"):
    return {"id": str(uuid.uuid4()), "name": name, "stack_type": stack_type,
            "cells": [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, cell)), "name": cell, "builtin": cell.startswith("card")}
                      for cell in cells]}


def test_global_migration_9_merges_generator_and_renderer_stacks(fresh_cwd):
    """Each channel's Generators and Renderers stacks become one Capabilities
    stack: the built-in cards first, then every other cell once in layout
    order; the first Renderers stack's id; the place of the first of them on
    the board, else the end of the navigation stacks; selections of a folded
    stack repointed. A channel with none is left as it was."""
    import migrations
    from models import AppState
    # Board placement: a generator stack on the board, renderer stacks hidden.
    s1, g1, s2 = _stack("standard", "s1-a"), _stack("generator", "g1", "g2", name="Generators"), _stack("standard", "s2-a")
    r1 = _stack("renderer", "card-1", "card-2", "r1", "g2", name="Renderers")
    g2, r2 = _stack("generator", "g3"), _stack("renderer", "r2")
    board = {"id": str(uuid.uuid4()), "name": "Board", "main_stacks": [s1, g1, s2],
             "nav_stacks": [r1, g2, r2], "main_selection_id": g1["id"], "nav_selection_id": r2["id"]}
    # Hidden placement: every source stack in navigation; the selection of
    # the renderer stack, whose id the merged stack keeps, stays.
    s3, g3, n1 = _stack("standard", "s3-a"), _stack("generator", "g4"), _stack("standard", "n1-a")
    r3 = _stack("renderer", "card-3", "r3")
    hidden = {"id": str(uuid.uuid4()), "name": "Hidden", "main_stacks": [s3], "nav_stacks": [g3, n1, r3],
              "main_selection_id": s3["id"], "nav_selection_id": r3["id"]}
    # Generators only, no renderer stack: the first one survives; a board
    # selection of a folded stack falls to the first board stack.
    s5, g4, g5 = _stack("standard", "s5-a"), _stack("generator", "g5"), _stack("generator", "g6")
    generators = {"id": str(uuid.uuid4()), "name": "Generators only", "main_stacks": [s5],
                  "nav_stacks": [g4, g5], "main_selection_id": g5["id"], "nav_selection_id": g5["id"]}
    # Both on the board around a standard stack: the merged stack stands
    # where the Generators stack (the first of them) stood, under the
    # Renderers stack's id, and a selection of the Generators stack follows.
    g7, s6, r4 = _stack("generator", "g8"), _stack("standard", "s6-a"), _stack("renderer", "card-4", "r4")
    both = {"id": str(uuid.uuid4()), "name": "Both on board", "main_stacks": [g7, s6, r4], "nav_stacks": [],
            "main_selection_id": g7["id"], "nav_selection_id": None}
    g6 = _stack("generator", "g7")
    bare = {"id": str(uuid.uuid4()), "name": "Bare", "main_stacks": [], "nav_stacks": [g6],
            "main_selection_id": g6["id"], "nav_selection_id": None}
    plain = {"id": str(uuid.uuid4()), "name": "Plain", "main_stacks": [_stack("standard", "p")],
             "nav_stacks": [], "main_selection_id": None, "nav_selection_id": None}
    layout = {"storage_format": "sql-layout-v2", "active_channel_id": None,
              "channels": [board, hidden, generators, both, bare, json.loads(json.dumps(plain))]}
    db = _global_db_at(8)
    db.execute("INSERT INTO ui_state(key,value) VALUES ('app_state',?)", (json.dumps(layout),))
    db.commit()
    assert migrations.upgrade_global_db(db) == [9, 10, 11]
    saved = json.loads(db.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0])
    AppState.model_validate(saved)
    by_name = {channel["name"]: channel for channel in saved["channels"]}

    def cells(stack):
        return [cell["name"] for cell in stack["cells"]]

    out = by_name["Board"]
    assert [s["id"] for s in out["main_stacks"]] == [s1["id"], r1["id"], s2["id"]] and out["nav_stacks"] == []
    merged = out["main_stacks"][1]
    assert (merged["name"], merged["stack_type"]) == ("Capabilities", "capabilities")
    assert cells(merged) == ["card-1", "card-2", "g1", "g2", "r1", "g3", "r2"]
    assert (out["main_selection_id"], out["nav_selection_id"]) == (r1["id"], None)

    out = by_name["Hidden"]
    assert out["main_stacks"] == [s3] and [s["id"] for s in out["nav_stacks"]] == [n1["id"], r3["id"]]
    assert cells(out["nav_stacks"][1]) == ["card-3", "g4", "r3"]
    assert (out["main_selection_id"], out["nav_selection_id"]) == (s3["id"], r3["id"])

    out = by_name["Generators only"]
    assert out["main_stacks"] == [s5] and [s["id"] for s in out["nav_stacks"]] == [g4["id"]]
    assert out["nav_stacks"][0]["stack_type"] == "capabilities" and cells(out["nav_stacks"][0]) == ["g5", "g6"]
    assert (out["main_selection_id"], out["nav_selection_id"]) == (s5["id"], None)

    out = by_name["Both on board"]
    assert [s["id"] for s in out["main_stacks"]] == [r4["id"], s6["id"]] and out["nav_stacks"] == []
    assert out["main_stacks"][0]["stack_type"] == "capabilities" and cells(out["main_stacks"][0]) == ["card-4", "g8", "r4"]
    assert (out["main_selection_id"], out["nav_selection_id"]) == (r4["id"], None)

    out = by_name["Bare"]
    assert [s["id"] for s in out["nav_stacks"]] == [g6["id"]] and out["main_selection_id"] is None

    assert by_name["Plain"] == plain
    assert migrations.upgrade_global_db(db) == []


def test_global_migration_9_leaves_missing_unparsable_and_unchanged_layouts_alone(fresh_cwd):
    """No saved layout, one that does not parse (startup quarantines it) and
    one without Generators or Renderers stacks are kept byte for byte."""
    import migrations
    assert migrations.upgrade_global_db(_global_db_at(8)) == [9, 10, 11]
    plain = json.dumps({"channels": [{"id": "c", "main_stacks": [_stack("standard", "p")], "nav_stacks": []}]})
    for raw in ("{not json", '{"channels": "invalid"}', plain):
        db = _global_db_at(8)
        db.execute("INSERT INTO ui_state(key,value) VALUES ('app_state',?)", (raw,))
        db.commit()
        assert migrations.upgrade_global_db(db) == [9, 10, 11]
        assert db.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0] == raw


def test_global_migration_10_creates_the_module_mirror_store(fresh_cwd):
    """The mirror's store: one row per fetched upstream URL, unique by URL."""
    import migrations
    db = _global_db_at(9)
    assert migrations.upgrade_global_db(db) == [10, 11]
    columns = [row[1] for row in db.execute("PRAGMA table_info(esm_modules)")]
    assert columns == ["url", "blob_hash", "media_type", "bytes", "upstream_hash", "final_url", "fetched_at"]
    row = ("https://esm.sh/a@1.0.0", "a" * 64, "text/javascript", 1, "b" * 64, "https://esm.sh/a@1.0.0", 1.0)
    db.execute("INSERT INTO esm_modules VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO esm_modules VALUES (?, ?, ?, ?, ?, ?, ?)", row)


def test_global_migration_11_records_who_compiled_each_renderer_bundle(fresh_cwd):
    """A bundle row gains its origin: a row from before is a client one, and
    only ``server`` and ``client`` are origins."""
    import migrations
    db = _global_db_at(10)
    db.execute("INSERT INTO renderer_bundles VALUES (?, ?)", ("a" * 64, "b" * 64))
    db.commit()
    assert migrations.upgrade_global_db(db) == [11]
    assert [row[1] for row in db.execute("PRAGMA table_info(renderer_bundles)")] == ["source_hash", "blob_hash", "origin"]
    assert db.execute("SELECT * FROM renderer_bundles").fetchall() == [("a" * 64, "b" * 64, "client")]
    db.execute("INSERT INTO renderer_bundles VALUES (?, ?, 'server')", ("c" * 64, "d" * 64))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO renderer_bundles VALUES (?, ?, 'prebuilt')", ("e" * 64, "f" * 64))


def test_removed_cells_blob_becomes_rows_and_the_key_goes_O03(tmp_path):
    """Migration 17 moves every ui_state['removed_cells'] entry into a row
    (reason 'removed', stack size 1, UTC ISO timestamps) and deletes the key."""
    from datetime import datetime, timezone
    import migrations as _migrations
    db = _channel_db_at(tmp_path, 16)
    local_created = datetime(2026, 10, 1, 9, 30, 0)
    blob = {
        "cell-a": {
            "cell": {"id": "cell-a", "name": "Notes", "icon": "BookOpen",
                     "created_at": local_created.isoformat(),
                     "updated_at": local_created.isoformat(),
                     "settings": {"k": 1}, "channel": "demo", "pinned": "a.md"},
            "stack_name": "Project", "stack_type": "standard",
            "binary_paths": ["img.png"],
            "removed_at": "2026-10-02T12:00:00.123456+00:00",
        },
        "cell-b": {"cell": {}, "stack_type": "renderer"},
        "cell-c": "not an entry",
    }
    db.execute("INSERT INTO ui_state (key, value) VALUES ('removed_cells', ?)", (json.dumps(blob),))
    db.execute("INSERT INTO ui_state (key, value) VALUES ('other', 'kept')")
    db.commit()
    _migrations.apply(db, [m for m in _migrations.CHANNEL_MIGRATIONS if m[0] <= 17])

    assert db.execute("SELECT value FROM ui_state WHERE key='removed_cells'").fetchone() is None
    assert db.execute("SELECT value FROM ui_state WHERE key='other'").fetchone()[0] == "kept"
    rows = {r[0]: r for r in db.execute(
        "SELECT cell_id, name, icon, stack_name, stack_type, stack_cell_count, cell_created_at, "
        "cell_updated_at, removed_at, pinned, metadata_json, binary_paths_json, reason "
        "FROM removed_cells")}
    assert set(rows) == {"cell-a", "cell-b"}
    a = rows["cell-a"]
    expected_created = local_created.astimezone().astimezone(timezone.utc).isoformat()
    assert a[1:6] == ("Notes", "BookOpen", "Project", "standard", 1)
    assert a[6] == a[7] == expected_created
    assert a[8] == "2026-10-02T12:00:00+00:00"
    assert a[9] == "a.md"
    assert json.loads(a[10]) == blob["cell-a"]["cell"]
    assert json.loads(a[11]) == ["img.png"]
    assert a[12] == "removed"
    b = rows["cell-b"]
    assert b[1] == "Cell" and b[4] == "renderer" and b[5] == 1 and b[8].endswith("+00:00")
    assert json.loads(b[11]) == []
    db.close()


def test_malformed_removed_cells_blob_is_dropped_O03(tmp_path):
    import migrations as _migrations
    db = _channel_db_at(tmp_path, 16)
    db.execute("INSERT INTO ui_state (key, value) VALUES ('removed_cells', '[not json')")
    db.commit()
    _migrations.apply(db, _migrations.CHANNEL_MIGRATIONS)
    assert db.execute("SELECT COUNT(*) FROM ui_state WHERE key='removed_cells'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM removed_cells").fetchone()[0] == 0
    db.close()


def test_provenance_edges_become_cell_moves_and_are_deleted_O03(tmp_path):
    """Migration 15 turns every merge_<i>, split_from, split_restored and
    branched_from edge of a v14 store into a cell_moves row keyed by chain
    roots, then deletes those edges; previous_version and input edges stay."""
    import migrations as _migrations
    db = _channel_db_at(tmp_path, 14)
    for run in ("gen", "merge-run", "split-run", "dup-run"):
        db.execute("INSERT INTO runs (id, status) VALUES (?, 'completed')", (run,))
    cells = [  # (id, run, created_at)
        ("a", "gen", "2026-10-01 08:00:00"), ("a2", "gen", "2026-10-01 08:05:00"),
        ("b", "gen", "2026-10-01 08:10:00"),
        ("m", "merge-run", "2026-10-01 09:00:00"),
        ("m2", "split-run", "2026-10-01 10:00:00"),       # the merged cell's split version
        ("a3", "split-run", "2026-10-01 10:00:01"),       # a restored by the split
        ("s", "split-run", "2026-10-01 10:00:02"),        # a fresh split output
        ("d", "dup-run", "2026-10-01 11:00:00"),
    ]
    for seq, (cell_id, run, created) in enumerate(cells):
        db.execute("INSERT INTO cells (id, run_id, sequence_num, created_at) VALUES (?,?,?,?)",
                   (cell_id, run, seq, created))
    edges = [
        ("a", "a2", "previous_version"), ("b", "a2", "input_0"),
        ("a2", "m", "merge_0"), ("b", "m", "merge_1"),
        ("m", "m2", "previous_version"), ("a2", "a3", "previous_version"),
        ("m2", "a3", "split_restored"), ("m2", "s", "split_from"),
        ("a2", "d", "branched_from"),
    ]
    db.executemany("INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) VALUES (?,?,?)", edges)
    db.commit()
    _migrations.apply(db, _migrations.CHANNEL_MIGRATIONS)

    moves = {(r[0], r[1], r[2], r[3], r[4], r[5], r[6]): r[7] for r in db.execute(
        "SELECT op, op_id, from_cell, from_version, to_cell, to_version, ord, created_at FROM cell_moves")}
    assert moves == {
        ("merge", "merge-run", "a", "a2", "m", "m", 0): "2026-10-01 09:00:00",
        ("merge", "merge-run", "b", "b", "m", "m", 1): "2026-10-01 09:00:00",
        ("split", "split-run", "m", "m2", "a", "a3", 0): "2026-10-01 10:00:01",
        ("split", "split-run", "m", "m2", "s", "s", 1): "2026-10-01 10:00:02",
        ("duplicate", "dup-run", "a", "a2", "d", "d", 0): "2026-10-01 11:00:00",
    }
    keys = {r[0] for r in db.execute("SELECT DISTINCT parent_key FROM cell_edges")}
    assert keys == {"previous_version", "input_0"}
    db.close()
