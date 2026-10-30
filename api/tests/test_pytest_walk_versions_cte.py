"""The recursive-CTE version-chain walkers.

``walk_versions`` (the history list) fetches the chain in a single
recursive CTE: start cell always included, chain order preserved, the
newest child per parent, and bounded behavior on cycles. The tip walk
(``versions.find_tip``) is one SQL statement too. Edge triggers maintain
or invalidate its disposable root index, preserving ``cell_edges`` as
the authority from every connection as soon as an edge is committed. Both
share a depth guard that raises instead of returning (or ending at) an
interior version.
"""
from __future__ import annotations

import versions

import sqlite3
import uuid

import pytest

from database import get_db_connection
from cell_files import walk_versions


@pytest.fixture
def channel_id(fresh_cwd):
    from models import Channel
    from state import state
    channel = Channel(name="CTE walk")
    state.channels = [channel]
    state.active_channel_id = channel.id
    return str(channel.id)


def _seed_chain(conn, run_id: str, root_id: str, length: int) -> list[str]:
    """Linear version chain of `length` cells starting at root_id.
    Returns the ordered ids."""
    ids = [root_id]
    conn.execute(
        "INSERT OR IGNORE INTO runs (id, prompt, status) VALUES (?, '', 'completed')",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO cells (id, run_id, generator_name, sequence_num) "
        "VALUES (?, ?, 'chat', 1)",
        (root_id, run_id),
    )
    for i in range(2, length + 1):
        new_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num) "
            "VALUES (?, ?, 'chat', ?)",
            (new_id, run_id, i),
        )
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')",
            (ids[-1], new_id),
        )
        ids.append(new_id)
    conn.commit()
    return ids


def test_single_cell_chain(channel_id):
    conn = get_db_connection(channel_id)
    try:
        root = str(uuid.uuid4())
        _seed_chain(conn, str(uuid.uuid4()), root, 1)
        assert walk_versions(conn, root) == [root]
    finally:
        conn.close()


def test_linear_chain_order_preserved(channel_id):
    conn = get_db_connection(channel_id)
    try:
        root = str(uuid.uuid4())
        ids = _seed_chain(conn, str(uuid.uuid4()), root, 7)
        assert walk_versions(conn, root) == ids
        # Walking from the middle returns the suffix.
        assert walk_versions(conn, ids[3]) == ids[3:]
        # The tip walks to itself.
        assert walk_versions(conn, ids[-1]) == [ids[-1]]
    finally:
        conn.close()


def test_unknown_start_returns_just_itself(channel_id):
    conn = get_db_connection(channel_id)
    try:
        ghost = str(uuid.uuid4())
        assert walk_versions(conn, ghost) == [ghost]
    finally:
        conn.close()


def test_multiple_children_takes_the_newest_branch_F0286(channel_id):
    """At a historical fork the NEWEST edge (max rowid) wins — the same
    branch rule the tip walker uses (versions.find_tip),
    so the version list the UI shows is the lineage the rest of the system
    resolves. Forks can no longer be created (every version write holds the
    cell's commit lock over a fresh tip read), but historical ones must
    resolve deterministically."""
    conn = get_db_connection(channel_id)
    try:
        root = str(uuid.uuid4())
        _seed_chain(conn, str(uuid.uuid4()), root, 2)
        # Second child off the root — a fork, inserted LATER.
        fork = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO cells (id, run_id, generator_name, sequence_num) "
            "VALUES (?, NULL, 'chat', 99)", (fork,),
        )
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')", (root, fork),
        )
        conn.commit()
        walked = walk_versions(conn, root)
        # Newest-inserted edge wins: the fork branch, exactly one branch,
        # no duplicates.
        assert walked == [root, fork]
    finally:
        conn.close()


def test_cycle_terminates_without_duplicates_F0286(channel_id):
    conn = get_db_connection(channel_id)
    try:
        root = str(uuid.uuid4())
        ids = _seed_chain(conn, str(uuid.uuid4()), root, 3)
        # Close the loop: tip -> root.
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')", (ids[-1], root),
        )
        conn.commit()
        walked = walk_versions(conn, root)
        assert walked == ids  # each id once, then the repeat stops the walk
    finally:
        conn.close()


def test_tip_walk_depth_guard_raises_never_returns_an_interior_version__F0002_F0065(
    channel_id, monkeypatch,
):
    """A chain up to the guard resolves to its tail; one version past it
    raises rather than hand back an interior node a new version would fork
    off. A cycle ends the walk without tripping the guard."""

    monkeypatch.setattr(versions, "MAX_VERSION_CHAIN", 5)
    conn = get_db_connection(channel_id)
    try:
        at_guard = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 5)
        assert versions.find_tip(conn, at_guard[0]) == at_guard[-1]

        past_guard = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 6)
        with pytest.raises(versions.VersionChainTooLong):
            versions.find_tip(conn, past_guard[0])
        # Walking from inside the chain stays within the guard.
        assert versions.find_tip(conn, past_guard[1]) == past_guard[-1]

        loop = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 3)
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')", (loop[-1], loop[0]),
        )
        conn.commit()
        assert versions.find_tip(conn, loop[0]) == loop[0]
    finally:
        conn.close()


def _traced_tip(conn, start_id: str) -> tuple[str, list[str], set[str]]:
    """``versions.find_tip`` from ``start_id``, the SQL it ran, and the
    tables that SQL reads. SQLite authorizes a statement's reads as it
    prepares the statement, and installing an authorizer expires the
    connection's prepared statements, so a statement an earlier call left in
    the cache is prepared again here and its reads are reported."""
    from versions import find_tip

    statements: list[str] = []
    tables: set[str] = set()

    def authorize(action, table, _column, _database, _source):
        if action == sqlite3.SQLITE_READ:
            tables.add(table)
        return sqlite3.SQLITE_OK

    conn.set_trace_callback(statements.append)
    conn.set_authorizer(authorize)
    try:
        return find_tip(conn, start_id), statements, tables
    finally:
        conn.set_authorizer(None)
        conn.set_trace_callback(None)


def test_find_tip_is_one_statement_at_any_chain_length__F0065_F0141(channel_id):
    """The tip lookup is one SQL statement whatever the chain length, not a
    round trip per version. It runs on every commit, emit, chat flush and
    live_files tick, so a per-hop walk cost a long-lived cell a round trip
    per version on each of them. P4F03 adds a transactional derived root accelerator; historical
    starts still use the canonical walk in the same single statement."""
    conn = get_db_connection(channel_id)
    try:
        for length in (1, 50, 5_000):
            ids = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), length)
            tip, statements, _tables = _traced_tip(conn, ids[0])
            assert tip == ids[-1]
            assert len(statements) == 1, (length, len(statements), statements[:3])
    finally:
        conn.close()


def test_the_edges_are_the_tips_only_source__F0141_P0F27(channel_id):
    """Edges remain authoritative after P4F03's derived root accelerator.

    Both writers and direct SQL append/branch paths must agree with the exact
    graph walk on an independent connection, including historical starts that
    must never resolve to a later sibling. SQL triggers update/invalidate the
    derived index, so an uncooperative edge writer cannot make it stale.
    """
    import cell_commits

    writer = get_db_connection(channel_id)
    reader = get_db_connection(channel_id)
    try:
        run_id = str(uuid.uuid4())
        chain = _seed_chain(writer, run_id, str(uuid.uuid4()), 20)
        cell = chain[0]  # the canvas cell: its id roots the chain and keys the lock
        versions.initialize_file_snapshot(writer, chain[-1])
        writer.commit()

        def by_insert_version() -> str:
            with cell_commits.cell_commit_lock(cell):
                tip = versions.find_tip(writer, cell)
                files = versions.tip_file_map(writer, tip)
                version_id, _event = versions.insert_version(
                    writer, tip_id=tip, parent_files=files, run_id=run_id, files=files,
                    file_entries=versions.version_file_entries(writer, tip),
                    version_name="insert_version", source_type="manual_edit",
                    generator_name="manual_edit",
                )
                writer.commit()
            return version_id

        def by_overlay() -> str:
            with cell_commits.cell_commit_lock(cell):
                version_id = versions.write_version_from_overlay(
                    writer, cell, run_id, [("notes.md", "set", f"version {len(chain) + 1}")],
                    version_name="overlay", source_type="manual_edit",
                    generator_name="manual_edit",
                )
                writer.commit()
            return version_id

        def by_bare_edge(parent_id: str) -> str:
            version_id = str(uuid.uuid4())
            writer.execute(
                "INSERT INTO cells (id, run_id, generator_name, sequence_num) "
                "SELECT ?, ?, 'chat', MAX(sequence_num) + 1 FROM cells WHERE run_id=?",
                (version_id, run_id, run_id),
            )
            writer.execute(
                "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
                "VALUES (?, ?, 'previous_version')", (parent_id, version_id),
            )
            versions.initialize_file_snapshot(writer, version_id)
            writer.commit()
            return version_id

        def became_tip(version_id: str) -> None:
            """``version_id`` joins the chain, and the reader resolves it from
            every version in it, in one statement with a derived root accelerator."""
            chain.append(version_id)
            for start in chain:
                tip, statements, tables = _traced_tip(reader, start)
                assert (tip, len(statements), tables) == (version_id, 1, {"cell_edges", "cell_version_heads"}), (
                    start, statements,
                )

        became_tip(by_insert_version())
        became_tip(by_bare_edge(chain[-1]))
        became_tip(by_overlay())
        became_tip(by_bare_edge(chain[-1]))
        became_tip(by_insert_version())
        assert walk_versions(reader, cell) == chain

        fork = by_bare_edge(chain[5])
        assert _traced_tip(reader, cell)[0] == walk_versions(reader, cell)[-1] == fork
        assert (_traced_tip(reader, chain[10])[0] == walk_versions(reader, chain[10])[-1]
                == chain[-1])
    finally:
        reader.close()
        writer.close()


def test_history_walk_has_no_silent_cap_and_raises_past_the_guard__F0002(
    channel_id, monkeypatch,
):
    """The history list (and fork's target-in-chain check) walks a chain past
    10,000 versions to its tip, since a long run mints a version per emit.
    A chain longer than MAX_VERSION_CHAIN raises rather than end the list at
    an interior version, as the tip walk does; a cycle within the guard ends
    the list before the first repeat."""

    conn = get_db_connection(channel_id)
    try:
        long_chain = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 10_050)
        assert walk_versions(conn, long_chain[0]) == long_chain

        monkeypatch.setattr(versions, "MAX_VERSION_CHAIN", 5)
        at_guard = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 5)
        assert walk_versions(conn, at_guard[0]) == at_guard
        past_guard = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 6)
        with pytest.raises(versions.VersionChainTooLong):
            walk_versions(conn, past_guard[0])
        assert walk_versions(conn, past_guard[1]) == past_guard[1:]

        loop = _seed_chain(conn, str(uuid.uuid4()), str(uuid.uuid4()), 5)
        conn.execute(
            "INSERT INTO cell_edges (parent_cell_id, child_cell_id, parent_key) "
            "VALUES (?, ?, 'previous_version')", (loop[-1], loop[0]),
        )
        conn.commit()
        assert walk_versions(conn, loop[2]) == loop[2:] + loop[:2]
    finally:
        conn.close()
