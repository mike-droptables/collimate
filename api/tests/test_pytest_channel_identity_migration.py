"""One numbered UUID cutover validates the complete plan before any move."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

import database
import migrations


def _channel(name):
    return SimpleNamespace(id=uuid4(), name=name)


def _source(channel):
    return Path('.collimate/channels') / f'{database._legacy_channel_basename(channel.name)}.db'


def _target(channel):
    return Path('.collimate/channels') / f'{channel.id}.db'


def _store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE owner(value TEXT NOT NULL)')
    connection.execute('INSERT INTO owner VALUES (?)', (value,))
    connection.commit()
    connection.close()


def _value(path):
    connection = sqlite3.connect(f'{path.absolute().as_uri()}?mode=ro', uri=True)
    try:
        return connection.execute('SELECT value FROM owner').fetchone()[0]
    finally:
        connection.close()


def _global(channels, head=7):
    from models import AppState, Channel
    from genesis import snapshot_app_state
    Path('.collimate').mkdir(exist_ok=True)
    connection = sqlite3.connect('.collimate/app.db')
    migrations.apply(connection, [entry for entry in migrations.GLOBAL_MIGRATIONS if entry[0] <= head])
    layout = AppState(channels=[Channel(id=channel.id, name=channel.name) for channel in channels])
    connection.execute("INSERT INTO ui_state(key,value) VALUES('app_state',?)", (json.dumps(snapshot_app_state(layout)),))
    connection.commit()
    return connection


def _migrate(connection):
    """Apply the global migrations the store lacks. These stores stop before
    the identity cutover (version 8), which ``upgrade_global_db`` refuses at
    startup; the tests drive the cutover itself."""
    return migrations.apply(connection, migrations.GLOBAL_MIGRATIONS)


def test_missing_later_database_refuses_before_first_move(fresh_cwd):
    first, missing = _channel('First source'), _channel('Missing source')
    _store(_source(first), 'retained')
    connection = _global([first, missing])
    try:
        with pytest.raises(OSError, match='Missing channel database'):
            _migrate(connection)
        assert _value(_source(first)) == 'retained'
        assert not _target(first).exists()
        assert not connection.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone()
    finally:
        connection.close()


def test_symbol_only_uuid_label_does_not_claim_unclassified_empty_basename(fresh_cwd):
    channel = _channel('!!!')
    _store(_target(channel), 'registered UUID owner')
    unclassified = Path('.collimate/channels/.db')
    _store(unclassified, 'unclassified owner')
    assert database.migrate_channel_database_identities([channel], require_existing=True) == []
    assert _value(_target(channel)) == 'registered UUID owner'
    assert _value(unclassified) == 'unclassified owner'


@pytest.mark.parametrize('head', [5, 7])
@pytest.mark.parametrize('identity', ['missing', 'uppercase', 'duplicate'])
def test_raw_layout_requires_explicit_canonical_unique_ids_before_conversion(fresh_cwd, head, identity):
    first, second = _channel('First retained source'), _channel('Second retained source')
    _store(_source(first), 'first')
    _store(_source(second), 'second')
    connection = _global([first, second], head=head)
    try:
        layout = json.loads(connection.execute("SELECT value FROM ui_state WHERE key='app_state'").fetchone()[0])
        if identity == 'missing':
            layout['channels'][1].pop('id')
        elif identity == 'uppercase':
            layout['channels'][1]['id'] = str(second.id).upper()
        else:
            layout['channels'][1]['id'] = str(first.id)
        connection.execute("UPDATE ui_state SET value=? WHERE key='app_state'", (json.dumps(layout),))
        connection.commit()
        with pytest.raises(OSError, match='(canonical layout UUIDs|Duplicate channel identity)'):
            _migrate(connection)
        assert _value(_source(first)) == 'first'
        assert _value(_source(second)) == 'second'
        assert not _target(first).exists()
        assert not _target(second).exists()
        assert not connection.execute('SELECT 1 FROM schema_migrations WHERE version>?', (head,)).fetchone()
    finally:
        connection.close()


@pytest.mark.parametrize('kind', ['shared-basename', 'dual-populated', 'cross-target', 'corrupt', 'source-symlink', 'target-symlink', 'wal-symlink', 'directory-symlink'])
def test_ambiguous_or_unreadable_plan_keeps_every_candidate_before_first_move(fresh_cwd, kind):
    first, second = _channel('Valid first'), _channel('Later source')
    _store(_source(first), 'first')
    _store(_source(second), 'second')
    original_second = _source(second)
    if kind == 'shared-basename':
        second = SimpleNamespace(id=second.id, name='Valid first!')
    elif kind == 'dual-populated':
        _store(_target(second), 'independent target')
    elif kind == 'cross-target':
        second = SimpleNamespace(id=second.id, name=str(first.id))
        _store(_source(second), 'another identity')
    elif kind == 'corrupt':
        _source(second).write_bytes(b'not a SQLite database')
    elif kind == 'source-symlink':
        original_second.rename(original_second.with_suffix('.kept'))
        original_second.symlink_to(original_second.with_suffix('.kept').name)
    elif kind == 'target-symlink':
        _target(second).symlink_to(original_second.name)
    elif kind == 'wal-symlink':
        Path(str(_source(second))+'-wal').symlink_to(original_second.name)
    elif kind == 'directory-symlink':
        directory = Path('.collimate/channels')
        directory.rename(directory.with_name('kept-channels'))
        directory.symlink_to('kept-channels', target_is_directory=True)
    with pytest.raises(OSError):
        database.migrate_channel_database_identities([first, second], require_existing=True)
    assert _value(_source(first)) == 'first'
    if kind == 'cross-target':
        assert _value(_target(first)) == 'another identity'
    else:
        assert not _target(first).exists()
    if kind == 'dual-populated':
        assert _value(_target(second)) == 'independent target'
    assert original_second.exists()


def test_duplicate_uuid_catalog_refuses_before_moving_a_source(fresh_cwd):
    channel = _channel('Same source')
    _store(_source(channel), 'retained')
    with pytest.raises(OSError, match='Duplicate channel identity'):
        database.migrate_channel_database_identities([channel, channel], require_existing=True)
    assert _value(_source(channel)) == 'retained'
    assert not _target(channel).exists()


def test_uuid_backed_duplicate_labels_do_not_require_disambiguation(fresh_cwd):
    first, second = _channel('Identical'), _channel('Identical')
    _store(_target(first), 'one')
    _store(_target(second), 'two')
    assert database.migrate_channel_database_identities([first, second], require_existing=True) == []
    assert _value(_target(first)) == 'one'
    assert _value(_target(second)) == 'two'


def test_busy_later_wal_refuses_before_first_move(fresh_cwd, monkeypatch):
    first, second = _channel('First'), _channel('Busy')
    _store(_source(first), 'first')
    _store(_source(second), 'before')
    writer = sqlite3.connect(_source(second))
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    reader = sqlite3.connect(_source(second))
    reader.execute('BEGIN')
    assert reader.execute('SELECT value FROM owner').fetchone()[0] == 'before'
    writer.execute("UPDATE owner SET value='committed WAL'")
    writer.commit()
    original = sqlite3.connect
    def connect(*args, **kwargs):
        kwargs['timeout'] = 0.01
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        with pytest.raises(OSError, match='busy'):
            database.migrate_channel_database_identities([first, second], require_existing=True)
        assert not _target(first).exists() and not _target(second).exists()
        assert _value(_source(first)) == 'first'
        assert _value(_source(second)) == 'committed WAL'
        assert Path(str(_source(second))+'-wal').exists()
    finally:
        reader.close()
        writer.close()


def test_all_source_checkpoints_close_before_first_rename(fresh_cwd, monkeypatch):
    channels = [_channel('One'), _channel('Two')]
    for index, channel in enumerate(channels):
        _store(_source(channel), str(index))
    closed = set()
    original_connect, original_rename = sqlite3.connect, database.os.rename
    class Connection:
        def __init__(self, actual, path):
            self.actual, self.path = actual, path
            self.checkpoint = False
        def execute(self, sql):
            self.checkpoint = sql == 'PRAGMA wal_checkpoint(TRUNCATE)'
            return self.actual.execute(sql)
        def close(self):
            self.actual.close()
            if self.checkpoint:
                closed.add(self.path)
    def connect(path, **kwargs):
        actual = original_connect(path, **kwargs)
        return Connection(actual, path) if 'mode=rw' in str(path) else actual
    def rename(source, target):
        assert len(closed) == len(channels)
        return original_rename(source, target)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    monkeypatch.setattr(database.os, 'rename', rename)
    assert len(database.migrate_channel_database_identities(channels, require_existing=True)) == 2
    assert [_value(_target(channel)) for channel in channels] == ['0', '1']


@pytest.mark.parametrize('after', [0, 1])
def test_interrupted_prefix_restarts_without_empty_substitute(fresh_cwd, monkeypatch, after):
    channels = [_channel('One'), _channel('Two')]
    for index, channel in enumerate(channels):
        _store(_source(channel), str(index))
    connection = _global(channels)
    original = database.os.rename
    count = 0
    def interrupt(source, target):
        nonlocal count
        if count == after:
            raise OSError('injected interruption')
        count += 1
        return original(source, target)
    try:
        with monkeypatch.context() as context:
            context.setattr(database.os, 'rename', interrupt)
            with pytest.raises(OSError, match='injected interruption'):
                _migrate(connection)
        assert not connection.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone()
        assert sum(_target(channel).exists() for channel in channels) == after
        assert _migrate(connection) == [8, 9, 10, 11]
        assert [_value(_target(channel)) for channel in channels] == ['0', '1']
        assert _migrate(connection) == []
    finally:
        connection.close()


def test_interruption_before_global_receipt_retries_completed_targets(fresh_cwd):
    channel = _channel('Completed source')
    _store(_source(channel), 'retained')
    connection = _global([channel])
    class InterruptedReceipt:
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO schema_migrations') and parameters[0] == 8:
                raise RuntimeError('receipt interruption')
            return connection.execute(sql, parameters)
        def commit(self):
            return connection.commit()
        def rollback(self):
            return connection.rollback()
    try:
        with pytest.raises(RuntimeError, match='receipt interruption'):
            _migrate(InterruptedReceipt())
        assert _value(_target(channel)) == 'retained'
        assert not _source(channel).exists()
        assert not connection.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone()
        assert _migrate(connection) == [8, 9, 10, 11]
        assert _value(_target(channel)) == 'retained'
    finally:
        connection.close()


def test_global5_layout_only_initialization_reaches_uuid_head8(fresh_cwd):
    from cas import store_bytes
    from models import AppState, Cell, Channel, Stack, VirtualFile
    channel = Channel(name='Layout-only historical source', main_stacks=[Stack(cells=[Cell(files=[VirtualFile(name='empty.txt',content='')])])])
    connection = _global([], head=5)
    layout = AppState(channels=[channel]).model_dump(mode='json')
    layout['storage_format'] = 'cas-layout-v1'
    file = layout['channels'][0]['main_stacks'][0]['cells'][0]['files'][0]
    file.pop('content')
    file['content_hash'] = store_bytes(b'')
    connection.execute("UPDATE ui_state SET value=? WHERE key='app_state'", (json.dumps(layout),))
    connection.commit()
    try:
        assert _migrate(connection) == [6, 7, 8, 9, 10, 11]
        target = _target(channel)
        current = sqlite3.connect(target)
        try:
            assert current.execute('SELECT encoding,body_hash FROM file_revisions').fetchone() == ('utf-8', file['content_hash'])
        finally:
            current.close()
        assert _migrate(connection) == []
    finally:
        connection.close()
