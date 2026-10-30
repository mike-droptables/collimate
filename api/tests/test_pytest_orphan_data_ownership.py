"""An orphan is disposable only when every application table is empty."""
from pathlib import Path
import sqlite3

import pytest

import database
import migrations


def _legacy_database(root: Path, basename='old_display_name') -> tuple[Path, sqlite3.Connection]:
    # A physical orphan fixture deliberately bypasses runtime channel addressing.
    path = root / f'.collimate/channels/{basename}.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    migrations.upgrade_channel_db(conn)
    return path, conn


def test_schema_only_orphan_is_deleted_despite_internal_bookkeeping(fresh_cwd):
    path, conn = _legacy_database(fresh_cwd)
    conn.execute('CREATE TABLE future_owner (id INTEGER PRIMARY KEY AUTOINCREMENT)')
    conn.execute('INSERT INTO future_owner DEFAULT VALUES')
    conn.execute('DELETE FROM future_owner')
    conn.commit()
    assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] > 0
    assert conn.execute('SELECT COUNT(*) FROM sqlite_sequence').fetchone()[0] == 1
    conn.close()

    assert not database.channel_db_holds_data(path.stem)
    assert 'removed the empty' in database.set_aside_orphan_channel_db(path.stem)
    assert not path.exists()
    assert not (path.parent / 'orphaned').exists()


@pytest.mark.parametrize('sql,query,expected', [
    ("INSERT INTO file_path_sequences VALUES ('cell', 'a.txt', 19)",
     'SELECT * FROM file_path_sequences', [('cell', 'a.txt', 19)]),
    ("INSERT INTO file_writer_sequences VALUES ('cell', 'a.txt', 'writer', 23)",
     'SELECT * FROM file_writer_sequences', [('cell', 'a.txt', 'writer', 23)]),
    ("INSERT INTO ui_state VALUES ('channel_settings', '{\"editing\":{}}')",
     'SELECT * FROM ui_state', [('channel_settings', '{"editing":{}}')]),
    ("INSERT INTO ui_state VALUES ('cell_display:cell', '{\"version\":1}')",
     'SELECT * FROM ui_state', [('cell_display:cell', '{"version":1}')]),
    ('CREATE TABLE "future""owner" (value TEXT)',
     'SELECT * FROM "future""owner"', [('durable future record',)]),
])
def test_orphan_parking_preserves_every_durable_owner(fresh_cwd, sql, query, expected):
    path, conn = _legacy_database(fresh_cwd)
    conn.execute(sql)
    if sql.startswith('CREATE TABLE'):
        conn.execute('INSERT INTO "future""owner" VALUES (?)', expected[0])
    conn.commit()
    conn.close()

    assert database.channel_db_holds_data(path.stem)
    assert 'moved the unowned' in database.set_aside_orphan_channel_db(path.stem)
    assert not path.exists()
    parked = list((path.parent / 'orphaned').glob('*.db'))
    assert len(parked) == 1
    with sqlite3.connect(parked[0]) as retained:
        assert retained.execute(query).fetchall() == expected


def test_unreadable_orphan_is_retained_without_rewriting(fresh_cwd):
    path = fresh_cwd / '.collimate/channels/corrupt_owner.db'
    path.parent.mkdir(parents=True)
    contents = b'not a SQLite database; preserve recovery evidence'
    path.write_bytes(contents)

    assert database.channel_db_holds_data(path.stem)
    assert 'moved the unowned' in database.set_aside_orphan_channel_db(path.stem)
    assert not path.exists()
    parked = list((path.parent / 'orphaned').glob('*.db'))
    assert len(parked) == 1
    assert parked[0].read_bytes() == contents


def test_schema_read_failure_retains_orphan(fresh_cwd, monkeypatch):
    path, conn = _legacy_database(fresh_cwd)
    conn.close()

    class RefusedConnection:
        closed = False

        def execute(self, sql):
            raise sqlite3.DatabaseError('unable to inspect the schema')

        def close(self):
            self.closed = True

    refused = RefusedConnection()
    monkeypatch.setattr(database.sqlite3, 'connect', lambda *args, **kwargs: refused)
    assert database.channel_db_holds_data(path.stem)
    assert refused.closed


def test_reconcile_parks_highwater_owner_and_deletes_only_schema_ghost(fresh_cwd):
    owner, conn = _legacy_database(fresh_cwd)
    conn.execute("INSERT INTO file_writer_sequences VALUES ('cell', 'a.txt', 'writer', 23)")
    conn.commit()
    conn.close()
    ghost, conn = _legacy_database(fresh_cwd, 'schema_ghost')
    conn.close()
    registered, conn = _legacy_database(fresh_cwd, 'c184a14e-e18b-48ce-b4e5-e13cd8c3d3a8')
    conn.close()

    outcomes = database.reconcile_orphan_channel_dbs({registered.stem})
    assert len(outcomes) == 2
    assert not owner.exists() and not ghost.exists()
    assert registered.exists()
    parked = list((owner.parent / 'orphaned').glob('*.db'))
    assert len(parked) == 1
    with sqlite3.connect(parked[0]) as retained:
        assert retained.execute('SELECT last_edit_seq FROM file_writer_sequences').fetchall() == [(23,)]
    assert database.reconcile_orphan_channel_dbs({registered.stem}) == []
