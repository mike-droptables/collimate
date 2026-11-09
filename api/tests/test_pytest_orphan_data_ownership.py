"""A folder that does not open is parked whole, never opened or deleted: every
durable owner inside it survives (the channel folders replaced the old
orphan-database reconcile, which deleted a database holding no data)."""
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

import collimate_server.channel_paths as channel_paths
import collimate_server.migrations as migrations


@pytest.fixture
def location(fresh_cwd):
    channel_paths.establish()
    return Path(os.environ['COLLIMATE_CHANNELS'])


def _folder(location: Path, *, manifest=True, identity=None) -> tuple[Path, sqlite3.Connection]:
    """A channel folder with a migrated channel.db; its channel.json is valid
    unless ``manifest`` is False (absent) or a string (its raw bytes)."""
    identity = identity or str(uuid4())
    folder = location / identity
    folder.mkdir()
    if manifest is True:
        (folder / 'channel.json').write_text(json.dumps({
            'format': 1, 'channel_id': identity, 'name': 'Probe', 'archived': False,
            'archived_at': None, 'installation_id': channel_paths.installation_id(),
            'created_at': '2026-11-05T00:00:00+00:00'}))
    elif manifest:
        (folder / 'channel.json').write_text(manifest)
    conn = sqlite3.connect(folder / 'channel.db')
    migrations.upgrade_channel_db(conn)
    return folder, conn


def _parked(location: Path) -> list[Path]:
    parked = location / '.orphaned'
    return sorted(parked.iterdir()) if parked.is_dir() else []


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
def test_parking_a_folder_preserves_every_durable_owner(location, sql, query, expected):
    folder, conn = _folder(location, manifest='not json')
    conn.execute(sql)
    if sql.startswith('CREATE TABLE'):
        conn.execute('INSERT INTO "future""owner" VALUES (?)', expected[0])
    conn.commit()
    conn.close()

    channel_paths.establish()

    assert not folder.exists()
    [parked] = _parked(location)
    assert parked.name.startswith(folder.name)
    with sqlite3.connect(parked / 'channel.db') as retained:
        assert retained.execute(query).fetchall() == expected
    assert channel_paths.home(folder.name) is None


def test_a_schema_only_folder_is_parked_not_deleted(location):
    """The old reconcile deleted a database holding no data; a folder is
    never judged by its contents, so even an empty one is kept."""
    folder, conn = _folder(location, manifest=False)
    conn.close()

    channel_paths.establish()

    assert not folder.exists()
    [parked] = _parked(location)
    assert (parked / 'channel.db').is_file()


def test_an_unreadable_database_folder_is_parked_without_rewriting(location):
    identity = str(uuid4())
    folder = location / identity
    folder.mkdir()
    contents = b'not a SQLite database; preserve recovery evidence'
    (folder / 'channel.db').write_bytes(contents)

    channel_paths.establish()

    assert not folder.exists()
    [parked] = _parked(location)
    assert (parked / 'channel.db').read_bytes() == contents


def test_an_openable_folder_stays_in_place_whatever_its_database_holds(location):
    owner, conn = _folder(location)
    conn.execute("INSERT INTO file_writer_sequences VALUES ('cell', 'a.txt', 'writer', 23)")
    conn.commit()
    conn.close()
    ghost, conn = _folder(location)
    conn.close()
    stray, conn = _folder(location, manifest='not json')
    conn.close()

    channel_paths.establish()

    assert owner.is_dir() and ghost.is_dir() and not stray.exists()
    assert set(channel_paths.indexed_channels()) == {owner.name, ghost.name}
    [parked] = _parked(location)
    assert parked.name.startswith(stray.name)
    # A second start finds nothing more to park.
    channel_paths.establish()
    assert _parked(location) == [parked]
